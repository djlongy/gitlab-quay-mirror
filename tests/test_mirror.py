"""Archive integrity, publication ordering and receipt recovery contracts."""
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror

REAL_IMAGE_BYTES = mirror.image_bytes


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
    return image | {'transfer': root['digest'], 'tags': [image['tag']]}, layer


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
            'TARGET_REGISTRY': 'low.example.internal',
            'MIRROR_STATE_DIR': str(self.root / 'work'), 'MIRROR_BUNDLE_DIR': str(self.root / 'out'),
            'IMPORT_STATE_DIR': str(self.root / 'import'),
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # Bundle sizing asks the registry for every manifest; tests that script run() do not expect it.
        sizing = mock.patch.object(mirror, 'image_bytes', return_value=1)
        sizing.start()
        self.addCleanup(sizing.stop)
        # CI runs send straight after sync: unless a test says otherwise, each written bundle is delivered.
        self.write_only = mirror.write_bundle

        def written_and_sent(work, out, state_file, state, *rest, **named):
            bundle = self.write_only(work, out, state_file, state, *rest, **named)
            (out / (bundle.name + '.send.json')).unlink()
            state['pending'] = False
            mirror.save_state(state_file, state)
            return bundle
        delivered = mock.patch.object(mirror, 'write_bundle', side_effect=written_and_sent)
        delivered.start()
        self.addCleanup(delivered.stop)

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
        image = json.dumps({'schemaVersion': 2, 'config': {'mediaType': 'application/vnd.oci.image.config.v1+json'},
                            'layers': []}).encode()
        index = json.dumps({'schemaVersion': 2, 'manifests': [
            {'digest': 'sha256:' + hashlib.sha256(image).hexdigest(),
             'platform': {'os': 'linux', 'architecture': 'amd64'}}]}).encode()
        pinned = 'sha256:' + hashlib.sha256(index).hexdigest()
        with mock.patch.object(mirror, 'run', side_effect=[index, image]) as lookup:
            mirror.add_image(path, 'docker.io/prom/prometheus:v3.13.4', 'team-dev/prom/prometheus')
        self.assertEqual(lookup.call_args_list[0].args[-1], 'docker://docker.io/prom/prometheus:v3.13.4')
        self.assertEqual(path.read_text(), f'docker.io/prom/prometheus:v3.13.4@{pinned} team-dev/prom/prometheus\n')
        self.assertEqual(mirror.normalise('alpine:3.20'), 'docker.io/library/alpine:3.20')
        for short, full in [('prom/prometheus:v3.13.4', 'docker.io/prom/prometheus:v3.13.4'),
                            ('alpine:3.20', 'docker.io/library/alpine:3.20')]:
            with self.assertRaisesRegex(mirror.MirrorError, f'no registry host; give the full reference, for example {full}'):
                mirror.add_image(path, short, 'team-dev/prom/prometheus')
            with self.assertRaises(mirror.MirrorError):
                mirror.parse_image(short + '@sha256:' + 'a'*64, 'team-dev/prom/prometheus')
        self.assertEqual(mirror.parse_image('registry:5000/a/b:1@sha256:' + 'a'*64, 'team/b')['tag'], '1')
        self.assertEqual(mirror.normalise('quay.io/org/image:1'), 'quay.io/org/image:1')
        self.assertEqual(mirror.normalise('localhost:5000/image:1'), 'localhost:5000/image:1')

    def test_add_refuses_signatures_and_metadata_artifacts(self):
        image_config = {'mediaType': 'application/vnd.docker.container.image.v1+json'}
        self.assertTrue(mirror.runnable({'schemaVersion': 2, 'config': image_config, 'layers': []}))
        # cosign's .sig tag: an image config media type, an empty {} config and a simplesigning layer.
        cosign = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                  'config': {'mediaType': 'application/vnd.oci.image.config.v1+json', 'size': 2,
                             'digest': 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'},
                  'layers': [{'mediaType': 'application/vnd.dev.cosign.simplesigning.v1+json',
                              'digest': 'sha256:' + 'ab'*32, 'size': 251}]}
        for artifact in ({'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + 'b'*64,
                                                             'artifactType': 'application/vnd.cncf.notary.signature'}]},
                         {'schemaVersion': 2, 'artifactType': 'application/vnd.example.metadata.config.v1+json',
                          'config': {'mediaType': 'application/vnd.oci.empty.v1+json'}, 'layers': []},
                         {'schemaVersion': 2, 'config': {'mediaType': 'application/vnd.dev.cosign.simplesigning.v1+json'}},
                         cosign,
                         {'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + 'b'*64,
                          'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                          'platform': {'os': 'unknown', 'architecture': 'unknown'}}]}):
            with self.subTest(artifact=artifact):
                self.assertFalse(mirror.runnable(artifact))
                with mock.patch.object(mirror, 'run', return_value=json.dumps(artifact).encode()), \
                        self.assertRaisesRegex(mirror.MirrorError, 'not a container image'):
                    mirror.add_image(self.root / 'images.txt', 'docker.io/bitnami/redis:sha256-' + 'c'*64 + '.sig',
                                     'mirror/bitnami/redis')
        self.assertFalse((self.root / 'images.txt').exists())

    def test_add_accepts_an_index_whose_children_omit_the_optional_platform(self):
        path = self.root / 'images.txt'
        image = {'schemaVersion': 2, 'config': {'mediaType': 'application/vnd.oci.image.config.v1+json'},
                 'layers': [{'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip'}]}
        child = json.dumps(image).encode()
        nested = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + hashlib.sha256(child).hexdigest()}]})
        index = json.dumps({'schemaVersion': 2, 'manifests': [
            {'mediaType': 'application/vnd.oci.image.manifest.v1+json',
             'digest': 'sha256:' + hashlib.sha256(child).hexdigest(), 'size': len(child)}]})
        # A nested index says nothing about its children, so each is fetched and checked by digest.
        outer = json.dumps({'schemaVersion': 2, 'manifests': [{
            'mediaType': 'application/vnd.oci.image.index.v1+json',
            'digest': 'sha256:' + hashlib.sha256(nested.encode()).hexdigest()}]})
        for raws, tag in (([index.encode(), child], '1.0'), ([outer.encode(), nested.encode(), child], '2.0')):
            with self.subTest(tag=tag), mock.patch.object(mirror, 'run', side_effect=raws) as call:
                self.assertEqual(mirror.main(['--catalog', str(path), 'add', f'example.org/team/image:{tag}',
                                              'mirror/image']), 0)
            self.assertEqual(call.call_count, len(raws))
        self.assertEqual(len(mirror.catalog(path)), 2)
        with mock.patch.object(mirror, 'run', side_effect=[outer.encode(), b'{"tampered": true}']), \
                self.assertRaisesRegex(mirror.MirrorError, 'manifest digest mismatch'):
            mirror.add_image(path, 'example.org/team/image:3.0', 'mirror/image')

    def test_add_accepts_every_layer_type_skopeo_copies(self):
        path = self.root / 'images.txt'
        layers = ('application/vnd.docker.image.rootfs.diff.tar', 'application/vnd.docker.image.rootfs.diff.tar.gzip',
                  'application/vnd.docker.image.rootfs.foreign.diff.tar',
                  'application/vnd.docker.image.rootfs.foreign.diff.tar.gzip',
                  'application/vnd.oci.image.layer.v1.tar', 'application/vnd.oci.image.layer.v1.tar+zstd',
                  'application/vnd.oci.image.layer.nondistributable.v1.tar+gzip')
        for number, layer in enumerate(layers):
            manifest = json.dumps({'schemaVersion': 2, 'mediaType': mirror.MANIFESTS[1],
                                   'config': {'mediaType': 'application/vnd.docker.container.image.v1+json'},
                                   'layers': [{'mediaType': layer}]}).encode()
            with self.subTest(layer=layer), mock.patch.object(mirror, 'run', return_value=manifest):
                mirror.add_image(path, f'example.org/team/image:{number}', 'mirror/image')
        self.assertEqual(len(mirror.catalog(path)), len(layers))

    def test_add_checks_each_index_child_and_refuses_an_index_of_signatures(self):
        path = self.root / 'images.txt'
        image_config = {'mediaType': 'application/vnd.oci.image.config.v1+json', 'size': 2,
                        'digest': 'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'}
        # A cosign signature uses the image manifest media type, so its descriptor alone cannot tell.
        signature = json.dumps({'schemaVersion': 2, 'mediaType': mirror.MANIFESTS[0], 'config': image_config,
                                'layers': [{'mediaType': 'application/vnd.dev.cosign.simplesigning.v1+json'}]}).encode()
        image = json.dumps({'schemaVersion': 2, 'mediaType': mirror.MANIFESTS[0], 'config': image_config,
                            'layers': [{'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip'}]}).encode()
        manifests = {'sha256:' + hashlib.sha256(raw).hexdigest(): raw for raw in (signature, image)}

        def listing(*children):
            return json.dumps({'schemaVersion': 2, 'mediaType': mirror.INDEXES[0], 'manifests': [
                {'mediaType': mirror.MANIFESTS[0], 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                 'size': len(raw)} for raw in children]}).encode()

        def registry(index):
            return lambda *args: manifests.get(args[-1].rsplit('@', 1)[-1], index)

        for index in (listing(signature), listing(signature, signature)):
            with self.subTest(index=index), mock.patch.object(mirror, 'run', side_effect=registry(index)), \
                    self.assertRaisesRegex(mirror.MirrorError, 'not a container image'):
                mirror.add_image(path, 'example.org/team/image:signatures', 'mirror/image')
        self.assertFalse(path.exists())
        with mock.patch.object(mirror, 'run', side_effect=registry(listing(signature, image))):
            mirror.add_image(path, 'example.org/team/image:1.0', 'mirror/image')
        self.assertEqual([i['tag'] for i in mirror.catalog(path)], ['1.0'])

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
        bundle = self.root / 'mirror-bad.tar'
        pack(self.stage, bundle, image=image)
        with mock.patch.object(mirror, 'copy') as push, self.assertRaisesRegex(mirror.MirrorError, 'corrupt image file'):
            mirror.import_one(bundle)
        push.assert_not_called()

    def test_metadata_digest_substitution_fails_before_push(self):
        image, _ = fixture(self.stage)
        image = mirror.parse_image('docker.io/library/alpine:3.20@sha256:' + 'f'*64, 'mirror/alpine')
        image.update(transfer=image['digest'], tags=[image['tag']])
        bundle = self.root / 'mirror-wrong.tar'
        pack(self.stage, bundle, image=image)
        with mock.patch.object(mirror, 'copy') as push, self.assertRaisesRegex(mirror.MirrorError, 'corrupt image file'):
            mirror.import_one(bundle)
        push.assert_not_called()

    def test_import_receipt_is_written_only_after_verified_push(self):
        bundle = self.root / 'mirror-first.tar'
        image = pack(self.stage, bundle)
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value='sha256:'+'0'*64), \
                self.assertRaisesRegex(mirror.MirrorError, 'high mirror digest mismatch'):
            mirror.import_one(bundle)
        self.assertFalse((self.root / 'import/received.json').exists())
        with mock.patch.object(mirror, 'copy') as push, mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            record = self.root / 'imported.json'
            record.write_text('[]\n')
            mirror.import_one(bundle, record=record)
            mirror.import_one(bundle, record=record)  # already imported: nothing new to record
        self.assertEqual(push.call_count, 1)
        self.assertEqual(json.loads(record.read_text()),
                         [{'target': image['target'], 'tag': tag, 'digest': image['transfer']} for tag in image['tags']])

    def test_promote_copies_recorded_digests_from_dev_to_prod_and_verifies_both(self):
        os.environ.update({'SOURCE_REGISTRY': 'dev.example.internal', 'TARGET_REGISTRY': 'prod.example.internal',
                           'SOURCE_REGISTRY_TLS_VERIFY': 'false'})
        old, new, other = ('sha256:' + c * 64 for c in 'abc')
        record = self.root / 'imported.json'
        record.write_text(json.dumps([{'target': 'team/app', 'tag': '1.0', 'digest': old},
                                      {'target': 'team/app', 'tag': '1.0', 'digest': new},
                                      {'target': 'team/db', 'tag': '2', 'digest': other}]))
        digests = {'docker://dev.example.internal/team/app@' + new: new, 'docker://prod.example.internal/team/app:1.0': new,
                   'docker://dev.example.internal/team/db@' + other: other, 'docker://prod.example.internal/team/db:2': other}
        with mock.patch.object(mirror, 'copy') as copied, mock.patch('builtins.print'), \
                mock.patch.object(mirror, 'raw_digest', side_effect=lambda ref, side: digests[ref]):
            mirror.promote(record)
        self.assertEqual([c.args for c in copied.call_args_list], [
            ('docker://dev.example.internal/team/app@' + new, 'docker://prod.example.internal/team/app:1.0',
             'SOURCE_REGISTRY', 'TARGET_REGISTRY'),
            ('docker://dev.example.internal/team/db@' + other, 'docker://prod.example.internal/team/db:2',
             'SOURCE_REGISTRY', 'TARGET_REGISTRY')])
        digests['docker://prod.example.internal/team/db:2'] = old
        with mock.patch.object(mirror, 'copy'), mock.patch('builtins.print'), \
                mock.patch.object(mirror, 'raw_digest', side_effect=lambda ref, side: digests[ref]), \
                self.assertRaisesRegex(mirror.MirrorError, 'digest mismatch in TARGET_REGISTRY after the copy'):
            mirror.promote(record)
        record.write_text(json.dumps([{'target': 'team/app', 'tag': '1.0', 'digest': 'latest'}]))
        with self.assertRaisesRegex(mirror.MirrorError, 'invalid entry'):
            mirror.promote(record)
        os.environ['TARGET_REGISTRY'] = 'dev.example.internal'
        with self.assertRaisesRegex(mirror.MirrorError, 'same registry'):
            mirror.promote(record)

    def test_bundle_from_a_sender_without_version_tags_imports_its_catalog_tag(self):
        image, _ = fixture(self.stage)
        image.update(tag='latest', source=image['source'].replace(':3.20@', ':latest@'))
        del image['tags']
        bundle = self.root / 'mirror-old.tar'
        pack(self.stage, bundle, image=image)
        with mock.patch.object(mirror, 'copy') as push, mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            self.assertEqual(mirror.main(['import', str(bundle)]), 0)
        self.assertEqual([c.args[1] for c in push.call_args_list], ['docker://low.example.internal/mirror/alpine:latest'])

    def test_missing_delta_and_old_replay_fail_full_resend_recovers(self):
        image, _ = fixture(self.stage)
        first, gap, recovery = [self.root / f'mirror-{n}.tar' for n in (1, 3, 4)]
        pack(self.stage, first, image=image)
        pack(self.stage, gap, sequence=3, full=False, image=image)
        pack(self.stage, recovery, sequence=4, full=True, image=image)
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            mirror.import_one(first)
            with self.assertRaisesRegex(mirror.MirrorError, r'missing earlier bundle 2 \(imported up to 1, received 3\)'
                                                            r'.*RESEND_SEQUENCE=2\.\.'):
                mirror.import_one(gap)
            mirror.import_one(recovery)
            with self.assertRaisesRegex(mirror.MirrorError, 'refusing rollback'):
                mirror.import_one(first)

    def test_new_stream_requires_explicit_adoption_of_full_bundle(self):
        image, _ = fixture(self.stage)
        first, other = self.root / 'mirror-first.tar', self.root / 'mirror-other.tar'
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
        pack(self.stage, inbox / 'mirror-3.tar', sequence=3, full=False, image=image)
        pack(self.stage, inbox / 'mirror-4.tar', sequence=4, full=True, image=image)
        with mock.patch.object(mirror, 'copy') as push, mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            mirror.import_inbox(inbox)
        self.assertEqual(push.call_count, 1)
        self.assertEqual(len(list((inbox / 'done').glob('*.tar'))), 2)

    def test_inbox_waits_for_readiness_marker(self):
        (self.root / 'mirror-partial.tar').write_bytes(b'partial')
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
            self.assertEqual(mirror.state_path('MIRROR_STATE_DIR', 'registry-mirror'), self.root / '.local/state/registry-mirror')

    def test_platform_selection_picks_one_child_or_fails(self):
        child = {'digest': 'sha256:' + 'b'*64, 'platform': {'os': 'linux', 'architecture': 'amd64'}}
        arm = {'digest': 'sha256:' + 'c'*64, 'platform': {'os': 'linux', 'architecture': 'arm', 'variant': 'v7'}}
        attestation = {'digest': 'sha256:' + 'd'*64, 'platform': {'os': 'unknown', 'architecture': 'unknown'}}
        listing = json.dumps({'schemaVersion': 2, 'manifests': [child, arm, attestation]}).encode()
        pinned = 'sha256:' + 'a'*64
        with mock.patch.object(mirror, 'run', return_value=listing):
            self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY'), pinned)
            for wanted, expected in (('linux/amd64', child['digest']), ('linux/arm/v7', arm['digest'])):
                with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': wanted}):
                    self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY'), expected)
            with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/s390x'}), \
                    self.assertRaisesRegex(mirror.MirrorError, 'found 0'):
                mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY')
        single = b'{"schemaVersion":2,"config":{},"layers":[]}'
        with mock.patch.object(mirror, 'run', side_effect=[single, b'{"os": "linux", "architecture": "amd64"}']), \
                mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
            self.assertEqual(mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY'), pinned)
        # A single image is checked against its config: the wrong architecture or variant fails.
        for wanted, config in (('linux/amd64', {'os': 'linux', 'architecture': 'arm64'}),
                               ('linux/arm/v7', {'os': 'linux', 'architecture': 'arm', 'variant': 'v6'}),
                               ('linux/amd64', [])):
            with self.subTest(wanted=wanted, config=config), \
                    mock.patch.object(mirror, 'run', side_effect=[single, json.dumps(config).encode()]) as call, \
                    mock.patch.dict(os.environ, {'MIRROR_PLATFORM': wanted}), \
                    self.assertRaisesRegex(mirror.MirrorError, 'image, not ' + wanted):
                mirror.platform_digest('docker://low/x@' + pinned, pinned, 'SOURCE_REGISTRY')
            self.assertIn('--config', call.call_args.args)

    def test_index_without_platforms_is_selected_by_each_child_config(self):
        amd, arm = 'sha256:' + 'b'*64, 'sha256:' + 'c'*64
        configs = {amd: {'os': 'linux', 'architecture': 'amd64'}, arm: {'os': 'linux', 'architecture': 'arm64'}}
        signature = {'digest': 'sha256:' + 'd'*64, 'artifactType': 'application/vnd.dev.cosign.artifact.sig.v1+json'}
        pinned = 'sha256:' + 'a'*64

        def registry(children):
            listing = json.dumps({'schemaVersion': 2, 'manifests': children}).encode()

            def fake_run(*args):
                if '--config' not in args:
                    return listing
                return json.dumps(configs[args[-1].rsplit('@', 1)[1]]).encode()
            return fake_run

        with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
            # An index child's platform is optional; the child's own config names it instead.
            for children, expected in (([{'digest': arm}, {'digest': amd}, signature], amd),
                                       ([{'digest': arm}, {'digest': amd, 'platform': configs[amd]}], amd)):
                with self.subTest(children=children), mock.patch.object(mirror, 'run', side_effect=registry(children)):
                    self.assertEqual(mirror.platform_digest('docker://up/x@' + pinned, pinned, 'SOURCE_REGISTRY'), expected)
            # Never the whole index: an arm64-only index fails, and so does a nested index nobody can select from.
            for children, found in (([{'digest': arm}], 0), ([{'digest': amd}, {'digest': amd}], 2),
                                    ([{'digest': amd, 'mediaType': mirror.INDEXES[0]}], 0)):
                with self.subTest(children=children), mock.patch.object(mirror, 'run', side_effect=registry(children)), \
                        self.assertRaisesRegex(mirror.MirrorError, f'found {found}'):
                    mirror.platform_digest('docker://up/x@' + pinned, pinned, 'SOURCE_REGISTRY')

    def test_referrer_or_nested_index_is_never_the_platform_image(self):
        amd, pinned = 'sha256:' + 'b'*64, 'sha256:' + 'a'*64
        wanted = {'os': 'linux', 'architecture': 'amd64'}
        image = {'mediaType': mirror.MANIFESTS[0], 'digest': amd, 'platform': wanted}
        # Each names linux/amd64 in its descriptor, but none is an image the index can select.
        impostors = {
            'signature': {'mediaType': mirror.MANIFESTS[0], 'digest': 'sha256:' + 'd'*64, 'platform': wanted,
                          'artifactType': 'application/vnd.dev.cosign.artifact.sig.v1+json'},
            'attestation': {'mediaType': mirror.MANIFESTS[0], 'digest': 'sha256:' + 'e'*64, 'platform': wanted,
                            'annotations': {'vnd.docker.reference.type': 'attestation-manifest'}},
            'nested index': {'mediaType': mirror.INDEXES[0], 'digest': 'sha256:' + 'f'*64, 'platform': wanted},
        }
        path = self.root / 'images.txt'
        path.write_text(f'docker.io/library/example:1.0@{pinned} mirror/example\n')
        with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
            for name, impostor in impostors.items():
                for children, found in (([impostor], 0), ([impostor, image], 1)):
                    listing = json.dumps({'schemaVersion': 2, 'manifests': children}).encode()
                    with self.subTest(name, children=len(children)), mock.patch.object(mirror, 'run', return_value=listing):
                        if found:
                            self.assertEqual(mirror.platform_digest('docker://up/x@' + pinned, pinned, 'SOURCE_REGISTRY'), amd)
                            continue
                        with self.assertRaisesRegex(mirror.MirrorError, 'found 0'):
                            mirror.platform_digest('docker://up/x@' + pinned, pinned, 'SOURCE_REGISTRY')
                        with mock.patch.object(mirror, 'copy') as transport, \
                                mock.patch.object(mirror, 'raw_digest', return_value=impostor['digest']):
                            self.assertEqual(mirror.main(['--catalog', str(path), 'sync']), 1)
                        transport.assert_not_called()
                        self.assertFalse((self.root / 'out').exists())

    def test_index_of_another_platform_without_platforms_is_not_mirrored(self):
        building = self.stage / 'building'
        child, _ = image_manifest(building, '{}.manifest.json')
        root = blob(building, {'schemaVersion': 2, 'manifests': [child]})
        index = (building / root['digest'][7:]).read_bytes()
        path = self.root / 'images.txt'
        path.write_text('docker.io/arm64v8/alpine:3.20@' + root['digest'] + ' mirror/alpine\n')
        raws = {False: index, True: b'{"os": "linux", "architecture": "arm64"}'}
        with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}), \
                mock.patch.object(mirror, 'run', side_effect=lambda *args: raws['--config' in args]), \
                mock.patch.object(mirror, 'copy') as transport, \
                mock.patch.object(mirror, 'raw_digest', return_value=root['digest']):
            self.assertEqual(mirror.main(['--catalog', str(path), 'sync']), 1)
        transport.assert_not_called()
        self.assertFalse((self.root / 'work/sent.json').exists())

    def test_single_image_of_another_platform_is_not_mirrored(self):
        building = self.stage / 'building'
        config = blob(building, {'architecture': 'arm64', 'os': 'linux'})
        root = blob(building, {'schemaVersion': 2, 'config': config, 'layers': []})
        directory = place(self.stage, building, root)
        path = self.root / 'images.txt'
        path.write_text('docker.io/arm64v8/alpine:3.20@' + root['digest'] + ' mirror/alpine\n')
        raws = {False: (directory / 'manifest.json').read_bytes(), True: (directory / config['digest'][7:]).read_bytes()}
        with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}), \
                mock.patch.object(mirror, 'run', side_effect=lambda *args: raws['--config' in args]), \
                mock.patch.object(mirror, 'copy') as transport, \
                mock.patch.object(mirror, 'raw_digest', return_value=root['digest']):
            self.assertEqual(mirror.main(['--catalog', str(path), 'sync']), 1)
        transport.assert_not_called()
        self.assertFalse((self.root / 'work/sent.json').exists())

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
        # Listed is not enough: a referrer or nested index is never the platform image.
        for listed in ({'artifactType': 'application/vnd.dev.cosign.artifact.sig.v1+json'},
                       {'annotations': {'vnd.docker.reference.type': 'attestation-manifest'}},
                       {'mediaType': mirror.INDEXES[0]}):
            index = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': image['transfer']} | listed]}).encode()
            approved = 'sha256:' + hashlib.sha256(index).hexdigest()
            (directory / f'{approved[7:]}.manifest.json').write_bytes(index)
            with self.subTest(listed=listed), self.assertRaisesRegex(mirror.MirrorError, 'not an image'):
                mirror.verify_platform(directory, platform | {'digest': approved})

    def test_latest_is_paired_with_its_version_tag(self):
        inspected = {'Labels': {'org.opencontainers.image.version': '8.10.2'}, 'Env': ['APP_VERSION=8.10.1']}
        with mock.patch.object(mirror, 'run', return_value=json.dumps(inspected).encode()) as call:
            self.assertEqual(mirror.app_version('docker://low/redis@sha256:' + 'a'*64, 'TARGET_REGISTRY'), '8.10.2')
            self.assertNotIn('--override-arch=amd64', call.call_args.args)
            with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}):
                mirror.app_version('docker://low/redis@sha256:' + 'a'*64, 'TARGET_REGISTRY')
            self.assertIn('--override-arch=amd64', call.call_args.args)
        with mock.patch.object(mirror, 'run', side_effect=mirror.MirrorError('inspect timed out')), \
                self.assertRaisesRegex(mirror.MirrorError, 'cannot read the version'):
            mirror.app_version('docker://low/redis@sha256:' + 'a'*64, 'TARGET_REGISTRY')
        # Without MIRROR_PLATFORM an index is read through one of its images, not the host's platform.
        index = {'schemaVersion': 2, 'manifests': [
            {'digest': 'sha256:' + 'd'*64, 'platform': {'os': 'unknown', 'architecture': 'unknown'}},
            {'digest': 'sha256:' + 'c'*64, 'platform': {'os': 'linux', 'architecture': 'arm64'}}]}
        with mock.patch.object(mirror, 'run', side_effect=[json.dumps(index).encode(),
                                                          json.dumps(inspected).encode()]) as call:
            self.assertEqual(mirror.app_version('docker://up/redis@sha256:' + 'a'*64, 'SOURCE_REGISTRY'), '8.10.2')
        self.assertEqual(call.call_args.args[-1], 'docker://up/redis@sha256:' + 'c'*64)
        for data, expected in (({'Env': ['APP_VERSION=8.10.1']}, '8.10.1'), ({'Labels': {'x': 'y'}}, ''),
                               ({'Labels': {'org.opencontainers.image.version': 'not a tag'}}, '')):
            with mock.patch.object(mirror, 'run', return_value=json.dumps(data).encode()):
                self.assertEqual(mirror.app_version('docker://low/redis', 'TARGET_REGISTRY'), expected)
        stored = 'sha256:' + 'b'*64
        for version, expected in (('8.10.2', ['latest', '8.10.2']), ('', ['latest'])):
            with mock.patch.object(mirror, 'app_version', return_value=version):
                self.assertEqual(mirror.publish_tags('latest', stored, 'docker://up/redis', 'SOURCE_REGISTRY'), expected)
                self.assertEqual(mirror.publish_tags('3.20', stored, 'docker://up/redis', 'SOURCE_REGISTRY'), ['3.20'])
        image, _ = fixture(self.stage)
        image.update(tag='latest', source=image['source'].replace(':3.20@', ':latest@'),
                     tags=['latest', '8.10.2'])
        bundle = self.root / 'mirror-latest.tar'
        pack(self.stage, bundle, image=image)
        with mock.patch.object(mirror, 'copy') as push, mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            mirror.import_one(bundle)
        self.assertEqual([c.args[1].rsplit(':', 1)[1] for c in push.call_args_list], image['tags'])
        versioned = image | {'tag': '3.20', 'source': image['source'].replace(':latest@', ':3.20@'), 'tags': ['3.20', '9.9']}
        for forged in (image | {'tags': ['latest', '8.10.2', '9.9']}, image | {'tags': ['8.10.2']}, versioned):
            pack(self.stage, bundle, image=forged, sequence=5)
            with self.subTest(tags=forged['tags']), mock.patch.object(mirror, 'copy'), \
                    self.assertRaisesRegex(mirror.MirrorError, 'invalid image entry'):
                mirror.import_one(bundle)

    def latest_entry(self):
        image, _ = fixture(self.stage)
        path = self.root / 'images.txt'
        path.write_text(image['source'].replace(':3.20@', ':latest@') + ' ' + image['target'] + '\n')
        low = {}

        def fake_copy(_source, destination, *_args):
            if destination.startswith('dir:'):
                shutil.copytree(self.stage / 'images' / image['digest'].replace(':', '-'), Path(destination[4:]))
            else:
                low[destination] = image['digest']

        def fake_digest(reference, _side):
            if reference.startswith('docker://low.example.internal/') and reference not in low:
                raise mirror.MirrorError('manifest unknown')
            return image['digest']

        return path, low, fake_copy, fake_digest

    def bundled_tags(self, bundle):
        with tarfile.open(bundle) as archive:
            return [i['tags'] for i in json.load(archive.extractfile('images.json'))['images']]

    def test_failed_version_lookup_fails_sync_and_is_asked_again(self):
        path, low, fake_copy, fake_digest = self.latest_entry()
        answers = [mirror.MirrorError('inspect timed out'), {}, {'Env': ['APP_VERSION=8.10.2']}]

        def fake_run(*args):
            if '--raw' in args:
                return b'{"schemaVersion": 2}'
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return json.dumps(answer).encode()

        with mock.patch.object(mirror, 'run', side_effect=fake_run), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest):
            self.assertEqual(mirror.main(['--catalog', str(path), 'sync']), 1)
            self.assertFalse((self.root / 'work/sent.json').exists())
            # The image reports no version yet: latest alone, and nothing cached for it.
            self.assertEqual(self.bundled_tags(mirror.sync(path)), [['latest']])
            self.assertEqual(self.bundled_tags(mirror.sync(path, full=True)), [['latest', '8.10.2']])
        self.assertIn('docker://low.example.internal/mirror/alpine:8.10.2', low)
        self.assertEqual(answers, [])

    def test_daily_sync_tracks_and_repairs_version_tags(self):
        path, low, fake_copy, fake_digest = self.latest_entry()
        version = 'docker://low.example.internal/mirror/alpine:8.10.2'
        reported = {}

        def fake_run(*args):
            return b'{"schemaVersion": 2}' if '--raw' in args else json.dumps(reported).encode()

        with mock.patch.object(mirror, 'run', side_effect=fake_run), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy) as transport, \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest):
            self.assertEqual(self.bundled_tags(mirror.sync(path)), [['latest']])
            # An unchanged latest pin that now reports a version sends a delta carrying the new tag.
            reported['Env'] = ['APP_VERSION=8.10.2']
            self.assertEqual(self.bundled_tags(mirror.sync(path)), [['latest', '8.10.2']])
            self.assertIn(version, low)
            before = transport.call_count
            self.assertIsNone(mirror.sync(path))
            self.assertEqual(transport.call_count, before)
            del low[version]  # deleted or expired: the daily run puts it back without sending
            self.assertIsNone(mirror.sync(path))
            self.assertIn(version, low)
            self.assertEqual(transport.call_args.args, ('docker://low.example.internal/mirror/alpine@'
                                                       + low[version], version, 'TARGET_REGISTRY', 'TARGET_REGISTRY'))
        ledger = json.loads((self.root / 'work/sent.json').read_text())
        self.assertEqual(ledger['aliases'], {'mirror/alpine:latest': ['8.10.2']})

    def test_removed_pin_keeps_its_tag_when_latest_reports_that_version(self):
        latest, pinned = 'sha256:' + 'a'*64, 'sha256:' + 'b'*64
        path = self.root / 'images.txt'
        entries = {'latest': f'docker.io/bitnami/redis:latest@{latest} mirror/redis\n',
                   'pin': f'docker.io/bitnami/redis:8.10.2@{pinned} mirror/redis\n'}
        version = 'docker://low.example.internal/mirror/redis:8.10.2'
        low = {}

        def fake_copy(source, destination, *_args):
            if destination.startswith('dir:'):
                Path(destination[4:]).mkdir(parents=True)
            else:
                low[destination] = source.rsplit('@', 1)[1]

        def fake_digest(reference, _side):
            if reference.startswith('docker://low.example.internal/') and reference not in low:
                raise mirror.MirrorError('manifest unknown')
            return low.get(reference) or reference.rsplit('@', 1)[1]

        def fake_run(*args):
            return b'{"schemaVersion": 2}' if '--raw' in args else b'{"Env": ["APP_VERSION=8.10.2"]}'

        with mock.patch.object(mirror, 'run', side_effect=fake_run), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch.object(mirror, 'verify_image'):
            path.write_text(entries['latest'] + entries['pin'])
            self.assertEqual(sorted(self.bundled_tags(mirror.sync(path))), [['8.10.2'], ['latest']])
            self.assertEqual(low[version], pinned)
            # Removing the pin stops its updates; latest does not take the tag the pin established.
            path.write_text(entries['latest'])
            self.assertIsNone(mirror.sync(path))
            self.assertEqual(low[version], pinned)
            self.assertEqual(self.bundled_tags(mirror.sync(path, full=True)), [['latest']])
            self.assertEqual(low[version], pinned)
        # A pin copied by a sync that failed before sent.json was updated is protected too.
        shutil.rmtree(self.root / 'work')
        shutil.rmtree(self.root / 'out')
        low.clear()
        with mock.patch.object(mirror, 'run', side_effect=fake_run), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch.object(mirror, 'verify_image'):
            path.write_text(entries['latest'] + entries['pin'])
            with mock.patch.object(mirror, 'write_tar', side_effect=mirror.MirrorError('disk full')):
                self.assertEqual(mirror.main(['--catalog', str(path), 'sync']), 1)
            self.assertEqual(low[version], pinned)
            self.assertEqual(json.loads((self.root / 'work/sent.json').read_text())['sent'], {})
            path.write_text(entries['latest'])
            self.assertEqual(self.bundled_tags(mirror.sync(path)), [['latest']])
            self.assertEqual(low[version], pinned)

    def test_ledger_from_before_version_tags_gains_the_version_tag(self):
        path, low, fake_copy, _ = self.latest_entry()
        image = mirror.catalog(path)[0]
        work = self.root / 'work'
        work.mkdir()
        # sent.json as written by the release without version tags: digest only, a transfer cache.
        mirror.write_json(work / 'sent.json', {
            'stream': 'a'*32, 'sequence': 1, 'registry': 'low.example.internal', 'pending': False,
            'sent': {'mirror/alpine:latest': image['digest']}, 'platforms': {image['digest']: image['digest']}})
        low['docker://low.example.internal/mirror/alpine:latest'] = image['digest']
        with mock.patch.object(mirror, 'run', return_value=json.dumps({'Env': ['APP_VERSION=8.10.2']}).encode()), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=lambda reference, side: image['digest']):
            bundle = mirror.sync(path)
        self.assertEqual(self.bundled_tags(bundle), [['latest', '8.10.2']])
        self.assertIn('docker://low.example.internal/mirror/alpine:8.10.2', low)

    def test_shared_digest_platform_change_and_wiped_target_registry(self):
        image, _ = fixture(self.stage)
        latest = image | {'tag': 'latest', 'source': image['source'].replace(':3.20@', ':latest@')}
        path = self.root / 'images.txt'
        path.write_text(f"{image['source']} {image['target']}\n{latest['source']} {latest['target']}\n")
        low = {}

        def fake_copy(source, destination, *_args):
            if destination.startswith('dir:'):
                shutil.copytree(self.stage / 'images' / image['digest'].replace(':', '-'), Path(destination[4:]))
            else:
                low[destination] = image['digest']

        def fake_digest(reference, _side):
            if reference.startswith('docker://low.example.internal/'):
                if reference not in low:
                    raise mirror.MirrorError('manifest unknown')
                return low[reference]
            return image['digest']

        # latest reports 3.20, which images.txt already pins: no clash, and each entry keeps its own tags.
        with mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch.object(mirror, 'app_version', return_value='3.20'):
            with tarfile.open(mirror.sync(path)) as archive:
                entries = json.load(archive.extractfile('images.json'))['images']
            self.assertEqual(sorted(e['tags'] for e in entries), [['3.20'], ['latest']])
            low.clear()  # target registry wiped: the next run repairs every tag without sending
            self.assertIsNone(mirror.sync(path))
            self.assertEqual(len(low), 2)
            with mock.patch.dict(os.environ, {'MIRROR_PLATFORM': 'linux/amd64'}), \
                    mock.patch.object(mirror, 'platform_digest', return_value=image['digest']):
                self.assertIsNotNone(mirror.sync(path))

    def test_concurrent_work_is_refused(self):
        with mirror.locked(self.root / 'work'), \
                self.assertRaisesRegex(mirror.MirrorError, 'another mirror process'), \
                mirror.locked(self.root / 'work'):
            self.fail('lock was not exclusive')

    def test_sync_repairs_low_even_when_nothing_is_due_and_failed_delivery_recovers(self):
        image, _ = fixture(self.stage)
        path = self.root / 'images.txt'
        path.write_text(image['source'] + ' ' + image['target'] + '\n')

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
            # Written but never sent: the CI job's outbox is gone with it, and the ledger is still pending.
            with mock.patch.object(mirror, 'write_bundle', side_effect=self.write_only):
                mirror.sync(path, full=True)
            with self.assertRaisesRegex(mirror.MirrorError, 'not sent yet'):
                mirror.sync(path)
            shutil.rmtree(self.root / 'out')
            recovery = mirror.sync(path)
            with tarfile.open(recovery) as archive:
                metadata = json.load(archive.extractfile('images.json'))
            self.assertTrue(metadata['full'])
            self.assertEqual(metadata['sequence'], 3)



