#!/usr/bin/env python3
"""Mirror reviewed container images into low Quay; export and import verified offline bundles."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
DIGEST = r"sha256:[0-9a-f]{64}"
SOURCE = re.compile(rf"(?P<host>[a-z0-9.-]+(?::[0-9]+)?)/(?P<repo>[a-z0-9._/-]+):(?P<tag>{TAG})")
TARGET = re.compile(r"[a-z0-9][a-z0-9_-]*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)+")
IMAGE_CONFIGS = ("application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json")
IMAGE_LAYER = re.compile(r"application/vnd\.oci\.image\.layer\.(?:nondistributable\.)?v1\.tar(?:\+gzip|\+zstd)?"
                         r"|application/vnd\.docker\.image\.rootfs\.(?:foreign\.)?diff\.tar(?:\.gzip)?")
MANIFESTS = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
INDEXES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")


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
    return [f"--{direction}tls-verify=false"] if os.environ.get(f"{side}_TLS_VERIFY") == "false" else []


def raw_digest(reference, side):
    # A manifest digest is the sha256 of its exact bytes, which --raw returns.
    raw = run("skopeo", "inspect", "--raw", *options(side), reference)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def fetch_manifest(reference, side="UPSTREAM"):
    """The manifest bytes at reference@digest, checked against that digest."""
    raw = run("skopeo", "inspect", "--raw", *options(side), reference)
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


def normalise(source):
    """Expand a short name the way docker pull does: alpine:3 is docker.io/library/alpine:3."""
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
    if "manifests" in manifest:
        return any(image_child(child, fetch, depth) for child in manifest["manifests"])
    layers = manifest.get("layers")
    return (manifest.get("config", {}).get("mediaType") in IMAGE_CONFIGS and isinstance(layers, list)
            and all(IMAGE_LAYER.fullmatch(str(layer.get("mediaType"))) for layer in layers))


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


def add_image(path, source, target):
    source = normalise(source)
    name = source.split("@", 1)[0]
    if not SOURCE.fullmatch(name) or not TARGET.fullmatch(target):
        raise MirrorError("add needs [registry/]repo:tag and org/repo[/path]")
    raw = run("skopeo", "inspect", "--raw", *options("UPSTREAM"), "docker://" + transport_reference(source))
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not runnable(json.loads(raw), lambda child: fetch_manifest(f"docker://{name.rsplit(':', 1)[0]}@{child}")):
        raise MirrorError(f"{name} is a signature, attestation or metadata artifact, not a container image; "
                          "add the tag you run, for example :latest, instead")
    image = parse_image(name + "@" + digest, target)
    if "@" in source and source.split("@", 1)[1] != digest:
        raise MirrorError("source digest does not match the manifest")
    repository = "docker://" + name.rsplit(":", 1)[0]
    tags = publish_tags(image["tag"], platform_digest(f"{repository}@{digest}", digest, "UPSTREAM"),
                        repository, "UPSTREAM")
    images = catalog(path) if path.exists() else []
    if any((i['target'], i['tag']) == (target, image['tag']) for i in images):
        raise MirrorError("target tag already exists; edit its reviewed catalog entry")
    with path.open("a") as stream:
        if path.stat().st_size:
            stream.write("\n")
        stream.write(f"{image['source']} {target}\n")
    print(f"added: {image['source']} -> {target}:{', :'.join(tags)}")


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
    """The tags an image gets on both Quays: the upstream tag, unchanged.

    latest names no release, and once it moves Quay garbage-collects the untagged
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
        platform = os.environ.get("MIRROR_PLATFORM", "").strip()
        # A tag a catalog entry pins, or pinned before its line was removed, is never a version tag.
        pinned = state.setdefault("pinned", sorted(state["sent"]))
        catalog_tags = {f"{i['target']}:{i['tag']}" for i in images} | set(pinned)
        for image in images:
            key = f"{image['target']}:{image['tag']}"
            # The ledger records the platform too, so changing MIRROR_PLATFORM resends the image.
            sent = f"{image['digest']} {platform}".strip()
            # Pin the source by digest; upstream tag movement cannot change approved bytes.
            source = transport_reference(image["source"])
            # Remember each platform and tag choice so an unchanged image needs no upstream request.
            lookup = f"{image['digest']} {platform} {image['tag']}"
            # A full sync is a recovery, so it asks upstream again.
            cached = None if full else known.get(lookup)
            if isinstance(cached, list) and len(cached) == 2 and isinstance(cached[1], list):
                transfer, tags = cached
            else:
                transfer = platform_digest("docker://" + source, image["digest"], "UPSTREAM")
                tags = publish_tags(image["tag"], transfer, "docker://" + source.split("@")[0], "UPSTREAM")
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
                write_json(state_file, state)
            known_tags = [image["tag"], *aliases] if state["sent"].get(key) == sent else []
            for number, tag in enumerate(tags):
                destination = f"docker://{low}/{image['target']}:{tag}"
                observed = None
                if tag in known_tags:
                    try:
                        observed = raw_digest(destination, "LOW_QUAY")
                    except MirrorError as error:
                        # Missing, deleted or expired: copy it again; a real fault fails the copy below.
                        say(f"low copy unreadable, recopying: {error}")
                if observed == transfer:
                    continue
                if number:  # a version tag points at the image already in low Quay
                    copy(f"docker://{low}/{image['target']}@{transfer}", destination, "LOW_QUAY", "LOW_QUAY")
                else:
                    copy("docker://" + source.split("@")[0] + "@" + transfer, destination, "UPSTREAM", "LOW_QUAY")
                if raw_digest(destination, "LOW_QUAY") != transfer:
                    raise MirrorError(f"low mirror digest mismatch: {image['target']}:{tag}")
            names = f"{image['target']}:{', :'.join(tags)}"
            due = full or state["sent"].get(key) != sent or aliases != tags[1:]
            if due:
                changed.append(image | {"transfer": transfer, "tags": tags})
                print(f"send: {names} {transfer}")
            else:
                say(f"unchanged: {names} {transfer}")
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
                    # Carry the approved index so import can check the platform image is listed in it.
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
        state["sent"].update({f"{i['target']}:{i['tag']}": f"{i['digest']} {platform}".strip() for i in changed})
        aliases = state.setdefault("aliases", {})
        for i in changed:
            if i["tags"][1:]:
                aliases[f"{i['target']}:{i['tag']}"] = i["tags"][1:]
            else:
                aliases.pop(f"{i['target']}:{i['tag']}", None)
        state["pending"] = False
        write_json(state_file, state)
        print(f"bundle: {bundle} ({len(changed)} image(s), {bundle.stat().st_size} bytes)")
        return bundle


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
            or not isinstance(manifest.get("images"), list) or not manifest["images"]):
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
            for tag in image["tags"]:
                destination = f"docker://{high}/{image['target']}:{tag}"
                copy(f"dir:{stage / 'images' / image['transfer'].replace(':', '-')}", destination,
                     destination_side="HIGH_QUAY")
                if raw_digest(destination, "HIGH_QUAY") != image["transfer"]:
                    raise MirrorError(f"high mirror digest mismatch: {image['target']}:{tag}")
                print(f"pushed: {image['target']}:{tag} {image['transfer']}")
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
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every skopeo and curl command and each image decision (or MIRROR_VERBOSE=true)")
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
    if args.verbose:
        os.environ["MIRROR_VERBOSE"] = "true"
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
