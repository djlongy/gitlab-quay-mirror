# gitlab-registry-mirror

Mirrors reviewed container images and Helm charts into a registry on a connected low side, then carries them in verified bundles across a one-way link into a registry on an isolated high side. Any OCI registry works on either side: Quay, Harbor, GitLab, Artifactory, Nexus or `registry:2`. [Quickstart](QUICKSTART.md).

Flow: `SOURCE_REGISTRY` (Docker Hub, quay.io, ...) to the low `TARGET_REGISTRY` and a bundle of what changed (`sync`), bundle to the low NiFi (`send`), across the link, NiFi files it in `IMPORT_STORE`, then `import` pushes it to the high `TARGET_REGISTRY`. Each project sets `TARGET_REGISTRY` to its own side's registry. `docs/dataflow.md` walks through every step.

## Requirements

- On each side's runner: `python3` 3.9+, `skopeo` 1.13+ and `curl`. In the job image on a Docker runner, on the host on a shell runner. No Docker daemon or privileged runner.
- One GitLab project per side. The low project uses `.gitlab-ci.yml`; the high project sets **Settings > CI/CD > CI/CD configuration file** to `.gitlab-ci-high.yml`.
- For the hands-off path, NiFi 2.x on each side of the link.

## Low side: pull, save, send

`sync` copies each `images.txt` entry from its source into `TARGET_REGISTRY` by digest and writes what changed as a bundle in an outbox; `send` delivers the outbox to the low NiFi and empties it. The pipeline runs both, one after the other, so you can see and change each step in `.gitlab-ci.yml`. Set these in **Settings > CI/CD > Variables**; mask the secrets and scope them to environment `mirror-low`.