class LedgerTest(unittest.TestCase):
    """The GitLab-package ledger, resend and registry import, against an in-memory package store."""

    setUp = MirrorTest.setUp
    latest_entry = MirrorTest.latest_entry
    bundled_tags = MirrorTest.bundled_tags

    def store(self):
        files = {}

        def get(package, version, name, destination=None):
            data = files.get((package, version, name))
            if data is None or destination is None:
                return data
            Path(destination).write_bytes(data)
            return destination

        def put(package, version, name, data):
            files[(package, version, name)] = data

        def exists(package, version, name):
            return (package, version, name) in files

        os.environ['MIRROR_LEDGER'] = 'true'
        for name, fake in (('package_get', get), ('package_put', put), ('store_exists', exists)):
            patcher = mock.patch.object(mirror, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        return files

    def manifest(self, bundle):
        with tarfile.open(bundle) as archive:
            return json.load(archive.extractfile('images.json'))

    def test_a_lost_runner_resumes_from_the_ledger_and_resends_by_sequence_or_image(self):
        files = self.store()
        path, _, fake_copy, fake_digest = self.latest_entry()
        with mock.patch.object(mirror, 'run', return_value=b'{"schemaVersion": 2}'), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest):
            first = mirror.sync(path)
            record = json.loads(files[('registry-mirror-ledger', '000000000001', 'images.json')])
            self.assertEqual((record['kind'], record['bundle'], record['sequence']), ('full', first.name, 1))
            self.assertRegex(record['created'], r'^\d{4}-\d\d-\d\dT')
            shutil.rmtree(self.root / 'work')  # the runner and its sent.json are gone
            self.assertIsNone(mirror.sync(path))
            by_image = self.manifest(mirror.resend(image='mirror/alpine'))
            self.assertEqual((by_image['sequence'], by_image['full'], 'from' in by_image), (2, False, False))
            by_range = self.manifest(mirror.resend(sequences='1..'))
            self.assertEqual((by_range['sequence'], by_range['from']), (3, 1))
            self.assertIsNone(mirror.resend(since='2999-01-01'))
        self.assertEqual(json.loads(files[('registry-mirror-ledger', 'head', 'state.json')])['sequence'], 3)
        for bad in ({}, {'since': 'yesterday'}, {'sequences': '3..1'}):
            with self.subTest(bad), self.assertRaises(mirror.MirrorError):
                mirror.resend(**bad)

    def test_a_resend_carries_the_newest_digest_of_each_tag(self):
        files = self.store()
        old, new = 'sha256:' + 'a'*64, 'sha256:' + 'b'*64

        def entry(digest):
            image = mirror.parse_image(f'docker.io/bitnami/redis:latest@{digest}', 'mirror/redis')
            return image | {'transfer': digest, 'tags': ['latest']}

        for sequence, digest, created in ((1, old, '2026-09-01T00:00:00Z'), (2, new, '2026-10-01T00:00:00Z')):
            files[('registry-mirror-ledger', f'{sequence:012d}', 'images.json')] = json.dumps(
                {'sequence': sequence, 'created': created, 'images': [entry(digest)]}).encode()
        files[('registry-mirror-ledger', 'head', 'state.json')] = json.dumps(
            {'stream': 'a'*32, 'sequence': 2, 'sent': {}, 'registry': 'low.example.internal'}).encode()
        with mock.patch.object(mirror, 'write_bundle') as send:
            mirror.resend(since='2026-08-01')
        self.assertEqual([i['transfer'] for i in send.call_args.args[5]], [new])
        self.assertEqual(send.call_args.args[7:], ('resend', 1))

    def test_import_accepts_a_resend_only_over_the_gap_it_covers(self):
        image, _ = fixture(self.stage)
        bundle = self.root / 'mirror-1.tar'
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']):
            pack(self.stage, bundle, image=image)
            mirror.import_one(bundle)
            for start, sequence in ((3, 4), (99, 4)):
                pack(self.stage, bundle, image=image, sequence=sequence, full=False)
                metadata = json.loads((self.stage / 'images.json').read_text()) | {'from': start}
                (self.stage / 'images.json').write_text(json.dumps(metadata))
                pack_metadata(self.stage, bundle)
                with self.subTest(start=start), self.assertRaises(mirror.MirrorError):
                    mirror.import_one(bundle)
            metadata['from'] = 2
            (self.stage / 'images.json').write_text(json.dumps(metadata))
            pack_metadata(self.stage, bundle)
            mirror.import_one(bundle)
        self.assertEqual(json.loads((self.root / 'import/received.json').read_text())['sequence'], 4)

    def test_registry_import_catches_up_on_missed_triggers_and_waits_for_half_a_bundle(self):
        files = self.store()
        image, _ = fixture(self.stage)
        stream = 'c' * 32
        for sequence in (1, 2, 3):
            stem = f'mirror-{stream}-{sequence:012d}'
            pack(self.stage, self.root / f'{stem}.tar', sequence=sequence, full=sequence == 1, image=image, stream=stream)
            for name in (f'{stem}.tar', f'{stem}.tar.sha256'):
                if (sequence, name[-6:]) != (3, 'sha256'):  # bundle 3's checksum has not arrived
                    files[('registry-mirror-bundles', stem, name)] = (self.root / name).read_bytes()
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']), \
                mock.patch('builtins.print') as said:
            # The first trigger was lost: the second still imports bundle 1 before bundle 2.
            mirror.import_registry(f'mirror-{stream}-000000000002.tar')
            mirror.import_registry(f'mirror-{stream}-000000000003.tar')
        self.assertEqual(json.loads((self.root / 'import/received.json').read_text())['sequence'], 2)
        self.assertIn(mock.call(f'waiting for mirror-{stream}-000000000003.tar.sha256'), said.call_args_list)
        with self.assertRaisesRegex(mirror.MirrorError, 'not a bundle name'):
            mirror.import_registry('../../etc/passwd')

    def test_the_registry_receipt_survives_a_fresh_runner(self):
        files = self.store()
        image, _ = fixture(self.stage)
        stream = 'd' * 32
        for sequence in (1, 2):
            stem = f'mirror-{stream}-{sequence:012d}'
            pack(self.stage, self.root / f'{stem}.tar', sequence=sequence, full=sequence == 1, image=image, stream=stream)
            for name in (f'{stem}.tar', f'{stem}.tar.sha256'):
                files[('registry-mirror-bundles', stem, name)] = (self.root / name).read_bytes()
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']), \
                mock.patch('builtins.print') as said:
            mirror.import_registry(f'mirror-{stream}-000000000001.tar')
            shutil.rmtree(self.root / 'import')  # the next job runs on a clean runner
            mirror.import_registry(f'mirror-{stream}-000000000001.tar')
        # Without the registry's receipt the second run would push both bundles again.
        self.assertEqual(said.call_args_list[-2:], [mock.call(f'superseded: mirror-{stream}-000000000001.tar'),
                                                    mock.call(f'not checking the store for mirror-{stream}-000000000001: '
                                                              'IMPORT_DELETE_BUNDLES is not true')])
        self.assertEqual(json.loads(files[('registry-mirror-receipt', 'head', 'received.json')])['sequence'], 2)

    def test_s3_store_signs_a_path_style_request_and_reads_a_missing_key_as_none(self):
        os.environ.update({'IMPORT_STORE': 's3', 'S3_ENDPOINT': 'https://s3.example.internal:8443',
                           'S3_BUCKET': 'mirror-high', 'AWS_ACCESS_KEY_ID': 'AKIDEXAMPLE',
                           'AWS_SECRET_ACCESS_KEY': 'secret'})
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"sequence": 4}'
        with mock.patch.object(mirror.urllib.request, 'urlopen', return_value=response) as opened:
            self.assertEqual(mirror.store_get('registry-mirror-receipt', 'head', 'received.json'), b'{"sequence": 4}')
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'https://s3.example.internal:8443/mirror-high/registry-mirror-receipt/head/received.json')
        os.environ['S3_PREFIX'] = 'mirror'  # no trailing slash: one is added
        with mock.patch.object(mirror.urllib.request, 'urlopen', return_value=response) as opened:
            mirror.store_get('registry-mirror-receipt', 'head', 'received.json')
        self.assertEqual(opened.call_args.args[0].full_url,
                         'https://s3.example.internal:8443/mirror-high/mirror/registry-mirror-receipt/head/received.json')
        del os.environ['S3_PREFIX']
        self.assertRegex(request.get_header('Authorization'),
                         r'^AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/[0-9]{8}/us-east-1/s3/aws4_request, '
                         r'SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}$')
        missing = mirror.urllib.error.HTTPError(request.full_url, 404, 'Not Found', {}, None)
        with mock.patch.object(mirror.urllib.request, 'urlopen', side_effect=missing):
            self.assertIsNone(mirror.store_get('registry-mirror-bundles', 'x', 'x.tar'))

    def test_delete_bundles_removes_each_import_and_a_late_trigger_is_already_imported(self):
        files = self.store()
        image, _ = fixture(self.stage)
        stream, stem = 'e' * 32, f"mirror-{'e' * 32}-000000000001"
        pack(self.stage, self.root / f'{stem}.tar', sequence=1, image=image, stream=stream)
        for name in (f'{stem}.tar', f'{stem}.tar.sha256'):
            files[('registry-mirror-bundles', stem, name)] = (self.root / name).read_bytes()
        os.environ['IMPORT_DELETE_BUNDLES'] = 'true'
        with self.assertRaisesRegex(mirror.MirrorError, 'needs PACKAGE_TOKEN'):  # a job token cannot delete
            mirror.import_registry(f'{stem}.tar')
        os.environ['PACKAGE_TOKEN'] = 'maintainer-token'
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']), \
                mock.patch.object(mirror, 'store_delete', side_effect=[1, 0]) as deleted, \
                mock.patch('builtins.print') as said:
            mirror.import_registry(f'{stem}.tar')
            mirror.import_registry(f'{stem}.tar.sha256')  # the second file's trigger, after the delete
        self.assertEqual(deleted.call_count, 2)  # the import, then the late trigger's check for a leftover
        deleted.assert_called_with('registry-mirror-bundles', stem, [f'{stem}.tar', f'{stem}.tar.sha256'])
        printed = [c.args[0] for c in said.call_args_list]
        self.assertIn(f'deleted from the store: {stem}', printed)
        self.assertEqual(printed[-2:], [f'already imported: {stem}.tar', f'nothing to delete in the store: {stem}'])

    def test_cleanup_says_why_a_bundle_stays_and_removes_a_leftover_once_enabled(self):
        files = self.store()
        image, _ = fixture(self.stage)
        stream, stem = 'f' * 32, f"mirror-{'f' * 32}-000000000001"
        pack(self.stage, self.root / f'{stem}.tar', sequence=1, image=image, stream=stream)
        for name in (f'{stem}.tar', f'{stem}.tar.sha256'):
            files[('registry-mirror-bundles', stem, name)] = (self.root / name).read_bytes()
        os.environ['CI_ENVIRONMENT_NAME'] = 'dev'
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']), \
                mock.patch.object(mirror, 'store_delete') as deleted, mock.patch('builtins.print') as said:
            mirror.import_registry(f'{stem}.tar')  # IMPORT_DELETE_BUNDLES never reached the job
        printed = [c.args[0] for c in said.call_args_list]
        deleted.assert_not_called()
        self.assertIn('setting: IMPORT_DELETE_BUNDLES is not set in this job (environment dev): '
                      'imported bundles stay in the store', printed)
        self.assertIn(f'kept in the store: {stem} (IMPORT_DELETE_BUNDLES is not true)', printed)
        os.environ.update({'IMPORT_DELETE_BUNDLES': 'true', 'PACKAGE_TOKEN': 'maintainer-token'})
        with mock.patch.object(mirror, 'store_delete', return_value=1) as deleted, mock.patch('builtins.print') as said:
            mirror.import_registry(f'{stem}.tar')  # a re-trigger once the setting is fixed
        deleted.assert_called_once()
        self.assertIn(mock.call(f'deleted from the store: {stem}'), said.call_args_list)
        self.assertIn(mock.call('setting: PACKAGE_TOKEN=(set)'), said.call_args_list)
        os.environ['NIFI_URL'] = 'https://user:secret@nifi.example.internal:9443/contentListener'
        with mock.patch('builtins.print') as said:
            mirror.settings(('NIFI_URL', 'not posted', False))
        said.assert_called_once_with('setting: NIFI_URL=https://nifi.example.internal:9443/contentListener')
        os.environ['IMPORT_DELETE_BUNDLES'] = 'yes'
        with self.assertRaisesRegex(mirror.MirrorError, "IMPORT_DELETE_BUNDLES must be true or false, not 'yes'"):
            mirror.import_registry(f'{stem}.tar')

    def test_send_infers_the_method_from_its_flags(self):
        self.assertEqual(mirror.send_arguments(['outbox'], '/mnt/transfer', None), ('dir', 'outbox'))
        self.assertEqual(mirror.send_arguments(['dir', 'outbox'], '/mnt/transfer', None), ('dir', 'outbox'))
        self.assertEqual(mirror.send_arguments([], None, 'transfer'), ('s3', None))
        self.assertEqual(mirror.send_arguments(['outbox'], None, None), ('nifi', 'outbox'))
        with self.assertRaises(mirror.MirrorError):
            mirror.send_arguments(['outbox', 'extra'], '/mnt/transfer', None)

    def test_send_takes_the_outbox_before_or_after_its_options(self):
        with mock.patch.object(mirror, 'send', return_value=0) as send:
            for argv in (['send', 'dir', '--path', '/m', 'ob'], ['send', '--path', '/m', 'ob'], ['send', 'dir', 'ob', '--path', '/m']):
                self.assertEqual(mirror.main(argv), 0)
                send.assert_called_with('dir', 'ob', '/m', 'ffv3')
        with self.assertRaises(SystemExit):
            mirror.main(['targets', 'extra'])

    def test_share_blobs_hardlinks_a_repeated_blob_and_keeps_it_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            layout, blob = Path(directory), 'a' * 64
            for image in ('000001', '000002'):
                (layout / image).mkdir()
                (layout / image / blob).write_bytes(b'layer')
            mirror.share_blobs(layout, {})
            self.assertTrue((layout / '000001' / blob).samefile(layout / '000002' / blob))
            self.assertEqual((layout / '000002' / blob).read_bytes(), b'layer')
            with mock.patch.object(mirror.os, 'link', side_effect=OSError('no hardlinks')):
                (layout / '000003').mkdir()
                (layout / '000003' / blob).write_bytes(b'layer')
                mirror.share_blobs(layout, {})
            self.assertEqual((layout / '000003' / blob).read_bytes(), b'layer')

    def test_carry_records_nothing_when_the_export_fails(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {'MIRROR_STATE_DIR': f'{directory}/work', 'MIRROR_LEDGER': 'false'}):
            catalog = Path(directory) / 'images.txt'
            catalog.write_text('docker.io/library/alpine:3.20@sha256:' + 'b' * 64 + ' mirror/alpine\n')
            target = Path(directory) / 'transfer'
            with mock.patch.object(mirror, 'sync'), \
                    mock.patch.object(mirror, 'export', side_effect=mirror.MirrorError('registry down')):
                with self.assertRaises(mirror.MirrorError):
                    mirror.carry(catalog, target)
            self.assertEqual(sorted(p.name for p in target.iterdir()), ['.staging'])
            self.assertEqual(list((target / '.staging').iterdir()), [])
            with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
                mirror.pending(catalog, carried=True, targets=True)
            self.assertEqual(out.getvalue(), 'mirror/alpine:3.20\n')  # still due next run

    def test_load_will_not_move_a_tag_without_force(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'TARGET_REGISTRY': 'registry.example.com'}):
            image = Path(directory) / '000001'
            image.mkdir()
            (image / 'manifest.json').write_bytes(b'{}')
            digest = 'sha256:' + hashlib.sha256(b'{}').hexdigest()
            Path(directory, mirror.EXPORT_INDEX).write_text(json.dumps(
                {'images': [{'dir': '000001', 'target': 'team/app', 'tag': 'v1', 'digest': digest}]}))
            with mock.patch.object(mirror, 'raw_digest', return_value='sha256:' + 'a' * 64), \
                    mock.patch.object(mirror, 'copy') as copy:
                with self.assertRaisesRegex(mirror.MirrorError, 'does not move without load --force'):
                    mirror.load(Path(directory))
                copy.assert_not_called()
            with mock.patch.object(mirror, 'raw_digest', side_effect=mirror.MirrorError('skopeo inspect failed: unauthorized')), \
                    mock.patch.object(mirror, 'copy') as copy:
                with self.assertRaisesRegex(mirror.MirrorError, 'unauthorized'):  # not read as an absent tag
                    mirror.load(Path(directory))
                copy.assert_not_called()

    def test_load_refuses_a_tar_whose_checksum_does_not_match(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'TARGET_REGISTRY': 'registry.example.com'}):
            bundle = Path(directory) / 'export.tar'
            bundle.write_bytes(b'not the exported bytes')
            with self.assertRaisesRegex(mirror.MirrorError, 'sha256 is missing'):
                mirror.load(bundle)
            bundle.with_name('export.tar.sha256').write_text('0' * 64 + '  export.tar\n')
            with self.assertRaisesRegex(mirror.MirrorError, 'does not match'):
                mirror.load(bundle)

    def test_export_refuses_a_directory_that_is_not_empty(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'TARGET_REGISTRY': 'registry.example.com'}):
            Path(directory, mirror.EXPORT_INDEX).write_text('{}')
            with self.assertRaisesRegex(mirror.MirrorError, 'is not empty'):
                mirror.export(Path(os.devnull), ['team/app:v1'], directory)

    def test_send_delivers_each_file_once_and_clears_the_pending_ledger(self):
        files = self.store()
        path, _, fake_copy, fake_digest = self.latest_entry()
        with mock.patch.object(mirror, 'run', return_value=b'{"schemaVersion": 2}'), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch.object(mirror, 'write_bundle', side_effect=self.write_only):
            bundle = mirror.sync(path)
        self.assertTrue(json.loads(files[('registry-mirror-ledger', 'head', 'state.json')])['pending'])
        posted = []
        with mock.patch.object(mirror, 'post_file', side_effect=[mirror.MirrorError('NiFi down'), None, None]) as post:
            with self.assertRaisesRegex(mirror.MirrorError, 'NiFi down'):
                mirror.send('nifi', target='http://nifi.example.internal:9098/contentListener')
            self.assertTrue(bundle.exists())  # still there to send again
            with mock.patch('builtins.print'):
                self.assertEqual(mirror.send('nifi', target='http://nifi.example.internal:9098/contentListener'), 1)
            posted = [c.args[0].name for c in post.call_args_list]
        self.assertEqual(posted, [bundle.name, bundle.name])
        self.assertEqual(list((self.root / 'out').iterdir()), [])
        self.assertFalse(json.loads(files[('registry-mirror-ledger', 'head', 'state.json')])['pending'])
        with mock.patch('builtins.print') as said:
            self.assertEqual(mirror.send('nifi', target='http://x'), 0)
        said.assert_called_once_with(f"nothing to send in {self.root / 'out'}")

    def test_a_lost_bundle_keeps_the_ledger_pending_past_an_unrelated_resend(self):
        files = self.store()
        path, _, fake_copy, fake_digest = self.latest_entry()
        head = lambda: json.loads(files[('registry-mirror-ledger', 'head', 'state.json')])  # noqa: E731
        with mock.patch.object(mirror, 'run', return_value=b'{"schemaVersion": 2}'), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch.object(mirror, 'write_bundle', side_effect=self.write_only), \
                mock.patch.object(mirror, 'post_file'), mock.patch('builtins.print'):
            mirror.sync(path)
            mirror.send('nifi', target='http://nifi')
            self.assertEqual((head()['pending'], head()['unsent']), (False, []))
            lost = mirror.sync(path, full=True)  # written, then the CI job's outbox is lost
            shutil.rmtree(self.root / 'out')
            mirror.resend(image='mirror/alpine')  # no bridge: it does not stand in for the lost one
            mirror.send('nifi', target='http://nifi')
            self.assertEqual((head()['pending'], head()['unsent']), (True, [lost.name]))
            recovery = mirror.sync(path)  # full, so it bridges the lost bundle
            self.assertTrue(self.manifest(recovery)['full'])
            mirror.send('nifi', target='http://nifi')
            self.assertEqual((head()['pending'], head()['unsent']), (False, []))

    def test_send_resumes_after_the_last_delivered_part_and_refuses_a_missing_one(self):
        name = outbox_with(self.root / 'out', b'0123456789', 4)
        posted, failures = [], [mirror.MirrorError('connection reset')]

        def post(path, *_args):
            if len(posted) == 1 and failures:
                raise failures.pop()
            posted.append(path.name)
        with mock.patch.object(mirror, 'post_file', side_effect=post), mock.patch('builtins.print'):
            with self.assertRaisesRegex(mirror.MirrorError, 'connection reset'):
                mirror.send('nifi', target='http://nifi')
            self.assertEqual(json.loads((self.root / 'out' / f'{name}.send.json').read_text())['delivered'],
                             [f'{name}.part-001'])
            (self.root / 'out' / f'{name}.part-003').unlink()
            with self.assertRaisesRegex(mirror.MirrorError, 'part-003 is missing'):
                mirror.send('nifi', target='http://nifi')
        self.assertEqual(posted, [f'{name}.part-001', f'{name}.part-002'])

    def test_unsent_tracking_through_bridges_crashes_and_old_ledgers(self):
        name = lambda n: f"mirror-{'a' * 32}-{n:012d}.tar"  # noqa: E731
        state = {'sequence': 5, 'unsent': [name(3), name(5)]}
        state['sequence'] = 8
        mirror.track_unsent(state, name(8), False, 4)  # a resend from 4 on covers 5, not 3
        self.assertEqual(state['unsent'], [name(3), name(8)])
        mirror.track_unsent(state, name(9), True, None)
        self.assertEqual(state['unsent'], [name(9)])
        old = {'sequence': 4, 'pending': True, 'unsent': ['an unsent bundle recorded by an older release']}
        mirror.track_unsent(old, name(5), False, 2)
        self.assertEqual(len(old['unsent']), 2)  # only a full bundle clears the old marker

    def test_a_sequence_reserved_and_never_written_waits_for_a_full_bundle(self):
        self.store()
        (self.root / 'work').mkdir()
        state = {'stream': 'a' * 32, 'sequence': 6, 'written': 5, 'unsent': [], 'pending': True, 'sent': {},
                 'registry': 'low.example.internal'}
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'verify_image'), \
                mock.patch('builtins.print'):
            self.write_only(self.root / 'work', mirror.outbox(self.root / 'work'), self.root / 'work/sent.json',
                            state, 'low', [], False, 'resend', None)
        self.assertEqual(state['unsent'][0], 'sequence 6, reserved and never written')
        self.assertEqual(len(state['unsent']), 2)

    def test_a_reservation_without_a_bundle_keeps_the_ledger_pending(self):
        files = self.store()
        files[('registry-mirror-ledger', 'head', 'state.json')] = json.dumps(
            {'stream': 'a' * 32, 'sequence': 6, 'written': 5, 'unsent': [], 'pending': True, 'sent': {},
             'registry': 'low.example.internal'}).encode()
        outbox_with(self.root / 'out', b'x', 1024, name=f"mirror-{'a' * 32}-000000000005.tar")
        with mock.patch.object(mirror, 'post_file'), mock.patch('builtins.print'):
            mirror.send('nifi', target='http://nifi')
        self.assertTrue(json.loads(files[('registry-mirror-ledger', 'head', 'state.json')])['pending'])

    def test_low_only_mirrors_low_and_records_nothing_sent(self):
        files = self.store()
        path, _, fake_copy, fake_digest = self.latest_entry()
        with mock.patch.object(mirror, 'run', return_value=b'{"schemaVersion": 2}'), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch('builtins.print') as said:
            self.assertIsNone(mirror.sync(path, low_only=True))
            self.assertIn('not bundled (--low-only)', said.call_args.args[0])
            state = json.loads(files[('registry-mirror-ledger', 'head', 'state.json')])
            self.assertEqual((state['sequence'], state['sent']), (0, {}))
            self.assertIsNotNone(mirror.sync(path))  # the next sync bundles what that one did not

    def test_send_refuses_what_it_cannot_do(self):
        with self.assertRaisesRegex(mirror.MirrorError, 'needs --url or NIFI_URL'):
            mirror.send('nifi')
        with self.assertRaisesRegex(mirror.MirrorError, '--format tar is for send dir'):
            mirror.send('s3', form='tar')
        out = self.root / 'out'
        out.mkdir()
        name = 'mirror-' + 'a' * 32 + '-000000000001.tar'
        (out / f'{name}.part-001').write_bytes(b'x')
        (out / f'{name}.sha256').write_text('x')
        (out / f'{name}.send.json').write_text('{"kind": "delta", "sha256": "x", "parts": 1}')
        with self.assertRaisesRegex(mirror.MirrorError, 'hand-carry needs whole bundles'):
            mirror.send('dir', target=str(self.root / 'usb'), form='tar')

    def test_hand_carry_copies_the_bundle_then_its_checksum(self):
        out, usb = self.root / 'out', self.root / 'usb'
        out.mkdir()
        name = 'mirror-' + 'b' * 32 + '-000000000001.tar'
        (out / name).write_bytes(b'tar')
        (out / f'{name}.sha256').write_text(f'{hashlib.sha256(b"tar").hexdigest()}  {name}\n')
        (out / f'{name}.send.json').write_text('{"kind": "delta", "sha256": "x", "parts": 0}')
        with mock.patch('builtins.print'):
            mirror.send('dir', target=str(usb), form='tar')
        self.assertEqual(sorted(p.name for p in usb.iterdir()), [name, f'{name}.sha256'])
        self.assertEqual(list(out.iterdir()), [])

    def test_old_variable_names_fail_with_their_new_names(self):
        os.environ.update({'LOW_QUAY_HOST': 'low.example.internal', 'EXPORT_SEQUENCE': '3..'})
        with mock.patch('sys.stderr') as err:
            self.assertEqual(mirror.main(['targets']), 1)
        said = ''.join(c.args[0] for c in err.write.call_args_list)
        self.assertIn('EXPORT_SEQUENCE is now RESEND_SEQUENCE', said)
        self.assertIn('LOW_QUAY_HOST is now TARGET_REGISTRY', said)

    def test_login_skips_a_registry_without_credentials_and_keeps_the_password_off_argv(self):
        os.environ.update({'TARGET_REGISTRY_USERNAME': 'robot', 'TARGET_REGISTRY_PASSWORD': 'pw',
                           'TARGET_REGISTRY_TLS_VERIFY': 'false'})
        with mock.patch.object(mirror.subprocess, 'run', return_value=mock.Mock(returncode=0)) as ran, \
                mock.patch('builtins.print'):
            mirror.login()
        ran.assert_called_once()  # no SOURCE_REGISTRY_USERNAME: no docker.io login
        args, kwargs = ran.call_args
        self.assertEqual(args[0], ['skopeo', 'login', '--tls-verify=false', '--username', 'robot',
                                   '--password-stdin', 'low.example.internal'])
        self.assertEqual(kwargs['input'], b'pw')

    def test_a_source_in_the_target_registry_reads_with_the_target_settings(self):
        self.assertEqual(mirror.side_of('low.example.internal/team/runner:1.2@sha256:' + 'a'*64), 'TARGET_REGISTRY')
        self.assertEqual(mirror.side_of('docker://low.example.internal/team/runner@sha256:' + 'a'*64), 'TARGET_REGISTRY')
        self.assertEqual(mirror.side_of('docker.io/library/alpine:3.20'), None)
        os.environ['TARGET_REGISTRY_TLS_VERIFY'] = 'false'
        self.assertEqual(mirror.options(mirror.side_of('low.example.internal/team/runner:1.2')), ['--tls-verify=false'])
        self.assertEqual(mirror.options(mirror.side_of('docker.io/library/alpine:3.20')), [])
        os.environ.update({'SOURCE_REGISTRY': 'mirror.example.internal:5000', 'SOURCE_REGISTRY_TLS_VERIFY': 'false'})
        self.assertEqual(mirror.options(mirror.side_of('mirror.example.internal:5000/a/b:1')), ['--tls-verify=false'])
        self.assertIsNone(mirror.side_of('quay.io/org/image:1'))
        self.assertEqual(mirror.options(mirror.side_of('quay.io/org/image:1')), [])  # TLS stays on for other hosts

    def test_login_without_a_username_logs_in_nowhere(self):
        with mock.patch.object(mirror.subprocess, 'run') as ran:
            mirror.login()  # a high registry without authentication
        ran.assert_not_called()

    def test_post_file_streams_only_the_bundle_with_its_checksum_header(self):
        bundle = self.root / 'mirror-x.tar'
        bundle.write_bytes(b'tar')
        url = 'http://user:secret@nifi.example.internal:9099/contentListener'
        with mock.patch.object(mirror, 'run', return_value=b'200') as call, \
                mock.patch('builtins.print') as said:
            mirror.post_file(bundle, mirror.attributes_of(bundle, 'resend'), url)
        (args,) = [c.args for c in call.call_args_list]  # one POST: no .sha256 crosses the diode
        self.assertIn('--http1.1', args)
        self.assertEqual(args[args.index('--upload-file') + 1], str(bundle))  # streamed, not read into memory
        self.assertNotIn('--data-binary', args)
        (logged,) = [c.args[0] for c in said.call_args_list]
        self.assertTrue(logged.startswith(
            'posted to NiFi: mirror-x.tar -> http://nifi.example.internal:9099/contentListener (HTTP 200'))
        self.assertNotIn('secret', logged)
        headers = [args[i + 1] for i, a in enumerate(args) if a == '--header']
        for header in ('X-Artifact-Type: container-images', 'X-Artifact-Action: mirror', 'X-Bundle-Kind: resend',
                       'X-Artifact-Format: tar', f'X-Sha256: {mirror.sha256(bundle)}', f'Filename: {bundle.name}',
                       'Expect:'):
            self.assertIn(header, headers)

    def test_pending_lists_what_the_ledger_has_not_sent(self):
        files = self.store()
        sent, fresh = 'sha256:' + 'a'*64, 'sha256:' + 'b'*64
        path = self.root / 'images.txt'
        path.write_text(f'docker.io/library/alpine:3.20@{sent} mirror/alpine\n'
                        f'docker.io/library/busybox:1.37@{fresh} mirror/busybox\n')
        files[('registry-mirror-ledger', 'head', 'state.json')] = json.dumps(
            {'sent': {'mirror/alpine:3.20': sent, 'mirror/busybox:1.37': 'sha256:' + 'c'*64}}).encode()
        with mock.patch('builtins.print') as said:
            mirror.pending(path)
        self.assertEqual([c.args[0] for c in said.call_args_list], [f'docker.io/library/busybox:1.37@{fresh}'])
        os.environ['MIRROR_PLATFORM'] = 'linux/amd64'  # a platform change resends, so both are pending
        with mock.patch('builtins.print') as said:
            mirror.pending(path)
        self.assertEqual(len(said.call_args_list), 2)

    def test_helm_charts_are_catalog_entries_without_a_platform(self):
        chart = {'schemaVersion': 2, 'config': {'mediaType': mirror.CHART_CONFIG},
                 'layers': [{'mediaType': mirror.CHART_LAYERS[0]}]}
        self.assertTrue(mirror.runnable(chart))
        self.assertFalse(mirror.runnable(chart | {'layers': [{'mediaType': 'text/plain'}]}))
        self.assertFalse(mirror.runnable(chart | {'layers': [{'mediaType': mirror.CHART_LAYERS[1]}]}))
        os.environ['MIRROR_PLATFORM'] = 'linux/amd64'
        with mock.patch.object(mirror, 'run', return_value=json.dumps(chart).encode()) as call:
            self.assertEqual(mirror.platform_digest('docker://up/chart@sha256:' + 'd'*64, 'sha256:' + 'd'*64, 'SOURCE_REGISTRY'),
                             'sha256:' + 'd'*64)
        self.assertEqual(call.call_count, 1)

    def test_covers_finds_images_the_mirror_lacks(self):
        digest = 'sha256:' + 'e'*64
        path = self.root / 'images.txt'
        path.write_text(f'docker.io/library/alpine:3.20@{digest} team-dev/library/alpine\n')
        containerfile = self.root / 'Containerfile'
        containerfile.write_text('ARG BASE=x\nFROM alpine:3.20 AS build\nFROM build\nFROM scratch\nFROM nginx AS nginx\n'
                                 'FROM --platform=linux/amd64 quay.example.internal/team-dev/library/alpine:3.20\n'
                                 'FROM ${BASE}\nFROM docker.io/library/busybox@' + digest + '\n')
        compose = self.root / 'compose.yaml'
        compose.write_text('services:\n  app:\n    image: "nginx:1.29"\n')
        with mock.patch('builtins.print') as said:
            with self.assertRaisesRegex(mirror.MirrorError, '1 image'):
                mirror.covers(path, [str(containerfile)])  # a stage named like its image is still checked
            with self.assertRaisesRegex(mirror.MirrorError, '2 image'):
                mirror.covers(path, [str(containerfile), str(compose)])
        self.assertIn(mock.call(f'missing: nginx:1.29 ({compose}:3)'), said.call_args_list)


