#!/usr/bin/env python3
"""Mirror approved OCI images through low Quay and carry changed tags to high Quay."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parent
REF = re.compile(r"^(?P<host>[^/\s]+)/(?P<repo>[^\s@]+):(?P<tag>[^\s:@/]+)@(?P<digest>sha256:[0-9a-f]{64})$")
TARGET = re.compile(r"^[a-z0-9._-]+(?:/[a-z0-9._-]+)+$")
OCI_REF = "org.opencontainers.image.ref.name"


class MirrorError(Exception):
    pass


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise MirrorError(f"{name} is required")
    return value.rstrip("/")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def catalog(path=ROOT / "images.txt"):
    images = []
    seen = set()
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        match = REF.fullmatch(fields[0]) if len(fields) == 2 else None
        if not match or not TARGET.fullmatch(fields[1]) or ".." in fields[1].split("/"):
            raise MirrorError(f"{path}:{number}: expected upstream/repo:tag@sha256:<64 hex> target/repo")
        image = match.groupdict() | {"target": fields[1]}
        key = f"{image['target']}:{image['tag']}"
        if key in seen:
            raise MirrorError(f"{path}:{number}: duplicate target tag {key}")
        seen.add(key)
        images.append(image)
    if not images:
        raise MirrorError(f"{path}: no images")
    return images


def proxy_orgs(path=ROOT / "proxy-orgs.txt"):
    result = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch(r"[a-z0-9._-]+", fields[1]):
            raise MirrorError(f"{path}:{number}: expected upstream-registry quay-proxy-org")
        result[fields[0]] = fields[1]
    return result


def run(*args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode:
        error = result.stderr.decode(errors="replace").strip()
        raise MirrorError(f"{args[0]} {args[1]} failed: {error[-400:]}")
    return result.stdout


def tls_option(side, direction=""):
    return [f"--{direction}tls-verify=false"] if os.environ.get(f"{side}_TLS_VERIFY") == "false" else []


def raw_digest(reference, authfile, side):
    raw = run("skopeo", "inspect", "--raw", "--authfile", authfile, *tls_option(side), reference)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def copy_to_layout(source, layout, ref, authfile):
    run("skopeo", "copy", "--all", "--preserve-digests", "--retry-times=3",
        "--src-authfile", authfile, *tls_option("LOW", "src-"), source, f"oci:{layout}:{ref}")


def copy_from_layout(layout, ref, target, authfile):
    run("skopeo", "copy", "--all", "--preserve-digests", "--retry-times=3",
        "--dest-authfile", authfile, *tls_option("HIGH", "dest-"), f"oci:{layout}:{ref}", target)


def sync(full=False):
    low = required_env("LOW_QUAY")
    auth = required_env("LOW_AUTH_FILE")
    work = Path(os.environ.get("MIRROR_WORK", ROOT / ".mirror-work"))
    outbox = Path(os.environ.get("MIRROR_OUTBOX", ROOT / "delta"))
    images, proxies = catalog(), proxy_orgs()
    work.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    state_file = work / "sent.json"
    sent = json.loads(state_file.read_text()) if state_file.exists() else {}
    if not isinstance(sent, dict):
        raise MirrorError(f"{state_file}: expected an object")
    # ponytail: image-level delta; add a blob ledger only if measured link volume needs it.
    changed = [image for image in images if full or sent.get(f"{image['target']}:{image['tag']}") != image["digest"]]
    if not changed:
        print("nothing to send")
        return None

    with tempfile.TemporaryDirectory(dir=work) as temporary:
        stage = Path(temporary)
        layout = stage / "oci"
        entries = []
        copied = set()
        for image in changed:
            org = proxies.get(image["host"])
            if not org:
                raise MirrorError(f"no proxy organisation for {image['host']} in proxy-orgs.txt")
            source = f"docker://{low}/{org}/{image['repo']}"
            observed = raw_digest(f"{source}:{image['tag']}", auth, "LOW")
            if observed != image["digest"]:
                raise MirrorError(f"{image['host']}/{image['repo']}:{image['tag']} moved: "
                                  f"catalog {image['digest']}, low Quay {observed}")
            ref = image["digest"].replace(":", "-")
            if ref not in copied:
                copy_to_layout(f"{source}@{image['digest']}", layout, ref, auth)
                copied.add(ref)
            entries.append({"target": image["target"], "tag": image["tag"],
                            "digest": image["digest"], "ref": ref})
        (stage / "images.json").write_text(json.dumps(entries, indent=2) + "\n")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bundle = outbox / f"quay-delta-{stamp}-{os.getpid()}.tar"
        temporary_bundle = outbox / f".{bundle.name}.tmp"
        with tarfile.open(temporary_bundle, "w") as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=str(path.relative_to(stage)), recursive=False)
        digest = sha256(temporary_bundle)
        os.replace(temporary_bundle, bundle)
        sidecar = bundle.with_name(bundle.name + ".sha256")
        temporary_sidecar = sidecar.with_name("." + sidecar.name + ".tmp")
        temporary_sidecar.write_text(f"{digest}  {bundle.name}\n")
        os.replace(temporary_sidecar, sidecar)

    nifi = os.environ.get("NIFI_URL", "")
    if nifi:
        for file in (sidecar, bundle):
            run("curl", "--fail", "--show-error", "--silent", "--retry", "2", "--request", "POST",
                "--header", f"Filename: {file.name}", "--header", f"X-Sha256: {sha256(file)}",
                "--header", "X-Artifact-Type: container-images",
                "--header", "Content-Type: application/octet-stream", "--data-binary", f"@{file}", nifi)
    sent.update({f"{image['target']}:{image['tag']}": image["digest"] for image in changed})
    temporary_state = state_file.with_suffix(".tmp")
    temporary_state.write_text(json.dumps(sent, indent=2, sort_keys=True) + "\n")
    os.replace(temporary_state, state_file)
    print(f"bundle: {bundle} ({len(entries)} tag(s), {bundle.stat().st_size} bytes)")
    return bundle


def extract(bundle, directory):
    with tarfile.open(bundle) as archive:
        for member in archive:
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts or not member.isfile():
                raise MirrorError(f"unsafe bundle entry: {member.name}")
            target = directory.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest)


def import_one(bundle):
    high = required_env("HIGH_QUAY")
    auth = required_env("HIGH_AUTH_FILE")
    sidecar = bundle.with_name(bundle.name + ".sha256")
    if not sidecar.exists():
        raise MirrorError(f"missing {sidecar.name}")
    fields = sidecar.read_text().split()
    if len(fields) != 2 or fields[1] != bundle.name or fields[0] != sha256(bundle):
        raise MirrorError(f"{bundle.name}: checksum mismatch")
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        extract(bundle, stage)
        images = json.loads((stage / "images.json").read_text())
        if not isinstance(images, list) or not images:
            raise MirrorError(f"{bundle.name}: empty image list")
        for image in images:
            if (not isinstance(image, dict) or not isinstance(image.get("target"), str)
                    or not isinstance(image.get("tag"), str) or not isinstance(image.get("digest"), str)
                    or not TARGET.fullmatch(image["target"])
                    or not re.fullmatch(r"[^\s:@/]+", image["tag"])
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"])
                    or image.get("ref") != image["digest"].replace(":", "-")):
                raise MirrorError(f"{bundle.name}: invalid image entry")
            target = f"docker://{high}/{image['target']}:{image['tag']}"
            copy_from_layout(stage / "oci", image["ref"], target, auth)
            observed = raw_digest(target, auth, "HIGH")
            if observed != image["digest"]:
                raise MirrorError(f"{target}: expected {image['digest']}, got {observed}")
    print(f"imported: {bundle.name} ({len(images)} tag(s), digests verified)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("targets", help="list the approved source and target references")
    low = commands.add_parser("sync", help="build a bundle of changed tags")
    low.add_argument("--full", action="store_true", help="bundle every tag to recover a missed transfer")
    high = commands.add_parser("import", help="verify and push a bundle to high Quay")
    high.add_argument("bundle", nargs="?", type=Path)
    high.add_argument("--inbox", type=Path, help="import all complete bundles in a directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "targets":
            for image in catalog():
                print(f"{image['host']}/{image['repo']}:{image['tag']}@{image['digest']} -> "
                      f"{image['target']}:{image['tag']}")
        elif args.command == "sync":
            sync(args.full)
        elif args.inbox:
            for bundle in sorted(args.inbox.glob("quay-delta-*.tar")):
                import_one(bundle)
                done = args.inbox / "done"
                done.mkdir(exist_ok=True)
                os.replace(bundle, done / bundle.name)
                os.replace(bundle.with_name(bundle.name + ".sha256"), done / (bundle.name + ".sha256"))
        elif args.bundle:
            import_one(args.bundle)
        else:
            parser.error("import needs a bundle or --inbox")
        return 0
    except (MirrorError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
