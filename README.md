# gitlab-registry-mirror

Mirrors reviewed container images and Helm charts into a registry on a connected low side, then carries them in verified bundles across a one-way link into a registry on an isolated high side. Any OCI registry works on either side: Quay, Harbor, GitLab, Artifactory, Nexus or `registry:2`. [Quickstart](QUICKSTART.md).

Flow: `SOURCE_REGISTRY` (Docker Hub, quay.io, ...) to the low `TARGET_REGISTRY` (`sync`), bundle to `NIFI_URL`, across the link, NiFi files it in `IMPORT_STORE`, then `import` pushes it to the high `TARGET_REGISTRY`. Each project sets `TARGET_REGISTRY` to its own side's registry. `docs/dataflow.md` walks through every step.

## Requirements

- On each side's runner: `python3` 3.9+, `skopeo` 1.13+ and `curl`. In the job image on a Docker runner, on the host on a shell runner. No Docker daemon or privileged runner.
- One GitLab project per side. The low project uses `.gitlab-ci.yml`; the high project sets **Settings > CI/CD > CI/CD configuration file** to `.gitlab-ci-high.yml`.
- For the hands-off path, NiFi 2.x on each side of the link.

## Low side: pull, save, send

`sync` copies each `images.txt` entry from its source into `TARGET_REGISTRY` by digest, then posts a bundle of what changed to `NIFI_URL`. Set these in **Settings > CI/CD > Variables**; mask the secrets and scope them to environment `mirror-low`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `TARGET_REGISTRY` | none | Low registry, `host[:port]` |
| Required | `TARGET_REGISTRY_USERNAME`, `TARGET_REGISTRY_PASSWORD` | none | Account that can push; leave unset for a registry without auth |
| Required, one of | `NIFI_URL` | none | NiFi ListenHTTP the bundles are posted to |
| Required, one of | `MIRROR_DROP` | none | Instead of posting: a directory the low NiFi lists (an NFS share such as `/mnt/transfer`), or `s3` for a bucket it lists. See [Low side without ListenHTTP](#low-side-without-listenhttp) |
| With `MIRROR_DROP=s3` | `S3_ENDPOINT`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`; optional `S3_PREFIX`, `S3_REGION`, `S3_CA_BUNDLE`, `S3_TLS_VERIFY` | none | The low-side bucket |
| Optional | `TARGET_REGISTRY_TLS_VERIFY` | `true` | `false` only for a plain-HTTP lab registry |
| Optional | `SOURCE_REGISTRY`, `SOURCE_REGISTRY_USERNAME`, `SOURCE_REGISTRY_PASSWORD`, `SOURCE_REGISTRY_TLS_VERIFY` | none | Login and TLS setting for one source registry, for example `docker.io` against rate limits; other source hosts pull anonymously with TLS verified |
| Optional | `MIRROR_PLATFORM` | all platforms | One platform, for example `linux/amd64` |
| Optional | `MIRROR_SCAN`, `GRYPE_FAIL_ON` | `true`, `critical` | Syft SBOM and Grype gate before `sync`; `false` skips both |
| Optional | `SYFT_IMAGE`, `GRYPE_IMAGE` | `docker.io/anchore/syft:v1.54.0-debug`, `docker.io/anchore/grype:v0.120.0-debug` | Scanner images |
| Optional | `MIRROR_BUNDLE_DIR` | none | Keep bundles in a directory for hand-carry instead of NiFi |
| Optional | `MIRROR_BUNDLE_MAX_SIZE` | `4GiB` | Largest file that leaves the low side: images are grouped into bundles up to this size, and a bundle over it (one larger image) goes in parts. Keep it under the high store's file limit (GitLab: 5 GiB by default) |
| Run pipeline | `RESEND_ALL`, `RESEND_SEQUENCE`, `RESEND_SINCE`, `RESEND_IMAGE` | none | Recovery, see [Resend](#resend) |

Add each image by the tag you run; `add` pins its current digest in `images.txt`:

```sh
python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus
python3 mirror.py add docker.io/bitnami/redis:latest team/redis            # also tagged with the version it reports
python3 mirror.py add docker.io/bitnamicharts/redis:22.0.7 charts/redis    # an OCI Helm chart
python3 mirror.py add registry.low.example.com/team/runner:1.2 team/runner   # your own image, already in TARGET_REGISTRY
```

To add many at once, list them in `.txt` files, one per line, and run `add-list` on the folder. A line without a registry host is read as `docker pull` reads it (`alpine` is `docker.io/library/alpine`), a line without a tag gets `:latest`, and each is written to `images.txt` in full with its digest. Each target is the source path without the host, under `--prefix` if given. `images.txt` is kept sorted by source; lines already present are skipped, and a line that fails is reported while the rest are still added:

```sh
python3 mirror.py add-list lists/ --prefix team   # lists/monitoring.txt: prom/prometheus:v3.13.4 -> team/prom/prometheus
```

A latest-only image also gets the version it reports, so `latest` can move while `8.10.2` stays. An image whose source is `TARGET_REGISTRY` itself is only verified there and sent, with that registry's login and TLS settings; no `SOURCE_REGISTRY` setting is needed. Renovate proposes updates as merge requests for the registries it can reach; `scan` must pass before one merges.

You know it works when the `sync` log shows `pushed to target registry: <host>/team/prometheus:v3.13.4@sha256:...` and `posted to NiFi: mirror-<stream>-<n>.tar -> <NIFI_URL> (HTTP 200, ...)`.

### Low side without ListenHTTP

When the runner cannot reach a NiFi listener, or NiFi should pull instead of being pushed to, set
`MIRROR_DROP`. Sync then writes each bundle (or part) as `<file>.ffv3`: the file already packaged
in NiFi's FlowFile v3 format with `filename` and the `X-` attributes NiFi would have taken from
the headers. The low NiFi only collects it and moves it into the link, and the high side is
unchanged.

| `MIRROR_DROP` | Sync writes | Low NiFi flow |
|---|---|---|
| a directory, e.g. `/mnt/transfer` | `<file>.ffv3.partial`, renamed to `<file>.ffv3` once complete | `flow.py --side low --source dir --param mirror.drop=/mnt/transfer`: ListFile `mirror-*.ffv3`, FetchFile, PutFile into the link, then DeleteFile |
| `s3` | `PUT <S3_PREFIX>/<file>.ffv3` | `flow.py --side low --source s3 --param s3.endpoint=... --param s3.bucket=... --param s3.prefix=...`: ListS3, FetchS3Object, PutFile into the link, then DeleteS3Object |

- Directory: mount the same share on the runner (a shell runner, or a `volumes` entry in a Docker
  runner's config) and in NiFi. NiFi's user must be able to read the files and delete them from
  the directory.
- S3: NiFi reads the keys from `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` when you run
  `flow.py`, and `s3.ca=@ca.pem` is needed only for a private CA. NiFi's ListS3, FetchS3Object
  and DeleteS3Object use path-style requests when an endpoint is set.
- Sync logs `dropped for NiFi: <file>.ffv3 -> <directory or bucket> (<bytes>, X-Sha256 ...)`.

## High side: receive, load, push

NiFi files each bundle in `IMPORT_STORE` and starts the pipeline with `BUNDLE`; `import` verifies every file against its digest and pushes the images to `TARGET_REGISTRY`. Scope the secrets to environment `dev`. The NiFi steps are in `nifi/HIGH-SIDE-BY-HAND.md`; `nifi/flow.py --side high` builds the same flow.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `TARGET_REGISTRY` | none | High registry, `host[:port]` |
| Required | `TARGET_REGISTRY_USERNAME`, `TARGET_REGISTRY_PASSWORD` | none | Account that can push; leave unset for a registry without auth |
| Optional | `TARGET_REGISTRY_TLS_VERIFY` | `true` | `false` only for a plain-HTTP lab registry |
| Optional | `IMPORT_STORE` | `gitlab` | Where bundles wait: `gitlab` (this project's package registry) or `s3` |
| When `s3` | `S3_ENDPOINT`, `S3_BUCKET` | none | `https://host:port` (path-style) and bucket |
| When `s3` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | none | Get, put and delete on the bucket |
| Optional | `S3_CA_BUNDLE`, `S3_REGION`, `S3_PREFIX`, `S3_TLS_VERIFY` | system CAs, `us-east-1`, none, `true` | `S3_CA_BUNDLE` is a File variable with a self-signed store's CA |
| Optional | `IMPORT_DELETE_BUNDLES` | `false` | `true` deletes each bundle from the store once imported; a re-trigger removes one an earlier run left |
| When deleting from `gitlab` | `PACKAGE_TOKEN` | job token | Project access token, role Maintainer, scope `api`; GitLab refuses the job token a delete |
| Optional | `IMPORT_INBOX_DIR` | none | Import hand-carried bundles from a directory instead of `IMPORT_STORE` |
| Optional | `IMPORT_GAP_GRACE` | `6` | Hours a bundle that arrived early waits for an earlier one. Meanwhile the job ends with a warning; after it, it fails with the resend instruction |
| Optional | `IMPORT_PATH_REWRITE`, `PROMOTE_PATH_REWRITE` | none | `old=new` prefix pairs, comma-separated, applied to each repository path on import (into dev) or on promote (into prod), e.g. `team=company-dev` puts `team/prom/prometheus` at `company-dev/prom/prometheus`. The longest matching prefix wins; every push logs the path it was sent as |

Give NiFi a project access token (Developer, scope `api`) and let Developers merge to the default branch: GitLab runs a pipeline on a protected branch only for a role that may merge to it. With S3, expire `registry-mirror-bundles/` by lifecycle rule if you like, never `registry-mirror-receipt/`.

You know it works when the `import` log shows `pushed to target registry: <host>/team/prometheus:v3.13.4@sha256:...` and `imported: mirror-<stream>-<n>.tar (... digests verified)`. Pull that exact reference to confirm.

### Large images and NiFi sizing

No file larger than `MIRROR_BUNDLE_MAX_SIZE` leaves the low side, so a 10 GB model image crosses as parts. Measured with a 10.2 GB image and 1 GiB parts: the low runner peaked at the image plus one part (10.5 GiB), and the import job at the unpacked image (9.7 GiB), since parts are read straight into the unpacked folder and deleted as they go. Plan each runner's disk at the size of the largest image plus one part.

NiFi copies a file at several steps (the low side's ListenHTTP and PackageFlowFile; the high side's FetchFile and UnpackContent), and by default keeps processed content in its archive until the content repository's disk is 90% full (`nifi.content.repository.archive.max.usage.percentage`). Give the content repository its own disk with room for several parts, or lower that percentage. When a NiFi queue reaches its back-pressure limit (1 GB by default), ListenHTTP answers 503 and sync retries with backoff.

### Promote dev to prod

With `MIRROR_PROMOTE=true`, the registry `import` pushes to is dev, and each pipeline ends in a manual `promote` job in environment `prod`: a play button in the pipeline's `promote` stage, and the pipeline shows as blocked until someone runs it. `MIRROR_PROMOTE` may be unscoped or scoped to `prod`; without it the pipeline has no `promote` stage. It copies exactly the images that pipeline imported, by digest, from dev to prod: `import --record imported.json` lists them and `mirror.py promote imported.json` copies them. The digest is checked in dev before the copy and in prod after it. Set these scoped to environment `prod`, protected and masked:

| Req | Name | Purpose |
|---|---|---|
| Required | `SOURCE_REGISTRY` | Dev registry, `host[:port]`, the same value as the dev `TARGET_REGISTRY` |
| Required | `SOURCE_REGISTRY_USERNAME`, `SOURCE_REGISTRY_PASSWORD` | Read access to dev; leave unset for a registry without auth |
| Required | `TARGET_REGISTRY` | Prod registry, `host[:port]` |
| Required | `TARGET_REGISTRY_USERNAME`, `TARGET_REGISTRY_PASSWORD` | Account that can push to prod |
| Optional | `SOURCE_REGISTRY_TLS_VERIFY`, `TARGET_REGISTRY_TLS_VERIFY` | `false` only for a plain-HTTP lab registry |

Only users allowed to merge to the protected default branch can run `promote`, and the job page records who ran it. So allow only Maintainers to merge, and give NiFi's project access token the Maintainer role (that token can run `promote` too, so keep it in NiFi's sensitive parameters only). GitLab Free has no required approvers on an environment, so nothing stops a Maintainer approving their own change. A pipeline whose import pushed nothing shows `nothing to promote`. Running `promote` on an older pipeline copies its digests again, which is how to roll prod back while that pipeline's `imported.json` artifact lasts (90 days).