def unpack_flowfile(data):
    """NiFi's FlowFileUnpackagerV3, for one packaged file."""
    assert data[:7] == b'NiFiFF3'
    position = 7

    def field():
        nonlocal position
        number = int.from_bytes(data[position:position + 2], 'big')
        position += 2
        if number == 0xFFFF:
            number = int.from_bytes(data[position:position + 4], 'big')
            position += 4
        return number

    def text():
        nonlocal position
        length = field()
        position += length
        return data[position - length:position].decode()
    attributes = {text(): text() for _ in range(field())}
    size = int.from_bytes(data[position:position + 8], 'big')
    content = data[position + 8:]
    assert len(content) == size
    return attributes, content


def outbox_with(out, data, cap, kind='delta', name='mirror-' + 'c' * 32 + '-000000000001.tar'):
    """An outbox holding one bundle as write_bundle leaves it: whole, or parts of at most cap bytes."""
    out.mkdir(exist_ok=True)
    sink = mirror.PartSink(out, name, cap)
    sink.write(data)
    sink.flush_part()
    parts = sink.number if sink.number > 1 else 0
    if not parts:
        os.replace(out / f'{name}.part-001', out / name)
    checksum = hashlib.sha256(data).hexdigest()
    (out / f'{name}.sha256').write_text(f'{checksum}  {name}' + (f'  {parts}' if parts else '') + '\n')
    (out / f'{name}.send.json').write_text(json.dumps({'kind': kind, 'sha256': checksum, 'parts': parts}))
    return name


