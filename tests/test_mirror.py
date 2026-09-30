"""Archive integrity, publication ordering and receipt recovery contracts."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror


def blob(layout, data):
    payload = json.dumps(data).encode()
    digest = hashlib.sha256(payload).hexdigest()
    path = layout / 'blobs' / 'sha256' / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {'digest': 'sha256:' + digest, 'size': len(payload)}


def fixture(stage):
    layout = stage / 'oci'
    config = blob(layout, {'architecture': 'amd64', 'os': 'linux'})
    layer = blob(layout, {'test': 'layer'})
    root = blob(layout, {'schemaVersion': 2, 'config': config, 'layers': [layer]})
    root['annotations'] = {mirror.OCI_REF: root['digest'].replace(':', '-')}
    (layout / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    (layout / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [root]}))
    image = mirror.parse_image('docker.io/library/alpine:3.20@' + root['digest'], 'mirror/alpine')
    return image, layer


def pack(stage, destination, sequence=1, full=True, image=None, stream='a' * 32):
    if image is None:
        image, _ = fixture(stage)
    metadata = {'schema': 1, 'stream': stream, 'sequence': sequence, 'full': full, 'images': [image]}
    (stage / 'images.json').write_text(json.dumps(metadata))
    with tarfile.open(destination, 'w') as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(stage)), recursive=False)
    destination.with_name(destination.name + '.sha256').write_text(f'{mirror.sha256(destination)}  {destination.name}\n')
    return image


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.stage = self.root / 'stage'
        self.stage.mkdir()
        self.env = mock.patch.dict(os.environ, {
            'LOW_QUAY': 'low.example.internal', 'LOW_AUTH_FILE': '/low-auth.json',
            'HIGH_QUAY': 'high.example.internal', 'HIGH_AUTH_FILE': '/high-auth.json',
            'MIRROR_WORK': str(self.root / 'work'), 'MIRROR_OUTBOX': str(self.root / 'out'),
            'IMPORT_WORK': str(self.root / 'import'),
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_catalog_requires_one_quay_repository_level_and_a_digest(self):
        for source, target in [('docker.io/library/alpine:3.20', 'mirror/alpine'),
                               ('docker.io/library/alpine:3.20@sha256:' + 'a'*64, 'mirror/library/alpine'),
                               ('docker.io/../alpine:3.20@sha256:' + 'a'*64, 'mirror/alpine')]:
            with self.subTest(source=source, target=target), self.assertRaises(mirror.MirrorError):
                mirror.parse_image(source, target)

    def test_duplicate_targets_fail(self):
        path = self.root / 'images.txt'
        path.write_text(('docker.io/library/alpine:3.20@sha256:' + 'a'*64 + ' mirror/alpine\n') * 2)
        with self.assertRaisesRegex(mirror.MirrorError, 'duplicate'):
            mirror.catalog(path)

    def test_add_resolves_a_pin(self):
        path = self.root / 'images.txt'
        with mock.patch.object(mirror, 'raw_digest', return_value='sha256:' + 'a'*64):
            mirror.add_image(path, 'docker.io/library/alpine:3.20', 'mirror/library--alpine')
        self.assertEqual(mirror.catalog(path)[0]['digest'], 'sha256:' + 'a'*64)

    def test_skopeo_transport_omits_tag_but_keeps_port_and_digest(self):
        reference = 'registry.example.internal:5000/team/image:1.0@sha256:' + 'a'*64
        self.assertEqual(mirror.transport_reference(reference),
                         'registry.example.internal:5000/team/image@sha256:' + 'a'*64)

    def test_nested_platform_blobs_are_verified(self):
        image, layer = fixture(self.stage)
        layout = self.stage / 'oci'
        child = json.loads((layout / 'index.json').read_text())['manifests'][0]
        root = blob(layout, {'schemaVersion': 2, 'manifests': [child]})
        root['annotations'] = {mirror.OCI_REF: root['digest'].replace(':', '-')}
        (layout / 'index.json').write_text(json.dumps({'manifests': [root]}))
        image = mirror.parse_image('docker.io/library/alpine:3.20@' + root['digest'], 'mirror/alpine')
        mirror.verify_layout(layout, [image])
        (layout / 'blobs' / 'sha256' / layer['digest'][7:]).write_text('corrupt')
        with self.assertRaisesRegex(mirror.MirrorError, 'corrupt OCI blob'):
            mirror.verify_layout(layout, [image])

    def test_import_rejects_corrupt_content_even_with_recomputed_archive_checksum(self):
        image, layer = fixture(self.stage)
        (self.stage / 'oci/blobs/sha256' / layer['digest'][7:]).write_text('bad')
        bundle = self.root / 'quay-bad.tar'
        pack(self.stage, bundle, image=image)
        with mock.patch.object(mirror, 'copy') as push, self.assertRaisesRegex(mirror.MirrorError, 'corrupt OCI blob'):
            mirror.import_one(bundle)
        push.assert_not_called()

    def test_metadata_digest_substitution_fails_before_push(self):
        image, _ = fixture(self.stage)
        image = mirror.parse_image('docker.io/library/alpine:3.20@sha256:' + 'f'*64, 'mirror/alpine')
        bundle = self.root / 'quay-wrong.tar'
        pack(self.stage, bundle, image=image)
        with mock.patch.object(mirror, 'copy') as push, self.assertRaisesRegex(mirror.MirrorError, 'OCI root'):
            mirror.import_one(bundle)
        push.assert_not_called()

    def test_import_receipt_is_written_only_after_verified_push(self):
        bundle = self.root / 'quay-first.tar'
        image = pack(self.stage, bundle)
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value='sha256:'+'0'*64):
            with self.assertRaisesRegex(mirror.MirrorError, 'high mirror digest mismatch'):
                mirror.import_one(bundle)
        self.assertFalse((self.root / 'import/received.json').exists())
        with mock.patch.object(mirror, 'copy') as push, mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            mirror.import_one(bundle)
            mirror.import_one(bundle)
        self.assertEqual(push.call_count, 1)

    def test_missing_delta_and_old_replay_fail_full_resend_recovers(self):
        image, _ = fixture(self.stage)
        first, gap, recovery = [self.root / f'quay-{n}.tar' for n in (1, 3, 4)]
        pack(self.stage, first, image=image)
        pack(self.stage, gap, sequence=3, full=False, image=image)
        pack(self.stage, recovery, sequence=4, full=True, image=image)
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            mirror.import_one(first)
            with self.assertRaisesRegex(mirror.MirrorError, 'missing earlier'):
                mirror.import_one(gap)
            mirror.import_one(recovery)
            with self.assertRaisesRegex(mirror.MirrorError, 'refusing rollback'):
                mirror.import_one(first)

    def test_new_stream_requires_explicit_adoption_of_full_bundle(self):
        image, _ = fixture(self.stage)
        first, other = self.root / 'quay-first.tar', self.root / 'quay-other.tar'
        pack(self.stage, first, image=image)
        pack(self.stage, other, image=image, stream='b'*32)
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            mirror.import_one(first)
            with self.assertRaisesRegex(mirror.MirrorError, 'different sender'):
                mirror.import_one(other)
            mirror.import_one(other, adopt_stream=True)

    def test_inbox_full_recovery_unblocks_waiting_deltas(self):
        image, _ = fixture(self.stage)
        inbox = self.root / 'inbox'
        inbox.mkdir()
        pack(self.stage, inbox / 'quay-3.tar', sequence=3, full=False, image=image)
        pack(self.stage, inbox / 'quay-4.tar', sequence=4, full=True, image=image)
        with mock.patch.object(mirror, 'copy') as push, mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            mirror.import_inbox(inbox)
        self.assertEqual(push.call_count, 1)
        self.assertEqual(len(list((inbox / 'done').glob('*.tar'))), 2)

    def test_inbox_waits_for_readiness_marker(self):
        (self.root / 'quay-partial.tar').write_bytes(b'partial')
        with mock.patch.object(mirror, 'import_one') as importer:
            self.assertEqual(mirror.main(['import', '--inbox', str(self.root)]), 0)
        importer.assert_not_called()

    def test_unsafe_duplicate_and_link_archive_members_fail(self):
        for names, kind in [(['../escape'], tarfile.REGTYPE),
                            (['images.json', 'images.json'], tarfile.REGTYPE),
                            (['images.json'], tarfile.SYMTYPE)]:
            with self.subTest(names=names, kind=kind):
                bundle = self.root / 'unsafe.tar'
                with tarfile.open(bundle, 'w') as archive:
                    for name in names:
                        item = tarfile.TarInfo(name)
                        item.type = kind
                        item.size = 1 if kind == tarfile.REGTYPE else 0
                        archive.addfile(item, io.BytesIO(b'x') if item.size else None)
                with self.assertRaisesRegex(mirror.MirrorError, 'unsafe or duplicate'):
                    mirror.extract(bundle, self.stage)

    def test_concurrent_work_is_refused(self):
        with mirror.locked(self.root / 'work'):
            with self.assertRaisesRegex(mirror.MirrorError, 'another mirror process'):
                with mirror.locked(self.root / 'work'):
                    self.fail('lock was not exclusive')

    def test_sync_repairs_low_even_when_nothing_is_due_and_failed_delivery_recovers(self):
        image, _ = fixture(self.stage)
        path = self.root / 'images.txt'
        path.write_text(image['source'] + ' ' + image['target'] + '\n')
        import shutil

        def fake_copy(_source, destination, *_args):
            if destination.startswith('oci:'):
                shutil.copytree(self.stage / 'oci', Path(destination.split(':')[1]), dirs_exist_ok=True)

        with mock.patch.object(mirror, 'copy', side_effect=fake_copy) as transport, \
                mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            first = mirror.sync(path)
            self.assertTrue(first.exists())
            before = transport.call_count
            self.assertIsNone(mirror.sync(path))
            self.assertEqual(transport.call_count, before)
            with mock.patch.object(mirror, 'notify', side_effect=mirror.MirrorError('delivery failed')):
                with self.assertRaisesRegex(mirror.MirrorError, 'delivery failed'):
                    mirror.sync(path, full=True)
            recovery = mirror.sync(path)
            with tarfile.open(recovery) as archive:
                metadata = json.load(archive.extractfile('images.json'))
            self.assertTrue(metadata['full'])
            self.assertEqual(metadata['sequence'], 3)


if __name__ == '__main__':
    unittest.main()
