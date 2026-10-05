# gitlab-quay-mirror

Mirrors reviewed images and Helm charts into low Quay and exports offline bundles for high Quay. [Quickstart](QUICKSTART.md).

## Requirements

- Python 3.11+ and skopeo 1.13+ on each side; no Docker daemon or privileged runner.
- GitLab with an unprivileged Docker runner for verification and a protected shell runner tagged `quay-mirror` for sync.
- For high-side import, a separate project with CI configuration path `.gitlab-ci-high.yml` and a protected shell runner tagged `quay-mirror-high`.
- For the hands-off path, NiFi 2.x on each side of the transfer link (`nifi/flow.py` builds both flows).

## Inputs

`mirror.py --help`, `.gitlab-ci.yml` and `.gitlab-ci-high.yml` define the command and pipeline interfaces.

| Req | Name | Default | Purpose |
|---|---|---|---|
| For sync | `LOW_QUAY_HOST` | none | Low Quay, as `host[:port]` |
| For import | `HIGH_QUAY_HOST` | none | High Quay, as `host[:port]` |
| Optional | `MIRROR_PLATFORM` | all platforms | One platform to mirror and transfer, e.g. `linux/amd64` |
| Optional | `MIRROR_LEDGER` | `true` in CI | Keep the sender ledger in the project's generic package registry instead of `sent.json` |
| Optional | `GITLAB_API_URL`, `PACKAGE_PROJECT`, `PACKAGE_TOKEN` | the CI job's API, project and job token | Package registry for the ledger and for `import --registry` outside CI |
| Optional | `MIRROR_STATE_DIR` | `~/.local/state/quay-mirror` | Work directory and, without the ledger, `sent.json`; one writer per directory |
| Optional | `MIRROR_BUNDLE_DIR` | `$MIRROR_STATE_DIR/bundles` | Where `sync` writes bundles for pickup |
| Optional | `IMPORT_STATE_DIR` | `~/.local/state/quay-mirror-import` | Receipt ledger and replay guard |
| In high CI | `IMPORT_INBOX_DIR` | none | Directory holding transferred archive/checksum pairs |
| Optional | `MIRROR_FULL` | `false` | Set `true` on Run pipeline to resend every approved image |
| Optional | `EXPORT_SINCE`, `EXPORT_SEQUENCE`, `EXPORT_IMAGE` | none | On Run pipeline, resend instead of sync: `YYYY-MM-DD`, `N`, `N..` or `N..M`, `org/repo[:tag]` |
| Optional | `SYFT_IMAGE`, `GRYPE_IMAGE` | `anchore/syft:v1.54.0-debug`, `anchore/grype:v0.120.0-debug` | SBOM and vulnerability scan of each pending image; debug variants, for their shell |
| Optional | `GRYPE_FAIL_ON` | `critical` | Severity at which a fixed finding fails `scan`, blocking the merge and the sync |
| Optional | `NIFI_URL` | none | NiFi ListenHTTP receiving both files by POST, with `Filename`, `X-Sha256`, `X-Artifact-Type: container-images`, `X-Artifact-Format` (`tar`, `sha256`), `X-Artifact-Action: mirror` and `X-Bundle-Kind` (`delta`, `full`, `resend`) |
| Optional | `UPSTREAM_TLS_VERIFY`, `LOW_QUAY_TLS_VERIFY`, `HIGH_QUAY_TLS_VERIFY` | `true` | Set `false` only for an HTTP lab registry |

## Secrets

`mirror.py` uses the credentials of `skopeo login` or `podman login`. Log in to each registry once; no auth-file variable is needed. A CI job has no `XDG_RUNTIME_DIR`, so the pipelines set `REGISTRY_AUTH_FILE` and log in from these protected, masked variables, scoped to environment `mirror-low` or `mirror-high`:

| Variable | When | Purpose |
|---|---|---|
| `LOW_QUAY_USERNAME`, `LOW_QUAY_PASSWORD` | Sync | Robot with push/pull on the low target organisations |
| `HIGH_QUAY_USERNAME`, `HIGH_QUAY_PASSWORD` | Import | Robot with push/pull on the high target organisations |
| `UPSTREAM_USERNAME`, `UPSTREAM_PASSWORD`, `UPSTREAM_REGISTRY` | Rate-limited upstream | Pull credentials; registry defaults to `docker.io` |

## Minimum configuration

Add each image by the tag you run. `add` looks up its current digest and appends a pinned line to `images.txt`. Destinations take any depth, and short names expand like `docker pull`.

```sh
python3 mirror.py add prom/prometheus:v3.13.4 team-dev/prom/prometheus
python3 mirror.py add bitnami/redis:latest team-dev/bitnami/redis
```

```text
docker.io/prom/prometheus:v3.13.4@sha256:87861b8c... team-dev/prom/prometheus
docker.io/bitnami/redis:latest@sha256:f4797b37... team-dev/bitnami/redis
```

| Image | Tags on low and high Quay | After Renovate's update merges |
|---|---|---|
| `prom/prometheus` (released tags) | `v3.13.4`, as on Docker Hub | `v3.15.0` added; `v3.13.4` stays |
| `bitnami/redis` (publishes only `latest`) | `latest` and `8.10.2`, the version the image reports | `latest` moves to the new digest and its version tag is added; `8.10.2` stays |

Add an OCI Helm chart the same way: `python3 mirror.py add bitnamicharts/redis:22.0.7 team-dev/charts/redis`. A chart has no platform, so `MIRROR_PLATFORM` leaves it whole.

The version comes from `org.opencontainers.image.version`, `app.kubernetes.io/version` or `APP_VERSION`. An image that reports none gets `latest` alone and is asked again on each sync; a failed lookup fails the run. `add` refuses signature and metadata artifacts such as bitnami's `sha256-<hex>` tags, which are not images.