class SendTest(unittest.TestCase):
    """mirror.py send: NiFi ListenHTTP, a directory or an S3 bucket the low NiFi collects from."""

    setUp = MirrorTest.setUp

    def test_parts_land_in_the_directory_as_flowfiles_with_their_attributes(self):
        name = outbox_with(self.root / 'out', b'0123456789', 4)
        drop = self.root / 'transfer'
        with mock.patch('builtins.print'):
            mirror.send('dir', target=str(drop))
        self.assertEqual(sorted(p.name for p in drop.iterdir()),
                         [f'{name}.part-00{n}.ffv3' for n in (1, 2, 3)])  # no .partial left
        attributes, content = unpack_flowfile((drop / f'{name}.part-002.ffv3').read_bytes())
        self.assertEqual(content, b'4567')
        self.assertEqual(attributes, {
            'filename': f'{name}.part-002', 'X-Sha256': hashlib.sha256(b'4567').hexdigest(),
            'X-Artifact-Type': 'container-images', 'X-Artifact-Format': 'tar', 'X-Artifact-Action': 'mirror',
            'X-Bundle-Kind': 'delta', 'X-Bundle-Name': name, 'X-Bundle-Sha256': hashlib.sha256(b'0123456789').hexdigest(),
            'X-Bundle-Parts': '3'})
        self.assertEqual(list((self.root / 'out').iterdir()), [])

    def test_a_long_value_uses_the_wide_length_field(self):
        attributes, content = unpack_flowfile(mirror.flowfile_v3({'a': 'v' * 70000}, 2) + b'ok')
        self.assertEqual((len(attributes['a']), content), (70000, b'ok'))

    def test_the_s3_drop_streams_the_package_with_its_length_and_metadata(self):
        name = outbox_with(self.root / 'out', b'bundle', 1024, kind='full')
        os.environ.update({'S3_BUCKET': 'transfer'})
        sent = {}

        def put(method, key, data, length, meta):
            sent.update(method=method, key=key, body=data.read(), length=length, meta=meta)
            return io.BytesIO()
        with mock.patch.object(mirror, 's3_request', side_effect=put), mock.patch('builtins.print'):
            mirror.send('s3')
        self.assertEqual((sent['method'], sent['key'], sent['length']), ('PUT', f'{name}.ffv3', len(sent['body'])))
        attributes, content = unpack_flowfile(sent['body'])
        self.assertEqual((attributes['filename'], attributes['X-Bundle-Kind'], content), (name, 'full', b'bundle'))
        self.assertEqual(sent['meta']['X-Sha256'], hashlib.sha256(b'bundle').hexdigest())

    def test_metadata_headers_are_signed(self):
        os.environ.update({'S3_ENDPOINT': 'http://s3.example.internal', 'S3_BUCKET': 'b',
                           'AWS_ACCESS_KEY_ID': 'k', 'AWS_SECRET_ACCESS_KEY': 's'})
        with mock.patch.object(mirror.urllib.request, 'urlopen') as opened:
            mirror.s3_request('PUT', 'x', b'', 0, {'X-Sha256': 'abc'})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header('X-amz-meta-x-sha256'), 'abc')
        self.assertIn('x-amz-meta-x-sha256', request.get_header('Authorization'))

    def test_mirror_drop_names_the_send_command(self):
        os.environ['MIRROR_DROP'] = 's3'
        with mock.patch('sys.stderr') as err:
            self.assertEqual(mirror.main(['targets']), 1)
        self.assertIn('MIRROR_DROP is now mirror.py send', ''.join(c.args[0] for c in err.write.call_args_list))


