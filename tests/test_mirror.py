"""One transfer contract check: select changes, verify bundle, import, reject corruption."""

import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror  # noqa: E402


class MirrorTest(unittest.TestCase):
    def test_catalog_rejects_duplicate_target_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "images.txt"
            line = f"docker.io/library/alpine:3.20@sha256:{'a' * 64} test/alpine\n"
            path.write_text(line * 2)
            with self.assertRaisesRegex(mirror.MirrorError, "duplicate target tag"):
                mirror.catalog(path)

    def test_sync_delta_and_import(self):
        digest = "sha256:" + "a" * 64
        image = {"host": "docker.io", "repo": "library/alpine", "tag": "3.20",
                 "digest": digest, "target": "test/alpine"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {"LOW_QUAY": "low.example.internal", "LOW_AUTH_FILE": str(root / "low.auth"),
                   "HIGH_QUAY": "high.example.internal", "HIGH_AUTH_FILE": str(root / "high.auth"),
                   "MIRROR_WORK": str(root / "work"), "MIRROR_OUTBOX": str(root / "out"),
                   "NIFI_URL": "https://nifi.example.internal/contentListener"}

            def fake_copy(_source, layout, _ref, _authfile):
                (layout / "blobs" / "sha256").mkdir(parents=True)
                (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
                (layout / "index.json").write_text('{"schemaVersion":2,"manifests":[]}')
                (layout / "blobs" / "sha256" / ("a" * 64)).write_text("manifest")

            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(mirror, "catalog", return_value=[image]), \
                 mock.patch.object(mirror, "proxy_orgs", return_value={"docker.io": "docker-hub"}), \
                 mock.patch.object(mirror, "raw_digest", return_value=digest), \
                 mock.patch.object(mirror, "copy_to_layout", side_effect=fake_copy), \
                 mock.patch.object(mirror, "run", return_value=b"") as transport:
                bundle = mirror.sync()
                self.assertIsNotNone(bundle)
                self.assertEqual(transport.call_count, 2)
                self.assertIn(f"@{bundle}.sha256", transport.call_args_list[0].args)
                self.assertIn(f"@{bundle}", transport.call_args_list[1].args)
                self.assertIsNone(mirror.sync())
                with tarfile.open(bundle) as archive:
                    self.assertIn("oci/blobs/sha256/" + "a" * 64, archive.getnames())
                    self.assertEqual(json.load(archive.extractfile("images.json"))[0]["digest"], digest)
                with mock.patch.object(mirror, "copy_from_layout") as push:
                    mirror.import_one(bundle)
                    self.assertEqual(push.call_count, 1)
                bundle.with_name(bundle.name + ".sha256").write_text("0" * 64 + "  " + bundle.name)
                with self.assertRaisesRegex(mirror.MirrorError, "checksum mismatch"):
                    mirror.import_one(bundle)

    def test_extract_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bad.tar"
            with tarfile.open(bundle, "w") as archive:
                payload = root / "payload"
                payload.write_text("bad")
                archive.add(payload, arcname="../escape")
            with self.assertRaisesRegex(mirror.MirrorError, "unsafe bundle entry"):
                mirror.extract(bundle, root / "unpack")


if __name__ == "__main__":
    unittest.main()
