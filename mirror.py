#!/usr/bin/env python3
"""Mirror reviewed OCI images into low Quay; export and import verified offline bundles."""

import argparse
from contextlib import contextmanager
import fcntl
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
import uuid

ROOT = Path(__file__).resolve().parent
TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
DIGEST = r"sha256:[0-9a-f]{64}"
SOURCE = re.compile(rf"(?P<host>[a-z0-9.-]+(?::[0-9]+)?)/(?P<repo>[a-z0-9._/-]+):(?P<tag>{TAG})")
TARGET = re.compile(r"[a-z0-9][a-z0-9_-]*/[a-z0-9]+(?:[._-]+[a-z0-9]+)*")
OCI_REF = "org.opencontainers.image.ref.name"


class MirrorError(Exception):
    pass


class SequenceGap(MirrorError):
    pass


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise MirrorError(f"{name} is required")
    return value.rstrip("/")


def registry(side):
    value = required_env(f"{side}_QUAY")
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", value):
        raise MirrorError(f"{side}_QUAY must be a registry host[:port], without scheme or path")
    return value


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
        raise MirrorError("expected registry/repo:tag@sha256:<64 hex> org/repo (one target slash)")
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


def run(*args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode:
        raise MirrorError(f"{args[0]} {args[1]} failed: {result.stderr.decode(errors='replace')[-500:].strip()}")
    return result.stdout


def options(side, direction=""):
    result = []
    auth = os.environ.get(f"{side}_AUTH_FILE")
    if auth:
        result += [f"--{direction}authfile", auth]
    elif side != "UPSTREAM":
        required_env(f"{side}_AUTH_FILE")
    if os.environ.get(f"{side}_TLS_VERIFY") == "false":
        result.append(f"--{direction}tls-verify=false")
    return result


def raw_digest(reference, side):
    raw = run("skopeo", "inspect", "--raw", *options(side), reference)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


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


def add_image(path, source, target):
    name = source.split("@", 1)[0]
    if not SOURCE.fullmatch(name) or not TARGET.fullmatch(target):
        raise MirrorError("add needs registry/repo:tag and org/repo (one target slash)")
    digest = raw_digest("docker://" + transport_reference(source), "UPSTREAM")
    image = parse_image(name + "@" + digest, target)
    if "@" in source and source.split("@", 1)[1] != digest:
        raise MirrorError("source digest does not match the manifest")
    images = catalog(path) if path.exists() else []
    if any((i['target'], i['tag']) == (target, image['tag']) for i in images):
        raise MirrorError("target tag already exists; edit its reviewed catalog entry")
    with path.open("a") as stream:
        if path.stat().st_size:
            stream.write("\n")
        stream.write(f"{image['source']} {target}\n")
    print(f"added: {image['source']} -> {target}:{image['tag']}")


def verify_layout(layout, images):
    if json.loads((layout / "oci-layout").read_text()) != {"imageLayoutVersion": "1.0.0"}:
        raise MirrorError("unsupported OCI layout")
    roots = json.loads((layout / "index.json").read_text())["manifests"]
    visited = set()

    def verify(descriptor, manifest=False):
        digest = descriptor["digest"]
        if not isinstance(digest, str) or not re.fullmatch(DIGEST, digest):
            raise MirrorError("invalid OCI digest")
        blob = layout / "blobs" / "sha256" / digest[7:]
        if not blob.is_file() or blob.stat().st_size != descriptor["size"] or sha256(blob) != digest[7:]:
            raise MirrorError(f"missing or corrupt OCI blob: {digest}")
        if not manifest or digest in visited:
            return
        visited.add(digest)
        data = json.loads(blob.read_text())
        if data.get("schemaVersion") != 2:
            raise MirrorError("unsupported image manifest schema")
        if "manifests" in data:
            for child in data["manifests"]:
                verify(child, True)
        else:
            verify(data["config"])
            for layer in data["layers"]:
                verify(layer)

    for image in images:
        ref = image["digest"].replace(":", "-")
        matches = [d for d in roots if d.get("annotations", {}).get(OCI_REF) == ref]
        if len(matches) != 1 or matches[0]["digest"] != image["digest"]:
            raise MirrorError(f"OCI root does not match {image['target']}:{image['tag']}")
        verify(matches[0], True)


def notify(bundle):
    url = os.environ.get("NIFI_URL")
    if url:
        for path in (bundle.with_name(bundle.name + ".sha256"), bundle):
            run("curl", "--fail", "--show-error", "--silent", "--connect-timeout", "15",
                "--max-time", "3600", "--retry", "2", "--request", "POST",
                "--header", f"Filename: {path.name}", "--header", "Content-Type: application/octet-stream",
                "--data-binary", f"@{path}", url)


def sync(path, full=False):
    low = registry("LOW")
    images = catalog(path)
    if not images:
        raise MirrorError("catalog is empty; add an image before syncing")
    work = Path(os.environ.get("MIRROR_WORK", ROOT / ".mirror-work"))
    outbox = Path(os.environ.get("MIRROR_OUTBOX", ROOT / "delta"))
    with locked(work):
        state_file = work / "sent.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {
            "stream": uuid.uuid4().hex, "sequence": 0, "sent": {}, "registry": low}
        if state.get("registry") != low:
            raise MirrorError("MIRROR_WORK belongs to another low registry; use a separate work directory")
        full = full or not state["sequence"] or state.get("pending", False)
        changed = []
        for image in images:
            # Pin the source by digest; upstream tag movement cannot change approved bytes.
            source = transport_reference(image["source"])
            destination = f"docker://{low}/{image['target']}:{image['tag']}"
            observed = None
            if state["sent"].get(f"{image['target']}:{image['tag']}") == image["digest"]:
                try:
                    observed = raw_digest(destination, "LOW")
                except MirrorError as error:
                    if not any(reason in str(error).lower() for reason in ('manifest unknown', 'name unknown')):
                        raise
            if observed != image["digest"]:
                copy("docker://" + source, destination, "UPSTREAM", "LOW")
            if raw_digest(destination, "LOW") != image["digest"]:
                raise MirrorError(f"low mirror digest mismatch: {image['target']}")
            if full or state["sent"].get(f"{image['target']}:{image['tag']}") != image["digest"]:
                changed.append(image)
        if not changed:
            print("nothing to send; low-side digests verified")
            return None
        outbox.mkdir(parents=True, exist_ok=True)
        # Reserve a sequence before publishing. An interrupted export forces the next one full.
        state.update(sequence=state["sequence"] + 1, pending=True)
        write_json(state_file, state)
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            stage = Path(temporary)
            copied = set()
            for image in changed:
                ref = image["digest"].replace(":", "-")
                if ref not in copied:
                    copy(f"docker://{low}/{image['target']}@{image['digest']}",
                         f"oci:{stage / 'oci'}:{ref}", "LOW")
                    copied.add(ref)
            verify_layout(stage / "oci", changed)
            write_json(stage / "images.json", {"schema": 1, "stream": state["stream"],
                       "sequence": state["sequence"], "full": full, "images": changed})
            bundle = outbox / f"quay-{state['stream']}-{state['sequence']:012d}.tar"
            temporary_bundle = bundle.with_suffix(".tmp")
            with tarfile.open(temporary_bundle, "w") as archive:
                for item in sorted(stage.rglob("*")):
                    if item.is_file():
                        archive.add(item, arcname=str(item.relative_to(stage)), recursive=False)
            os.replace(temporary_bundle, bundle)
            sidecar = bundle.with_name(bundle.name + ".sha256")
            temporary_sidecar = sidecar.with_suffix(".tmp")
            temporary_sidecar.write_text(f"{sha256(bundle)}  {bundle.name}\n")
            os.replace(temporary_sidecar, sidecar)  # readiness marker, published last
        notify(bundle)
        state["sent"].update({f"{i['target']}:{i['tag']}": i["digest"] for i in changed})
        state["pending"] = False
        write_json(state_file, state)
        print(f"bundle: {bundle} ({len(changed)} image(s), {bundle.stat().st_size} bytes)")
        return bundle