class BlobDeltaTest(unittest.TestCase):
    """MIRROR_BLOB_DELTA: blobs the high side already holds, or this bundle carries once, are left out."""

    setUp = MirrorTest.setUp

    def two_images_one_layer(self):
        first, layer = fixture(self.stage)
        building = self.stage / 'second'
        config = blob(building, {'architecture': 'arm64', 'os': 'linux'})
        held = self.stage / 'images' / first['transfer'].replace(':', '-')
        shutil.copy(held / layer['digest'][7:], building / layer['digest'][7:])
        root = blob(building, {'schemaVersion': 2, 'config': config, 'layers': [layer]})
        place(self.stage, building, root)
        second = mirror.parse_image('docker.io/library/busybox:1@' + root['digest'], 'mirror/busybox')
        return first, second | {'transfer': root['digest'], 'tags': ['1']}, layer

    def metadata(self, images, **extra):
        return {'schema': 1, 'stream': 'a' * 32, 'sequence': 2, 'full': False, 'images': images, **extra}

    def test_a_layer_twice_in_one_bundle_travels_once(self):
        first, second, layer = self.two_images_one_layer()
        metadata = self.metadata([first, second])
        with mock.patch('builtins.print'):
            mirror.omit_known_blobs(self.stage, [first, second], {}, metadata)
        name = second['transfer'].replace(':', '-')
        self.assertEqual(metadata['omitted'], {name: {layer['digest']: ['bundle:' + first['transfer'].replace(':', '-')]}})
        self.assertFalse((self.stage / 'images' / name / layer['digest'][7:]).exists())
        mirror.validate_manifest(metadata)
        mirror.restore_blobs(self.stage, metadata, 'high.example.internal', self.root)
        for image in (first, second):
            mirror.verify_image(self.stage / 'images' / image['transfer'].replace(':', '-'), image['transfer'])

    def test_a_layer_an_earlier_bundle_carried_is_read_back_from_the_high_registry(self):
        first, second, layer = self.two_images_one_layer()
        earlier = self.root / 'on-high'
        shutil.copytree(self.stage / 'images' / first['transfer'].replace(':', '-'), earlier)
        shutil.rmtree(self.stage / 'images' / first['transfer'].replace(':', '-'))
        state = {}
        mirror.remember_blobs(state, [first], {first['transfer']: {layer['digest']}})
        metadata = self.metadata([second])
        with mock.patch('builtins.print'):
            mirror.omit_known_blobs(self.stage, [second], state, metadata)
        hint = 'mirror/alpine@' + first['transfer']
        self.assertEqual(list(metadata['omitted'].values()), [{layer['digest']: [hint]}])
        mirror.validate_manifest(metadata)
        os.environ['IMPORT_PATH_REWRITE'] = 'mirror/=prod/'
        fetched = []

        def fake_copy(source, destination, *_args):
            fetched.append(source)
            shutil.copytree(earlier, Path(destination[4:]))
        with mock.patch.object(mirror, 'copy', side_effect=fake_copy), mock.patch('builtins.print'):
            mirror.restore_blobs(self.stage, metadata, 'high.example.internal', self.root)
        self.assertEqual(fetched, ['docker://high.example.internal/prod/alpine@' + first['transfer']])
        mirror.verify_image(self.stage / 'images' / second['transfer'].replace(':', '-'), second['transfer'])

    def test_two_tags_on_one_digest_keep_their_shared_blobs(self):
        first, _, layer = self.two_images_one_layer()
        twin = first | {'tag': 'latest', 'tags': ['latest']}
        metadata = self.metadata([first, twin])
        with mock.patch('builtins.print'):
            mirror.omit_known_blobs(self.stage, [first, twin], {}, metadata)
        self.assertNotIn('omitted', metadata)
        self.assertTrue((self.stage / 'images' / first['transfer'].replace(':', '-') / layer['digest'][7:]).exists())

    def test_the_blob_memory_drops_the_oldest_first(self):
        state = {'blobs': {f'sha256:{n:064x}': ['a/b@x'] for n in range(5)}}
        with mock.patch.object(mirror, 'BLOB_MEMORY', 4):
            mirror.remember_blobs(state, [{'target': 'a/c', 'transfer': 't'}], {'t': {f'sha256:{0:064x}'}})
        self.assertEqual(list(state['blobs']), [f'sha256:{n:064x}' for n in (2, 3, 4, 0)])

    def test_an_expired_hint_names_the_resend(self):
        first, second, layer = self.two_images_one_layer()
        metadata = self.metadata([second], schema=2, omitted={
            second['transfer'].replace(':', '-'): {layer['digest']: ['mirror/alpine@' + first['transfer'],
                                                                     'mirror/old@' + first['transfer']]}})
        with mock.patch.object(mirror, 'copy', side_effect=mirror.MirrorError('manifest unknown')) as tried, \
                self.assertRaisesRegex(mirror.MirrorError, 'RESEND_ALL=true'):
            mirror.restore_blobs(self.stage, metadata, 'high.example.internal', self.root)
        self.assertEqual(tried.call_count, 2)  # each named image is tried before giving up

    def test_omitted_metadata_is_checked(self):
        first, second, layer = self.two_images_one_layer()
        good = {second['transfer'].replace(':', '-'): {layer['digest']: ['mirror/alpine@' + first['transfer']]}}
        for bad in ({'sha256-' + 'f' * 64: good[next(iter(good))]},  # not an image in this bundle
                    {next(iter(good)): {layer['digest']: ['../../etc@' + first['transfer']]}},
                    {next(iter(good)): {layer['digest']: ['bundle:sha256-' + 'e' * 64]}},
                    {next(iter(good)): {layer['digest']: []}},
                    {next(iter(good)): {layer['digest']: 'mirror/alpine@' + first['transfer']}}):
            with self.subTest(bad), self.assertRaisesRegex(mirror.MirrorError, 'omitted-blob'):
                mirror.validate_manifest(self.metadata([second], schema=2, omitted=bad))
        with self.assertRaises(mirror.MirrorError):
            mirror.validate_manifest(self.metadata([second], omitted=good))  # schema 1 cannot omit


