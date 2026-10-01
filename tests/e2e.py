#!/usr/bin/env python3
"""Exercise real registry transfers using the normal CLI (isolated test repositories only).

Log in to both registries with skopeo first. E2E_FIXTURES=true pushes two OCI indexes;
otherwise E2E_SOURCE and E2E_UPDATE_SOURCE name upstream tags, e.g. Docker manifest lists.
"""
import json
import gzip
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mirror  # noqa: E402


def cli(*args, fail=None):
    result = subprocess.run([sys.executable, str(ROOT / 'mirror.py'), '--catalog', str(catalog), *args],
                            capture_output=True, text=True)
    print(result.stdout, end='', flush=True)
    if fail:
        assert result.returncode != 0 and fail in result.stderr, result.stdout + result.stderr
        print('expected rejection:', fail, flush=True)
    else:
        assert result.returncode == 0, result.stderr
    return result


def last_bundle():
    return sorted(outbox.glob('quay-*.tar'))[-1]


def transfer(bundle):
    for path in (bundle, bundle.with_name(bundle.name + '.sha256')):
        shutil.copy2(path, inbox / path.name)
    return inbox / bundle.name


def compare(image):
    for side in ('LOW_QUAY', 'HIGH_QUAY'):
        actual = mirror.raw_digest(f"docker://{os.environ[side + '_HOST']}/{image['target']}:{image['tag']}", side)
        assert actual == image['digest'], (side, image['target'], actual)
    raw = mirror.run('skopeo', 'inspect', '--raw', *mirror.options('HIGH_QUAY'),
                    f"docker://{os.environ['HIGH_QUAY_HOST']}/{image['target']}:{image['tag']}")
    platforms = [child.get('platform', {}) for child in json.loads(raw).get('manifests', [])]
    assert len(platforms) >= 2, 'test image must retain a multi-platform index'
    return {'image': image['target'] + ':' + image['tag'], 'digest': image['digest'], 'platforms': platforms}


def seed_fixtures(directory, target):
    """Two reproducible, complete OCI indexes without external registry downloads."""
    sources = []
    for version in ('1.0.0', '1.0.1'):
        layout = directory / version
        (layout / 'blobs/sha256').mkdir(parents=True)

        def put(data, media_type):
            digest = hashlib.sha256(data).hexdigest()
            (layout / 'blobs/sha256' / digest).write_bytes(data)
            return {'mediaType': media_type, 'size': len(data), 'digest': 'sha256:' + digest}

        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w') as archive:
            item = tarfile.TarInfo('VERSION')
            content = version.encode()
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
        layer = put(gzip.compress(payload.getvalue(), mtime=0), 'application/vnd.oci.image.layer.v1.tar+gzip')
        children = []
        for arch in ('amd64', 'arm64'):
            config = put(json.dumps({'architecture': arch, 'os': 'linux', 'config': {},
                         'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(payload.getvalue()).hexdigest()]}}).encode(),
                         'application/vnd.oci.image.config.v1+json')
            child = put(json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                        'config': config, 'layers': [layer]}).encode(), 'application/vnd.oci.image.manifest.v1+json')
            child['platform'] = {'architecture': arch, 'os': 'linux'}
            children.append(child)
        index = put(json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json',
                    'manifests': children}).encode(), 'application/vnd.oci.image.index.v1+json')
        index['annotations'] = {'org.opencontainers.image.ref.name': version}
        (layout / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
        (layout / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [index]}))
        source = os.environ['LOW_QUAY_HOST'] + '/' + target + '-source:' + version
        mirror.copy(f'oci:{layout}:{version}', 'docker://' + source, destination_side='LOW_QUAY')
        sources.append(source + '@' + index['digest'])
    os.environ['UPSTREAM_TLS_VERIFY'] = os.environ.get('LOW_QUAY_TLS_VERIFY', 'true')
    return sources


if __name__ == '__main__':
    for key in ('LOW_QUAY_HOST', 'HIGH_QUAY_HOST'):
        mirror.required_env(key)
    with tempfile.TemporaryDirectory(prefix='quay-mirror-e2e-') as temporary:
        directory = Path(temporary)
        catalog = directory / 'images.txt'
        outbox, inbox = directory / 'out', directory / 'in'
        inbox.mkdir()
        os.environ.update(MIRROR_STATE_DIR=str(directory / 'work'), MIRROR_BUNDLE_DIR=str(outbox),
                          IMPORT_STATE_DIR=str(directory / 'import'))
        os.environ.pop('NIFI_URL', None)
        # Nested destination: organisation, then a repository path with its own slash.
        target = os.environ.get('E2E_ORG', 'mirror') + '/proof-' + uuid.uuid4().hex[:12] + '/image'
        if os.environ.get('E2E_FIXTURES') == 'true':
            source, update_source = seed_fixtures(directory, target)
        else:
            source = os.environ.get('E2E_SOURCE', 'docker.io/library/alpine:3.20.3')
            update_source = os.environ.get('E2E_UPDATE_SOURCE', 'docker.io/library/alpine:3.20.4')
        cli('add', source, target)
        cli('targets')
        cli('sync')
        first = transfer(last_bundle())
        cli('import', str(first))
        report = [compare(image) for image in mirror.catalog(catalog)]
        before = len(list(outbox.glob('*.tar')))
        cli('sync')
        assert len(list(outbox.glob('*.tar'))) == before
        # Apply the tag/digest edit that a merged Renovate update produces.
        catalog.write_text('')
        cli('add', update_source, target)
        cli('sync')
        updated = transfer(last_bundle())
        cli('import', str(updated))
        report = [compare(image) for image in mirror.catalog(catalog)]
        expected_tag = update_source.split('@')[0].rsplit(':', 1)[1]
        assert report[0]['image'].endswith(':' + expected_tag), report
        # Losing a subsequent new-image bundle must be detected on the high side.
        cli('add', source, target + '-second')
        cli('sync')  # deliberately lose this delta
        cli('add', source, target + '-third')
        cli('sync')
        missing = transfer(last_bundle())
        cli('import', str(missing), fail='missing earlier bundle')
        cli('sync', '--full')
        recovery = transfer(last_bundle())
        cli('import', str(recovery))
        cli('import', str(recovery))
        cli('import', str(first), fail='refusing rollback')
        report = [compare(image) for image in mirror.catalog(catalog)]
        # Checksum failure cannot be disguised as successful delivery.
        with recovery.open('ab') as stream:
            stream.write(b'corrupt')
        cli('import', str(recovery), fail='mismatched checksum')
        shutil.copy2(last_bundle(), recovery)
        cli('import', '--inbox', str(inbox))
        assert not list(inbox.glob('*.tar')), 'completed/superseded bundles must leave the inbox'
        with tarfile.open(inbox / 'done' / first.name) as archive:
            assert json.load(archive.extractfile('images.json'))['full'] is True
        result = {'status': 'passed', 'checks': ['catalog-add', 'version-update', 'low-hosted-mirror', 'offline-transfer',
                  'high-import', 'multi-platform-digest-equality', 'no-change', 'missing-delta-rejected',
                  'full-recovery', 'duplicate-import', 'stale-replay-rejected', 'corruption-rejected'],
                  'images': report}
        output = os.environ.get('E2E_REPORT')
        if output:
            Path(output).write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
