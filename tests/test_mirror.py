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
            mirror.add_image(path, 'prom/prometheus:v3.13.4', 'team-dev/prom/prometheus')
        self.assertEqual(lookup.call_args_list[0].args[-1], 'docker://docker.io/prom/prometheus:v3.13.4')
        self.assertEqual(path.read_text(), f'docker.io/prom/prometheus:v3.13.4@{pinned} team-dev/prom/prometheus\n')
        self.assertEqual(mirror.normalise('alpine:3.20'), 'docker.io/library/alpine:3.20')
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
                    mirror.add_image(self.root / 'images.txt', 'bitnami/redis:sha256-' + 'c'*64 + '.sig',
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
            mirror.import_one(bundle)
            mirror.import_one(bundle)
        self.assertEqual(push.call_count, 1)

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
            with mock.patch.object(mirror, 'notify', side_effect=mirror.MirrorError('delivery failed')):
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
            with mock.patch.object(mirror, 'notify', side_effect=mirror.MirrorError('delivery failed')), \
                    self.assertRaisesRegex(mirror.MirrorError, 'delivery failed'):
                mirror.sync(path, full=True)
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

        os.environ['MIRROR_LEDGER'] = 'true'
        for name, fake in (('package_get', get), ('package_put', put)):
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
        with mock.patch.object(mirror, 'send') as send:
            mirror.resend(since='2026-08-01')
        self.assertEqual([i['transfer'] for i in send.call_args.args[4]], [new])
        self.assertEqual(send.call_args.args[6:], ('resend', 1))

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
        self.assertEqual(said.call_args_list[-1], mock.call(f'superseded: mirror-{stream}-000000000001.tar'))
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
                mock.patch.object(mirror, 'store_delete') as deleted, mock.patch('builtins.print') as said:
            mirror.import_registry(f'{stem}.tar')
            mirror.import_registry(f'{stem}.tar.sha256')  # the second file's trigger, after the delete
        deleted.assert_called_once_with('registry-mirror-bundles', stem, [f'{stem}.tar', f'{stem}.tar.sha256'])
        self.assertEqual(said.call_args_list[-1], mock.call(f'already imported: {stem}.tar'))

    def test_a_delivered_bundle_leaves_nothing_on_the_runner(self):
        self.store()
        path, _, fake_copy, fake_digest = self.latest_entry()
        os.environ['NIFI_URL'] = 'http://nifi.example.internal:9098/contentListener'
        with mock.patch.object(mirror, 'run', return_value=b'{"schemaVersion": 2}'), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch.object(mirror, 'notify') as sent:
            bundle = mirror.sync(path)
        self.assertEqual(sent.call_args.args[0], bundle)
        self.assertEqual(list((self.root / 'out').iterdir()), [])

    def test_without_a_destination_sync_mirrors_low_and_records_nothing_sent(self):
        files = self.store()
        path, _, fake_copy, fake_digest = self.latest_entry()
        del os.environ['MIRROR_BUNDLE_DIR']
        with mock.patch.object(mirror, 'run', return_value=b'{"schemaVersion": 2}'), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch.object(mirror, 'notify') as sent, \
                mock.patch('builtins.print') as said:
            self.assertIsNone(mirror.sync(path))
        sent.assert_not_called()
        self.assertIn('not sent to the high side', said.call_args.args[0])
        state = json.loads(files[('registry-mirror-ledger', 'head', 'state.json')])
        self.assertEqual((state['sequence'], state['sent']), (0, {}))
        # Once NiFi is configured, the next sync sends what the first one could not.
        os.environ['NIFI_URL'] = 'http://nifi.example.internal:9098/contentListener'
        with mock.patch.object(mirror, 'run', return_value=b'{"schemaVersion": 2}'), \
                mock.patch.object(mirror, 'copy', side_effect=fake_copy), \
                mock.patch.object(mirror, 'raw_digest', side_effect=fake_digest), \
                mock.patch.object(mirror, 'notify') as sent:
            self.assertIsNotNone(mirror.sync(path))
        sent.assert_called_once()

    def test_resend_refuses_without_a_destination(self):
        self.store()
        del os.environ['MIRROR_BUNDLE_DIR']
        with self.assertRaisesRegex(mirror.MirrorError, 'set NIFI_URL'):
            mirror.resend(sequences='1..')

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
        self.assertEqual(mirror.side_of('docker.io/library/alpine:3.20'), 'SOURCE_REGISTRY')
        os.environ['TARGET_REGISTRY_TLS_VERIFY'] = 'false'
        self.assertEqual(mirror.options(mirror.side_of('low.example.internal/team/runner:1.2')), ['--tls-verify=false'])
        self.assertEqual(mirror.options(mirror.side_of('docker.io/library/alpine:3.20')), [])

    def test_login_without_a_username_logs_in_nowhere(self):
        with mock.patch.object(mirror.subprocess, 'run') as ran:
            mirror.login()  # a high registry without authentication
        ran.assert_not_called()

    def test_notify_sends_the_routing_headers(self):
        bundle = self.root / 'mirror-x.tar'
        bundle.write_bytes(b'tar')
        bundle.with_name(bundle.name + '.sha256').write_text('sum\n')
        os.environ['NIFI_URL'] = 'http://user:secret@nifi.example.internal:9099/contentListener'
        with mock.patch.object(mirror, 'run', return_value=b'200') as call, \
                mock.patch('builtins.print') as said:
            mirror.notify(bundle, 'resend')
        sent = [c.args for c in call.call_args_list]
        self.assertTrue(all('--http1.1' in args for args in sent))
        logged = [c.args[0] for c in said.call_args_list]
        self.assertEqual(len(logged), 2)
        self.assertTrue(logged[1].startswith(
            'posted to NiFi: mirror-x.tar -> http://nifi.example.internal:9099/contentListener (HTTP 200'))
        self.assertNotIn('secret', ''.join(logged))
        self.assertEqual([a[a.index('--data-binary') + 1] for a in sent], [f'@{bundle}.sha256', f'@{bundle}'])
        for args, form, path in zip(sent, ('sha256', 'tar'), (bundle.with_name(bundle.name + '.sha256'), bundle)):
            headers = [args[i + 1] for i, a in enumerate(args) if a == '--header']
            for header in ('X-Artifact-Type: container-images', 'X-Artifact-Action: mirror', 'X-Bundle-Kind: resend',
                           f'X-Artifact-Format: {form}', f'X-Sha256: {mirror.sha256(path)}', f'Filename: {path.name}'):
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


def pack_metadata(stage, destination):
    """Re-pack a stage whose images.json a test edited, with a fresh checksum."""
    with tarfile.open(destination, 'w') as archive:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(stage)), recursive=False)
    destination.with_name(destination.name + '.sha256').write_text(f'{mirror.sha256(destination)}  {destination.name}\n')


if __name__ == '__main__':
    unittest.main()
