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


def blob(directory, data, name='{}'):
    payload = json.dumps(data).encode()
    digest = hashlib.sha256(payload).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name.format(digest)).write_bytes(payload)
    return {'digest': 'sha256:' + digest, 'size': len(payload)}


def image_manifest(directory, name='{}'):
    config = blob(directory, {'architecture': 'amd64', 'os': 'linux'})
    layer = blob(directory, {'test': 'layer'})
    return blob(directory, {'schemaVersion': 2, 'config': config, 'layers': [layer]}, name), layer


def place(stage, directory, root):
    """Name a skopeo dir: copy after its root digest, as sync does."""
    (directory / 'manifest.json').write_bytes((directory / root['digest'][7:]).read_bytes())
    (directory / root['digest'][7:]).unlink()
    final = stage / 'images' / root['digest'].replace(':', '-')
    final.parent.mkdir(parents=True, exist_ok=True)
    directory.rename(final)
    (final / 'version').write_text('Directory Transport Version: 1.1\n')
    return final


def fixture(stage):
    root, layer = image_manifest(stage / 'building')
    place(stage, stage / 'building', root)
    image = mirror.parse_image('docker.io/library/alpine:3.20@' + root['digest'], 'mirror/alpine')
    return image | {'transfer': root['digest']}, layer


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
            'LOW_QUAY_HOST': 'low.example.internal', 'HIGH_QUAY_HOST': 'high.example.internal',
            'MIRROR_STATE_DIR': str(self.root / 'work'), 'MIRROR_BUNDLE_DIR': str(self.root / 'out'),
            'IMPORT_STATE_DIR': str(self.root / 'import'),
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_catalog_requires_a_digest_and_an_org_and_repository(self):
        pinned = 'docker.io/library/alpine:3.20@sha256:' + 'a'*64
        self.assertEqual(mirror.parse_image(pinned, 'team-dev/library/alpine')['target'], 'team-dev/library/alpine')
        for source, target in [('docker.io/library/alpine:3.20', 'mirror/alpine'),
                               (pinned, 'alpine'), (pinned, 'mirror/../alpine'), (pinned, 'mirror//alpine'),
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
        with mock.patch.object(mirror, 'raw_digest', return_value='sha256:' + 'a'*64) as lookup:
            mirror.add_image(path, 'prom/prometheus:v3.13.4', 'team-dev/prom/prometheus')
        lookup.assert_called_once_with('docker://docker.io/prom/prometheus:v3.13.4', 'UPSTREAM')
        self.assertEqual(path.read_text(), 'docker.io/prom/prometheus:v3.13.4@sha256:' + 'a'*64 + ' team-dev/prom/prometheus\n')
        self.assertEqual(mirror.normalise('alpine:3.20'), 'docker.io/library/alpine:3.20')
        self.assertEqual(mirror.normalise('quay.io/org/image:1'), 'quay.io/org/image:1')
        self.assertEqual(mirror.normalise('localhost:5000/image:1'), 'localhost:5000/image:1')

    def test_skopeo_transport_omits_tag_but_keeps_port_and_digest(self):
        reference = 'registry.example.internal:5000/team/image:1.0@sha256:' + 'a'*64
        self.assertEqual(mirror.transport_reference(reference),
                         'registry.example.internal:5000/team/image@sha256:' + 'a'*64)

    def test_manifest_list_children_and_blobs_are_verified(self):
        building = self.stage / 'building'
        child, layer = image_manifest(building, '{}.manifest.json')
        child['platform'] = {'architecture': 'amd64', 'os': 'linux'}
        root = blob(building, {'schemaVersion': 2, 'manifests': [child]})
        directory = place(self.stage, building, root)
        mirror.verify_image(directory, root['digest'])
        (directory / layer['digest'][7:]).write_text('corrupt')
        with self.assertRaisesRegex(mirror.MirrorError, 'corrupt image file'):
            mirror.verify_image(directory, root['digest'])
        (directory / layer['digest'][7:]).unlink()
        with self.assertRaisesRegex(mirror.MirrorError, 'corrupt image file'):
            mirror.verify_image(directory, root['digest'])

    def test_import_rejects_corrupt_content_even_with_recomputed_archive_checksum(self):
        image, layer = fixture(self.stage)
        (self.stage / 'images' / image['digest'].replace(':', '-') / layer['digest'][7:]).write_text('bad')
        bundle = self.root / 'quay-bad.tar'
        pack(self.stage, bundle, image=image)
        with mock.patch.object(mirror, 'copy') as push, self.assertRaisesRegex(mirror.MirrorError, 'corrupt image file'):
            mirror.import_one(bundle)
        push.assert_not_called()

    def test_metadata_digest_substitution_fails_before_push(self):
        image, _ = fixture(self.stage)
        image = mirror.parse_image('docker.io/library/alpine:3.20@sha256:' + 'f'*64, 'mirror/alpine')
        image['transfer'] = image['digest']
        bundle = self.root / 'quay-wrong.tar'
        pack(self.stage, bundle, image=image)
        with mock.patch.object(mirror, 'copy') as push, self.assertRaisesRegex(mirror.MirrorError, 'corrupt image file'):
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

    def test_state_defaults_to_the_home_directory(self):
        with mock.patch.dict(os.environ, {'HOME': str(self.root)}, clear=True):
            self.assertEqual(mirror.state_path('MIRROR_STATE_DIR', 'quay-mirror'), self.root / '.local/state/quay-mirror')

    def test_platform_selection_picks_one_child_or_fails(self):
        child = {'digest': 'sha256:' + 'b'*64, 'platform': {'os': 'linux', 'architecture': 'amd64'}}
        arm = {'digest': 'sha256:' + 'c'*64, 'platform': {'os': 'linux', 'architecture': 'arm', 'variant': 'v7'}}
        attestation = {'digest': 'sha256:' + 'd'*64, 'platform': {'os': 'unknown', 'architecture': 'unknown'}}
        listing = json.dumps({'schemaVersion': 2, 'manifests': [child, arm, attestation]}).encode()
        pinned = 'sha256:' + 'a'*64
        with mock.patch.object(mirror, 'run', return_value=listing):
            self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'UPSTREAM'), pinned)
            for wanted, expected in (('linux/amd64', child['digest']), ('linux/arm/v7', arm['digest'])):
                with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': wanted}):
                    self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'UPSTREAM'), expected)
            with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/s390x'}), \
                    self.assertRaisesRegex(mirror.MirrorError, 'found 0'):
                mirror.platform_digest('docker://low/x@' + pinned, pinned, 'UPSTREAM')
        for single in (b'{"schemaVersion":2,"config":{},"layers":[]}',
                       json.dumps({'schemaVersion': 2, 'manifests': [{'digest': child['digest']}]}).encode()):
            with mock.patch.object(mirror, 'run', return_value=single), \
                    mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
                self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'UPSTREAM'), pinned)

    def test_platform_image_must_belong_to_the_approved_index(self):
        image, _ = fixture(self.stage)
        directory = self.stage / 'images' / image['transfer'].replace(':', '-')
        index = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': image['transfer']}]}).encode()
        approved = 'sha256:' + hashlib.sha256(index).hexdigest()
        (directory / f'{approved[7:]}.manifest.json').write_bytes(index)
        platform = image | {'source': 'docker.io/library/alpine:3.20@' + approved, 'digest': approved}
        mirror.verify_platform(directory, platform)
        for forged in ({'transfer': 'sha256:' + 'e'*64}, {'digest': 'sha256:' + 'f'*64}):
            with self.subTest(forged=forged), self.assertRaises(mirror.MirrorError):
                mirror.verify_platform(directory, platform | forged)

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
            if destination.startswith('dir:'):
                shutil.copytree(self.stage / 'images' / image['digest'].replace(':', '-'), Path(destination[4:]))

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