def extract(bundle, directory):
    allowed = re.compile(r"(?:images\.json|oci/(?:oci-layout|index\.json|blobs/sha256/[0-9a-f]{64}))")
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
            or not isinstance(manifest.get("images"), list) or not manifest["images"]):
        raise MirrorError("invalid bundle metadata")
    seen = set()
    for image in manifest["images"]:
        if not isinstance(image, dict) or not all(isinstance(image.get(k), str) for k in ('source', 'target', 'tag', 'digest')):
            raise MirrorError("invalid image entry")
        if parse_image(image["source"], image["target"]) != image:
            raise MirrorError("image metadata does not match pinned source")
        key = f"{image['target']}:{image['tag']}"
        if key in seen:
            raise MirrorError("duplicate target tag in bundle")
        seen.add(key)


def import_one(bundle, adopt_stream=False, superseded_ok=False):
    high = registry("HIGH")
    work = Path(os.environ.get("IMPORT_WORK", ROOT / ".import-work"))
    sidecar = bundle.with_name(bundle.name + ".sha256")
    fields = sidecar.read_text().split() if sidecar.exists() else []
    digest = sha256(bundle)
    if fields != [digest, bundle.name]:
        raise MirrorError(f"{bundle.name}: missing or mismatched checksum")
    with locked(work), tempfile.TemporaryDirectory(dir=work) as temporary:
        stage = Path(temporary)
        extract(bundle, stage)
        manifest = json.loads((stage / "images.json").read_text())
        validate_manifest(manifest)
        verify_layout(stage / "oci", manifest["images"])
        receipt_file = work / "received.json"
        receipt = json.loads(receipt_file.read_text()) if receipt_file.exists() else {}
        if receipt and receipt.get("registry") != high:
            raise MirrorError("IMPORT_WORK belongs to another high registry")
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
        if manifest["sequence"] != previous + 1 and not manifest["full"]:
            raise SequenceGap("missing earlier bundle; recover with sync --full")
        for image in manifest["images"]:
            destination = f"docker://{high}/{image['target']}:{image['tag']}"
            copy(f"oci:{stage / 'oci'}:{image['digest'].replace(':', '-')}", destination,
                 destination_side="HIGH")
            if raw_digest(destination, "HIGH") != image["digest"]:
                raise MirrorError(f"high mirror digest mismatch: {image['target']}")
        write_json(receipt_file, {"stream": manifest["stream"], "sequence": manifest["sequence"],
                                "digest": digest, "registry": high})
    print(f"imported: {bundle.name} ({len(manifest['images'])} image(s), digests verified)")


