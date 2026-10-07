## What this is

`images.txt` is the reviewed image list. The low project's pipeline (`.gitlab-ci.yml`) copies it into your low registry and sends bundles of what changed to NiFi; the high project's pipeline (`.gitlab-ci-high.yml`) pushes them into your high registry. Any OCI registry works on either side. It does not approve updates or deploy anything.

## How to use it

Low side:

1. Add an image: `python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus`, then merge `images.txt`.
2. In **Settings > CI/CD > Variables**, set `TARGET_REGISTRY`, `NIFI_URL`, and masked `TARGET_REGISTRY_USERNAME` and `TARGET_REGISTRY_PASSWORD` scoped to `mirror-low`.
3. Select **Build > Pipelines > Run pipeline** on the default branch.

High side:

4. Create a project from this repository and set its CI/CD configuration file to `.gitlab-ci-high.yml`.
5. Set `TARGET_REGISTRY`, and masked `TARGET_REGISTRY_USERNAME` and `TARGET_REGISTRY_PASSWORD` scoped to `mirror-high`. For S3, also `IMPORT_STORE=s3`, `S3_ENDPOINT`, `S3_BUCKET`, the `AWS_*` keys and `S3_CA_BUNDLE`.
6. Build the NiFi flow by hand from `nifi/HIGH-SIDE-BY-HAND.md`.

You know it works when the low `sync` log shows `posted to NiFi: ... (HTTP 200` and the high `import` log shows `pushed to target registry: <host>/team/prometheus:v3.13.4@sha256:...`.

If it fails:
- Import names a missing bundle: Run pipeline on the low project with `RESEND_SEQUENCE=<N>..` as printed.
- A job says a variable is renamed: rename that CI variable as the message says.
