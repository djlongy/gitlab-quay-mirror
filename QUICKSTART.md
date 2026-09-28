## What this is

`mirror.py` reads approved `tag@sha256` images from `images.txt`, pulls them through low Quay, and writes a `quay-delta-*.tar` plus `.sha256` to `delta/`. Move both files through NiFi or an SMB share; the same script imports them into high Quay. It sends changed tags and preserves manifest digests and platforms. It does not delete high-side tags or transfer detached signatures.

## How to use it

1. Get a digest: `skopeo inspect --raw docker://docker.io/library/alpine:3.20.3 | sha256sum`
2. Add that `tag@sha256` and its high-side repository to `images.txt`.
3. Set the low login paths: `export LOW_QUAY=quay.low.example.internal LOW_AUTH_FILE=/path/to/low-auth.json`
4. Build the transfer: `python3 mirror.py sync`
5. Carry both files: `cp delta/quay-delta-* /path/to/diode-outbox/`
6. Set the high login paths: `export HIGH_QUAY=quay.high.example.internal HIGH_AUTH_FILE=/path/to/high-auth.json`
7. Import the tar from your high-side inbox: `python3 mirror.py import /path/to/inbox/quay-delta-*.tar`

You know it works when import prints `digests verified` for every tag.

If it fails:
- A pack was lost: run `python3 mirror.py sync --full` on the low side and transfer that bundle.
- The catalogue digest differs from low Quay: update `images.txt` through review, then rerun sync.
