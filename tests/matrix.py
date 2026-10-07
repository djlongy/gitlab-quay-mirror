#!/usr/bin/env python3
"""Mirror a set of real source references through the low registry, a bundle and the high registry.

Log in to both registries with skopeo first and set E2E_LOW_REGISTRY, E2E_HIGH_REGISTRY and E2E_ORG.
Each argument is a catalog source: [registry/]repo:tag, or one already pinned with @sha256.
Set MIRROR_PLATFORM to check single-platform transfer.
"""
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mirror

REGISTRIES = {"low": os.environ["E2E_LOW_REGISTRY"], "high": os.environ["E2E_HIGH_REGISTRY"]}
with tempfile.TemporaryDirectory(prefix="registry-mirror-matrix-") as temporary:
    directory = Path(temporary)
    catalog = directory / "images.txt"
    catalog.touch()
    os.environ.update(MIRROR_STATE_DIR=str(directory / "work"), IMPORT_STATE_DIR=str(directory / "import"))
    os.environ.pop("NIFI_URL", None)
    prefix = os.environ["E2E_ORG"] + "/matrix-" + uuid.uuid4().hex[:8]

    def cli(*args):
        side = "high" if args[0] == "import" else "low"
        result = subprocess.run([sys.executable, str(ROOT / "mirror.py"), "--catalog", str(catalog), *args],
                                capture_output=True, text=True, check=False,
                                env=dict(os.environ, TARGET_REGISTRY=REGISTRIES[side]))
        assert result.returncode == 0, f"{args}: {result.stderr}"
        return result.stdout

    for number, source in enumerate(sys.argv[1:]):
        cli("add", source, f"{prefix}/{number}/{source.split('@')[0].rsplit(':', 1)[0].rsplit('/', 1)[-1]}")
    cli("sync")
    bundle = next((directory / "work" / "bundles").glob("mirror-*.tar"))
    cli("import", str(bundle))
    with tarfile.open(bundle) as archive:
        entries = json.load(archive.extractfile("images.json"))["images"]
    for entry in entries:
        high = f"docker://{REGISTRIES['high']}/{entry['target']}:{entry['tag']}"
        raw = json.loads(mirror.run("skopeo", "inspect", "--raw", *mirror.options("TARGET_REGISTRY"), high))
        assert mirror.raw_digest(high, "TARGET_REGISTRY") == entry["transfer"], entry
        kind = raw.get("mediaType") or raw.get("config", {}).get("mediaType")
        platforms = sorted({f"{m['platform']['os']}/{m['platform']['architecture']}"
                            for m in raw.get("manifests", []) if "platform" in m})
        print(f"PASS {entry['source']}\n     high {entry['transfer'][:19]} {kind} {platforms or 'single'}")
    print(f"bundle {bundle.stat().st_size} bytes, {len(entries)} image(s)")