| Req | Name | Default | Purpose |
|---|---|---|---|
| Required | `TARGET_REGISTRY` | none | Low registry, `host[:port]` |
| Required | `TARGET_REGISTRY_USERNAME`, `TARGET_REGISTRY_PASSWORD` | none | Account that can push; leave unset for a registry without auth |
| Required | `MIRROR_SEND` | `none` | How images leave: `none` (sync `TARGET_REGISTRY` only; carry with [Export and load](#export-and-load) by hand), `export` (sync, then export what the run added as one tar into `MIRROR_SEND_PATH`), or bundles by `nifi`, `dir` or `s3`. See [Send](#send) |
| `MIRROR_SEND=nifi` | `NIFI_URL` | none | NiFi ListenHTTP the bundles are posted to |
| `MIRROR_SEND=export` or `dir` | `MIRROR_SEND_PATH` | none | Directory the low NiFi lists, e.g. an NFS share at `/mnt/transfer` mounted on the runner; it must exist |
| `MIRROR_SEND=export` | `MIRROR_STAGING_PATH` | `MIRROR_SEND_PATH/.staging` | Where the tar is built before it moves into `MIRROR_SEND_PATH`; same filesystem |
| `MIRROR_SEND=s3` | `S3_ENDPOINT`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`; optional `S3_PREFIX`, `S3_REGION`, `S3_CA_BUNDLE`, `S3_TLS_VERIFY` | none | The low-side bucket the low NiFi lists |
| Optional | `TARGET_REGISTRY_TLS_VERIFY` | `true` | `false` only for a plain-HTTP lab registry |
| Optional | `SOURCE_REGISTRY`, `SOURCE_REGISTRY_USERNAME`, `SOURCE_REGISTRY_PASSWORD`, `SOURCE_REGISTRY_TLS_VERIFY` | none | Login and TLS setting for one source registry, for example `docker.io` against rate limits; other source hosts pull anonymously with TLS verified |
| Optional | `MIRROR_PLATFORM` | all platforms | One platform, for example `linux/amd64` |
| Optional | `MIRROR_SCAN`, `GRYPE_FAIL_ON` | `true`, `critical` | Syft SBOM and Grype gate before `sync`; `false` skips both |
| Optional | `SYFT_IMAGE`, `GRYPE_IMAGE` | `docker.io/anchore/syft:v1.54.0-debug`, `docker.io/anchore/grype:v0.120.0-debug` | Scanner images |
| Optional | `MIRROR_BUNDLE_DIR` | `outbox/` in `MIRROR_STATE_DIR` | Outbox when `--out` is not given (the pipeline passes `--out outbox`) |
| Optional | `MIRROR_BLOB_DELTA` | `false` | `true` leaves out of each delta bundle the layers and configs the high side already has, and any second copy within the bundle. See [Blob delta](#blob-delta) |
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

You know it works when the `sync` log shows `pushed to target registry: <host>/team/prometheus:v3.13.4@sha256:...` and the `send` log shows `posted to NiFi: mirror-<stream>-<n>.tar -> <NIFI_URL> (HTTP 200, ...)` (or `dropped for NiFi: ...`), then `sent: mirror-<stream>-<n>.tar`.

### Send

`sync --out outbox` writes each bundle as `mirror-<stream>-<n>.tar` (or `.part-001`, `.part-002`... when
it is over `MIRROR_BUNDLE_MAX_SIZE`), its `.sha256`, and last a `.send.json` that marks it complete.
`send <method> outbox` delivers every complete bundle, oldest first, and deletes each file once it is
delivered, recording each in the `.send.json`. A send that stops part-way leaves only what it has not
delivered: run it again. The ledger lists every bundle written and not yet sent and stays pending
while that list is not empty, so if the outbox is lost (a failed CI job) the next `sync` is full and
the high side catches up; a full bundle or a resend that bridges replaces the list. `sync` refuses an
outbox that still holds unsent bundles. Hand-carry counts as sent once copied: a lost USB stick shows
up as a gap on the high side, recovered with a resend.

```sh
python3 mirror.py sync --out outbox
python3 mirror.py send nifi --url https://nifi.low:9443/contentListener outbox   # default NIFI_URL
python3 mirror.py send dir --path /mnt/transfer outbox                             # NFS share for NiFi
python3 mirror.py send --path /mnt/transfer outbox                                 # the same: --path implies dir
python3 mirror.py send s3 --bucket transfer --prefix mirror outbox                 # S3_* and AWS_* env
python3 mirror.py send dir --path /media/usb --format tar outbox                   # hand-carry
```

Flags win over the environment; the environment only fills in what a flag leaves out.

| Method | What arrives | Low NiFi flow |
|---|---|---|
| `nifi` | HTTP POST, the attributes as `X-` headers | `flow.py --side low`: ListenHTTP, PackageFlowFile, PutFile into the link |
| `dir` | `<file>.ffv3`: the file packaged in NiFi's FlowFile v3 format with `filename` and the same `X-` attributes, written as `.ffv3.partial` and renamed when complete | `flow.py --side low --source dir --param mirror.drop=/mnt/transfer`: ListFile `mirror-*.ffv3`, FetchFile, PutFile into the link, then DeleteFile |
| `s3` | `<S3_PREFIX>/<file>.ffv3`, the same package, with the attributes also as S3 user metadata | `flow.py --side low --source s3 --param s3.endpoint=... --param s3.bucket=... [--param s3.prefix=...]`: ListS3, FetchS3Object, PutFile into the link, then DeleteS3Object |
| `dir --format tar` | the plain bundle and its `.sha256`, for `import --inbox` | none: carried by hand. Needs whole bundles, so set `MIRROR_BUNDLE_MAX_SIZE` above the largest |

- The high side is the same for every method: the attributes cross the link inside the FlowFile package.
- To see the attributes on the low side too, and check each file there before it enters the link, add
  `--check` to `flow.py --side low --source dir|s3`: UnpackContent, CryptographicHashContent, a route
  on `X-Artifact-Type` and `X-Sha256`, then PackageFlowFile again. A file that fails waits in
  "Rejected (inspect queue)" and stays in the drop. With `s3` the attributes are also the object's
  user metadata, which FetchS3Object turns into attributes without unpacking.
- `dir`: mount the share on the runner (a shell runner, or a `volumes` entry in a Docker runner's
  config) and in NiFi. NiFi's user must be able to read and delete the files.
- `s3`: `flow.py` reads the keys from `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`; `s3.ca=@ca.pem`
  only for a private CA. NiFi's ListS3, FetchS3Object and DeleteS3Object use path-style requests
  when an endpoint is set.
- One file per bundle: set `MIRROR_BUNDLE_MAX_SIZE` above the largest image (for example `100GiB`).
  Keep it under what the high store takes: a GitLab generic package is 5 GiB by default, S3 has no
  practical limit.

### Blob delta

By default a bundle carries every changed image whole. With `MIRROR_BLOB_DELTA=true` (or
`sync --blob-delta`) the ledger remembers every layer and config each bundle carried, and the
newest image that holds it. A later delta bundle then leaves out a blob the high side already has,
and a second copy of one in the same bundle; `images.json` (schema 2) names, for each left-out
blob, the image to read it from: one already in the high registry, or another image in the bundle.
`import` reads those images back from the high `TARGET_REGISTRY` into the job, puts each blob in
place, and checks every file against its digest as before.

- An app rebuilt on the same base sends only its own layers. In the lab, a 46 MB image rebuilt on
  the same python base crossed as a 2 MB bundle; two node images on one base crossed as one copy.
- Full bundles (the first, `RESEND_ALL`, recovery) and resends always carry every blob, so they
  are also how the high side recovers.
- The high import account needs read access to the images it pushed. If the high registry has
  expired or deleted one (tag expiry, retention, garbage collection), import fails and names it:
  Run pipeline on the low side with `RESEND_ALL=true`.
- `IMPORT_PATH_REWRITE` applies to those reads too. An importer from before this release refuses
  a schema 2 bundle rather than pushing an incomplete image: upgrade the high side first.

### Export and load

For hand-carry, `export` copies images that are already in a registry into one directory, and optionally one tar with a `.sha256`. It does not sync, pull from upstream or touch the ledger, so it runs as fast as the registry and disk allow: sync first (`MIRROR_SEND=none` in the pipeline), export when you need a carry. Each image is a byte-for-byte `dir:` copy, so tags, digests and Docker manifest lists or OCI indexes are kept, and a layer shared by several images is hardlinked and stored once.

```sh
TARGET_REGISTRY=quay.low.example.com mirror.py export --out /share/export --tar /share/export.tar   # the catalog
mirror.py export --out /share/export team/prometheus:v3.13.4 team/grafana       # in TARGET_REGISTRY: a tag, every tag
mirror.py export --out /share/export quay.low.example.com/team/app:v1.2.3       # any registry, full reference
mirror.py export --out /share/export --tar /share/carry.tar --list carry.txt    # a batch: one reference per line
```

On the high side, `load` checks the tar against its `.sha256`, pushes every image to the same path in `TARGET_REGISTRY` and verifies each digest there. An image already present at its digest is skipped, so a rerun is cheap. A tag that points at a different digest is refused unless you pass `--force`.

```sh
TARGET_REGISTRY=quay.high.example.com mirror.py load /media/usb/export.tar
```

With `MIRROR_SEND=export` the pipeline runs `mirror.py carry --path "$MIRROR_SEND_PATH"`: it syncs `TARGET_REGISTRY`, exports the catalog images not carried yet (every one with `RESEND_ALL=true`) as `mirror-export-<UTC time>-<pipeline>.tar` in `MIRROR_SEND_PATH/.staging`, then renames the tar and, last, its `.sha256` into `MIRROR_SEND_PATH`. `MIRROR_STAGING_PATH` (`carry --staging`) puts the build directory elsewhere; it must be on the same filesystem as `MIRROR_SEND_PATH`, or carry refuses to start. The ledger records an image as carried only after both files are in place, so a failed run carries it again next time, and a failed run leaves nothing behind in `.staging`. A NiFi `ListFile` on that path therefore never sees half a file; set its **Recurse Subdirectories** to `false` so it does not list `.staging`. On the high side the tar lands wherever NiFi puts it, and `load` is run by hand.

An export is not a bundle: `import` does not read it, and it carries no sequence or ledger state. The `.sha256` catches corruption in transit, not deliberate tampering, the same trust as any hand-carried media.

A registry that is neither `TARGET_REGISTRY` nor `SOURCE_REGISTRY` is read with TLS verified and the system trust store.

sync, send, carry, export and load print the directories they write to before they start: work and ledger, outbox, staging, export, tar, skopeo scratch and where a tar unpacks. Each has a flag: `--state-dir` (any command), `sync --out`, `send --path`, `carry --path --staging`, `export --out --tar --tmp`, `load --scratch`.

`--state-dir DIR` (any command) moves the work directory, staging and default outbox off `~/.local/state`, for a host whose home disk is small. `load` unpacks a tar into it, else beside the tar.

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
- `sync` records a bundle in the ledger when it writes it; the ledger stays pending until `send` empties the outbox. `sync --low-only` updates the low registry, records nothing and prints `not bundled (--low-only)`.
- `sync` and `resend` run only where `TARGET_REGISTRY` is set, so the source project runs the tests and scan only.
- Import refuses a replayed older bundle and a gap in the sequence, and reports a duplicate trigger as `already imported`.

## Out of scope

- Removing old tags, mirroring every source tag, or deploying charts.
- Signatures and cross-domain approval: a checksum proves integrity, not who sent the bundle.
- Configuring the transfer link, GitLab schedules or the Renovate service.