Upgrading: the import job's environment was `mirror-high`. Rescope those variables to `dev`; a variable scoped to `mirror-high` no longer reaches the job. The NiFi parameters are now `gitlab.api.url`, `gitlab.container.projectId`, `gitlab.container.branch` and `gitlab.container.token` (were `gitlab.api`, `gitlab.project`, `gitlab.ref`, `gitlab.token`): `nifi/flow.py` rebuilds the context with the new names; a flow built by hand needs the parameters and the `#{...}` references renamed.

Each `import` and `sync` log starts with the settings it read, for example `setting: IMPORT_DELETE_BUNDLES is not set in this job (environment dev): imported bundles stay in the store`, and each bundle ends with `deleted from the store`, `nothing to delete in the store` or `kept in the store (IMPORT_DELETE_BUNDLES is not true)`.

## Both sides

| Req | Name | Default | Purpose |
|---|---|---|---|
| Optional | `MIRROR_JOB_TAG` | empty (untagged) | Runner tag for every job, for example `shell` |
| Optional | `CA_BUNDLE` | runner's `tls-ca-file` | Extra CA for the GitLab API |
| Optional | `MIRROR_VERBOSE` | `false` | Print every skopeo and curl command |
| Outside CI | `GITLAB_API_URL`, `PACKAGE_PROJECT`, `PACKAGE_TOKEN`, `MIRROR_LEDGER` | the job's values in CI | Package registry for the ledger and receipt |
| Outside CI | `MIRROR_STATE_DIR`, `IMPORT_STATE_DIR` | `~/.local/state/registry-mirror[-import]` | Work directories |

