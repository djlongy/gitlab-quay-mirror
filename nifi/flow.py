#!/usr/bin/env python3
"""Build the NiFi flows that carry quay-*.tar bundles across a one-way link into a high GitLab. Stdlib only.

usage: flow.py --side low|high [--url https://nifi:8443] [--user admin] [--password ...]
               [--param name=value ...] [--insecure] [--export FILE]

low:  ListenHTTP :#{quay.port}/contentListener   (sync and export POST each file here, NIFI_URL)
        -> PackageFlowFile                       (keeps Filename and the X- headers with the content)
        -> PutFile #{quay.diode}                 (the diode's ingress directory, as <file>.ffv3)
high: ListFile #{quay.diode} -> FetchFile        (the diode's egress directory; deletes what it takes)
        -> UnpackContent flowfile-stream-v3      (the original file and its attributes again)
        -> RouteOnAttribute container-images     (X-Artifact-Type and X-Artifact-Action)
        -> CryptographicHashContent -> RouteOnAttribute verified   (content matches X-Sha256)
        -> InvokeHTTP PUT  generic package quay-bundles/<bundle>/<file>   (PRIVATE-TOKEN)
        -> InvokeHTTP POST pipeline?ref=#{gitlab.ref} with BUNDLE=<file> (mirror.py import --registry)

Anything not matching ends in "Rejected (inspect queue)", left stopped so it waits there.
gitlab.token is a sensitive parameter: a project access token, role Developer, scope api.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

DEFAULTS = {"low": {"quay.port": "9098", "quay.diode": "/diode/quay"},
            "high": {"quay.diode": "/diode/quay", "gitlab.api": "https://gitlab.example.com/api/v4",
                     "gitlab.project": "", "gitlab.ref": "main", "gitlab.token": ""}}
SENSITIVE = {"gitlab.token"}


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
    for g in n.call("GET", f"/process-groups/{root}/process-groups")["processGroups"]:
        if g["component"]["name"] == name:
            n.call("PUT", f"/flow/process-groups/{g['id']}", {"id": g["id"], "state": "STOPPED"})
            time.sleep(2)
            n.call("POST", f"/process-groups/{g['id']}/empty-all-connections-requests")
            time.sleep(1)
            version = n.call("GET", f"/process-groups/{g['id']}")["revision"]["version"]
            n.call("DELETE", f"/process-groups/{g['id']}?version={version}&clientId=quay")
    for c in n.call("GET", "/flow/parameter-contexts")["parameterContexts"]:
        if c["component"]["name"] == name:
            n.call("DELETE", f"/parameter-contexts/{c['id']}?version={c['revision']['version']}&clientId=quay")
    ctx = n.call("POST", "/parameter-contexts", {"revision": {"version": 0}, "component": {
        "name": name, "parameters": [{"parameter": {"name": k, "value": v, "sensitive": k in SENSITIVE}}
                                     for k, v in params.items()]}})
    return n.call("POST", f"/process-groups/{root}/process-groups",
                  {"revision": {"version": 0},
                   "component": {"name": name, "position": {"x": 0, "y": 0}, "parameterContext": {"id": ctx["id"]}}})["id"]


def build_low(n, pg):
    listen = n.processor(pg, "ListenHTTP", "Receive bundle", 0,
                         {"Listening Port": "#{quay.port}", "Base Path": "contentListener",
                          # The Filename header becomes the filename; X- headers become attributes as named.
                          "HTTP Headers for Attributes|HTTP Headers to receive as Attributes (Regex)": "(?i)x-.*"})
    remember = n.processor(pg, "UpdateAttribute", "Remember the name", 1, {"diode.name": "${filename}"})
    package = n.processor(pg, "PackageFlowFile", "Keep attributes across the diode", 2, {}, terminate=("original",))
    # PackageFlowFile names its output by UUID; the package itself still carries the original filename.
    rename = n.processor(pg, "UpdateAttribute", "Name the package", 3, {"filename": "${diode.name}.ffv3"})
    put = n.processor(pg, "PutFile", "Into the diode", 4,
                      {"Directory": "#{quay.diode}", "Conflict Resolution Strategy": "fail",
                       "Create Missing Directories": "true"}, terminate=("success",))
    n.connect(pg, listen, remember, ["success"])
    n.connect(pg, remember, package, ["success"])
    n.connect(pg, package, rename, ["success"])
    n.connect(pg, rename, put, ["success"])
    n.connect(pg, put, put, ["failure"])
    return [listen, remember, package, rename, put]


def build_high(n, pg):
    gitlab = "#{gitlab.api}/projects/#{gitlab.project}"
    listing = n.processor(pg, "ListFile", "List the diode", 0,
                          {"Input Directory": "#{quay.diode}", "File Filter": r".*\.ffv3",
                           "Recurse Subdirectories": "false", "Minimum File Age": "5 sec"}, schedule="10 sec")
    fetch = n.processor(pg, "FetchFile", "Take from the diode", 1, {"Completion Strategy": "Delete File"},
                        terminate=("not.found",))
    unpack = n.processor(pg, "UnpackContent", "Restore file and attributes", 2,
                         {"Packaging Format": "flowfile-stream-v3"}, terminate=("original",))
    route = n.processor(pg, "RouteOnAttribute", "Container bundles only", 3, {
        "Routing Strategy": "Route to Property name",
        "container-images": "${X-Artifact-Type:equals('container-images'):and(${X-Artifact-Action:equals('mirror')})"
                            ":and(${filename:matches('quay-[0-9a-f]{32}-[0-9]{12}[.]tar([.]sha256)?')})}"})
    hashing = n.processor(pg, "CryptographicHashContent", "Hash file", 4, {"Hash Algorithm": "SHA-256"})
    verified = n.processor(pg, "RouteOnAttribute", "File matches X-Sha256", 5, {
        "Routing Strategy": "Route to Property name", "verified": "${content_SHA-256:equals(${X-Sha256})}"})
    upload = n.processor(pg, "InvokeHTTP", "Upload to GitLab", 6, {
        "HTTP Method": "PUT", "Request Body Enabled": "true",
        "HTTP URL": gitlab + "/packages/generic/quay-bundles/${filename:substringBefore('.tar')}/${filename}",
        "PRIVATE-TOKEN": "#{gitlab.token}", "Response Body Attribute Name": "gitlab.response"},
        terminate=("Response",), sensitive=("PRIVATE-TOKEN",))
    start = n.processor(pg, "InvokeHTTP", "Start the import pipeline", 7, {
        "HTTP Method": "POST", "Request Body Enabled": "false",
        # variables[][key]=BUNDLE&variables[][value]=<file>, brackets encoded
        "HTTP URL": gitlab + "/pipeline?ref=#{gitlab.ref}&variables%5B%5D%5Bkey%5D=BUNDLE"
                             "&variables%5B%5D%5Bvalue%5D=${filename:urlEncode()}",
        "PRIVATE-TOKEN": "#{gitlab.token}", "Response Body Attribute Name": "gitlab.response"},
        terminate=("Response",), sensitive=("PRIVATE-TOKEN",))
    done = n.processor(pg, "UpdateAttribute", "Delivered", 8, {})
    rejected = n.processor(pg, "UpdateAttribute", "Rejected (inspect queue)", 9, {})
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
    n.connect(pg, upload, start, ["Original"])
    n.connect(pg, upload, upload, ["Retry"])
    n.connect(pg, upload, rejected, ["No Retry", "Failure"])
    n.connect(pg, start, done, ["Original"])
    n.connect(pg, start, start, ["Retry"])
    n.connect(pg, start, rejected, ["No Retry", "Failure"])
    return [listing, fetch, unpack, route, hashing, verified, upload, start]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--side", choices=("low", "high"), required=True)
    ap.add_argument("--url", default=os.environ.get("NIFI_API_URL", "https://localhost:8443"))
    ap.add_argument("--user", default=os.environ.get("NIFI_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("NIFI_PASSWORD"))
    ap.add_argument("--param", action="append", default=[], help="name=value; see DEFAULTS. "
                    "gitlab.token is read from GITLAB_TOKEN when not given")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed NiFi)")
    ap.add_argument("--export", help="also write the group's flow definition to this file")
    args = ap.parse_args()
    if not args.password:
        raise SystemExit("set --password or NIFI_PASSWORD")
    params = dict(DEFAULTS[args.side], **dict(p.split("=", 1) for p in args.param))
    if args.side == "high":
        params["gitlab.token"] = params["gitlab.token"] or os.environ.get("GITLAB_TOKEN", "")
        if not params["gitlab.project"] or not params["gitlab.token"]:
            raise SystemExit("high side needs --param gitlab.project=<id> and GITLAB_TOKEN")
    n = Nifi(args.url, args.user, args.password, not args.insecure)
    n.login()
    root = n.call("GET", "/flow/process-groups/root")["processGroupFlow"]["id"]
    pg = replace_group(n, root, f"quay {args.side} side", params)
    (build_low if args.side == "low" else build_high)(n, pg)
    # Start every processor but the two end points, whose queues are the record.
    n.call("PUT", f"/flow/process-groups/{pg}", {"id": pg, "state": "RUNNING"})
    for p in n.call("GET", f"/process-groups/{pg}/processors")["processors"]:
        if p["component"]["name"] in ("Delivered", "Rejected (inspect queue)"):
            n.call("PUT", f"/processors/{p['id']}/run-status",
                   {"revision": p["revision"], "state": "STOPPED"})
    shown = {k: ("***" if k in SENSITIVE else v) for k, v in params.items()}
    print(f"quay {args.side} side running in process group {pg}: {shown}")
    if args.export:
        req = urllib.request.Request(f"{n.api}/process-groups/{pg}/download", headers={"Authorization": f"Bearer {n.token}"})
        with urllib.request.urlopen(req, timeout=60, context=n.ctx) as r:
            open(args.export, "wb").write(r.read())
        print(f"flow definition written to {args.export}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