## Usage

```sh
python3 mirror.py sync
```

## Preconditions

- Create the target organisation on each Quay. Put its robot in a Creator team so new catalog repositories can be created, or pre-create repositories and grant the robot push/pull access.
- Protect the default branch and mirror runner. Merge-request verification uses no mirror credentials or persistent state.
- Enable Renovate on the GitLab copy and give its bot merge rights on the default branch (Maintainer, or Developers allowed to merge); without them GitLab refuses the merge with 401. Digest, patch and minor updates merge once `verify` and `scan` pass; majors wait for review. Turn on **Pipelines must succeed** so a failed scan also blocks a manual merge.
- Create a nightly GitLab schedule against the protected default branch. Schedules mirror the reviewed catalog; they do not approve Renovate updates.
- Keep the receipt state directory between runs: a shell runner's home directory does. On a container executor, point `IMPORT_STATE_DIR` at a persistent mount. Without the ledger, the same holds for `MIRROR_STATE_DIR`; use separate sender state for separate low registries.
- For `import --registry`, give the uploader a project access token on the high project (Developer, scope `api`) and let Developers merge to the default branch: GitLab runs a pipeline on a protected branch only for a role that may merge to it.
- Let the `scan` runner reach Grype's vulnerability database (`grype.anchore.io`), or point `GRYPE_DB_UPDATE_URL` at a mirror of it.
- Provision registry trust through the host's containers certificate configuration; keep TLS verification enabled outside disposable labs.

## Behaviour

Default-branch catalog/script/pipeline changes, schedules and Run pipeline run sync after verification.
Every sync verifies each tagged low-side copy, version tags included, restores any that is missing and copies new or changed approved digests; unchanged entries need no upstream pull.
The ledger records the digest and version tags last sent for each destination tag, and only entries where either changed enter a bundle. With `MIRROR_LEDGER=true` it is generic package `quay-mirror-ledger`: `head/state.json`, plus `<sequence>/images.json` for every bundle with its creation time, so any runner can take the next run. Keep one writer at a time: in CI, `sync` and `export` share `resource_group: quay-mirror`; do not run either by hand against the same ledger meanwhile. A daily run with no change prints `nothing to send` and writes no bundle. Sync never reads or deletes the bundle directory; the pickup removes bundles.
`mirror.py pending` lists what the next sync sends: catalog entries the ledger has not sent at their current digest and platform. `sbom` writes a CycloneDX SBOM of each with Syft (`MIRROR_PLATFORM`, else `linux/amd64`), `scan` checks it with Grype (`--only-fixed`), and `sync` waits for `scan`. After a sync, each SBOM and Grype report is kept as generic package `quay-mirror-sbom`, version the image digest (`sha256-<hex>`). Charts hold no packages and have neither.
`-v` (or `MIRROR_VERBOSE=true`) prints every skopeo and curl command and each unchanged entry; normal output prints one `send:` line per bundled image.
With `MIRROR_PLATFORM` set, low Quay, the archive and high Quay hold only that platform's image: its digest is the one the index lists for that platform, not the index digest in `images.txt`. Where the index names no platforms, each child image's config decides. A signature, attestation or nested index is never selected, whatever platform it names. An image or index with no image for that platform fails sync before any copy. Changing it resends every image.
Quay refuses Windows image manifests, so an all-platform mirror of an image with Windows children (such as `registry.k8s.io/pause`) fails. Set `MIRROR_PLATFORM` for it.

Carry the `.tar` and matching `.sha256` together. Import checks the archive and every manifest, config and layer against its digest before pushing.
`python3 mirror.py import --inbox /path/to/inbox` processes ready bundles in order and moves successful pairs to `done/`.
Missing checksums wait; missing sequence numbers, conflicting replays and corrupt content fail.
A missed transfer is recovered with `python3 mirror.py export --sequence N..` (N is the first missing bundle, which import names), `--since YYYY-MM-DD` or `--image org/repo[:tag]`. A resend is the next bundle and carries the newest recorded digest of each tag, copied from low Quay or, if gone, from upstream; one that covers every bundle from N on is accepted over the gap. `sync --full` resends everything; deleting sender state requires a reviewed full bundle and `import --adopt-stream`.

Upload both files of a bundle to the high project's generic package `quay-bundles`, version the file name without `.tar`, then start a pipeline with `BUNDLE` set to the file name. `import --registry --name "$BUNDLE"` imports it and any earlier bundle whose pipeline never started; half a bundle waits for its other file.
`nifi/flow.py --side low` builds ListenHTTP, PackageFlowFile and PutFile into the link, so the `X-` headers cross it. `--side high` lists the link, unpacks, routes on `X-Artifact-Type: container-images`, checks `X-Sha256`, and does both steps with a sensitive `gitlab.token`.

For Helm workloads, render the chart with your actual values and add the resulting images; a chart version or `appVersion` does not list them all. `python3 mirror.py covers Containerfile k8s.yaml` (or `helm template ... | python3 mirror.py covers -`) fails when a `FROM` or `image:` reference is not in the catalog, by upstream or mirror name; with `MIRROR_LEDGER=true` every tag ever sent counts.

## Out of scope

- Removing old tags, mirroring every upstream tag, Helm repositories served over HTTP, or deploying charts.
- Detached signatures/referrers and cross-domain release approval; a checksum proves integrity, not sender authenticity. Images already sent are not rescanned.
- Configuring the physical transfer link, GitLab schedules or Renovate service.

## Expected result

Sync prints one `send:` line per image and the bundle path, or `nothing to send; low-side digests verified`. Import prints one `pushed:` line per tag and `digests verified`.
Verify the catalog at either end with `python3 mirror.py targets`.
