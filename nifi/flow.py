#!/usr/bin/env python3
"""Build the NiFi flows that carry mirror-*.tar bundles across a one-way link to the high side. Stdlib only.

usage: flow.py --side low|high [--url https://nifi:8443] [--user admin] [--password ...]
               [--param name=value ...] [--insecure] [--export FILE]

low:  ListenHTTP :#{mirror.port}/contentListener   (sync and resend POST each bundle here, NIFI_URL)
        -> PackageFlowFile                       (keeps Filename and the X- headers with the content)
        -> PutFile #{mirror.diode}                 (the diode's ingress directory, as <file>.ffv3)
high: ListFile #{mirror.diode} -> FetchFile        (the diode's egress directory; deletes what it takes)
        -> UnpackContent flowfile-stream-v3      (the original file and its attributes again)
        -> RouteOnAttribute container-images     (X-Artifact-Type, X-Artifact-Action, a .tar or .tar.part-NNN)
        -> CryptographicHashContent -> RouteOnAttribute verified   (content matches X-Sha256)
        -> InvokeHTTP PUT  generic package registry-mirror-bundles/<bundle>/<bundle>.tar   (PRIVATE-TOKEN)
        -> ReplaceText "<sha256>  <bundle>.tar[  <parts>]" -> InvokeHTTP PUT <bundle>.tar.sha256  (written here)
        -> InvokeHTTP POST pipeline?ref=#{gitlab.container.branch} with BUNDLE=<bundle>.tar (one per bundle)

Anything not matching ends in "Rejected (inspect queue)", left stopped so it waits there.
gitlab.container.token is a sensitive parameter: a project access token, role Developer, scope api.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

DEFAULTS = {"low": {"mirror.port": "9098", "mirror.diode": "/diode/mirror"},
            "high": {"mirror.diode": "/diode/mirror", "gitlab.api.url": "https://gitlab.example.com/api/v4",
                     "gitlab.container.projectId": "", "gitlab.container.branch": "main", "gitlab.container.token": ""}}
# --store s3: the high side files bundles in an S3-compatible bucket instead of the generic package registry.
S3_DEFAULTS = {"s3.endpoint": "", "s3.bucket": "", "s3.region": "us-east-1", "s3.prefix": "",
               "s3.access_key": "", "s3.secret_key": "", "s3.ca": ""}
SENSITIVE = {"gitlab.container.token", "s3.access_key", "s3.secret_key"}


class Nifi:
    def __init__(self, url, user, password, verify_tls):
        self.api = url.rstrip("/") + "/nifi-api"
        self.user, self.password, self.token, self.types = user, password, None, {}
        self.ctx = ssl.create_default_context()
        if not verify_tls:
            self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE

    def login(self):
        data = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        req = urllib.request.Request(self.api + "/access/token", data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            self.token = r.read().decode()

    def call(self, method, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def processor(self, pg, type_name, name, y, props, terminate=(), schedule=None, sensitive=()):
        if not self.types:
            self.types = {t["type"]: t["bundle"] for t in self.call("GET", "/flow/processor-types")["processorTypes"]}
        full = next(t for t in self.types if t.endswith("." + type_name))
        cfg = {"properties": self.resolve(full, props), "autoTerminatedRelationships": list(terminate),
               "sensitiveDynamicPropertyNames": list(sensitive)}
        if schedule:
            cfg["schedulingPeriod"] = schedule
        return self.call("POST", f"/process-groups/{pg}/processors",
                         {"revision": {"version": 0}, "component": {"type": full, "bundle": self.types[full], "name": name,
                                                                    "position": {"x": 0, "y": y * 180}, "config": cfg}})["id"]

    def service(self, pg, type_name, name, props):
        """Create a controller service in the group and enable it; its id goes in processor properties."""
        types = {t["type"]: t["bundle"] for t in self.call("GET", "/flow/controller-service-types")["controllerServiceTypes"]}
        full = next(t for t in types if t.endswith("." + type_name))
        created = self.call("POST", f"/process-groups/{pg}/controller-services",
                            {"revision": {"version": 0},
                             "component": {"type": full, "bundle": types[full], "name": name, "properties": props}})
        for _ in range(30):
            current = self.call("GET", f"/controller-services/{created['id']}")
            if current["component"]["validationStatus"] == "VALID":
                break
            time.sleep(1)
        else:
            raise SystemExit(f"{name}: {current['component'].get('validationErrors')}")
        self.call("PUT", f"/controller-services/{created['id']}/run-status",
                  {"revision": current["revision"], "state": "ENABLED"})
        return created["id"]

    def resolve(self, full, props):
        """Property keys as this NiFi names them. "A|B" tries each spelling, by name or display name,
        since NiFi versions rename properties. A key the processor does not define is dynamic and stays."""
        b = self.types[full]
        known = self.call("GET", f"/flow/processor-definition/{b['group']}/{b['artifact']}/{b['version']}/{full}")
        names = {}
        for key, descriptor in (known.get("propertyDescriptors") or {}).items():
            names[key] = names[descriptor.get("displayName", key)] = key
        resolved = {}
        for spellings, value in props.items():
            found = [names[s] for s in spellings.split("|") if s in names]
            resolved[found[0] if found else spellings.split("|")[0]] = value
        return resolved

    def connect(self, pg, src, dst, rels):
        self.call("POST", f"/process-groups/{pg}/connections",
                  {"revision": {"version": 0},
                   "component": {"source": {"id": src, "groupId": pg, "type": "PROCESSOR"},
                                 "destination": {"id": dst, "groupId": pg, "type": "PROCESSOR"},
                                 "selectedRelationships": list(rels)}})


def replace_group(n, root, name, params):
    """Remove a group of this name and its parameter context, then create both afresh."""
    # "quay <side> side" is this flow's name before the registry-neutral rename.
    legacy = name.replace("registry mirror", "quay", 1)
    for g in n.call("GET", f"/process-groups/{root}/process-groups")["processGroups"]:
        if g["component"]["name"] in (name, legacy):
            n.call("PUT", f"/flow/process-groups/{g['id']}", {"id": g["id"], "state": "STOPPED"})
            # A group with enabled controller services cannot be deleted.
            n.call("PUT", f"/flow/process-groups/{g['id']}/controller-services", {"id": g["id"], "state": "DISABLED"})
            time.sleep(2)
            n.call("POST", f"/process-groups/{g['id']}/empty-all-connections-requests")
            time.sleep(1)
            version = n.call("GET", f"/process-groups/{g['id']}")["revision"]["version"]
            n.call("DELETE", f"/process-groups/{g['id']}?version={version}&clientId=registry-mirror")
    for c in n.call("GET", "/flow/parameter-contexts")["parameterContexts"]:
        if c["component"]["name"] == name:
            n.call("DELETE", f"/parameter-contexts/{c['id']}?version={c['revision']['version']}&clientId=registry-mirror")
    ctx = n.call("POST", "/parameter-contexts", {"revision": {"version": 0}, "component": {
        "name": name, "parameters": [{"parameter": {"name": k, "value": v, "sensitive": k in SENSITIVE}}
                                     for k, v in params.items()]}})
    return n.call("POST", f"/process-groups/{root}/process-groups",
                  {"revision": {"version": 0},
                   "component": {"name": name, "position": {"x": 0, "y": 0}, "parameterContext": {"id": ctx["id"]}}})["id"]


def build_low(n, pg):
    listen = n.processor(pg, "ListenHTTP", "Receive bundle", 0,
                         {"Listening Port": "#{mirror.port}", "Base Path": "contentListener",
                          # The Filename header becomes the filename; X- headers become attributes as named.
                          "HTTP Headers for Attributes|HTTP Headers to receive as Attributes (Regex)": "(?i)x-.*"})
    remember = n.processor(pg, "UpdateAttribute", "Remember the name", 1, {"diode.name": "${filename}"})
    package = n.processor(pg, "PackageFlowFile", "Keep attributes across the diode", 2, {}, terminate=("original",))
    # PackageFlowFile names its output by UUID; the package itself still carries the original filename.
    rename = n.processor(pg, "UpdateAttribute", "Name the package", 3, {"filename": "${diode.name}.ffv3"})
    put = n.processor(pg, "PutFile", "Into the diode", 4,
                      {"Directory": "#{mirror.diode}", "Conflict Resolution Strategy": "fail",
                       "Create Missing Directories": "true"}, terminate=("success",))
    n.connect(pg, listen, remember, ["success"])
    n.connect(pg, remember, package, ["success"])
    n.connect(pg, package, rename, ["success"])
    n.connect(pg, rename, put, ["success"])
    n.connect(pg, put, put, ["failure"])
    return [listen, remember, package, rename, put]


def build_high(n, pg, store="gitlab"):
    gitlab = "#{gitlab.api.url}/projects/#{gitlab.container.projectId}"
    listing = n.processor(pg, "ListFile", "List the diode", 0,
                          {"Input Directory": "#{mirror.diode}", "File Filter": r".*\.ffv3",
                           "Recurse Subdirectories": "false", "Minimum File Age": "5 sec"}, schedule="10 sec")
    fetch = n.processor(pg, "FetchFile", "Take from the diode", 1, {"Completion Strategy": "Delete File"},
                        terminate=("not.found",))
    unpack = n.processor(pg, "UnpackContent", "Restore file and attributes", 2,
                         {"Packaging Format": "flowfile-stream-v3"}, terminate=("original",))
    # One file per bundle crosses the link. A .sha256 from a low side that still sends one is dropped:
    # this flow writes its own from the verified X-Sha256.
    route = n.processor(pg, "RouteOnAttribute", "Container bundles only", 3, {
        "Routing Strategy": "Route to Property name",
        "container-images": "${X-Artifact-Type:equals('container-images'):and(${X-Artifact-Action:equals('mirror')})"
                            ":and(${filename:matches('mirror-[0-9a-f]{32}-[0-9]{12}[.]tar([.]part-[0-9]{3})?')})}",
        "old-checksum": "${filename:matches('mirror-[0-9a-f]{32}-[0-9]{12}[.]tar[.]sha256')}"},
        terminate=("old-checksum",))
    hashing = n.processor(pg, "CryptographicHashContent", "Hash file", 4, {"Hash Algorithm": "SHA-256"})
    verified = n.processor(pg, "RouteOnAttribute", "File matches X-Sha256", 5, {
        "Routing Strategy": "Route to Property name", "verified": "${content_SHA-256:equals(${X-Sha256})}"})
    def uploader(name, y):
        if store == "s3":
            # The same <package>/<version>/<file> layout as the generic package, so import reads either store.
            return n.processor(pg, "PutS3Object", name, y, {
                "Bucket": "#{s3.bucket}", "Region": "#{s3.region}", "Endpoint Override URL": "#{s3.endpoint}",
                # s3.prefix matches the pipeline's S3_PREFIX, kept with a trailing slash.
                "Object Key": "#{s3.prefix}registry-mirror-bundles/${filename:substringBefore('.tar')}/${filename}",
                "use-path-style-access": "true", "AWS Credentials Provider service": credentials,
                "SSL Context Service": trust})
        return n.processor(pg, "InvokeHTTP", name, y, {
            "HTTP Method": "PUT", "Request Body Enabled": "true",
            "HTTP URL": gitlab + "/packages/generic/registry-mirror-bundles/${filename:substringBefore('.tar')}/${filename}",
            "PRIVATE-TOKEN": "#{gitlab.container.token}", "Response Body Attribute Name": "gitlab.response"},
            terminate=("Response",), sensitive=("PRIVATE-TOKEN",))

    credentials = trust = None
    if store == "s3":
        credentials = n.service(pg, "AWSCredentialsProviderControllerService", "S3 credentials",
                                {"Access Key": "#{s3.access_key}", "Secret Key": "#{s3.secret_key}"})
        trust = n.service(pg, "PEMEncodedSSLContextProvider", "S3 CA", {
            "Private Key Source": "UNDEFINED", "Certificate Authorities Source": "PROPERTIES",
            "Certificate Authorities": "#{s3.ca}"})
    upload = uploader("Upload the bundle", 6)
    # The checksum the low side sent, as the .sha256 import reads beside the bundle. A bundle sent in
    # parts carries the whole bundle's name, checksum and part count in X-Bundle-*; each part writes the
    # same file, and import joins the parts once all have arrived.
    whole = "${X-Bundle-Name:replaceNull(${filename})}"
    checksum = n.processor(pg, "UpdateAttribute", "Name the checksum", 7,
                           {"bundle.name": whole, "filename": whole + ".sha256"})
    content = n.processor(pg, "ReplaceText", "Write the checksum", 8, {
        "Replacement Strategy": "Always Replace", "Evaluation Mode": "Entire text",
        "Replacement Value": "${X-Bundle-Sha256:replaceNull(${X-Sha256})}  ${bundle.name}"
                             "${X-Bundle-Parts:isNull():ifElse('', ${X-Bundle-Parts:prepend('  ')})}\n"},
        terminate=("failure",))
    sidecar = uploader("Upload the checksum", 9)
    # One pipeline per bundle, started once both files are in the store.
    start = n.processor(pg, "InvokeHTTP", "Start the import pipeline", 10, {
        "HTTP Method": "POST", "Request Body Enabled": "false",
        # variables[][key]=BUNDLE&variables[][value]=<file>, brackets encoded
        "HTTP URL": gitlab + "/pipeline?ref=#{gitlab.container.branch}&variables%5B%5D%5Bkey%5D=BUNDLE"
                             "&variables%5B%5D%5Bvalue%5D=${bundle.name:urlEncode()}",
        "PRIVATE-TOKEN": "#{gitlab.container.token}", "Response Body Attribute Name": "gitlab.response"},
        terminate=("Response",), sensitive=("PRIVATE-TOKEN",))
    done = n.processor(pg, "UpdateAttribute", "Delivered", 11, {})
    rejected = n.processor(pg, "UpdateAttribute", "Rejected (inspect queue)", 12, {})
    n.connect(pg, listing, fetch, ["success"])
    n.connect(pg, fetch, unpack, ["success"])
    n.connect(pg, fetch, fetch, ["failure", "permission.denied"])
    n.connect(pg, unpack, route, ["success"])
    n.connect(pg, unpack, rejected, ["failure"])
    n.connect(pg, route, hashing, ["container-images"])
    n.connect(pg, route, rejected, ["unmatched"])
    n.connect(pg, hashing, verified, ["success"])
    n.connect(pg, hashing, rejected, ["failure"])
    n.connect(pg, verified, upload, ["verified"])
    n.connect(pg, verified, rejected, ["unmatched"])
    for put, following in ((upload, checksum), (sidecar, start)):
        if store == "s3":
            n.connect(pg, put, following, ["success"])
            n.connect(pg, put, rejected, ["failure"])
        else:
            n.connect(pg, put, following, ["Original"])
            n.connect(pg, put, put, ["Retry"])
            n.connect(pg, put, rejected, ["No Retry", "Failure"])
    n.connect(pg, checksum, content, ["success"])
    n.connect(pg, content, sidecar, ["success"])
    n.connect(pg, start, done, ["Original"])
    n.connect(pg, start, start, ["Retry"])
    n.connect(pg, start, rejected, ["No Retry", "Failure"])
    return [listing, fetch, unpack, route, hashing, verified, upload, checksum, content, sidecar, start]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--side", choices=("low", "high"), required=True)
    ap.add_argument("--url", default=os.environ.get("NIFI_API_URL", "https://localhost:8443"))
    ap.add_argument("--user", default=os.environ.get("NIFI_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("NIFI_PASSWORD"))
    ap.add_argument("--param", action="append", default=[], help="name=value; see DEFAULTS. "
                    "gitlab.container.token is read from GITLAB_TOKEN when not given")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed NiFi)")
    ap.add_argument("--export", help="also write the group's flow definition to this file")
    ap.add_argument("--store", choices=("gitlab", "s3"), default="gitlab",
                    help="high side: where bundles wait for import (the pipeline's IMPORT_STORE)")
    args = ap.parse_args()
    if not args.password:
        raise SystemExit("set --password or NIFI_PASSWORD")
    defaults = dict(DEFAULTS[args.side], **(S3_DEFAULTS if args.store == "s3" else {}))
    params = dict(defaults, **dict(p.split("=", 1) for p in args.param))
    if args.side == "high":
        params["gitlab.container.token"] = params["gitlab.container.token"] or os.environ.get("GITLAB_TOKEN", "")
        if not params["gitlab.container.projectId"] or not params["gitlab.container.token"]:
            raise SystemExit("high side needs --param gitlab.container.projectId=<id> and GITLAB_TOKEN")
        if args.store == "s3":
            params["s3.access_key"] = params["s3.access_key"] or os.environ.get("AWS_ACCESS_KEY_ID", "")
            params["s3.secret_key"] = params["s3.secret_key"] or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
            params["s3.prefix"] = params["s3.prefix"].strip("/") + "/" if params["s3.prefix"].strip("/") else ""
            if params["s3.ca"].startswith("@"):  # a PEM file, read here so the parameter holds the text
                params["s3.ca"] = open(params["s3.ca"][1:], encoding="utf-8").read()
            missing = [k for k in ("s3.endpoint", "s3.bucket", "s3.access_key", "s3.secret_key", "s3.ca") if not params[k]]
            if missing:
                raise SystemExit(f"--store s3 needs {', '.join(missing)} (keys from AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY)")
    n = Nifi(args.url, args.user, args.password, not args.insecure)
    n.login()
    root = n.call("GET", "/flow/process-groups/root")["processGroupFlow"]["id"]
    pg = replace_group(n, root, f"registry mirror {args.side} side", params)
    if args.side == "low":
        build_low(n, pg)
    else:
        build_high(n, pg, args.store)
    # Start every processor but the two end points, whose queues are the record.
    n.call("PUT", f"/flow/process-groups/{pg}", {"id": pg, "state": "RUNNING"})
    for p in n.call("GET", f"/process-groups/{pg}/processors")["processors"]:
        if p["component"]["name"] in ("Delivered", "Rejected (inspect queue)"):
            n.call("PUT", f"/processors/{p['id']}/run-status",
                   {"revision": p["revision"], "state": "STOPPED"})
    shown = {k: ("***" if k in SENSITIVE else "<pem>" if k == "s3.ca" else v) for k, v in params.items()}
    print(f"registry mirror {args.side} side running in process group {pg}: {shown}")
    if args.export:
        req = urllib.request.Request(f"{n.api}/process-groups/{pg}/download", headers={"Authorization": f"Bearer {n.token}"})
        with urllib.request.urlopen(req, timeout=60, context=n.ctx) as r:
            open(args.export, "wb").write(r.read())
        print(f"flow definition written to {args.export}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