`python3 mirror.py -h` and `python3 mirror.py <command> -h` list every command with examples.

## Resend

The high side cannot ask for a bundle over a one-way link, so a lost one is resent from the low side. Import names it: `missing earlier bundle 7 to 8 (imported up to 6, received 9); on the low side, Run pipeline with RESEND_SEQUENCE=7..`. Run pipeline on the low project with that variable; `RESEND_SINCE=YYYY-MM-DD`, `RESEND_IMAGE=repo[:tag]` and `RESEND_ALL=true` work the same way. `docs/ledger.md` explains the ledger, the receipt and every recovery case.

## Preconditions

- Each target registry has the repositories or a namespace the push account may create them in (a Quay Creator team, a Harbor project robot, a GitLab project's container registry, an Artifactory Docker repository).
- Protect the default branch. Merge-request pipelines run the tests and scan without registry credentials.
- Turn on **Pipelines must succeed** so a failed `scan` blocks the merge, and add a nightly schedule on the default branch.
- The `scan` runner reaches `grype.anchore.io`, or `GRYPE_DB_UPDATE_URL` points at a mirror. On a shell runner `sbom` and `scan` run their images with `podman run` and need about 2 GB free in the build directory.
- Quay refuses Windows manifests; set `MIRROR_PLATFORM` for images with Windows children such as `registry.k8s.io/pause`.

## Behaviour

- Nothing persists on a runner. The ledger (`registry-mirror-ledger`), SBOMs (`registry-mirror-sbom`) and receipt (`registry-mirror-receipt`) live in the package registry or the bucket; images live in the registries.
- A bundle is recorded as sent only once NiFi accepts it or it is written to `MIRROR_BUNDLE_DIR`. With neither, `sync` updates the low registry and prints `not sent to the high side`.
- `sync` and `resend` run only where `TARGET_REGISTRY` is set, so the source project runs the tests and scan only.
- Import refuses a replayed older bundle and a gap in the sequence, and reports a duplicate trigger as `already imported`.

## Out of scope

- Removing old tags, mirroring every source tag, or deploying charts.
- Signatures and cross-domain approval: a checksum proves integrity, not who sent the bundle.
- Configuring the transfer link, GitLab schedules or the Renovate service.
