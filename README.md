# gitlab-quay-mirror

Mirrors approved container tags from low Quay proxy organisations to high Quay with `python3 mirror.py sync`.

## Requirements

- Python 3.11+, skopeo, and a low-side runner with a persistent work and outbox directory.
- The high side needs Python 3.11+, skopeo, and a Quay account that can push to the target organisation.

## Flags

The full command set is in `python3 mirror.py --help`; registry mapping is in `proxy-orgs.txt`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `LOW_QUAY`, `LOW_AUTH_FILE` | none | Low Quay host and skopeo auth file for `sync` |
| Required | `HIGH_QUAY`, `HIGH_AUTH_FILE` | none | High Quay host and skopeo auth file for `import` |
| Optional | `MIRROR_WORK` | `.mirror-work/` | Persistent record of sent tag digests |
| Optional | `MIRROR_OUTBOX` | `delta/` | Bundle pickup directory |
| Optional | `NIFI_URL` | unset | POST each completed bundle to NiFi |
| Optional | `LOW_TLS_VERIFY`, `HIGH_TLS_VERIFY` | `true` | Set `false` only for an HTTP lab registry |

## Minimum configuration

Add one line per approved tag to `images.txt`:

```text
docker.io/library/alpine:3.20.3@sha256:1e42bbe2508154c9126d48c2b8a75420c3544343bf86fd041fb7527e017a4b4a mirror/library/alpine
```

The digest must be 64 lowercase hex characters. `renovate.json` updates the upstream tag and its digest. `proxy-orgs.txt` maps the upstream registry to a low Quay pull-through organisation. Each target organisation must exist on high Quay.

## Usage

```sh
python3 mirror.py sync
```

## Preconditions

- Log in to each Quay with `skopeo login --authfile <path> <host>` and set the matching auth-file variable.
- The low runner can pull from the proxy organisations. The high-side account can push to each target organisation.
- CI uses a `quay-mirror` runner with persistent `MIRROR_WORK` and `MIRROR_OUTBOX` paths. `resource_group` serialises sync jobs.
- If `NIFI_URL` is set, its listener writes both posted files unchanged into the high-side inbox.

## Behaviour

- `sync` verifies each tag against its approved digest, then copies every platform with `skopeo --all --preserve-digests` into one OCI layout. It bundles tags whose digest has changed since the last successful run.
- A rerun with no changes sends nothing. `sync --full` bundles every declared tag after a lost transfer.
- Each `quay-delta-*.tar` has a `.sha256` sidecar. Move both through NiFi or an SMB share. `NIFI_URL` POSTs the sidecar, then the tar; a failed POST leaves both files and fails the sync.
- `import <bundle>` verifies the checksum, pushes every tag to high Quay, and reads each manifest back to verify its digest. `import --inbox <dir>` processes complete bundles and moves successes to `done/`.
- The transfer delta is at the **tag/image** level. A new image sends its layers even if an older pack carried some of them. Add blob-level state only if measured transfer volume requires it.

## Out of scope

- Deleting high-side tags that disappear from `images.txt`.
- Transferring detached signatures, SBOM referrers, or vulnerability scans.
- Creating Quay organisations or the physical diode flow.

## Expected result

After import, `<HIGH_QUAY>/<target>:<tag>` has the same manifest digest as the approved `images.txt` entry. Verify with `skopeo inspect --raw docker://<high-quay>/<target>:<tag> | sha256sum`.
