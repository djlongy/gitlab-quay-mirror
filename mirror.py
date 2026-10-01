#!/usr/bin/env python3
"""Mirror reviewed container images into low Quay; export and import verified offline bundles."""

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
TARGET = re.compile(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)+")


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
    value = required_env(f"{side}_HOST")
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", value):
        raise MirrorError(f"{side}_HOST must be a registry host[:port], without scheme or path")
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


def run(*args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode:
        raise MirrorError(f"{args[0]} {args[1]} failed: {result.stderr.decode(errors='replace')[-500:].strip()}")
    return result.stdout


# Credentials come from skopeo's default auth file, written by `skopeo login` or
# `podman login`. REGISTRY_AUTH_FILE moves it for both the login and this script.
def options(side, direction=""):
    return [f"--{direction}tls-verify=false"] if os.environ.get(f"{side}_TLS_VERIFY") == "false" else []


def raw_digest(reference, side):
    # A manifest digest is the sha256 of its exact bytes, which --raw returns.
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


def normalise(source):
    """Expand a short name the way docker pull does: alpine:3 is docker.io/library/alpine:3."""
    first, _, rest = source.partition("/")
    if not rest or not ("." in first or ":" in first or first == "localhost"):
        source = "docker.io/" + source
    host, _, repo = source.partition("/")
    if host == "docker.io" and "/" not in repo:
        source = f"{host}/library/{repo}"
    return source


def add_image(path, source, target):
    source = normalise(source)
    name = source.split("@", 1)[0]
    if not SOURCE.fullmatch(name) or not TARGET.fullmatch(target):
        raise MirrorError("add needs [registry/]repo:tag and org/repo[/path]")
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
    if "manifests" not in data or not any("platform" in child for child in data["manifests"]):
        return digest  # a single image, or an artifact index such as signatures, goes whole
    keys = ("os", "architecture", "variant")
    want = dict(zip(keys, wanted.split("/")))
    matches = [child["digest"] for child in data["manifests"]
               if {k: child.get("platform", {}).get(k) for k in want} == want]
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
    if image["transfer"] not in [child.get("digest") for child in children]:
        raise MirrorError(f"{image['transfer']} is not part of approved {image['digest']}")


def notify(bundle):
    url = os.environ.get("NIFI_URL")
    if url:
        for path in (bundle.with_name(bundle.name + ".sha256"), bundle):
            run("curl", "--fail", "--show-error", "--silent", "--connect-timeout", "15",
                "--max-time", "3600", "--retry", "2", "--request", "POST",
                "--header", f"Filename: {path.name}", "--header", "Content-Type: application/octet-stream",
                "--data-binary", f"@{path}", url)


def sync(path, full=False):
    low = registry("LOW_QUAY")
    images = catalog(path)
    if not images:
        raise MirrorError("catalog is empty; add an image before syncing")
    work = state_path("MIRROR_STATE_DIR", "quay-mirror")
    outbox = Path(os.environ.get("MIRROR_BUNDLE_DIR") or work / "bundles")
    with locked(work):
        state_file = work / "sent.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {
            "stream": uuid.uuid4().hex, "sequence": 0, "sent": {}, "registry": low}
        if state.get("registry") != low:
            raise MirrorError(f"{work} belongs to another low registry; set a separate MIRROR_STATE_DIR")
        full = full or not state["sequence"] or state.get("pending", False)
        changed, known, platforms = [], state.get("platforms", {}), {}
        for image in images:
            # Pin the source by digest; upstream tag movement cannot change approved bytes.
            source = transport_reference(image["source"])
            destination = f"docker://{low}/{image['target']}:{image['tag']}"
            # Remember each platform choice so an unchanged image needs no upstream request.
            lookup = f"{image['digest']} {os.environ.get('MIRROR_PLATFORM', '').strip()}"
            transfer = known.get(lookup) or platform_digest("docker://" + source, image["digest"], "UPSTREAM")
            platforms[lookup] = transfer
            observed = None
            if state["sent"].get(f"{image['target']}:{image['tag']}") == image["digest"]:
                try:
                    observed = raw_digest(destination, "LOW_QUAY")
                except MirrorError as error:
                    if not any(reason in str(error).lower() for reason in ('manifest unknown', 'name unknown')):
                        raise
            if observed != transfer:
                copy("docker://" + source.split("@")[0] + "@" + transfer, destination, "UPSTREAM", "LOW_QUAY")
            if raw_digest(destination, "LOW_QUAY") != transfer:
                raise MirrorError(f"low mirror digest mismatch: {image['target']}")
            if full or state["sent"].get(f"{image['target']}:{image['tag']}") != image["digest"]:
                changed.append(image | {"transfer": transfer})
        state["platforms"] = platforms
        if not changed:
            write_json(state_file, state)
            print("nothing to send; low-side digests verified")
            return None
        outbox.mkdir(parents=True, exist_ok=True)
        # Reserve a sequence before publishing. An interrupted export forces the next one full.
        state.update(sequence=state["sequence"] + 1, pending=True)
        write_json(state_file, state)
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            stage = Path(temporary)
            (stage / "images").mkdir()
            for image in changed:
                directory = stage / "images" / image["transfer"].replace(":", "-")
                if not directory.exists():
                    # dir: keeps a Docker manifest list byte for byte; oci: would have to convert it.
                    copy(f"docker://{low}/{image['target']}@{image['transfer']}", f"dir:{directory}", "LOW_QUAY")
                    verify_image(directory, image["transfer"])
                if image["transfer"] != image["digest"]:
                    # Carry the approved index so import can prove the platform image belongs to it.
                    index = run("skopeo", "inspect", "--raw", *options("UPSTREAM"),
                                "docker://" + transport_reference(image["source"]))
                    (directory / f"{image['digest'][7:]}.manifest.json").write_bytes(index)
                    verify_platform(directory, image)
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
    allowed = re.compile(r"images\.json|images/sha256-[0-9a-f]{64}/(?:version|manifest\.json|[0-9a-f]{64}(?:\.manifest\.json)?)")
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
        fields = ('source', 'target', 'tag', 'digest', 'transfer')
        if (not isinstance(image, dict) or set(image) != set(fields)
                or not all(isinstance(image[k], str) for k in fields) or not re.fullmatch(DIGEST, image["transfer"])):
            raise MirrorError("invalid image entry")
        if parse_image(image["source"], image["target"]) | {"transfer": image["transfer"]} != image:
            raise MirrorError("image metadata does not match pinned source")
        key = f"{image['target']}:{image['tag']}"
        if key in seen:
            raise MirrorError("duplicate target tag in bundle")
        seen.add(key)


def import_one(bundle, adopt_stream=False, superseded_ok=False):
    high = registry("HIGH_QUAY")
    work = state_path("IMPORT_STATE_DIR", "quay-mirror-import")
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
        for image in manifest["images"]:
            verify_image(stage / "images" / image["transfer"].replace(":", "-"), image["transfer"])
            verify_platform(stage / "images" / image["transfer"].replace(":", "-"), image)
        receipt_file = work / "received.json"
        receipt = json.loads(receipt_file.read_text()) if receipt_file.exists() else {}
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
        if manifest["sequence"] != previous + 1 and not manifest["full"]:
            raise SequenceGap("missing earlier bundle; recover with sync --full")
        for image in manifest["images"]:
            destination = f"docker://{high}/{image['target']}:{image['tag']}"
            copy(f"dir:{stage / 'images' / image['transfer'].replace(':', '-')}", destination,
                 destination_side="HIGH_QUAY")
            if raw_digest(destination, "HIGH_QUAY") != image["transfer"]:
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
    add.add_argument("source", help="[registry/]repository:tag, e.g. prom/prometheus:v3.13.4")
    add.add_argument("target", help="Quay organisation/repository, any depth, e.g. team-dev/prom/prometheus")
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
