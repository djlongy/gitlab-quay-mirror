# gitlab-quay-mirror

Mirrors reviewed images into low Quay and exports offline bundles for high Quay. [Quickstart](QUICKSTART.md).

## Requirements

- Python 3.11+ and skopeo 1.13+ on each side; no Docker daemon or privileged runner.
- GitLab with an unprivileged Docker runner for verification and a protected shell runner tagged `quay-mirror` for sync.
- For scheduled high-side import, a separate project with CI configuration path `.gitlab-ci-high.yml` and a protected shell runner tagged `quay-mirror-high`.

## Inputs

`mirror.py --help`, `.gitlab-ci.yml` and `.gitlab-ci-high.yml` define the command and pipeline interfaces.

| Req | Name | Default | Purpose |
|---|---|---|---|
| For sync | `LOW_QUAY_HOST` | none | Low Quay, as `host[:port]` |
| For import | `HIGH_QUAY_HOST` | none | High Quay, as `host[:port]` |
| Optional | `MIRROR_PLATFORM` | all platforms | One platform to mirror and transfer, e.g. `linux/amd64` |
| Optional | `MIRROR_STATE_DIR` | `~/.local/state/quay-mirror` | Sender ledger; one writer per directory |
| Optional | `MIRROR_BUNDLE_DIR` | `$MIRROR_STATE_DIR/bundles` | Where `sync` writes bundles for pickup |
| Optional | `IMPORT_STATE_DIR` | `~/.local/state/quay-mirror-import` | Receipt ledger and replay guard |
| In high CI | `IMPORT_INBOX_DIR` | none | Directory holding transferred archive/checksum pairs |
| Optional | `MIRROR_FULL` | `false` | Set `true` on Run pipeline to resend every approved image |
| Optional | `NIFI_URL` | none | HTTP(S) listener receiving both files by POST with a `Filename` header |
| Optional | `UPSTREAM_TLS_VERIFY`, `LOW_QUAY_TLS_VERIFY`, `HIGH_QUAY_TLS_VERIFY` | `true` | Set `false` only for an HTTP lab registry |

## Secrets

`mirror.py` uses the credentials of `skopeo login` or `podman login`. Log in to each registry once; no auth-file variable is needed. A CI job has no `XDG_RUNTIME_DIR`, so the pipelines set `REGISTRY_AUTH_FILE` and log in from these protected, masked variables, scoped to environment `mirror-low` or `mirror-high`:

| Variable | When | Purpose |
|---|---|---|
| `LOW_QUAY_USERNAME`, `LOW_QUAY_PASSWORD` | Sync | Robot with push/pull on the low target organisations |
| `HIGH_QUAY_USERNAME`, `HIGH_QUAY_PASSWORD` | Import | Robot with push/pull on the high target organisations |
| `UPSTREAM_USERNAME`, `UPSTREAM_PASSWORD`, `UPSTREAM_REGISTRY` | Rate-limited upstream | Pull credentials; registry defaults to `docker.io` |

Keep credentials out of `images.txt`, Git and transfer bundles.

## Minimum configuration

Add each image with its tag and destination. `add` looks up the tag's current digest and appends the pinned line to `images.txt`:

```sh
python3 mirror.py add prom/prometheus:v3.13.4 team-dev/prom/prometheus
```

```text
docker.io/prom/prometheus:v3.13.4@sha256:87861b8cf91579109319ebc300f3f1060e6da9c05d6ae8ad15a20c879e84e32e team-dev/prom/prometheus
```

Both Quays use the destination `team-dev/prom/prometheus:v3.13.4`. The destination is an organisation and a repository path of any depth. Short names expand like `docker pull`: `alpine:3.20` is `docker.io/library/alpine:3.20`.

For an image published only as `latest`, such as `bitnami/redis`, pin `latest`: `add bitnami/redis:latest mirror/bitnami/redis`. Renovate then proposes the new digest whenever `latest` moves. Its `sha256-<hex>` tags are signature indexes, not versions.

## Usage

```sh
python3 mirror.py sync
```

## Preconditions

- Create the target organisation on each Quay. Put its robot in a Creator team so new catalog repositories can be created, or pre-create repositories and grant the robot push/pull access.
- Protect the default branch and mirror runner. Merge-request verification uses no mirror credentials or persistent state.
- Enable Renovate on the GitLab copy. It queries upstream registries, proposes tag/digest changes in `images.txt`, and requires your review before merge.
- Create a nightly GitLab schedule against the protected default branch. Schedules mirror the reviewed catalog; they do not approve Renovate updates.
- Keep the sender and receipt state directories between runs: a shell runner's home directory does. On a container executor, point them at a persistent mount. Use separate sender state for separate low registries.
- Provision registry trust through the host's containers certificate configuration; keep TLS verification enabled outside disposable labs.

## Behaviour

Default-branch catalog/script/pipeline changes, schedules and Run pipeline run sync after verification.
Every sync verifies tagged low-side copies and copies new or changed approved digests; unchanged entries need no upstream pull.
Only changed target/tag/digest entries enter the archive. Each archive holds every platform of those images, or only `MIRROR_PLATFORM`'s.
With `MIRROR_PLATFORM` set, low Quay, the archive and high Quay hold that platform's image, whose digest differs from the index digest in `images.txt`. Run `sync --full` after changing it.
Quay refuses Windows image manifests, so an all-platform mirror of an image with Windows children (such as `registry.k8s.io/pause`) fails. Set `MIRROR_PLATFORM` for it.

Carry the `.tar` and matching `.sha256` together. Import checks the archive and every manifest, config and layer against its digest before pushing.
`python3 mirror.py import --inbox /path/to/inbox` processes ready bundles in order and moves successful pairs to `done/`.
Missing checksums wait; missing sequence numbers, conflicting replays and corrupt content fail.
A missed transfer is recovered with `python3 mirror.py sync --full`; deleting sender state requires a reviewed full bundle and `import --adopt-stream`.

For Helm workloads, render the chart with your actual values and add the resulting image references to the catalog.
A chart version or `appVersion` is not a complete list of its images, init containers or hooks.

## Out of scope

- Removing old tags, mirroring every upstream tag, or deploying Helm charts.
- Detached signatures/referrers, scanning and cross-domain release approval; a checksum proves integrity, not sender authenticity.
- Configuring the physical transfer link, NiFi flow, GitLab schedules or Renovate service.

## Expected result

Sync prints a bundle path, or `nothing to send; low-side digests verified`.
High-side import prints `digests verified` after reading the destination manifests back.
Verify the catalog at either end with `python3 mirror.py targets`.
