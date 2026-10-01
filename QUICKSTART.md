## What this is

`gitlab-quay-mirror/images.txt` is your reviewed image list. `gitlab-quay-mirror/.gitlab-ci.yml` mirrors it into low Quay and writes changed-image archives; `gitlab-quay-mirror/mirror.py` imports them into high Quay. Run commands from your checkout with Python 3.11+ and skopeo installed. The workflow preserves every platform and digest; it does not deploy charts or approve updates for you.

## How to use it

1. Log in to the upstream registry if it is private, then add an image: `python3 mirror.py add prom/prometheus:v3.13.4 team-dev/prom/prometheus`
2. Submit `gitlab-quay-mirror/images.txt` as a merge request in your GitLab copy for review.
3. In **Settings > CI/CD > Variables**, set `LOW_QUAY_HOST`, and masked `LOW_QUAY_USERNAME` and `LOW_QUAY_PASSWORD` scoped to `mirror-low`. For amd64 only, add `MIRROR_PLATFORM=linux/amd64`.
4. Create a nightly **Build > Pipeline schedules** entry for the protected default branch.
5. Select **Build > Pipelines > Run pipeline** on the default branch.
6. Transfer the `.tar` and matching `.sha256` from the runner's `~/.local/state/quay-mirror/bundles/` to the high-side inbox.
7. On the high side, run `skopeo login <high-quay>`, set `HIGH_QUAY_HOST`, then run `python3 mirror.py import --inbox /path/to/inbox`.

You know it works when high-side import prints `digests verified` and the destination serves the catalog's digests.

If it fails:
- A bundle is missing: run `python3 mirror.py sync --full` on the low side and transfer the new pair.
- A new sender replaces lost state: inspect its full bundle, then run `python3 mirror.py import --adopt-stream /path/to/full-bundle.tar` on the high side.
