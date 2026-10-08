#!/usr/bin/env python3
"""Mirror reviewed container images into a registry and carry them across a one-way link in verified bundles."""

import argparse
import fcntl
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
DIGEST = r"sha256:[0-9a-f]{64}"
HOST = r"(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?::[0-9]+)?|[a-z0-9-]+:[0-9]+"  # a dot, a port or localhost
SOURCE = re.compile(rf"(?P<host>{HOST})/(?P<repo>[a-z0-9._/-]+):(?P<tag>{TAG})")
TARGET = re.compile(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)+")
IMAGE_CONFIGS = ("application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json")
IMAGE_LAYER = re.compile(r"application/vnd\.oci\.image\.layer\.(?:nondistributable\.)?v1\.tar(?:\+gzip|\+zstd)?"
                         r"|application/vnd\.docker\.image\.rootfs\.(?:foreign\.)?diff\.tar(?:\.gzip)?")
MANIFESTS = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
CHART_CONFIG = "application/vnd.cncf.helm.config.v1+json"
CHART_LAYERS = ("application/vnd.cncf.helm.chart.content.v1.tar+gzip", "application/vnd.cncf.helm.chart.provenance.v1.prov")
LEDGER, BUNDLES, RECEIPTS = "registry-mirror-ledger", "registry-mirror-bundles", "registry-mirror-receipt"
BUNDLE_NAME = re.compile(r"(mirror-[0-9a-f]{32}-[0-9]{12})\.tar(?:\.sha256|\.part-[0-9]{3})?")
INDEXES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")


class MirrorError(Exception):
    pass


class SequenceGap(MirrorError):
    pass


class GapWaiting(MirrorError):
    """A later bundle is stored and waits for an earlier one still on its way: exit 3, a CI warning."""


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise MirrorError(f"{name} is required")
    return value.rstrip("/")


def registry(side):
    value = required_env(side)
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", value):
        raise MirrorError(f"{side} must be a registry host[:port], without scheme or path")
    return value


def state_path(name, default):
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return Path(os.environ.get(name) or base / default)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def locked(work):
    work.mkdir(parents=True, exist_ok=True)
    with (work / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MirrorError(f"another mirror process holds {work}") from error
        yield


def parse_image(source, target):
    name, separator, digest = source.partition("@")
    match = SOURCE.fullmatch(name)
    if (not match or not separator or not re.fullmatch(DIGEST, digest)
            or not TARGET.fullmatch(target) or any(p in ("", ".", "..") for p in match['repo'].split('/'))):
        raise MirrorError("expected registry/repo:tag@sha256:<64 hex> org/repo[/path]")
    return {"source": source, "target": target, "tag": match["tag"], "digest": digest}


def catalog(path):
    images, seen = [], set()
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2:
            raise MirrorError(f"{path}:{number}: expected a pinned source and target")
        image = parse_image(*fields)
        key = f"{image['target']}:{image['tag']}"
        if key in seen:
            raise MirrorError(f"{path}:{number}: duplicate target tag {key}")
        seen.add(key)
        images.append(image)
    return images


def flag(name):
    """A true/false setting. Anything else is an error, so a typo cannot read as off."""
    value = os.environ.get(name, "").strip()
    if value.lower() not in ("", "true", "false"):
        raise MirrorError(f"{name} must be true or false, not {value!r}")
    return value.lower() == "true"


def settings(*entries):
    """Print each setting a command reads, and what leaving it unset means. A variable that never
    reaches the job, such as one scoped to another CI environment, shows here instead of silently
    changing behaviour."""
    environment = os.environ.get("CI_ENVIRONMENT_NAME")
    where = f" in this job (environment {environment})" if environment else ""
    for name, unset, secret in entries:
        value = os.environ.get(name, "").strip()
        if value:
            if "://" in value:  # a URL may carry credentials, as NIFI_URL can
                parts = urllib.parse.urlsplit(value)
                value = parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()
            print(f"setting: {name}={'(set)' if secret else value}")
        else:
            print(f"setting: {name} is not set{where}: {unset}")


def say(message):
    if os.environ.get("MIRROR_VERBOSE") == "true":
        print(message, file=sys.stderr, flush=True)


def run(*args):
    say("+ " + " ".join(args))
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise MirrorError(f"{args[0]} {args[1]} failed: {result.stderr.decode(errors='replace')[-500:].strip()}")
    return result.stdout


# Credentials come from skopeo's default auth file, written by `skopeo login` or
# `podman login`. REGISTRY_AUTH_FILE moves it for both the login and this script.
def options(side, direction=""):
    return [f"--{direction}tls-verify=false"] if side and os.environ.get(f"{side}_TLS_VERIFY") == "false" else []


def raw_digest(reference, side):
    # A manifest digest is the sha256 of its exact bytes, which --raw returns.
    raw = run("skopeo", "inspect", "--raw", *options(side), reference)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def side_of(reference):
    """Which registry's settings read a catalog source: TARGET_REGISTRY or SOURCE_REGISTRY when
    its host is that registry, else None (defaults: TLS verified, no login)."""
    host = reference.split("://", 1)[-1].split("/", 1)[0]
    for side in ("TARGET_REGISTRY", "SOURCE_REGISTRY"):
        if host and host == os.environ.get(side, "").strip().rstrip("/"):
            return side
    return None


def fetch_manifest(reference, side=None):
    """The manifest bytes at reference@digest, checked against that digest."""
    raw = run("skopeo", "inspect", "--raw", *options(side or side_of(reference)), reference)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != reference.rsplit("@", 1)[1]:
        raise MirrorError(f"manifest digest mismatch: {reference}")
    return raw


def transport_reference(source):
    name, separator, digest = source.partition("@")
    return name.rsplit(":", 1)[0] + "@" + digest if separator else name


def copy(source, destination, source_side=None, destination_side=None):
    args = ["skopeo", "copy", "--all", "--preserve-digests", "--retry-times=3"]
    if source_side:
        args += options(source_side, "src-")
    if destination_side:
        args += options(destination_side, "dest-")
    run(*args, source, destination)


def ledger_enabled():
    return os.environ.get("MIRROR_LEDGER") == "true"


def packages():
    """The project's generic package API and the header that authenticates to it.

    PACKAGE_TOKEN (a personal, project or group access token) wins; in CI the job
    token reads and writes the job's own project, which is all sync and import need.
    """
    api = (os.environ.get("GITLAB_API_URL") or os.environ.get("CI_API_V4_URL") or "").rstrip("/")
    project = os.environ.get("PACKAGE_PROJECT") or os.environ.get("CI_PROJECT_ID") or ""
    token = os.environ.get("PACKAGE_TOKEN")
    header = ("PRIVATE-TOKEN", token) if token else ("JOB-TOKEN", os.environ.get("CI_JOB_TOKEN", ""))
    if not api or not project or not header[1]:
        raise MirrorError("the package registry needs GITLAB_API_URL, PACKAGE_PROJECT and PACKAGE_TOKEN "
                          "(a CI job supplies all three)")
    return f"{api}/projects/{urllib.parse.quote(project, safe='')}/packages/generic", header


def package_request(method, package, version, name, data=None):
    base, (key, value) = packages()
    url = f"{base}/{package}/{version}/{urllib.parse.quote(name)}"
    say(f"+ {method} {url}")
    context = ssl.create_default_context()
    # A private CA, added to the system roots: CA_BUNDLE, or the one a runner with tls-ca-file hands every job.
    for bundle in {os.environ.get("CA_BUNDLE"), os.environ.get("CI_SERVER_TLS_CA_FILE")} - {None, ""}:
        context.load_verify_locations(cafile=bundle)
    request = urllib.request.Request(url, data=data, method=method, headers={key: value})
    return urllib.request.urlopen(request, timeout=600, context=context)


def package_api(method, url, key, value):
    say(f"+ {method} {url}")
    context = ssl.create_default_context()
    for bundle in {os.environ.get("CA_BUNDLE"), os.environ.get("CI_SERVER_TLS_CA_FILE")} - {None, ""}:
        context.load_verify_locations(cafile=bundle)
    request = urllib.request.Request(url, method=method, headers={key: value})
    with urllib.request.urlopen(request, timeout=600, context=context) as response:
        return response.read()


def package_get(package, version, name, destination=None):
    """A generic package file's bytes, or written to destination; None when GitLab has none."""
    try:
        with package_request("GET", package, version, name) as response:
            if destination is None:
                return response.read()
            with open(destination, "wb") as stream:
                shutil.copyfileobj(response, stream)
            return destination
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise MirrorError(f"GET {package}/{version}/{name}: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise MirrorError(f"GET {package}/{version}/{name}: {error.reason}") from error


def package_put(package, version, name, data):
    try:
        package_request("PUT", package, version, name, data).close()
    except urllib.error.HTTPError as error:
        raise MirrorError(f"PUT {package}/{version}/{name}: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise MirrorError(f"PUT {package}/{version}/{name}: {error.reason}") from error


def load_state(state_file):
    """Sender state: the ledger's head when MIRROR_LEDGER=true, else the runner's sent.json."""
    if ledger_enabled():
        raw = package_get(LEDGER, "head", "state.json")
        return json.loads(raw) if raw else None
    return json.loads(state_file.read_text()) if state_file.exists() else None


def save_state(state_file, state, remote=True):
    """Write sent.json and, with remote, the ledger's head. GitLab serves the newest upload of a name."""
    write_json(state_file, state)
    if remote and ledger_enabled():
        package_put(LEDGER, "head", "state.json", state_file.read_bytes())


def s3_enabled():
    return os.environ.get("IMPORT_STORE", "gitlab") == "s3"


def s3_request(method, key, data=None):
    """One path-style request to an S3-compatible store, signed with AWS Signature Version 4."""
    endpoint = os.environ.get("S3_ENDPOINT", "").rstrip("/")
    bucket = os.environ.get("S3_BUCKET", "")
    access, secret = os.environ.get("AWS_ACCESS_KEY_ID", ""), os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if not (endpoint and bucket and access and secret):
        raise MirrorError("IMPORT_STORE=s3 needs S3_ENDPOINT, S3_BUCKET, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
    region = os.environ.get("S3_REGION") or "us-east-1"
    prefix = os.environ.get("S3_PREFIX", "").strip("/")
    url = f"{endpoint}/{urllib.parse.quote(bucket)}/{urllib.parse.quote((prefix + '/' if prefix else '') + key)}"
    parts = urllib.parse.urlsplit(url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = {"host": parts.netloc, "x-amz-content-sha256": "UNSIGNED-PAYLOAD", "x-amz-date": stamp}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, parts.path, "", *(f"{name}:{headers[name]}" for name in sorted(headers)),
                           "", signed, "UNSIGNED-PAYLOAD"])
    scope = f"{stamp[:8]}/{region}/s3/aws4_request"
    signing = ("AWS4" + secret).encode()
    for part in (stamp[:8], region, "s3", "aws4_request"):
        signing = hmac.new(signing, part.encode(), hashlib.sha256).digest()
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, "
                                f"Signature={hmac.new(signing, to_sign.encode(), hashlib.sha256).hexdigest()}")
    del headers["host"]  # urllib sends the same value
    context = ssl.create_default_context()
    if os.environ.get("S3_TLS_VERIFY") == "false":
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    elif os.environ.get("S3_CA_BUNDLE"):  # a self-signed or private CA, added to the system roots
        context.load_verify_locations(cafile=os.environ["S3_CA_BUNDLE"])
        # Python 3.13+ also demands RFC 5280 extensions an appliance's own CA often lacks (key usage);
        # trust still comes only from this CA.
        context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    say(f"+ {method} {url}")
    return urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers),
                                  timeout=600, context=context)


