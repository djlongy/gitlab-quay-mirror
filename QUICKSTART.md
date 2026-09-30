## What this is

`gitlab-quay-mirror/images.txt` is your reviewed image list. `gitlab-quay-mirror/.gitlab-ci.yml` mirrors it into low Quay and writes changed-image archives; `gitlab-quay-mirror/mirror.py` imports them into high Quay. Run commands from your checkout with Python 3.11+ and skopeo installed. The workflow preserves every platform and digest; it does not deploy charts or approve updates for you.

## How to use it

1. Add an image: `python3 mirror.py add docker.io/library/alpine:3.20.3 mirror/library--alpine`
2. Submit `gitlab-quay-mirror/images.txt` as a merge request in your GitLab copy for review.
3. In **Settings > CI/CD > Variables**, set `LOW_QUAY`, file variable `LOW_AUTH_FILE` scoped to `mirror-low`, and persistent paths `MIRROR_WORK` and `MIRROR_OUTBOX`.
4. Create a nightly **Build > Pipeline schedules** entry for the protected default branch.
5. Select **Build > Pipelines > Run pipeline** on the default branch.
6. Transfer the `.tar` and matching `.sha256` from `MIRROR_OUTBOX` to the high-side inbox.
7. With `HIGH_QUAY`, `HIGH_AUTH_FILE` and persistent `IMPORT_WORK` set on the high-side host, run `python3 mirror.py import --inbox /path/to/inbox`.

You know it works when high-side import prints `digests verified` and the destination serves the catalog's digests.

If it fails:
- A bundle is missing: run `python3 mirror.py sync --full` on the low side and transfer the new pair.
- A new sender replaces lost state: inspect its full bundle, then run `python3 mirror.py import --adopt-stream /path/to/full-bundle.tar` on the high side.
