---
title: Image mirror workflow
type: reference
status: implemented
tags: [quay, oci, gitlab-ci, offline-transfer]
updated: 2026-09-30
---

# Image mirror workflow

One image catalog is the operational boundary. Upstream registries supply immutable content; low and high Quay retain tagged copies. The pipeline does not infer an inventory by listing a proxy cache and does not read a Dockerfile.

Renovate proposes version/digest edits in `images.txt`. An administrator reviews and merges them. Default-branch pushes run sync; a nightly schedule reconciles the same approved set and repairs low-side copies. A nightly run does not mean “approve the latest version”. Adding an image uses `mirror.py add`, followed by the same review path.

## Helm

Render a pinned chart with the values, Kubernetes version and API capabilities used by the deployment. Inspect `image` fields in pod templates, init containers and hooks. Record each resulting registry reference in the catalog. Chart-generated or dynamically looked-up resources need explicit review. A chart's version and `appVersion` do not reliably identify all dependent container images.

A Helm repository may propose a catalog change after its Renovate chart update is merged. Keep that proposal as a normal reviewed image-list change. Cross-project trigger credentials, chart discovery and deployment ownership are outside this small repository.

## Transfer and recovery

A bundle holds an OCI image layout and `images.json`. The metadata declares a sender stream, increasing sequence number, full/delta flag and source/destination/digest mappings. Files are written to temporary names; the final checksum file is the readiness marker.

The sender reserves its sequence before publication. An interrupted export or failed HTTP delivery forces the next export to be full. The sender ledger records export, never proof of high-side receipt. A lost bundle therefore requires `sync --full`. A full bundle carries every current approved entry and may bridge a sequence gap.

Import validates every archive path, metadata entry, referenced blob size/hash and image-root digest before any registry push. It then copies all platforms with digest preservation, reads the high-side manifests back and commits the receipt only after all pushes verify. A partially failed import can be retried. The registry may contain some completed copies, but no successful receipt is recorded prematurely.

Identical retransmission is a no-op. Older or conflicting sequences cannot roll tags back. A different stream requires explicit adoption of a reviewed full bundle. Sender state, receipt state and outbox files are persistent operational data, not GitLab caches.

No tag deletion is implied by catalog removal. Removal stops future updates; existing low/high tags remain. This avoids turning an incomplete catalog or damaged transfer into a deletion instruction. Administrators retain control of registry retention.

## CI boundary

GitLab runs the production schedule on protected shell runners with skopeo and Python. Verification runs without registry credentials. There is no published shared-CI composition for this exact self-contained, file-transfer protocol; the existing shared container-mirror composition instead selects RKE2 image lists and uses an object store/receiver inventory. This repository owns its small pipeline and CLI.

GitHub Actions validates code and runs the complete CLI transfer/recovery scenario against disposable OCI registries. `tests/e2e.py` also accepts real Quay endpoints and auth-file paths, using unique repositories under `E2E_ORG`. These tests push test images and retain their tags; use a disposable organisation with an appropriate retention policy.

GitHub does not schedule production mirroring into private registries. Import the repository into GitLab and configure the documented protected runners, variables and schedule.

## References

- [Skopeo copy: all platforms and digest preservation](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md)
- [Renovate Docker versions and digests](https://docs.renovatebot.com/docker/)
- [Renovate Helm values manager](https://docs.renovatebot.com/modules/manager/helm-values/)
- [OCI image layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)

## Verification

Verified on 2026-09-30:

- Python unit tests cover catalog validation, archive traversal/duplicate/link rejection, OCI graph integrity, sequence gaps, replay refusal, full recovery and exclusive sender locking.
- A real Alpine 3.20.3 image traversed upstream Docker Hub, two Quay 3.15.7 instances and a filesystem transfer. Its multi-platform index remained `sha256:1e42bbe2508154c9126d48c2b8a75420c3544343bf86fd041fb7527e017a4b4a` at both registry destinations.
- `E2E_FIXTURES=true python3 tests/e2e.py` passed against the two Quays with reproducible `amd64`/`arm64` images. It exercised version 1.0.0 to 1.0.1, no-change sync, a lost delta, full recovery, replay/corruption rejection and inbox completion. Fixtures avoid public-registry rate limits in CI.
- Renovate 44.121.4 validated the configuration and extracted the active catalog reference, tag and digest in a local extraction-only run.
- GitLab's project CI Lint API accepted both pipeline files without warnings. This is syntax/merged-config evidence, not execution of a scheduled GitLab job.

The test transfer uses a filesystem inbox, not a physical diode. Production schedules, scoped robot credentials, protected runners and transfer pickup remain operator configuration. `NIFI_URL` delivery is optional; the Quay proof does not claim a live NiFi delivery.