def store_get(package, version, name, destination=None):
    """A high-side file from IMPORT_STORE: the bucket's <package>/<version>/<name>, or the generic package."""
    if not s3_enabled():
        return package_get(package, version, name, destination)
    try:
        with s3_request("GET", f"{package}/{version}/{name}") as response:
            if destination is None:
                return response.read()
            with open(destination, "wb") as stream:
                shutil.copyfileobj(response, stream)
            return destination
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise MirrorError(f"S3 GET {package}/{version}/{name}: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise MirrorError(f"S3 GET {package}/{version}/{name}: {error.reason}") from error


def store_put(package, version, name, data):
    if not s3_enabled():
        return package_put(package, version, name, data)
    try:
        s3_request("PUT", f"{package}/{version}/{name}", data).close()
    except urllib.error.URLError as error:
        raise MirrorError(f"S3 PUT {package}/{version}/{name}: {getattr(error, 'code', error.reason)}") from error


def store_exists(package, version, name):
    """Whether the store holds a file, without downloading it."""
    try:
        if s3_enabled():
            s3_request("HEAD", f"{package}/{version}/{name}").close()
        else:
            package_request("HEAD", package, version, name).close()
        return True
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise MirrorError(f"HEAD {package}/{version}/{name}: HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise MirrorError(f"HEAD {package}/{version}/{name}: {error.reason}") from error


def store_age(package, version, name):
    """Hours since a bundle reached the store, from GitLab's package created_at or S3's Last-Modified."""
    try:
        if s3_enabled():
            with s3_request("HEAD", f"{package}/{version}/{name}") as response:
                stored = parsedate_to_datetime(response.headers["Last-Modified"])
        else:
            base, (key, value) = packages()
            query = urllib.parse.urlencode({"package_type": "generic", "package_name": package,
                                            "package_version": version})
            found = [p for p in json.loads(package_api("GET", f"{base.rsplit('/generic', 1)[0]}?{query}", key, value))
                     if p["name"] == package and p["version"] == version]
            if not found:
                return None
            stored = datetime.fromisoformat(found[0]["created_at"].replace("Z", "+00:00"))
    except (urllib.error.URLError, KeyError, ValueError) as error:
        say(f"cannot tell how long {version} has waited: {error}")
        return None
    return (datetime.now(timezone.utc) - stored).total_seconds() / 3600


def gap_grace():
    """IMPORT_GAP_GRACE: hours a later bundle may wait for an earlier one before import fails."""
    text = os.environ.get("IMPORT_GAP_GRACE", "").strip() or "6"
    try:
        return float(text)
    except ValueError:
        raise MirrorError(f"IMPORT_GAP_GRACE must be a number of hours, not {text!r}") from None


def store_delete(package, version, names):
    """Remove an imported bundle: its objects in the bucket, or its generic package version.
    Returns how many generic packages were deleted; S3 does not report a missing object."""
    try:
        if s3_enabled():
            for name in names:
                s3_request("DELETE", f"{package}/{version}/{name}").close()
            return len(names)
        base, (key, value) = packages()
        query = urllib.parse.urlencode({"package_type": "generic", "package_name": package,
                                        "package_version": version, "per_page": 100})
        listing = base.rsplit("/generic", 1)[0]
        deleted = 0
        for found in json.loads(package_api("GET", f"{listing}?{query}", key, value)):
            if found["name"] == package and found["version"] == version:
                package_api("DELETE", f"{listing}/{found['id']}", key, value)
                deleted += 1
        return deleted
    except urllib.error.URLError as error:
        raise MirrorError(f"delete {package}/{version}: {getattr(error, 'code', error.reason)}") from error


def load_receipt(receipt_file):
    """The high side's last import: the store's copy when MIRROR_LEDGER=true, else received.json."""
    if ledger_enabled():
        raw = store_get(RECEIPTS, "head", "received.json")
        return json.loads(raw) if raw else {}
    return json.loads(receipt_file.read_text()) if receipt_file.exists() else {}


def save_receipt(receipt_file, receipt):
    write_json(receipt_file, receipt)
    if ledger_enabled():
        store_put(RECEIPTS, "head", "received.json", receipt_file.read_bytes())


def ledger_records(last):
    """Every recorded bundle, oldest first: one GET per sequence. Past a few thousand bundles,
    list the packages API instead."""
    records = []
    for sequence in range(1, last + 1):
        raw = package_get(LEDGER, f"{sequence:012d}", "images.json")
        if raw:  # an interrupted run reserves a sequence and records nothing
            records.append(json.loads(raw))
    return records


def normalise(source):
    """Expand a short name the way docker pull does: alpine:3 is docker.io/library/alpine:3.
    Only for references mirror.py reads elsewhere (FROM lines) and for the hint in add; the
    catalog holds every source in full."""
    first, _, rest = source.partition("/")
    if not rest or not ("." in first or ":" in first or first == "localhost"):
        source = "docker.io/" + source
    host, _, repo = source.partition("/")
    if host == "docker.io" and "/" not in repo:
        source = f"{host}/library/{repo}"
    return source


def runnable(manifest, fetch=None, depth=0):
    """True for an image or an image index; false for signatures and other OCI artifacts.

    A cosign signature uses the OCI image config media type, so an image is also
    recognised by its layers. With fetch, each index child is fetched and checked
    itself; without it, a child is judged by its descriptor.
    """
    if manifest.get("artifactType"):
        return False
    if chart(manifest):
        return True
    if "manifests" in manifest:
        return any(image_child(child, fetch, depth) for child in manifest["manifests"])
    layers = manifest.get("layers")
    return (manifest.get("config", {}).get("mediaType") in IMAGE_CONFIGS and isinstance(layers, list)
            and all(IMAGE_LAYER.fullmatch(str(layer.get("mediaType"))) for layer in layers))


def chart(manifest):
    """A Helm chart pushed to an OCI registry: one chart config, a chart tarball and maybe its provenance."""
    layers = manifest.get("layers")
    kinds = [layer.get("mediaType") for layer in layers] if isinstance(layers, list) else []
    return (manifest.get("config", {}).get("mediaType") == CHART_CONFIG and CHART_LAYERS[0] in kinds
            and all(kind in CHART_LAYERS for kind in kinds))


def referrer(child):
    """A signature or a BuildKit attestation listed beside the images of an index."""
    return bool(child.get("artifactType") or (child.get("platform") or {}).get("os") == "unknown"
                or "vnd.docker.reference.type" in (child.get("annotations") or {}))


def image_child(child, fetch, depth):
    if referrer(child):
        return False
    kind = child.get("mediaType")
    if kind not in (*MANIFESTS, *INDEXES, None):
        return False
    if fetch and depth < 3 and re.fullmatch(DIGEST, str(child.get("digest"))):
        # A cosign signature shares the image manifest media type, so only the child itself can tell.
        return runnable(json.loads(fetch(child["digest"])), fetch, depth + 1)
    return kind in MANIFESTS or (kind is None and bool(child.get("platform")))


def rewrite(path, setting):
    """A repository path under the first matching old=new prefix rule in setting (comma-separated;
    the longest old prefix wins, matched on whole path segments)."""
    rules = []
    for rule in filter(None, (part.strip() for part in os.environ.get(setting, "").split(","))):
        old, separator, new = rule.partition("=")
        old, new = old.strip().strip("/"), new.strip().strip("/")
        if not separator or not old or not new:
            raise MirrorError(f"{setting}: expected old=new prefix pairs, not {rule!r}")
        rules.append((old, new))
    for old, new in sorted(rules, key=lambda rule: -len(rule[0])):
        if path == old or path.startswith(old + "/"):
            moved = new + path[len(old):]
            if not TARGET.fullmatch(moved):
                raise MirrorError(f"{setting}: {path} becomes {moved}, not a valid repository path")
            return moved
    return path


def resolve_entry(source, target):
    """Pin source to the digest its tag points at now; returns the catalog image and the tags it publishes."""
    name = source.split("@", 1)[0]
    if not re.match(rf"(?:{HOST})/", name):
        raise MirrorError(f"{source} has no registry host; give the full reference, for example {normalise(source)}")
    if not SOURCE.fullmatch(name) or not TARGET.fullmatch(target):
        raise MirrorError("add needs registry/repo:tag and org/repo[/path]")
    raw = run("skopeo", "inspect", "--raw", *options(side_of(source)), "docker://" + transport_reference(source))
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not runnable(json.loads(raw), lambda child: fetch_manifest(f"docker://{name.rsplit(':', 1)[0]}@{child}")):
        raise MirrorError(f"{name} is a signature, attestation or metadata artifact, not a container image; "
                          "add the tag you run, for example :latest, instead")
    image = parse_image(name + "@" + digest, target)
    if "@" in source and source.split("@", 1)[1] != digest:
        raise MirrorError("source digest does not match the manifest")
    repository = "docker://" + name.rsplit(":", 1)[0]
    tags = publish_tags(image["tag"], platform_digest(f"{repository}@{digest}", digest, side_of(source)),
                        repository, side_of(source))
    return image, tags


def write_catalog(path, lines):
    """Add catalog lines and keep the file sorted a to z by source. Leading comments stay at the
    top; a comment directly above an entry moves with it."""
    header, groups, notes = [], [], []
    for raw in (path.read_text().splitlines() if path.exists() else []):
        text = raw.strip()
        if not groups and (not text or text.startswith("#")):
            header.append(raw)
        elif text.startswith("#"):
            notes.append(raw)
        elif text:
            groups.append((text, notes + [raw]))
            notes = []
    groups += [(line, [line]) for line in lines]
    groups.sort(key=lambda group: group[0])
    groups += [("", notes)] if notes else []  # comments after the last entry stay last
    body = "\n".join(header + [line for _, block in groups for line in block])
    path.write_text(body + "\n" if body else "")


def add_image(path, source, target):
    image, tags = resolve_entry(source, target)
    images = catalog(path) if path.exists() else []
    if any((i['target'], i['tag']) == (target, image['tag']) for i in images):
        raise MirrorError("target tag already exists; edit its reviewed catalog entry")
    write_catalog(path, [f"{image['source']} {target}"])
    print(f"added: {image['source']} -> {target}:{', :'.join(tags)}")


def list_reference(text):
    """An image named in a list file, as docker pull reads it: no registry host means docker.io
    (and library/ for a one-part name), no tag means :latest. The catalog gets the full form."""
    full = normalise(text)
    name, separator, digest = full.partition("@")
    if ":" not in name.rsplit("/", 1)[-1]:
        name += ":latest"
    full = name + separator + digest
    if full != text:
        print(f"expanded: {text} -> {full}")
    return full


def add_list(path, folder, prefix=None):
    """Add every image named in folder/*.txt, one per line. Each target is the source path without
    its registry host, under prefix. What resolves is written even when other lines fail."""
    files = sorted(Path(folder).glob("*.txt"))
    if not files:
        raise MirrorError(f"{folder}: no *.txt files")
    existing = {(i["target"], i["tag"]) for i in (catalog(path) if path.exists() else [])}
    added, failed = [], []
    for file in files:
        for number, raw in enumerate(file.read_text().splitlines(), 1):
            text = raw.split("#", 1)[0].strip()
            if not text:
                continue
            where = f"{file.name}:{number}"
            try:
                source = list_reference(text)
                name = source.split("@", 1)[0]
                repository, tag = name.rsplit(":", 1)
                target = repository.split("/", 1)[1]
                target = f"{prefix.strip('/')}/{target}" if prefix else target
                if (target, tag) in existing:
                    print(f"already in the catalog: {where} {source} -> {target}:{tag}")
                    continue
                image, tags = resolve_entry(source, target)
                existing.add((target, tag))
                added.append(f"{image['source']} {target}")
                print(f"added: {where} {image['source']} -> {target}:{', :'.join(tags)}")
            except MirrorError as error:
                failed.append(f"{where} {text}: {error}")
    if added:
        write_catalog(path, added)
    print(f"{len(added)} added to {path.name}, {len(failed)} failed")
    if failed:
        raise MirrorError(f"{len(failed)} line(s) not added:\n  " + "\n  ".join(failed))


def verify_image(directory, digest):
    """Check a skopeo dir: copy: every manifest, config and layer against its digest."""
    def check(descriptor, name):
        expected = descriptor.get("digest") if isinstance(descriptor, dict) else None
        if not isinstance(expected, str) or not re.fullmatch(DIGEST, expected):
            raise MirrorError("invalid image digest")
        path = directory / name.format(expected[7:])
        if (not path.is_file() or path.stat().st_size != descriptor.get("size", path.stat().st_size)
                or sha256(path) != expected[7:]):
            raise MirrorError(f"missing or corrupt image file: {expected}")
        return path

    def manifest(descriptor, name):
        data = json.loads(check(descriptor, name).read_bytes())
        if not isinstance(data, dict) or data.get("schemaVersion") != 2:
            raise MirrorError("unsupported image manifest schema")
        if "manifests" in data:
            for child in data["manifests"]:
                manifest(child, "{}.manifest.json")
        else:
            for blob in [data["config"], *data["layers"]]:
                check(blob, "{}")

    manifest({"digest": digest}, "manifest.json")


def platform_digest(reference, digest, side):
    """The digest to mirror: the whole image, or one platform's image when MIRROR_PLATFORM is set."""
    wanted = os.environ.get("MIRROR_PLATFORM", "").strip()
    if not wanted:
        return digest
    data = json.loads(run("skopeo", "inspect", "--raw", *options(side), reference))
    keys = ("os", "architecture", "variant")
    want = dict(zip(keys, wanted.split("/")))
    if chart(data):
        return digest  # a chart has no platform
    if "manifests" not in data:
        # A single image goes whole, but only when its config names the wanted platform.
        config = json.loads(run("skopeo", "inspect", "--config", "--raw", *options(side), reference))
        found = {k: config.get(k) for k in want} if isinstance(config, dict) else {}
        if found != want:
            raise MirrorError(f"{reference}: is a {'/'.join(str(v) for v in found.values())} image, not {wanted}")
        return digest
    repository = reference.rsplit("@", 1)[0]

    def platform(child):
        # Checked before the platform field: a referrer or nested index may name a platform too.
        if (child.get("mediaType") not in (*MANIFESTS, None) or referrer(child)
                or not re.fullmatch(DIGEST, str(child.get("digest")))):
            return {}  # a nested index or a referrer: not an image this index can select
        if "platform" in child:
            return child["platform"] or {}
        # The platform is optional in an index; the child image's config names it instead.
        config = json.loads(run("skopeo", "inspect", "--config", "--raw", *options(side),
                                f"{repository}@{child['digest']}"))
        return config if isinstance(config, dict) else {}

    matches = [child["digest"] for child in data["manifests"]
               if {k: platform(child).get(k) for k in want} == want]
    if len(matches) != 1 or not re.fullmatch(DIGEST, matches[0]):
        raise MirrorError(f"{reference}: expected one {wanted} image, found {len(matches)}")
    return matches[0]


def verify_platform(directory, image):
    """A platform image must be a child of the approved index carried beside it."""
    if image["transfer"] == image["digest"]:
        return
    path = directory / f"{image['digest'][7:]}.manifest.json"
    if not path.is_file() or sha256(path) != image["digest"][7:]:
        raise MirrorError(f"missing or corrupt approved index: {image['digest']}")
    children = json.loads(path.read_bytes()).get("manifests", [])
    listed = [child for child in children if child.get("digest") == image["transfer"]]
    if not listed:
        raise MirrorError(f"{image['transfer']} is not part of approved {image['digest']}")
    if any(referrer(child) or child.get("mediaType") not in (*MANIFESTS, None) for child in listed):
        raise MirrorError(f"{image['transfer']} in approved {image['digest']} is not an image")


def publish_tags(tag, transfer, repository, side):
    """The tags an image gets on both target registries: the source tag, unchanged.

    latest names no release, and once it moves the registry may garbage-collect the untagged
    old image. A latest image is therefore also tagged with the version it reports.
    """
    if tag != "latest":
        return [tag]
    version = app_version(f"{repository}@{transfer}", side)
    return ["latest", *([version] if version not in ("", "latest") else [])]


def app_version(reference, side):
    """The version an image reports about itself, for images published as latest.

    Bitnami's Docker Hub images ship only latest; every other tag is a signature or
    metadata artifact. Same order of preference as quay-diode-poc's pack-inventory.
    An image that reports no version gives "". A failed lookup raises, so a run
    never reports success while the version tag is missing.
    """
    wanted = os.environ.get("MIRROR_PLATFORM", "").strip()
    os_name, _, arch = wanted.partition("/")
    override = [f"--override-os={os_name}", f"--override-arch={arch.split('/')[0]}"] if wanted else []
    try:
        if not wanted:
            # Inspect one image of an index, not whichever platform this host happens to run.
            children = json.loads(run("skopeo", "inspect", "--raw", *options(side), reference)).get("manifests")
            images = [child["digest"] for child in children or [] if image_child(child, None, 0)]
            reference = reference.rsplit("@", 1)[0] + "@" + images[0] if images else reference
        data = json.loads(run("skopeo", *override, "inspect", *options(side), reference))
    except MirrorError as error:
        raise MirrorError(f"cannot read the version of {reference}: {error}") from error
    labels = data.get("Labels") or {}
    found = [labels.get("org.opencontainers.image.version"), labels.get("app.kubernetes.io/version")]
    found += [env.split("=", 1)[1] for env in data.get("Env") or [] if env.startswith("APP_VERSION=")]
    return next((v for v in found if v and re.fullmatch(TAG, v)), "")


def post_file(path, kind, extra=()):
    """POST one file to NIFI_URL with its sha256 in X-Sha256 and the routing headers."""
    url = os.environ["NIFI_URL"]
    parts = urllib.parse.urlsplit(url)
    shown = parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()  # no credentials in the log
    file_sum, file_size = sha256(path), path.stat().st_size
    # HTTP/1.1 keeps the header names as written; HTTP/2 would lowercase them and break a case-sensitive
    # RouteOnAttribute. --upload-file streams the file (--data-binary would read it all into memory),
    # an empty Expect skips the 100-continue round trip, and the time limit allows 1 MB/s. NiFi answers
    # 503 while its queue is full (back pressure); ten retries back off from 1 s to about 8 minutes.
    headers = ["Expect:", f"Filename: {path.name}", "Content-Type: application/octet-stream",
               f"X-Sha256: {file_sum}", "X-Artifact-Type: container-images", "X-Artifact-Format: tar",
               "X-Artifact-Action: mirror", f"X-Bundle-Kind: {kind}", *extra]
    status = run("curl", "--http1.1", "--fail", "--show-error", "--silent", "--connect-timeout", "15",
                 "--max-time", str(max(3600, file_size // 1_000_000)), "--retry", "10", "--request", "POST",
                 "--output", "/dev/null", "--write-out", "%{http_code}",
                 *[item for header in headers for item in ("--header", header)], "--upload-file", str(path), url)
    print(f"posted to NiFi: {path.name} -> {shown} (HTTP {status.decode().strip()}, {file_size} bytes, "
          f"X-Sha256 {file_sum})")


def notify(bundle, kind):
    """POST a written bundle to NIFI_URL. One file, one webhook on the high side: NiFi there verifies
    X-Sha256 and writes the .sha256 beside the bundle in the store. A bundle over
    MIRROR_BUNDLE_MAX_SIZE goes as <bundle>.part-001.. of at most that size (send() streams such a
    bundle straight into parts when it does not also keep it for hand-carry); X-Bundle-Name,
    X-Bundle-Sha256 and X-Bundle-Parts let the high side record the whole and import rejoin it."""
    if not os.environ.get("NIFI_URL"):
        return
    checksum, size, cap = sha256(bundle), bundle.stat().st_size, bundle_cap()
    count = -(-size // cap)
    if count <= 1:
        post_file(bundle, kind)
        return
    print(f"{bundle.name} is {size} bytes, over MIRROR_BUNDLE_MAX_SIZE ({cap}): sending {count} parts")
    extra = (f"X-Bundle-Name: {bundle.name}", f"X-Bundle-Sha256: {checksum}", f"X-Bundle-Parts: {count}")
    with bundle.open("rb") as whole:
        for number in range(1, count + 1):
            part = bundle.with_name(f"{bundle.name}.part-{number:03d}")
            with part.open("wb") as out:  # one part on disk at a time
                remaining = cap
                while remaining and (block := whole.read(min(remaining, 8 * 1024 * 1024))):
                    out.write(block)
                    remaining -= len(block)
            try:
                post_file(part, kind, extra)
            finally:
                part.unlink()


def sync(path, full=False):
    low = registry("TARGET_REGISTRY")
    settings(("NIFI_URL", "bundles are not posted to NiFi", False),
             ("MIRROR_BUNDLE_DIR", "bundles are not kept for hand-carry", False),
             ("MIRROR_LEDGER", "what was sent is kept only in MIRROR_STATE_DIR, which a CI job does not keep", False),
             ("MIRROR_PLATFORM", "every platform of each image is sent", False),
             ("MIRROR_BUNDLE_MAX_SIZE", "bundles are split at 4GiB", False))
    images = catalog(path)
    if not images:
        raise MirrorError("catalog is empty; add an image before syncing")
    work = state_path("MIRROR_STATE_DIR", "registry-mirror")
    with locked(work):
        state_file = work / "sent.json"
        state = load_state(state_file) or {
            "stream": uuid.uuid4().hex, "sequence": 0, "sent": {}, "registry": low}
        loaded = json.dumps(state, sort_keys=True)
        if state.get("registry") != low:
            raise MirrorError(f"{work} belongs to another low registry; set a separate MIRROR_STATE_DIR")
        full = full or not state["sequence"] or state.get("pending", False)
        changed, known, platforms = [], state.get("platforms", {}), {}
        platform = os.environ.get("MIRROR_PLATFORM", "").strip()
        # A tag a catalog entry pins, or pinned before its line was removed, is never a version tag.
        pinned = state.setdefault("pinned", sorted(state["sent"]))
        catalog_tags = {f"{i['target']}:{i['tag']}" for i in images} | set(pinned)
        for image in images:
            key = f"{image['target']}:{image['tag']}"
            # The ledger records the platform too, so changing MIRROR_PLATFORM resends the image.
            sent = f"{image['digest']} {platform}".strip()
            # Pin the source by digest; source tag movement cannot change approved bytes.
            source = transport_reference(image["source"])
            # Remember each platform and tag choice so an unchanged image needs no source request.
            lookup = f"{image['digest']} {platform} {image['tag']}"
            # A full sync is a recovery, so it asks the source again.
            cached = None if full else known.get(lookup)
            if isinstance(cached, list) and len(cached) == 2 and isinstance(cached[1], list):
                transfer, tags = cached
            else:
                transfer = platform_digest("docker://" + source, image["digest"], side_of(source))
                tags = publish_tags(image["tag"], transfer, "docker://" + source.split("@")[0], side_of(source))
            if tags != ["latest"]:  # a latest image with no version yet is asked again next run
                platforms[lookup] = [transfer, tags]
            for tag in tags[1:]:
                if f"{image['target']}:{tag}" in catalog_tags:
                    print(f"warning: {key} reports version {tag}, which images.txt pins or once pinned; not tagging it",
                          file=sys.stderr)
            tags = [tags[0], *[t for t in tags[1:] if f"{image['target']}:{t}" not in catalog_tags]]
            # sent.json records the digest per catalog tag and, under aliases, the version tags sent with it.
            aliases = state.get("aliases", {}).get(key, [])
            if key not in pinned:
                # Recorded before the first copy, so a run that fails after it still protects the tag.
                pinned.append(key)
                save_state(state_file, state, remote=False)
            known_tags = [image["tag"], *aliases] if state["sent"].get(key) == sent else []
            for number, tag in enumerate(tags):
                destination = f"docker://{low}/{image['target']}:{tag}"
                observed = None
                if tag in known_tags or side_of(source) == "TARGET_REGISTRY":  # an own image is already there
                    try:
                        observed = raw_digest(destination, "TARGET_REGISTRY")
                    except MirrorError as error:
                        # Missing, deleted or expired: copy it again; a real fault fails the copy below.
                        say(f"low copy unreadable, recopying: {error}")
                if observed == transfer:
                    print(f"already in target registry: {low}/{image['target']}:{tag}@{transfer}")
                    continue
                if number:  # a version tag points at the image already in the target registry
                    copy(f"docker://{low}/{image['target']}@{transfer}", destination, "TARGET_REGISTRY", "TARGET_REGISTRY")
                else:
                    copy("docker://" + source.split("@")[0] + "@" + transfer, destination, side_of(source), "TARGET_REGISTRY")
                if raw_digest(destination, "TARGET_REGISTRY") != transfer:
                    raise MirrorError(f"low mirror digest mismatch: {image['target']}:{tag}")
                print(f"pushed to target registry: {low}/{image['target']}:{tag}@{transfer}")
            names = f"{image['target']}:{', :'.join(tags)}"
            due = full or state["sent"].get(key) != sent or aliases != tags[1:]
            if due:
                changed.append(image | {"transfer": transfer, "tags": tags})
                print(f"send: {names} {transfer}")
            else:
                say(f"unchanged: {names} {transfer}")
        state["platforms"] = platforms
        if changed and not bundle_destination():
            # The ledger says what crossed to the high side, so it records only a bundle that left.
            save_state(state_file, state, remote=json.dumps(state, sort_keys=True) != loaded)
            print(f"target registry updated; {len(changed)} image(s) not sent to the high side: "
                  "set NIFI_URL, or MIRROR_BUNDLE_DIR to hand-carry bundles")
            return None
        if not changed:
            # A quiet day rewrites the ledger head only when the cached tag choices changed.
            save_state(state_file, state, remote=json.dumps(state, sort_keys=True) != loaded)
            print("nothing to send; low-side digests verified")
            return None
        return send_batches(work, state_file, state, low, changed, full, "full" if full else "delta")


def bundle_cap():
    """MIRROR_BUNDLE_MAX_SIZE in bytes: a plain number or with KiB, MiB, GiB or TiB."""
    text = os.environ.get("MIRROR_BUNDLE_MAX_SIZE", "").strip() or "4GiB"
    match = re.fullmatch(r"([0-9]+)\s*(KiB|MiB|GiB|TiB)?", text, re.IGNORECASE)
    if not match or not int(match[1]):
        raise MirrorError(f"MIRROR_BUNDLE_MAX_SIZE must be bytes or a number with KiB, MiB, GiB or TiB, not {text!r}")
    return int(match[1]) * 1024 ** ["", "kib", "mib", "gib", "tib"].index((match[2] or "").lower())


def image_bytes(reference, side):
    """The bytes skopeo copy --all moves for reference (by digest): each manifest, config and layer once."""
    seen, total = set(), 0
    pending = [reference]
    while pending:
        current = pending.pop()
        raw = run("skopeo", "inspect", "--raw", *options(side), current)
        manifest = json.loads(raw)
        total += len(raw)
        for child in manifest.get("manifests", []):
            if child["digest"] not in seen:
                seen.add(child["digest"])
                pending.append(current.rsplit("@", 1)[0] + "@" + child["digest"])
        for blob in [manifest.get("config") or {}, *manifest.get("layers", [])]:
            if blob.get("digest") and blob["digest"] not in seen:
                seen.add(blob["digest"])
                total += int(blob.get("size", 0))
    return total


def batches(images, low):
    """Split images into bundles of at most MIRROR_BUNDLE_MAX_SIZE, in catalog order. A store,
    NiFi or link limit then applies to one bundle, and the runner holds one bundle at a time.
    An image larger than the cap travels alone."""
    cap, groups, size = bundle_cap(), [[]], 0
    for image in images:
        try:
            need = image_bytes(f"docker://{low}/{image['target']}@{image['transfer']}", "TARGET_REGISTRY")
        except MirrorError:  # a resend of an image the target registry lost: ask the source
            upstream = transport_reference(image["source"]).rsplit("@", 1)[0]
            need = image_bytes(f"docker://{upstream}@{image['transfer']}", side_of(upstream))
        if need > cap:
            print(f"{image['target']}:{image['tag']} is {need} bytes, over MIRROR_BUNDLE_MAX_SIZE ({cap}): "
                  "it goes in a bundle of its own, sent in parts")
        if groups[-1] and size + need > cap:
            groups.append([])
            size = 0
        groups[-1].append(image)
        size += need
    if len(groups) > 1:
        print(f"{len(images)} image(s) split into {len(groups)} bundles of at most {cap} bytes")
    return groups


def send_batches(work, state_file, state, low, changed, full, kind, bridges=None):
    """send() each size-capped batch in turn. Only the first may be full or bridge a gap: the high
    side accepts that one over a gap and the rest follow it in sequence."""
    bundle = None
    for number, batch in enumerate(batches(changed, low)):
        bundle = send(work, state_file, state, low, batch, full and not number, kind, None if number else bridges)
    return bundle


def bundle_destination():
    """Where a bundle goes: NiFi, or a directory someone carries across. None means nowhere."""
    return os.environ.get("NIFI_URL") or os.environ.get("MIRROR_BUNDLE_DIR")


def send(work, state_file, state, low, changed, full, kind, bridges=None):
    """Write the next bundle, record it in the ledger and hand it to NiFi.

    bridges: the first sequence a resend stands in for, so the high side accepts it over a gap.
    """
    platform = os.environ.get("MIRROR_PLATFORM", "").strip()
    outbox = Path(os.environ.get("MIRROR_BUNDLE_DIR") or work / "bundles")
    outbox.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("MIRROR_BUNDLE_DIR"):
        # Bundles or parts a failed run left behind are never sent: its sequence was reserved, so this
        # run is full and carries their images again.
        for stale in outbox.glob("mirror-*"):
            print(f"removed {stale.name}: left by a run that did not deliver it")
            stale.unlink()
    # Reserve a sequence before publishing. An interrupted run forces the next one full.
    state.update(sequence=state["sequence"] + 1, pending=True)
    save_state(state_file, state)
    metadata = {"schema": 1, "stream": state["stream"], "sequence": state["sequence"], "full": full,
                "images": changed, **({"from": bridges} if bridges else {})}
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        stage = Path(temporary)
        (stage / "images").mkdir()
        for image in changed:
            directory = stage / "images" / image["transfer"].replace(":", "-")
            if not directory.exists():
                # dir: keeps a Docker manifest list byte for byte; oci: would have to convert it.
                try:
                    copy(f"docker://{low}/{image['target']}@{image['transfer']}", f"dir:{directory}", "TARGET_REGISTRY")
                except MirrorError:
                    if kind != "resend":
                        raise
                    # The target registry lost it: a resend takes the same digest from the source.
                    shutil.rmtree(directory, ignore_errors=True)
                    upstream = transport_reference(image["source"]).rsplit("@", 1)[0]
                    copy(f"docker://{upstream}@{image['transfer']}", f"dir:{directory}", side_of(upstream))
                verify_image(directory, image["transfer"])
            if image["transfer"] != image["digest"]:
                # Carry the approved index so import can check the platform image is listed in it.
                index = run("skopeo", "inspect", "--raw", *options(side_of(image["source"])),
                            "docker://" + transport_reference(image["source"]))
                (directory / f"{image['digest'][7:]}.manifest.json").write_bytes(index)
                verify_platform(directory, image)
        write_json(stage / "images.json", metadata)
        bundle = outbox / f"mirror-{state['stream']}-{state['sequence']:012d}.tar"
        streamed = None
        if os.environ.get("NIFI_URL") and not os.environ.get("MIRROR_BUNDLE_DIR"):
            # Measure first: a bundle over the cap goes to NiFi as parts straight from the tar writer,
            # so the runner never holds the whole tar beside the staged images.
            measure = TarSink()
            write_tar(stage, measure)
            if measure.size > bundle_cap():
                streamed = measure
        if streamed is None:
            temporary_bundle = bundle.with_suffix(".tmp")
            with temporary_bundle.open("wb") as stream:
                write_tar(stage, stream)
            os.replace(temporary_bundle, bundle)
            sidecar = bundle.with_name(bundle.name + ".sha256")
            temporary_sidecar = sidecar.with_suffix(".tmp")
            temporary_sidecar.write_text(f"{sha256(bundle)}  {bundle.name}\n")
            os.replace(temporary_sidecar, sidecar)  # readiness marker, published last
        else:
            if ledger_enabled():  # recorded before the first part leaves, as for a whole bundle
                record_ledger(state, metadata, kind, bundle.name)
            post_parts(stage, bundle, streamed, kind)
    if streamed is None:
        if ledger_enabled():
            record_ledger(state, metadata, kind, bundle.name)
        size = bundle.stat().st_size
        notify(bundle, kind)
        if os.environ.get("NIFI_URL"):
            # Delivered: the images stay in the target registry and the record in the ledger, so nothing stays on the runner.
            for path in (bundle, bundle.with_name(bundle.name + ".sha256")):
                path.unlink()
    else:
        size = streamed.size
    if kind != "resend":  # a resend repeats what the ledger already records
        state["sent"].update({f"{i['target']}:{i['tag']}": f"{i['digest']} {platform}".strip() for i in changed})
        aliases = state.setdefault("aliases", {})
        for i in changed:
            if i["tags"][1:]:
                aliases[f"{i['target']}:{i['tag']}"] = i["tags"][1:]
            else:
                aliases.pop(f"{i['target']}:{i['tag']}", None)
    state["pending"] = False
    save_state(state_file, state)
    print(f"bundle: {bundle.name} ({len(changed)} image(s), {size} bytes)"
          + (", delivered and removed" if os.environ.get("NIFI_URL") else f", left in {bundle.parent}"))
    return bundle


def record_ledger(state, metadata, kind, name):
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = metadata | {"created": created, "kind": kind, "bundle": name}
    package_put(LEDGER, f"{state['sequence']:012d}", "images.json", json.dumps(record, indent=2, sort_keys=True).encode())


def write_tar(stage, stream):
    """The bundle's tar, in a fixed order, so writing it twice gives the same bytes."""
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for item in sorted(stage.rglob("*")):
            if item.is_file():
                archive.add(item, arcname=str(item.relative_to(stage)), recursive=False)


class TarSink:
    """A write-only file that keeps only the size and sha256 of what passes through."""

    def __init__(self):
        self.size, self.digest = 0, hashlib.sha256()

    def write(self, data):
        self.size += len(data)
        self.digest.update(data)
        return len(data)

    def tell(self):
        return self.size


class PartSink(TarSink):
    """Writes the tar into part files of at most cap bytes, handing each to post() once full."""

    def __init__(self, directory, name, cap, post):
        super().__init__()
        self.directory, self.name, self.cap, self.post = directory, name, cap, post
        self.number, self.current, self.filled = 0, None, 0

    def write(self, data):
        view = memoryview(data)
        while view:
            if self.current is None:
                self.number += 1
                self.current = (self.directory / f"{self.name}.part-{self.number:03d}").open("wb")
                self.filled = 0
            taken = view[:self.cap - self.filled]
            self.current.write(taken)
            self.filled += len(taken)
            view = view[len(taken):]
            if self.filled == self.cap:
                self.flush_part()
        return super().write(data)

    def flush_part(self):
        if self.current is not None:
            self.current.close()
            path = Path(self.current.name)
            self.current = None
            try:
                self.post(path)
            finally:
                path.unlink(missing_ok=True)


def post_parts(stage, bundle, measured, kind):
    """Send a bundle over the cap as parts written straight from the tar, one part on disk at a time.
    The first pass gave the size and checksum every part carries; this pass must match it."""
    cap = bundle_cap()
    count = -(-measured.size // cap)
    checksum = measured.digest.hexdigest()
    print(f"{bundle.name} is {measured.size} bytes, over MIRROR_BUNDLE_MAX_SIZE ({cap}): streaming {count} parts")
    extra = (f"X-Bundle-Name: {bundle.name}", f"X-Bundle-Sha256: {checksum}", f"X-Bundle-Parts: {count}")
    sink = PartSink(bundle.parent, bundle.name, cap, lambda path: post_file(path, kind, extra))
    write_tar(stage, sink)
    sink.flush_part()
    if (sink.size, sink.digest.hexdigest()) != (measured.size, checksum):
        raise MirrorError(f"{bundle.name}: the streamed tar differs from the measured one; the parts sent are unusable")


def sequence_range(text, last):
    """N, N.. or N..M as an inclusive range; an open end means the newest bundle."""
    match = re.fullmatch(r"([0-9]+)(\.\.([0-9]*))?", text or "")
    if not match:
        raise MirrorError(f"--sequence {text!r}: expected N, N.. or N..M")
    first = int(match[1])
    final = int(match[3]) if match[3] else (last if match[2] else first)
    if not 1 <= first <= final:
        raise MirrorError(f"--sequence {text!r}: empty range")
    return first, final


def resend(since=None, sequences=None, image=None):
    """Resend recorded images as the next bundle: by date, by sequence or by target."""
    if not ledger_enabled():
        raise MirrorError("resend reads the ledger; set MIRROR_LEDGER=true")
    if since and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", since):
        raise MirrorError(f"--since {since!r}: expected YYYY-MM-DD")
    if not (since or sequences or image):
        raise MirrorError("resend needs --since, --sequence or --image")
    if not bundle_destination():
        raise MirrorError("resend writes a bundle; set NIFI_URL, or MIRROR_BUNDLE_DIR to hand-carry it")
    low = registry("TARGET_REGISTRY")
    work = state_path("MIRROR_STATE_DIR", "registry-mirror")
    with locked(work):
        state_file = work / "sent.json"
        state = load_state(state_file)
        if not state or not state["sequence"]:
            raise MirrorError("the ledger records no bundle yet; run sync first")
        if state.get("registry") != low:
            raise MirrorError("the ledger belongs to another low registry")
        first, final = sequence_range(sequences, state["sequence"]) if sequences else (1, state["sequence"])
        latest, wanted, matched = {}, set(), []
        for record in ledger_records(state["sequence"]):
            in_range = (first <= record["sequence"] <= final
                        and (not since or record["created"][:10] >= since))
            if in_range:
                matched.append(record["sequence"])
            for item in record["images"]:
                key = f"{item['target']}:{item['tag']}"
                latest[key] = item
                if in_range and (not image or image in (item["target"], key)):
                    wanted.add(key)
        if not wanted:
            print("nothing recorded matches; no bundle written")
            return None
        # The newest record of each tag: an older digest would roll a moved tag back on the high side.
        chosen = [latest[key] for key in sorted(wanted)]
        for item in chosen:
            print(f"resend: {item['target']}:{', :'.join(item['tags'])} {item['transfer']}")
        # Everything recorded from the first match to the newest bundle stands in for those bundles.
        bridges = matched[0] if not image and final >= state["sequence"] else None
        return send_batches(work, state_file, state, low, chosen, False, "resend", bridges)


BUNDLE_ENTRY = re.compile(r"images\.json|images/sha256-[0-9a-f]{64}/(?:version|manifest\.json|signature-[0-9]+|"
                          r"[0-9a-f]{64}(?:\.manifest\.json)?)")


class PartReader(io.RawIOBase):
    """The parts of a bundle read as one stream, hashed as they pass. Each part is fetched only when
    the reader reaches it and deleted once read, so the job holds one part, not the joined bundle."""

    def __init__(self, fetch, names, scratch):
        super().__init__()
        self.fetch, self.names, self.scratch = fetch, list(names), scratch
        self.digest, self.current, self.number = hashlib.sha256(), None, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.current is None:
                if self.number == len(self.names):
                    return 0
                path = self.scratch / "part.download"
                if not self.fetch(self.names[self.number], path):
                    raise MirrorError(f"{self.names[self.number]} disappeared from the store during import")
                self.current, self.number = path.open("rb"), self.number + 1
            count = self.current.readinto(buffer)
            if count:
                self.digest.update(memoryview(buffer)[:count])
                return count
            self.current.close()
            (self.scratch / "part.download").unlink()
            self.current = None

    def close(self):
        if self.current is not None:  # an extract that failed part way leaves a part open
            self.current.close()
            (self.scratch / "part.download").unlink(missing_ok=True)
            self.current = None
        super().close()


def extract_stream(stream, directory):
    """Unpack a bundle read front to back (no seeking), with the same entry rules as extract."""
    seen = set()
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if not member.isfile() or not BUNDLE_ENTRY.fullmatch(member.name) or member.name in seen:
                raise MirrorError(f"unsafe or duplicate bundle entry: {member.name}")
            seen.add(member.name)
            if member.size > shutil.disk_usage(directory).free:
                raise MirrorError("insufficient space to unpack bundle")
            target = directory / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
    while stream.read(1 << 20):  # tar padding after the end marker still counts toward the checksum
        pass


def extract(bundle, directory):
    allowed = re.compile(r"images\.json|images/sha256-[0-9a-f]{64}/(?:version|manifest\.json|signature-[0-9]+|[0-9a-f]{64}(?:\.manifest\.json)?)")
    with tarfile.open(bundle) as archive:
        seen = set()
        members = archive.getmembers()
        if sum(m.size for m in members) > shutil.disk_usage(directory).free:
            raise MirrorError("insufficient space to unpack bundle")
        for member in members:
            if not member.isfile() or not allowed.fullmatch(member.name) or member.name in seen:
                raise MirrorError(f"unsafe or duplicate bundle entry: {member.name}")
            seen.add(member.name)
        for member in members:
            target = directory / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or not isinstance(manifest.get("stream"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", manifest["stream"])
            or type(manifest.get("sequence")) is not int or manifest["sequence"] < 1
            or type(manifest.get("full")) is not bool
            or not isinstance(manifest.get("images"), list) or not manifest["images"]
            or ("from" in manifest and (type(manifest["from"]) is not int
                                        or not 1 <= manifest["from"] < manifest["sequence"]))):
        raise MirrorError("invalid bundle metadata")
    seen = set()
    for image in manifest["images"]:
        fields = ('source', 'target', 'tag', 'digest', 'transfer')
        if isinstance(image, dict) and "tags" not in image:
            image["tags"] = [image.get("tag")]  # a sender from before version tags: the catalog tag alone
        tags = image.get("tags") if isinstance(image, dict) else None
        if (not isinstance(image, dict) or set(image) != {*fields, "tags"}
                or not all(isinstance(image[k], str) for k in fields) or not re.fullmatch(DIGEST, image["transfer"])
                or not isinstance(tags, list) or not all(isinstance(t, str) and re.fullmatch(TAG, t) for t in tags)
                or tags[:1] != [image["tag"]] or len(set(tags)) != len(tags)
                or len(tags) > (2 if image["tag"] == "latest" else 1)):
            raise MirrorError("invalid image entry")
        if parse_image(image["source"], image["target"]) | {k: image[k] for k in ("transfer", "tags")} != image:
            raise MirrorError("image metadata does not match pinned source")
        key = f"{image['target']}:{image['tag']}"
        if key in seen:
            raise MirrorError("duplicate target tag in bundle")
        seen.add(key)


def import_one(bundle, adopt_stream=False, superseded_ok=False, record=None, unpack=None):
    """Verify a bundle and push its images. unpack(stage) streams a bundle that came in parts into
    stage and returns its sha256; the checksum is then compared before anything is pushed."""
    high = registry("TARGET_REGISTRY")
    work = state_path("IMPORT_STATE_DIR", "registry-mirror-import")
    sidecar = bundle.with_name(bundle.name + ".sha256")
    fields = sidecar.read_text().split()[:2] if sidecar.exists() else []
    if unpack is None:
        digest = sha256(bundle)
        if fields != [digest, bundle.name]:
            raise MirrorError(f"{bundle.name}: missing or mismatched checksum")
    with locked(work), tempfile.TemporaryDirectory(dir=work) as temporary:
        stage = Path(temporary)
        if unpack is None:
            extract(bundle, stage)
        else:
            digest = unpack(stage)
            if fields != [digest, bundle.name]:
                raise MirrorError(f"{bundle.name}: the joined parts do not match the bundle checksum")
        manifest = json.loads((stage / "images.json").read_text())
        validate_manifest(manifest)
        for image in manifest["images"]:
            verify_image(stage / "images" / image["transfer"].replace(":", "-"), image["transfer"])
            verify_platform(stage / "images" / image["transfer"].replace(":", "-"), image)
        receipt_file = work / "received.json"
        receipt = load_receipt(receipt_file)
        if receipt and receipt.get("registry") != high:
            raise MirrorError(f"{work} belongs to another high registry; set a separate IMPORT_STATE_DIR")
        if receipt and receipt["stream"] != manifest["stream"]:
            if not adopt_stream or not manifest["full"]:
                raise MirrorError("different sender stream; review a full bundle and use --adopt-stream")
            receipt = {}
        previous = receipt.get("sequence", 0)
        if manifest["sequence"] == previous and digest == receipt.get("digest"):
            print(f"already imported: {bundle.name}")
            return
        if manifest["sequence"] <= previous:
            if superseded_ok and manifest["sequence"] < previous:
                print(f"superseded: {bundle.name}")
                return
            raise MirrorError("stale or conflicting bundle; refusing rollback")
        # A resend from the low side stands in for every bundle from manifest["from"] on.
        if (manifest["sequence"] != previous + 1 and not manifest["full"]
                and manifest.get("from", previous + 2) > previous + 1):
            missing = (f"{previous + 1}" if manifest["sequence"] == previous + 2
                       else f"{previous + 1} to {manifest['sequence'] - 1}")
            raise SequenceGap(f"missing earlier bundle {missing} (imported up to {previous}, received "
                              f"{manifest['sequence']}); on the low side, Run pipeline with "
                              f"RESEND_SEQUENCE={previous + 1}.. (mirror.py resend --sequence {previous + 1}..)")
        pushed = []
        for image in manifest["images"]:
            target = rewrite(image["target"], "IMPORT_PATH_REWRITE")
            moved = f" (sent as {image['target']}, IMPORT_PATH_REWRITE)" if target != image["target"] else ""
            for tag in image["tags"]:
                destination = f"docker://{high}/{target}:{tag}"
                copy(f"dir:{stage / 'images' / image['transfer'].replace(':', '-')}", destination,
                     destination_side="TARGET_REGISTRY")
                if raw_digest(destination, "TARGET_REGISTRY") != image["transfer"]:
                    raise MirrorError(f"high mirror digest mismatch: {target}:{tag}")
                print(f"pushed to target registry: {high}/{target}:{tag}@{image['transfer']}{moved}")
                pushed.append({"target": target, "tag": tag, "digest": image["transfer"]})
        if record:
            record.write_text(json.dumps(json.loads(record.read_text()) + pushed, indent=2) + "\n")
        save_receipt(receipt_file, {"stream": manifest["stream"], "sequence": manifest["sequence"],
                                    "digest": digest, "registry": high})
    print(f"imported: {bundle.name} ({len(manifest['images'])} image(s), digests verified)")


def import_inbox(inbox, adopt_stream=False, record=None):
    def consume(bundle):
        import_one(bundle, adopt_stream, superseded_ok=True, record=record)
        done = inbox / "done"
        done.mkdir(exist_ok=True)
        os.replace(bundle.with_name(bundle.name + ".sha256"), done / (bundle.name + ".sha256"))
        os.replace(bundle, done / bundle.name)

    waiting = []
    for bundle in sorted(inbox.glob("mirror-*.tar")):
        if not bundle.with_name(bundle.name + ".sha256").exists():
            print(f"waiting for checksum: {bundle.name}")
            continue
        try:
            consume(bundle)
        except SequenceGap:
            waiting.append(bundle)
    # A later full bundle can bridge a gap without the operator deleting old inbox files.
    for bundle in waiting:
        consume(bundle)


def import_registry(name=None, adopt_stream=False, record=None):
    """Import bundles NiFi uploaded to IMPORT_STORE (the generic package registry or S3), oldest first.

    name is the file a trigger announces. The receipt names the next sequence, so bundles
    whose trigger never arrived are fetched too. A bundle missing either file waits.
    """
    cleanup = flag("IMPORT_DELETE_BUNDLES")
    settings(("IMPORT_STORE", "bundles are read from this project's package registry (gitlab)", False),
             ("MIRROR_LEDGER", "the receipt is kept only in IMPORT_STATE_DIR, which a CI job does not keep", False),
             ("IMPORT_DELETE_BUNDLES", "imported bundles stay in the store", False),
             ("PACKAGE_TOKEN", "the job token is used; GitLab refuses it a package delete", True),
             ("IMPORT_PATH_REWRITE", "images land at the path the low side sent", False),
             ("IMPORT_GAP_GRACE", "a later bundle waits 6 h for an earlier one before import fails", False))
    if cleanup and not s3_enabled() and not os.environ.get("PACKAGE_TOKEN"):
        # Checked before importing: a job token reads packages but GitLab refuses it a delete (HTTP 403).
        raise MirrorError("IMPORT_DELETE_BUNDLES with the package registry needs PACKAGE_TOKEN "
                          "(a project access token, role Maintainer, scope api)")
    work = state_path("IMPORT_STATE_DIR", "registry-mirror-import")
    downloads = work / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    removed = set()

    def remove(stem, names=None):
        if stem in removed:
            return
        removed.add(stem)
        if not cleanup:
            print(f"kept in the store: {stem} (IMPORT_DELETE_BUNDLES is not true)")
            return
        if names is None:  # a leftover: its checksum file says whether it came in parts
            sidecar = store_get(BUNDLES, stem, f"{stem}.tar.sha256")
            if sidecar is None:  # S3 reports success for a missing object, so ask first
                print(f"nothing to delete in the store: {stem}")
                return
            fields = sidecar.decode().split()
            count = int(fields[2]) if len(fields) == 3 and fields[2].isdigit() else 0
            names = [f"{stem}.tar.part-{n:03d}" for n in range(1, count + 1)] or [f"{stem}.tar"]
        if store_delete(BUNDLES, stem, [*names, f"{stem}.tar.sha256"]):
            print(f"deleted from the store: {stem}")
        else:
            print(f"nothing to delete in the store: {stem}")

    def consume(stem, quiet=False):
        bundle = downloads / f"{stem}.tar"
        sidecar = bundle.with_name(bundle.name + ".sha256")
        if not store_get(BUNDLES, stem, sidecar.name, sidecar):
            if not quiet:
                print(f"waiting for {sidecar.name}")
            return False
        # NiFi writes "<sha256>  <bundle>.tar" and, for a bundle sent in parts, the number of parts.
        fields = sidecar.read_text().split()
        count = int(fields[2]) if len(fields) == 3 and fields[2].isdigit() else 0
        names = [f"{bundle.name}.part-{n:03d}" for n in range(1, count + 1)] or [bundle.name]
        unpack = None
        if count:
            missing = [name for name in names if not store_exists(BUNDLES, stem, name)]
            if missing:
                print(f"waiting for {', '.join(missing)} ({len(missing)} of {count} parts)")
                return False

            def unpack(stage):
                with PartReader(lambda name, path: store_get(BUNDLES, stem, name, path), names, downloads) as reader:
                    extract_stream(io.BufferedReader(reader, 8 * 1024 * 1024), stage)
                print(f"read {count} parts of {bundle.name} in order, one on disk at a time")
                return reader.digest.hexdigest()
        elif not store_get(BUNDLES, stem, bundle.name, bundle):
            if not quiet:
                print(f"waiting for {bundle.name}")
            return False
        import_one(bundle, adopt_stream, superseded_ok=True, record=record, unpack=unpack)
        for path in (bundle, sidecar):
            path.unlink(missing_ok=True)
        remove(stem, names)  # its images are in the target registry and the receipt has moved past it
        return True

    match = BUNDLE_NAME.fullmatch(name or "")
    if name and not match:
        raise MirrorError(f"{name!r} is not a bundle name")
    receipt = load_receipt(work / "received.json")
    # Catch up from the receipt, or on a first import from the start of the announced stream.
    stream = receipt.get("stream") or (match[1].split("-")[1] if match else None)
    if stream:
        sequence = receipt.get("sequence", 0) + 1
        while consume(f"mirror-{stream}-{sequence:012d}", quiet=True):
            sequence += 1
    if match:
        current = load_receipt(work / "received.json")
        stem_stream, stem_sequence = match[1].split("-")[1], int(match[1].split("-")[2])
        if current.get("stream") == stem_stream and stem_sequence <= current.get("sequence", 0):
            # Imported already, by an earlier trigger or the catch-up above; its files may be gone.
            done = "already imported" if stem_sequence == current.get("sequence") else "superseded"
            print(f"{done}: {match[1]}.tar")
            if cleanup:  # left behind by a run that imported it without cleanup
                remove(match[1])
            else:
                print(f"not checking the store for {match[1]}: IMPORT_DELETE_BUNDLES is not true")
        else:
            try:
                consume(match[1])
            except SequenceGap as gap:
                # Bundles cross the link in any order. A later one waits in the store and the catch-up
                # above imports it when the earlier one arrives; only a long wait means one was lost.
                waited, grace = store_age(BUNDLES, match[1], f"{match[1]}.tar"), gap_grace()
                if waited is None or waited >= grace:
                    age = "for an unknown time" if waited is None else f"for {waited:.1f} h"
                    raise SequenceGap(f"{gap} [{match[1]}.tar has waited {age}; IMPORT_GAP_GRACE is {grace:g} h]") \
                        from None
                raise GapWaiting(f"{match[1]}.tar is stored and imports when the earlier bundle arrives "
                                 f"(waited {waited:.1f} h of IMPORT_GAP_GRACE {grace:g} h). If it never "
                                 f"arrives: {gap}") from None


def promote(record):
    """Copy the images an import recorded from SOURCE_REGISTRY (dev) to TARGET_REGISTRY (prod), by digest.

    The digest is checked in dev before the copy and in prod after it, so prod holds the bytes
    that were imported and approved, whatever the dev tags point at by now.
    """
    dev, prod = registry("SOURCE_REGISTRY"), registry("TARGET_REGISTRY")
    settings(("PROMOTE_PATH_REWRITE", "images keep their dev path in prod", False))
    if dev == prod:
        raise MirrorError("SOURCE_REGISTRY and TARGET_REGISTRY are the same registry; nothing to promote")
    images = {}
    for entry in json.loads(record.read_text()):
        if (not isinstance(entry, dict) or not TARGET.fullmatch(str(entry.get("target")))
                or not re.fullmatch(TAG, str(entry.get("tag"))) or not re.fullmatch(DIGEST, str(entry.get("digest")))):
            raise MirrorError(f"{record}: invalid entry {entry!r}")
        images[f"{entry['target']}:{entry['tag']}"] = entry  # a later bundle's digest for the same tag wins
    if not images:
        print(f"nothing to promote: {record} lists no images")
        return
    for key, entry in images.items():
        source = f"docker://{dev}/{entry['target']}@{entry['digest']}"
        if raw_digest(source, "SOURCE_REGISTRY") != entry["digest"]:
            raise MirrorError(f"digest mismatch in SOURCE_REGISTRY: {dev}/{entry['target']}@{entry['digest']}")
        key = f"{rewrite(entry['target'], 'PROMOTE_PATH_REWRITE')}:{entry['tag']}"
        destination = f"docker://{prod}/{key}"
        copy(source, destination, "SOURCE_REGISTRY", "TARGET_REGISTRY")
        if raw_digest(destination, "TARGET_REGISTRY") != entry["digest"]:
            raise MirrorError(f"digest mismatch in TARGET_REGISTRY after the copy: {prod}/{key}")
        print(f"promoted: {dev}/{entry['target']}@{entry['digest']} -> {prod}/{key}")
    print(f"promoted {len(images)} image tag(s) from {dev} to {prod}, digests verified")


def pending(path):
    """Catalog entries the ledger has not sent at their current digest and platform: what sync sends next."""
    platform = os.environ.get("MIRROR_PLATFORM", "").strip()
    state = load_state(Path(os.devnull)) if ledger_enabled() else None
    sent = (state or {}).get("sent", {})
    for image in catalog(path):
        if sent.get(f"{image['target']}:{image['tag']}") != f"{image['digest']} {platform}".strip():
            print(image["source"])


def references(files):
    """Image references in Containerfiles (FROM) and manifests or rendered charts (image:)."""
    found = []
    for name in files:
        text = sys.stdin.read() if name == "-" else Path(name).read_text()
        stages = set()
        for number, line in enumerate(text.splitlines(), 1):
            match = re.match(r"\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
            if match:
                # An earlier stage, not an image; checked before this line's own alias is known.
                if match[1].lower() not in stages and match[1] != "scratch":
                    found.append((match[1], f"{name}:{number}"))
                if match[2]:
                    stages.add(match[2].lower())
                continue
            match = re.match(r"\s*(?:-\s*)?image:\s*[\"']?([^\"'\s#]+)", line)
            if match:
                found.append((match[1], f"{name}:{number}"))
    return found


def covers(path, files):
    """Fail when a referenced image is neither in the catalog nor ever sent, by source or target name."""
    images = catalog(path)
    if ledger_enabled():
        state = load_state(Path(os.devnull))
        images += [i for r in ledger_records(state["sequence"] if state else 0) for i in r["images"]]
    names, targets, digests = set(), set(), set()
    for image in images:
        names.add(image["source"].split("@")[0])
        targets.update(f"{image['target']}:{tag}" for tag in image.get("tags", [image["tag"]]))
        digests.update({image["digest"], image.get("transfer", image["digest"])})
    missing = 0
    for reference, where in references(files):
        if "$" in reference:
            print(f"skipped: {reference} ({where}) is built from a variable", file=sys.stderr)
            continue
        name, _, digest = reference.partition("@")
        name = normalise(name)
        if ":" not in name.rsplit("/", 1)[-1]:
            name += ":latest"
        if digest in digests or name in names or name.split("/", 1)[1] in targets:
            continue
        print(f"missing: {reference} ({where})")
        missing += 1
    if missing:
        raise MirrorError(f"{missing} image(s) not in the mirror catalog; add them with mirror.py add")
    print("every image is in the mirror catalog")


# Hard cutover from the product-named variables; each maps to exactly one new name.
RENAMED = {
    "LOW_QUAY_HOST": "TARGET_REGISTRY", "HIGH_QUAY_HOST": "TARGET_REGISTRY",
    "LOW_QUAY_USERNAME": "TARGET_REGISTRY_USERNAME", "HIGH_QUAY_USERNAME": "TARGET_REGISTRY_USERNAME",
    "LOW_QUAY_PASSWORD": "TARGET_REGISTRY_PASSWORD", "HIGH_QUAY_PASSWORD": "TARGET_REGISTRY_PASSWORD",
    "LOW_QUAY_TLS_VERIFY": "TARGET_REGISTRY_TLS_VERIFY", "HIGH_QUAY_TLS_VERIFY": "TARGET_REGISTRY_TLS_VERIFY",
    "UPSTREAM_REGISTRY": "SOURCE_REGISTRY", "UPSTREAM_USERNAME": "SOURCE_REGISTRY_USERNAME",
    "UPSTREAM_PASSWORD": "SOURCE_REGISTRY_PASSWORD", "UPSTREAM_TLS_VERIFY": "SOURCE_REGISTRY_TLS_VERIFY",
    "EXPORT_SINCE": "RESEND_SINCE", "EXPORT_SEQUENCE": "RESEND_SEQUENCE", "EXPORT_IMAGE": "RESEND_IMAGE",
    "MIRROR_FULL": "RESEND_ALL",
}


def check_renamed():
    stale = sorted(f"{old} is now {new}" for old, new in RENAMED.items() if os.environ.get(old))
    if stale:
        raise MirrorError("renamed variables are set; rename them: " + "; ".join(stale))


def login():
    """skopeo login to TARGET_REGISTRY, and to SOURCE_REGISTRY when SOURCE_REGISTRY_USERNAME is set.

    A registry without authentication needs no login: leave its USERNAME unset.
    """
    for side in ("TARGET_REGISTRY", "SOURCE_REGISTRY"):
        user = os.environ.get(f"{side}_USERNAME")
        if not user:
            continue
        host = registry(side)
        tls = "false" if os.environ.get(f"{side}_TLS_VERIFY") == "false" else "true"
        result = subprocess.run(["skopeo", "login", f"--tls-verify={tls}", "--username", user, "--password-stdin", host],
                                input=os.environ.get(f"{side}_PASSWORD", "").encode(), capture_output=True, check=False)
        if result.returncode:
            raise MirrorError(f"skopeo login {host} failed: {result.stderr.decode(errors='replace').strip()[-300:]}")
        print(f"logged in to {host} as {user}")


EXAMPLES = """
Low side (pull from the source, save to your registry, send bundles):
  TARGET_REGISTRY=registry.low.example.com mirror.py login
  mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus
  mirror.py add-list lists/ --prefix team   # every image in lists/*.txt
  mirror.py sync                      # NIFI_URL set: bundle posted to NiFi
  mirror.py resend --sequence 7..     # the high side reported bundle 7 missing

High side (receive bundles, push to your registry, promote dev to prod):
  TARGET_REGISTRY=registry.high.example.com mirror.py login
  mirror.py import --registry --name mirror-<stream>-000000000008.tar --record imported.json
  mirror.py import --inbox /transfer/in
  SOURCE_REGISTRY=registry.dev.example.com TARGET_REGISTRY=registry.prod.example.com mirror.py promote imported.json

Run "mirror.py <command> -h" for each command's options and examples.
"""


def main(argv=None):
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(description=__doc__, epilog=EXAMPLES, formatter_class=raw)
    parser.add_argument("--catalog", type=Path, default=ROOT / "images.txt", help="image catalog (default images.txt)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every skopeo and curl command and each image decision (or MIRROR_VERBOSE=true)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    def command(name, summary, examples):
        return commands.add_parser(name, help=summary, description=summary, epilog="examples:\n" + examples,
                                   formatter_class=raw)

    command("login", "log in to TARGET_REGISTRY, and SOURCE_REGISTRY when it has credentials",
            "  TARGET_REGISTRY=registry.example.com TARGET_REGISTRY_USERNAME=robot \\\n"
            "    TARGET_REGISTRY_PASSWORD=... mirror.py login\n"
            "  SOURCE_REGISTRY=docker.io SOURCE_REGISTRY_USERNAME=me SOURCE_REGISTRY_PASSWORD=... mirror.py login")
    command("targets", "validate and list the image catalog", "  mirror.py targets")
    add = command("add", "resolve a tag to its digest and append a catalog entry",
                  "  mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus\n"
                  "  mirror.py add quay.io/prometheus/node-exporter:v1.9.1 team/node-exporter\n"
                  "  mirror.py add docker.io/bitnamicharts/redis:22.0.7 charts/redis   # an OCI Helm chart")
    add.add_argument("source", help="registry/repository:tag in full, e.g. docker.io/library/alpine:3.20")
    add.add_argument("target", help="repository path in TARGET_REGISTRY, any depth, e.g. team/prometheus")
    sync_command = command("sync", "mirror the catalog into TARGET_REGISTRY and send what changed",
                           "  mirror.py sync          # send changed images; NIFI_URL or MIRROR_BUNDLE_DIR receives\n"
                           "  mirror.py sync --all    # send every approved image (recovery, RESEND_ALL=true in CI)")
    sync_command.add_argument("--all", action="store_true", help="send every approved image, not only changes")
    resend_command = command("resend", "resend recorded images as the next bundle (MIRROR_LEDGER=true)",
                             "  mirror.py resend --sequence 7..        # bundles 7 to the newest (RESEND_SEQUENCE)\n"
                             "  mirror.py resend --sequence 7..9\n"
                             "  mirror.py resend --since 2026-10-01    # (RESEND_SINCE)\n"
                             "  mirror.py resend --image team/prometheus:v3.13.4   # (RESEND_IMAGE)")
    resend_command.add_argument("--since", help="images recorded on or after YYYY-MM-DD")
    resend_command.add_argument("--sequence", help="bundles N, N.. (to the newest) or N..M")
    resend_command.add_argument("--image", help="one target, repo or repo:tag")
    add_list_command = command(
        "add-list", "add every image named in a folder of .txt files, keeping images.txt sorted",
        "  mirror.py add-list lists/                 # docker.io/prom/prometheus:v3.13.4 -> prom/prometheus\n"
        "  mirror.py add-list lists/ --prefix team   # -> team/prom/prometheus\n"
        "  lists/*.txt hold one image per line, # comments allowed. A line without a registry host\n"
        "  is read as docker pull reads it (alpine -> docker.io/library/alpine); without a tag, :latest.")
    add_list_command.add_argument("folder", type=Path, help="a directory of *.txt files")
    add_list_command.add_argument("--prefix", help="repository path the targets go under, e.g. team")
    command("pending", "list catalog sources the ledger has not sent yet, one per line", "  mirror.py pending")
    check = command("covers", "fail when a Containerfile or manifest uses an image the mirror lacks",
                    "  mirror.py covers Containerfile\n  helm template chart/ | mirror.py covers -")
    check.add_argument("files", nargs="+", help="Containerfiles, manifests or - for rendered YAML on stdin")
    high = command("import", "verify bundles and push their images to TARGET_REGISTRY",
                   "  mirror.py import mirror-<stream>-000000000001.tar        # one bundle file\n"
                   "  mirror.py import --inbox /transfer/in                  # every bundle in a directory\n"
                   "  mirror.py import --registry --name mirror-<stream>-000000000008.tar\n"
                   "      # from IMPORT_STORE (gitlab or s3), catching up on any missed bundle")
    selection = high.add_mutually_exclusive_group(required=True)
    selection.add_argument("bundle", nargs="?", type=Path, help="a bundle .tar, its .sha256 beside it")
    selection.add_argument("--inbox", type=Path, help="a directory of bundles; imported ones move to done/")
    selection.add_argument("--registry", action="store_true",
                           help="fetch bundles from IMPORT_STORE: the generic package registry or S3")
    high.add_argument("--name", help="with --registry: the bundle file a trigger announced (BUNDLE)")
    high.add_argument("--adopt-stream", action="store_true", help="accept a full bundle from a replacement sender")
    high.add_argument("--record", type=Path, help="write the image tags and digests this run pushed, for promote")
    promote_command = command("promote", "copy recorded images from SOURCE_REGISTRY (dev) to TARGET_REGISTRY (prod) by digest",
                              "  SOURCE_REGISTRY=registry.dev.example.com TARGET_REGISTRY=registry.prod.example.com \\\n"
                              "    mirror.py promote imported.json      # the file import --record wrote")
    promote_command.add_argument("record", type=Path, help="the file import --record wrote")
    args = parser.parse_args(argv)
    if args.verbose:
        os.environ["MIRROR_VERBOSE"] = "true"
    try:
        check_renamed()
        if args.command == "login":
            login()
        elif args.command == "targets":
            for image in catalog(args.catalog):
                print(f"{image['source']} -> {image['target']}:{image['tag']}")
        elif args.command == "add":
            add_image(args.catalog, args.source, args.target)
        elif args.command == "sync":
            sync(args.catalog, args.all)
        elif args.command == "resend":
            resend(args.since, args.sequence, args.image)
        elif args.command == "add-list":
            add_list(args.catalog, args.folder, args.prefix)
        elif args.command == "pending":
            pending(args.catalog)
        elif args.command == "covers":
            covers(args.catalog, args.files)
        elif args.command == "promote":
            promote(args.record)
        else:
            if args.record:
                args.record.write_text("[]\n")  # this run's pushes only; nothing new leaves an empty list
            if args.registry:
                import_registry(args.name, args.adopt_stream, args.record)
            elif args.inbox:
                import_inbox(args.inbox, args.adopt_stream, args.record)
            else:
                import_one(args.bundle, args.adopt_stream, record=args.record)
        return 0
    except GapWaiting as waiting:
        print(f"waiting: {waiting}", file=sys.stderr)
        return 3
    except (MirrorError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