def import_inbox(inbox, adopt_stream=False):
    def consume(bundle):
        import_one(bundle, adopt_stream, superseded_ok=True)
        done = inbox / "done"
        done.mkdir(exist_ok=True)
        os.replace(bundle.with_name(bundle.name + ".sha256"), done / (bundle.name + ".sha256"))
        os.replace(bundle, done / bundle.name)

    waiting = []
    for bundle in sorted(inbox.glob("quay-*.tar")):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "images.txt")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("targets", help="validate and list the image catalog")
    add = commands.add_parser("add", help="resolve a tag to its digest and append a reviewed catalog entry")
    add.add_argument("source", help="registry/repository:tag")
    add.add_argument("target", help="Quay organisation/repository (one slash)")
    low = commands.add_parser("sync", help="mirror to low Quay and export changed images")
    low.add_argument("--full", action="store_true", help="export all approved images for recovery")
    high = commands.add_parser("import", help="verify and push offline bundles to high Quay")
    selection = high.add_mutually_exclusive_group(required=True)
    selection.add_argument("bundle", nargs="?", type=Path)
    selection.add_argument("--inbox", type=Path)
    high.add_argument("--adopt-stream", action="store_true", help="accept a full bundle from a replacement sender")
    args = parser.parse_args(argv)
    try:
        if args.command == "targets":
            for image in catalog(args.catalog):
                print(f"{image['source']} -> {image['target']}:{image['tag']}")
        elif args.command == "add":
            add_image(args.catalog, args.source, args.target)
        elif args.command == "sync":
            sync(args.catalog, args.full)
        elif args.inbox:
            import_inbox(args.inbox, args.adopt_stream)
        else:
            import_one(args.bundle, args.adopt_stream)
        return 0
    except (MirrorError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