class LargeAndBulkTest(unittest.TestCase):
    """Size-capped bundles, bulk lists, path rewrites and out-of-order arrival."""

    setUp = MirrorTest.setUp
    store = LedgerTest.store

    def test_the_bundle_cap_parses_units_and_rejects_others(self):
        for text, expected in (('', 4 * 1024 ** 3), ('500MiB', 500 * 1024 ** 2), ('2gib', 2 * 1024 ** 3), ('123', 123)):
            os.environ['MIRROR_BUNDLE_MAX_SIZE'] = text
            self.assertEqual(mirror.bundle_cap(), expected)
        for text in ('4GB', '0', 'big'):
            os.environ['MIRROR_BUNDLE_MAX_SIZE'] = text
            with self.assertRaisesRegex(mirror.MirrorError, 'MIRROR_BUNDLE_MAX_SIZE'):
                mirror.bundle_cap()

    def test_images_split_into_bundles_under_the_cap_and_a_big_one_goes_alone(self):
        os.environ['MIRROR_BUNDLE_MAX_SIZE'] = '4MiB'
        mebibyte = 1024 ** 2
        images = [{'target': f'team/{name}', 'tag': '1', 'transfer': 'sha256:' + c * 64, 'source': 'x'}
                  for name, c in zip('abcd', '1234')]
        sizes = {'a': 1, 'b': 2, 'c': 2, 'd': 5}
        with mock.patch.object(mirror, 'image_bytes', side_effect=lambda ref, side: sizes[ref.split('/team/')[1][0]] * mebibyte), \
                mock.patch('builtins.print') as said:
            groups = mirror.batches(images, 'low.example.internal')
        self.assertEqual([[i['target'][-1] for i in group] for group in groups], [['a', 'b'], ['c'], ['d']])
        self.assertIn(mock.call('team/d:1 is 5242880 bytes, over MIRROR_BUNDLE_MAX_SIZE (4194304): it goes in a bundle of its own, sent in parts'), said.call_args_list)

    def test_only_the_first_bundle_of_a_split_run_is_full_or_bridges_a_gap(self):
        with mock.patch.object(mirror, 'batches', return_value=[['a'], ['b'], ['c']]), \
                mock.patch.object(mirror, 'write_bundle', return_value='bundle') as sent:
            mirror.send_batches('work', 'out', 'state', {}, 'low', ['a', 'b', 'c'], True, 'resend', bridges=7)
        self.assertEqual([c.args[5:] for c in sent.call_args_list],
                         [(['a'], True, 'resend', 7), (['b'], False, 'resend', None), (['c'], False, 'resend', None)])

    def test_image_bytes_counts_every_manifest_config_and_layer_once(self):
        layer = {'digest': 'sha256:' + 'l' * 64, 'size': 100}
        child = json.dumps({'config': {'digest': 'sha256:' + 'c' * 64, 'size': 10}, 'layers': [layer, layer]}).encode()
        index = json.dumps({'manifests': [{'digest': 'sha256:' + 'a' * 64}, {'digest': 'sha256:' + 'b' * 64}]}).encode()
        with mock.patch.object(mirror, 'run', side_effect=lambda *args: index if args[-1].endswith('@root') else child):
            total = REAL_IMAGE_BYTES('docker://low/team/app@root', 'TARGET_REGISTRY')
        self.assertEqual(total, len(index) + 2 * len(child) + 10 + 100)  # the shared layer and config count once

    def test_add_list_expands_short_names_assumes_latest_skips_known_and_keeps_going(self):
        path = self.root / 'images.txt'
        path.write_text('# reviewed catalog\n\n'
                        'quay.io/prometheus/node-exporter:v1.9.1@sha256:' + 'e' * 64 + ' team/prometheus/node-exporter\n')
        lists = self.root / 'lists'
        lists.mkdir()
        (lists / 'b.txt').write_text('# monitoring\nprom/prometheus:v3.13.4\nquay.io/prometheus/node-exporter:v1.9.1\n')
        (lists / 'a.txt').write_text('alpine\nghcr.io/broken/Name:1\n')

        def resolve(source, target):
            if 'broken' in source:
                raise mirror.MirrorError('add needs registry/repo:tag and org/repo[/path]')
            name = source.split('@')[0]
            return mirror.parse_image(name + '@sha256:' + 'f' * 64, target), [name.rsplit(':', 1)[1]]

        with mock.patch.object(mirror, 'resolve_entry', side_effect=resolve), mock.patch('builtins.print') as said, \
                self.assertRaisesRegex(mirror.MirrorError, r'1 line\(s\) not added:\n  a.txt:2 ghcr.io/broken/Name:1'):
            mirror.add_list(path, lists, prefix='team')
        printed = [c.args[0] for c in said.call_args_list]
        self.assertIn('expanded: alpine -> docker.io/library/alpine:latest', printed)
        self.assertIn('expanded: prom/prometheus:v3.13.4 -> docker.io/prom/prometheus:v3.13.4', printed)
        self.assertIn('already in the catalog: b.txt:3 quay.io/prometheus/node-exporter:v1.9.1 -> '
                      'team/prometheus/node-exporter:v1.9.1', printed)
        self.assertEqual(path.read_text().splitlines(), [
            '# reviewed catalog', '',
            'docker.io/library/alpine:latest@sha256:' + 'f' * 64 + ' team/library/alpine',
            'docker.io/prom/prometheus:v3.13.4@sha256:' + 'f' * 64 + ' team/prom/prometheus',
            'quay.io/prometheus/node-exporter:v1.9.1@sha256:' + 'e' * 64 + ' team/prometheus/node-exporter'])
        self.assertEqual(len(mirror.catalog(path)), 3)

    def test_write_catalog_moves_an_entrys_comment_with_it(self):
        path = self.root / 'images.txt'
        path.write_text('# header\nquay.io/z/z:1@sha256:' + 'a' * 64 + ' t/z\n# why b is pinned\n'
                        'docker.io/b/b:1@sha256:' + 'b' * 64 + ' t/b\n')
        mirror.write_catalog(path, ['ghcr.io/g/g:1@sha256:' + 'c' * 64 + ' t/g'])
        self.assertEqual([line.split('@')[0] for line in path.read_text().splitlines()],
                         ['# header', '# why b is pinned', 'docker.io/b/b:1', 'ghcr.io/g/g:1', 'quay.io/z/z:1'])

    def test_rewrite_moves_whole_segments_and_the_longest_prefix_wins(self):
        os.environ['IMPORT_PATH_REWRITE'] = 'team=company-dev, team/special=company-dev/vip'
        self.assertEqual(mirror.rewrite('team/prom/prometheus', 'IMPORT_PATH_REWRITE'), 'company-dev/prom/prometheus')
        self.assertEqual(mirror.rewrite('team/special/app', 'IMPORT_PATH_REWRITE'), 'company-dev/vip/app')
        self.assertEqual(mirror.rewrite('teamwork/app', 'IMPORT_PATH_REWRITE'), 'teamwork/app')  # not a segment match
        os.environ['IMPORT_PATH_REWRITE'] = 'team'
        with self.assertRaisesRegex(mirror.MirrorError, 'old=new prefix pairs'):
            mirror.rewrite('team/app', 'IMPORT_PATH_REWRITE')

    def test_import_and_promote_push_to_the_rewritten_path(self):
        image, _ = fixture(self.stage)
        bundle = self.root / 'mirror-1.tar'
        pack(self.stage, bundle, image=image)
        record = self.root / 'imported.json'
        record.write_text('[]\n')
        os.environ['IMPORT_PATH_REWRITE'] = 'mirror=company-dev'
        with mock.patch.object(mirror, 'copy') as copied, mock.patch('builtins.print') as said, \
                mock.patch.object(mirror, 'raw_digest', return_value=image['transfer']):
            mirror.import_one(bundle, record=record)
        self.assertEqual(copied.call_args.args[1], f"docker://low.example.internal/company-dev/alpine:{image['tag']}")
        self.assertTrue(any('(sent as mirror/alpine, IMPORT_PATH_REWRITE)' in str(c) for c in said.call_args_list))
        self.assertEqual(json.loads(record.read_text())[0]['target'], 'company-dev/alpine')
        os.environ.update({'SOURCE_REGISTRY': 'dev.example.internal', 'TARGET_REGISTRY': 'prod.example.internal',
                           'PROMOTE_PATH_REWRITE': 'company-dev=company-prod'})
        with mock.patch.object(mirror, 'copy') as copied, mock.patch('builtins.print'), \
                mock.patch.object(mirror, 'raw_digest', return_value=image['transfer']):
            mirror.promote(record)
        self.assertEqual(copied.call_args.args[:2], (f"docker://dev.example.internal/company-dev/alpine@{image['transfer']}",
                                                     f"docker://prod.example.internal/company-prod/alpine:{image['tag']}"))

    def test_a_bundle_over_the_cap_is_posted_as_parts_with_the_whole_checksum(self):
        name = outbox_with(self.root / 'out', b'0123456789', 4)
        seen = []

        def post(*args):
            path = Path(args[args.index('--upload-file') + 1])
            seen.append((path.name, path.read_bytes(), [args[i + 1] for i, a in enumerate(args) if a == '--header']))
            return b'200'

        with mock.patch.object(mirror, 'run', side_effect=post), mock.patch('builtins.print'):
            mirror.send('nifi', target='http://nifi.example.internal:9099/contentListener')
        self.assertEqual([(n, data) for n, data, _ in seen],
                         [(f'{name}.part-001', b'0123'), (f'{name}.part-002', b'4567'), (f'{name}.part-003', b'89')])
        for _, data, headers in seen:
            self.assertIn(f'X-Sha256: {hashlib.sha256(data).hexdigest()}', headers)
            self.assertIn(f'X-Bundle-Sha256: {hashlib.sha256(b"0123456789").hexdigest()}', headers)
            self.assertIn(f'X-Bundle-Name: {name}', headers)
            self.assertIn('X-Bundle-Parts: 3', headers)
        self.assertEqual(list((self.root / 'out').iterdir()), [])

    def test_import_joins_the_parts_waits_for_a_missing_one_and_deletes_them_all(self):
        files = self.store()
        image, _ = fixture(self.stage)
        stream = '7' * 32
        stem = f'mirror-{stream}-000000000001'
        pack(self.stage, self.root / f'{stem}.tar', image=image, stream=stream)
        whole = (self.root / f'{stem}.tar').read_bytes()
        size = -(-len(whole) // 3)
        chunks = [whole[i:i + size] for i in range(0, len(whole), size)]
        files[('registry-mirror-bundles', stem, f'{stem}.tar.sha256')] = \
            f'{hashlib.sha256(whole).hexdigest()}  {stem}.tar  {len(chunks)}\n'.encode()
        for number, chunk in enumerate(chunks[:-1], 1):
            files[('registry-mirror-bundles', stem, f'{stem}.tar.part-{number:03d}')] = chunk
        os.environ.update({'IMPORT_DELETE_BUNDLES': 'true', 'PACKAGE_TOKEN': 'maintainer-token'})
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']), \
                mock.patch.object(mirror, 'store_delete', return_value=1) as deleted, mock.patch('builtins.print') as said:
            mirror.import_registry(f'{stem}.tar')
            self.assertIn(mock.call(f'waiting for {stem}.tar.part-00{len(chunks)} (1 of {len(chunks)} parts)'),
                          said.call_args_list)
            files[('registry-mirror-bundles', stem, f'{stem}.tar.part-00{len(chunks)}')] = chunks[-1]
            mirror.import_registry(f'{stem}.tar')
        self.assertIn(mock.call(f'read {len(chunks)} parts of {stem}.tar in order, one on disk at a time'),
                      said.call_args_list)
        self.assertEqual(list((self.root / 'import/downloads').iterdir()), [])  # no part or joined bundle left
        self.assertEqual(json.loads((self.root / 'import/received.json').read_text())['sequence'], 1)
        self.assertEqual(deleted.call_args.args[2],
                         [f'{stem}.tar.part-{n:03d}' for n in range(1, len(chunks) + 1)] + [f'{stem}.tar.sha256'])

    def test_parts_join_to_the_same_tar(self):
        fixture(self.stage)
        (self.stage / 'images.json').write_text('{}')
        whole = io.BytesIO()
        mirror.write_tar(self.stage, whole)
        out = self.root / 'parts'
        out.mkdir()
        sink = mirror.PartSink(out, 'mirror-s.tar', 4096)
        mirror.write_tar(self.stage, sink)
        sink.flush_part()
        parts = sorted(out.iterdir())
        self.assertEqual([p.name for p in parts], [f'mirror-s.tar.part-{n:03d}' for n in range(1, len(parts) + 1)])
        self.assertEqual(b''.join(p.read_bytes() for p in parts), whole.getvalue())
        self.assertTrue(all(p.stat().st_size <= 4096 for p in parts))
        self.assertEqual(sink.digest.hexdigest(), hashlib.sha256(whole.getvalue()).hexdigest())

    def test_a_corrupted_part_is_refused_before_anything_is_pushed(self):
        files = self.store()
        image, _ = fixture(self.stage)
        stem = f"mirror-{'6' * 32}-000000000001"
        pack(self.stage, self.root / f'{stem}.tar', image=image, stream='6' * 32)
        whole = (self.root / f'{stem}.tar').read_bytes()
        half = len(whole) // 2
        files[('registry-mirror-bundles', stem, f'{stem}.tar.sha256')] = f'{hashlib.sha256(whole).hexdigest()}  {stem}.tar  2\n'.encode()
        files[('registry-mirror-bundles', stem, f'{stem}.tar.part-001')] = whole[:half]
        tampered = bytearray(whole[half:])
        tampered[-2000] ^= 1  # inside the tar padding: unpacking succeeds, the checksum does not
        files[('registry-mirror-bundles', stem, f'{stem}.tar.part-002')] = bytes(tampered)
        with mock.patch.object(mirror, 'copy') as copied, mock.patch('builtins.print'), \
                self.assertRaisesRegex(mirror.MirrorError, 'joined parts do not match the bundle checksum'):
            mirror.import_registry(f'{stem}.tar')
        copied.assert_not_called()

    def test_closing_a_part_reader_mid_part_removes_the_download(self):
        def fetch(name, path):
            path.write_bytes(b'x' * 100)
            return True
        with mirror.PartReader(fetch, ['a', 'b'], self.root) as reader:
            reader.read(10)
            self.assertTrue((self.root / 'part.download').exists())
        self.assertFalse((self.root / 'part.download').exists())

    def test_a_later_bundle_waits_for_an_earlier_one_until_the_grace_runs_out(self):
        files = self.store()
        image, _ = fixture(self.stage)
        stream = '9' * 32
        stems = {n: f'mirror-{stream}-{n:012d}' for n in (1, 2, 3, 4)}
        for n, stem in stems.items():
            pack(self.stage, self.root / f'{stem}.tar', sequence=n, full=n == 1, image=image, stream=stream)
        def arrive(n):
            for name in (f'{stems[n]}.tar', f'{stems[n]}.tar.sha256'):
                files[('registry-mirror-bundles', stems[n], name)] = (self.root / name).read_bytes()
        arrive(1)
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']), \
                mock.patch('builtins.print'):
            mirror.import_registry(f'{stems[1]}.tar')
            arrive(3)  # 3 overtakes 2 on the link
            with mock.patch.object(mirror, 'store_age', return_value=0.5), \
                    self.assertRaisesRegex(mirror.GapWaiting, 'imports when the earlier bundle arrives'):
                mirror.import_registry(f'{stems[3]}.tar')
            arrive(4)
            with mock.patch.object(mirror, 'store_age', return_value=0.4), self.assertRaises(mirror.GapWaiting):
                mirror.import_registry(f'{stems[4]}.tar')
            arrive(2)  # the straggler: one trigger imports 2, 3 and 4 in order
            mirror.import_registry(f'{stems[2]}.tar')
        self.assertEqual(json.loads((self.root / 'import/received.json').read_text())['sequence'], 4)
        self.assertEqual(mirror.main(['--catalog', str(self.root / 'none.txt'), 'import', '--registry']), 0)

    def test_a_gap_older_than_the_grace_fails_with_the_resend_instruction(self):
        files = self.store()
        image, _ = fixture(self.stage)
        stream = '8' * 32
        for n in (1, 3):
            stem = f'mirror-{stream}-{n:012d}'
            pack(self.stage, self.root / f'{stem}.tar', sequence=n, full=n == 1, image=image, stream=stream)
            for name in (f'{stem}.tar', f'{stem}.tar.sha256'):
                files[('registry-mirror-bundles', stem, name)] = (self.root / name).read_bytes()
        os.environ['IMPORT_GAP_GRACE'] = '2'
        with mock.patch.object(mirror, 'copy'), mock.patch.object(mirror, 'raw_digest', return_value=image['digest']), \
                mock.patch('builtins.print'), mock.patch.object(mirror, 'store_age', return_value=2.5):
            mirror.import_registry(f'mirror-{stream}-000000000001.tar')
            with self.assertRaisesRegex(mirror.SequenceGap, r'RESEND_SEQUENCE=2\.\..*waited for 2\.5 h; IMPORT_GAP_GRACE is 2 h'):
                mirror.import_registry(f'mirror-{stream}-000000000003.tar')
            with mock.patch('sys.stderr'):
                self.assertEqual(mirror.main(['import', '--registry', '--name', f'mirror-{stream}-000000000003.tar']), 1)
            with mock.patch.object(mirror, 'store_age', return_value=0.1), mock.patch('sys.stderr'):
                self.assertEqual(mirror.main(['import', '--registry', '--name', f'mirror-{stream}-000000000003.tar']), 3)


def pack_metadata(stage, destination):
    """Re-pack a stage whose images.json a test edited, with a fresh checksum."""
    with tarfile.open(destination, 'w') as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(stage)), recursive=False)
    destination.with_name(destination.name + '.sha256').write_text(f'{mirror.sha256(destination)}  {destination.name}\n')


if __name__ == '__main__':
    unittest.main()
