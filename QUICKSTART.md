## What this is

`images.txt` is the reviewed image list. The low project's pipeline (`.gitlab-ci.yml`) copies it into your low registry. What happens next depends on `MIRROR_SEND`:

| `MIRROR_SEND` | The low pipeline | The high side |
|---|---|---|
| `none` (default) | syncs the low registry only | you carry images by hand: `mirror.py export` on the low side, `mirror.py load` on the high side |
| `export` | syncs, then writes the new images as one `.tar` and `.sha256` into `MIRROR_SEND_PATH` (built in `MIRROR_STAGING_PATH`, default `MIRROR_SEND_PATH/.staging`) | NiFi carries the tar; you run `mirror.py load <tar>` |
| `nifi`, `dir`, `s3` | syncs and sends sequenced bundles | the high pipeline (`.gitlab-ci-high.yml`) imports them |

Any OCI registry works on either side. It does not approve updates or deploy anything. sync, send, carry, export and load print the directories they write to before they start, and each has a flag (`--state-dir`, `--tar`, `--staging`, `--out`, `--tmp`, `--scratch`). Only the pipeline dedupes; a manual export is fresh every time.

## How to use it

Low side:

1. Add an image: `python3 mirror.py add docker.io/prom/prometheus:v3.13.4 team/prometheus`, or many at once from `.txt` lists: `python3 mirror.py add-list lists/ --prefix team`. Then merge `images.txt`.
2. In **Settings > CI/CD > Variables**, set `TARGET_REGISTRY` and masked `TARGET_REGISTRY_USERNAME` and `TARGET_REGISTRY_PASSWORD` scoped to `mirror-low`. Then pick `MIRROR_SEND` from the table above: for `export`, set `MIRROR_SEND_PATH` (for example `/mnt/transfer`, mounted on the runner) and optionally `MIRROR_STAGING_PATH`, and set the low NiFi `ListFile` on that path to **Recurse Subdirectories** `false`; for `nifi`, `NIFI_URL`; for `dir` or `s3`, see README, Send.
3. Select **Build > Pipelines > Run pipeline** on the default branch.

By hand, on any host with skopeo (the low side, then the high side):

```sh
export TARGET_REGISTRY=quay.low.example.com
python3 mirror.py --state-dir /share/mirror sync --low-only           # export reads the low registry: sync first
python3 mirror.py export --tar /share/carry.tar --staging /share/tmp --list carry.txt   # one reference per line
# one image the high side is missing: python3 mirror.py export --tar /share/fix.tar team/app:v1.2.3
# carry carry.tar and carry.tar.sha256 together; load refuses a tar without its .sha256
TARGET_REGISTRY=quay.high.example.com python3 mirror.py load /media/usb/carry.tar --scratch /share/unpack
```

High side, for `nifi`, `dir` or `s3` bundles:

4. Create a project from this repository and set its CI/CD configuration file to `.gitlab-ci-high.yml`.
5. Set `TARGET_REGISTRY`, and masked `TARGET_REGISTRY_USERNAME` and `TARGET_REGISTRY_PASSWORD` scoped to `dev`. For S3, also `IMPORT_STORE=s3`, `S3_ENDPOINT`, `S3_BUCKET`, the `AWS_*` keys and `S3_CA_BUNDLE`.
6. Build the NiFi flow by hand from `nifi/HIGH-SIDE-BY-HAND.md`.
7. Optional, a prod registry behind an approval: set `MIRROR_PROMOTE=true` and the `prod`-scoped variables in the README's "Promote dev to prod". Each pipeline then waits for a Maintainer to run `promote`.

You know it works when, with `export`, the low log ends `carried: /mnt/transfer/mirror-export-<time>-<pipeline>.tar (N image tag(s))` and `load` ends `loaded N image tag(s) into <host>, digests verified`; with bundles, when the low `sync` log shows `posted to NiFi: ... (HTTP 200` and the high `import` log shows `pushed to target registry: <host>/team/prometheus:v3.13.4@sha256:...`.

If it fails:
- Import names a missing bundle: Run pipeline on the low project with `RESEND_SEQUENCE=<N>..` as printed.
- A job says a variable is renamed: rename that CI variable as the message says.
