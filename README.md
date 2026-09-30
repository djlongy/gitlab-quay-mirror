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
| For sync | `LOW_QUAY` | none | Low hosted registry, as `host[:port]` |
| For import | `HIGH_QUAY` | none | High hosted registry, as `host[:port]` |
| In low CI | `MIRROR_WORK` | `.mirror-work/` locally | Persistent sender ledger; one writer per directory |
| In low CI | `MIRROR_OUTBOX` | `delta/` locally | Persistent transfer pickup directory |
| In high CI | `IMPORT_WORK` | `.import-work/` locally | Persistent receipt ledger and replay guard |
| In high CI | `MIRROR_INBOX` | none | Directory holding transferred archive/checksum pairs |
| Optional | `MIRROR_FULL` | `false` | Set `true` on Run pipeline to resend every approved image |
| Optional | `NIFI_URL` | none | HTTP(S) listener receiving both files by POST with a `Filename` header |

## Secrets

Use protected GitLab **File** variables containing skopeo auth JSON, scoped to environment `mirror-low` or `mirror-high`. The script receives a file path; verification jobs receive no registry secrets.

| Variable | When | Purpose |
|---|---|---|
| `UPSTREAM_AUTH_FILE` | Private or rate-limited upstreams | Pull credentials for upstream registries |
| `LOW_AUTH_FILE` | Sync | Push/pull credentials scoped to the low target organisation |
| `HIGH_AUTH_FILE` | Import | Push/pull credentials scoped to the high target organisation |

Locally, create each file with `skopeo login --authfile /path/to/auth.json <registry>`.
Keep credentials out of `images.txt`, Git and transfer bundles.

## Minimum configuration

Your `images.txt` has one pinned upstream image and one destination per line:

```text
docker.io/library/alpine:3.20.3@sha256:1e42bbe2508154c9126d48c2b8a75420c3544343bf86fd041fb7527e017a4b4a mirror/library--alpine
```

Both Quays use the destination `mirror/library--alpine:3.20.3`. Quay targets have exactly one slash.
`python3 mirror.py add <registry/repo:tag> <org/repo>` resolves and appends the digest for you.

## Usage

```sh
python3 mirror.py sync
```

## Preconditions

- Create the target organisation on each Quay. Put its robot in a Creator team so new catalog repositories can be created, or pre-create repositories and grant the robot push/pull access.
- Protect the default branch and mirror runner. Merge-request verification uses no mirror credentials or persistent state.
- Enable Renovate on the GitLab copy. It queries upstream registries, proposes tag/digest changes in `images.txt`, and requires your review before merge.
- Create a nightly GitLab schedule against the protected default branch. Schedules mirror the reviewed catalog; they do not approve Renovate updates.
- Mount persistent sender, outbox and receipt directories. Use separate sender state for separate low registries.
- Provision registry trust through the host's containers certificate configuration; keep TLS verification enabled outside disposable labs.

## Behaviour

Default-branch catalog/script/pipeline changes, schedules and Run pipeline run sync after verification.
Every sync verifies tagged low-side copies and copies new or changed approved digests; unchanged entries need no upstream pull.
Only changed target/tag/digest entries enter the archive. Each archive contains complete image content for those entries, including all platforms.

Carry the `.tar` and matching `.sha256` together. Import checks the archive, every referenced OCI blob and expected image digest before pushing.
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
