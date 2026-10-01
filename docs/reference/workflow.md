---
title: Image mirror workflow
type: reference
status: implemented
tags: [quay, oci, gitlab-ci, offline-transfer]
updated: 2026-10-01
---

# Image mirror workflow

One image catalog is the operational boundary. Upstream registries supply immutable content; low and high Quay retain tagged copies. The pipeline does not infer an inventory by listing a proxy cache and does not read a Dockerfile.

Renovate proposes version/digest edits in `images.txt`. An administrator reviews and merges them. Default-branch pushes run sync; a nightly schedule reconciles the same approved set and repairs low-side copies. A nightly run does not mean “approve the latest version”. Adding an image uses `mirror.py add`, followed by the same review path.

## Helm

Render a pinned chart with the values, Kubernetes version and API capabilities used by the deployment. Inspect `image` fields in pod templates, init containers and hooks. Record each resulting registry reference in the catalog. Chart-generated or dynamically looked-up resources need explicit review. A chart's version and `appVersion` do not reliably identify all dependent container images.

A Helm repository may propose a catalog change after its Renovate chart update is merged. Keep that proposal as a normal reviewed image-list change. Cross-project trigger credentials, chart discovery and deployment ownership are outside this small repository.

## Transfer and recovery

A bundle holds one skopeo `dir:` copy per image digest and `images.json`. An `oci:` layout cannot hold a Docker manifest list with its digest preserved, because skopeo must convert the list to an OCI index; `dir:` stores the manifest bytes unchanged. The metadata declares a sender stream, increasing sequence number, full/delta flag and source/destination/digest mappings. `transfer` is the digest carried: the approved digest, or its `MIRROR_PLATFORM` child. Files are written to temporary names; the final checksum file is the readiness marker.

The sender ledger records, per catalog tag, the digest sent and the version tags sent with a `latest` image. A version tag that appears later, for example after an upgrade or once the image reports one, enters the next delta. Every sync reads each low-side tag back, version tags included, and restores a missing one from the image already in low Quay.

The sender reserves its sequence before publication. An interrupted export or failed HTTP delivery forces the next export to be full. The sender ledger records export, never proof of high-side receipt. A lost bundle therefore requires `sync --full`. A full bundle carries every current approved entry and may bridge a sequence gap.

Import validates every archive path, metadata entry, manifest, config and layer before any registry push. It then copies each image with digest preservation, reads the high-side manifests back and commits the receipt only after all pushes verify. A partially failed import can be retried. The registry may contain some completed copies, but no successful receipt is recorded prematurely.

An image entry without `tags`, written by a sender from before version tags, imports under its catalog tag alone. An importer from before version tags refuses an entry that has `tags` with `invalid image entry`, so update the high side first; the refused bundle imports once it is updated.

Identical retransmission is a no-op. Older or conflicting sequences cannot roll tags back. A different stream requires explicit adoption of a reviewed full bundle. Sender state, receipt state and outbox files are persistent operational data, not GitLab caches.

No tag deletion is implied by catalog removal. Removal stops future updates; existing low/high tags remain. A `latest` image whose reported version names a tag a catalog line pins, or pinned before it was removed, does not take that tag. The sender ledger records each catalog tag before sync first copies it, so this holds even when that sync failed. This avoids turning an incomplete catalog or damaged transfer into a deletion instruction. Administrators retain control of registry retention.

## CI boundary

GitLab runs the production schedule on protected shell runners with skopeo and Python. Verification runs without registry credentials. There is no published shared-CI composition for this exact self-contained, file-transfer protocol; the existing shared container-mirror composition instead selects RKE2 image lists and uses an object store/receiver inventory. This repository owns its small pipeline and CLI.

GitHub Actions validates code and runs the complete CLI transfer/recovery scenario against disposable OCI registries. `tests/e2e.py` also accepts real Quay endpoints and auth-file paths, using unique repositories under `E2E_ORG`. These tests push test images and retain their tags; use a disposable organisation with an appropriate retention policy.

GitHub does not schedule production mirroring into private registries. Import the repository into GitLab and configure the documented protected runners, variables and schedule.

## References

- [Skopeo copy: all platforms and digest preservation](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md)
- [Renovate Docker versions and digests](https://docs.renovatebot.com/docker/)
- [Renovate Helm values manager](https://docs.renovatebot.com/modules/manager/helm-values/)
- [Skopeo dir transport](https://github.com/containers/image/blob/main/docs/containers-transports.5.md)

## Verification

Verified on 2026-09-30:

- Python unit tests cover catalog validation, archive traversal/duplicate/link rejection, OCI graph integrity, sequence gaps, replay refusal, full recovery and exclusive sender locking.
- A real Alpine 3.20.3 image traversed upstream Docker Hub, two Quay 3.15.7 instances and a filesystem transfer. Its multi-platform index remained `sha256:1e42bbe2508154c9126d48c2b8a75420c3544343bf86fd041fb7527e017a4b4a` at both registry destinations.
- `E2E_FIXTURES=true python3 tests/e2e.py` passed against the two Quays with reproducible `amd64`/`arm64` images. It exercised version 1.0.0 to 1.0.1, no-change sync, a lost delta, full recovery, replay/corruption rejection and inbox completion. Fixtures avoid public-registry rate limits in CI.
- Renovate 44.121.4 validated the configuration and extracted the active catalog reference, tag and digest in a local extraction-only run.
- GitLab's project CI Lint API accepted both pipeline files without warnings. This is syntax/merged-config evidence, not execution of a scheduled GitLab job.

Verified on 2026-10-01, Quay 3.15.7 low and high, Skopeo 1.22.2, Python 3.13, as a non-root Linux user whose only credentials came from `skopeo login`:

- `tests/e2e.py` passed with `E2E_FIXTURES=true`, and with real Docker Hub `prom/prometheus` v3.13.3 then v3.13.4. Prometheus is a Docker manifest list, the format the `oci:` layout failed on; all six platforms and the index digest reached high Quay. Destinations were nested (`<org>/<path>/image`).
- `MIRROR_PLATFORM=linux/amd64 tests/matrix.py` passed for ten references: `prom/prometheus` (Docker list), `alpine` (OCI index with attestations), `quay.io/prometheus/node-exporter`, `ghcr.io/stefanprodan/podinfo`, `registry.k8s.io/pause` (Windows children), `amd64/alpine`, `bitnami/redis:latest` and `bitnami/redis:latest` pinned to an older digest. Two further references, a `sha256-<hex>` tag and a `.sig` tag, copied with matching digests but are signature artifacts, not images; `add` now refuses them.
- All platforms passed for `alpine` and old `bitnami/redis:latest`. `registry.k8s.io/pause` failed at low Quay with `manifest invalid` on its Windows child.
- `bitnami/redis:latest` pinned to an older digest, then updated as Renovate would: both Quays held `latest` and `8.10.2` on the new image, with `8.10.0` still on the old one. Podman pulled `latest` and `8.10.0` from high Quay as `linux/amd64` and ran `redis-server` (8.10.2 and 8.10.0); `redis-cli ping` returned `PONG`.
- Renovate 44.112.3 with `renovate.json`, `--platform=local --dry-run=lookup`: an old `bitnami/redis:latest` digest received a digest update to the current `latest`, and `prom/prometheus:v3.13.3` a minor update to v3.15.0.

The test transfer uses a filesystem inbox, not a physical diode. Production schedules, scoped robot credentials, protected runners and transfer pickup remain operator configuration. `NIFI_URL` delivery is optional; the Quay proof does not claim a live NiFi delivery.
