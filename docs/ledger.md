# How bundles are built, tracked and imported

This page explains the state behind `mirror.py sync`, `export` and `import`: what is
recorded, where, and how the two sides stay in step over a one-way link. The README covers
the inputs and commands.

## The pieces

| Term | Meaning |
|---|---|
| Catalog | `images.txt`: one pinned upstream reference (`repo:tag@sha256:...`) and one destination (`org/repo`) per line. Changing it is the only way to approve an image. |
| Low Quay, high Quay | The registry on each side. Low Quay holds every approved image; high Quay receives them through bundles. |
| Bundle | One tar file plus its `.sha256` checksum. It carries `images.json` (what is inside) and a `dir:` copy of each image: every manifest, config and layer, byte for byte. |
| Stream | A random id chosen once by the sender. Every bundle from that sender carries it, so the high side can tell one sender's history from another's. |
| Sequence | The bundle's number within the stream: 1, 2, 3 and so on, with no reuse. |
| Ledger | The sender's record: what each destination tag was last sent as, and every bundle ever written. |
| Receipt | The high side's record: the last bundle it imported. |

## Where the state lives

Nothing the mirror needs survives on a runner. In CI each job's work directory is removed
when it ends.

| What | Where (CI, `MIRROR_LEDGER=true`) | Without the ledger |
|---|---|---|
| Sender state | low project, generic package `quay-mirror-ledger`, `head/state.json` | `sent.json` in `MIRROR_STATE_DIR` |
| One record per bundle | low project, `quay-mirror-ledger/<sequence>/images.json` | none |
| Images | low Quay, then high Quay | same |
| SBOM and Grype report per sent image | low project, `quay-mirror-sbom/sha256-<hex>/` | none |
| Bundles in transit | NiFi, then the high project's `quay-bundles/<bundle name>/` | `MIRROR_BUNDLE_DIR`, carried by hand |
| Import receipt | high project, `quay-import-receipt/head/received.json` | `received.json` in `IMPORT_STATE_DIR` |

GitLab keeps every upload of a generic package file and serves the newest. So `head/`
always reads as the latest state, and the older uploads remain as history.

## The sender state (`state.json`)

| Field | Meaning |
|---|---|
| `stream` | This sender's stream id. |
| `sequence` | The last sequence number used. |
| `registry` | The low Quay this state belongs to. A state for another low registry is refused. |
| `sent` | For each destination tag (`org/repo:tag`), the digest and platform last sent, for example `"sha256:ab... linux/amd64"`. |
| `aliases` | Version tags sent with a `latest` entry, such as `["8.10.2"]` for `bitnami/redis:latest`. |
| `pinned` | Every destination tag the catalog pins or once pinned. A version tag never takes one of these over. |
| `platforms` | A cache of which platform image and tags each entry resolved to, so an unchanged entry needs no upstream request. |
| `pending` | True while a bundle is being written. If a run dies part way, the next run notices and sends a full bundle. |

## What `sync` does

1. Reads the state from the ledger, or starts a new stream if there is none.
2. For each catalog line:
   - copies the image to low Quay if low Quay lacks it or holds another digest;
   - decides whether it needs sending: its digest, platform or version tags differ from
     what `sent` records. A full sync sends everything.
3. If nothing needs sending, prints `nothing to send` and stops. No bundle, no new sequence.
4. Otherwise reserves the next sequence. It writes `pending: true` to the ledger first, so a
   crash cannot reuse the number.
5. Copies each changed image out of low Quay into the bundle, checks every file against its
   digest, and writes `images.json` and the `.sha256`.
6. Uploads `<sequence>/images.json` to the ledger with the time it was created.
7. POSTs the checksum, then the bundle, to `NIFI_URL`, and deletes both once NiFi accepts them.
8. Updates `sent`, `aliases` and `pending: false` in the ledger.

The bundle is named `quay-<stream>-<sequence>.tar`, for example
`quay-1039946406c84e0ea1b2da9b9f231775-000000000004.tar`.

## What the high side accepts

`import` reads the receipt, then for each bundle:

| Bundle | Result |
|---|---|
| Checksum or any image file does not match its digest | refused before anything is pushed |
| Same stream, the next sequence | imported; the receipt moves to it |
| Same stream, same sequence and same checksum as the receipt | `already imported`, nothing pushed |
| Same stream, an older sequence | refused as a rollback (`superseded` when catching up) |
| Same stream, a later sequence with a gap | refused: `missing earlier bundle N`, unless the bundle is full or a resend that covers the gap |
| Another stream | refused unless it is a full bundle and `--adopt-stream` is given |

`import --registry` reads bundles from the high project's `quay-bundles` package. It
takes the one named by `BUNDLE`, and before that any later sequence the receipt points to
whose pipeline never started. A bundle missing either file waits for it.

## Resending

`export` builds the next bundle from the ledger's records, without changing the catalog:

- `--sequence N..` resends everything recorded from bundle N to the newest;
- `--since YYYY-MM-DD` does the same from a date;
- `--image org/repo[:tag]` resends one image.

Each destination tag goes in at the **newest** digest recorded for it, never an older one,
so a resend cannot roll a moved tag such as `latest` back on the high side. Images come from
low Quay; one low Quay no longer holds is copied from upstream by the same digest.

A resend gets a new sequence number, because the high side refuses a second bundle with an
old number and different content. When it covers every bundle from N to the newest, it
carries `"from": N`. The high side then accepts it over a gap that starts at N.

Worked example: the high side imported bundle 6, then bundles 7 and 8 were lost in
transit, and bundle 9 arrives.

1. Import refuses 9: `missing earlier bundle 7; on the low side run mirror.py export --sequence 7..`.
2. On the low side, Run pipeline with `EXPORT_SEQUENCE=7..`. Export writes bundle 10,
   holding the newest digest of every tag recorded in 7, 8 and 9, with `"from": 7`.
3. The high side imports 10 over the gap (7 <= 6 + 1). Bundle 9 is now older than the
   receipt and is reported `superseded`.

## Failures and recovery

| What happened | What you see | What to do |
|---|---|---|
| A bundle never reached the high side | the next import names the missing sequence | `export --sequence N..` |
| A run died after reserving a sequence | `pending: true` in the ledger | nothing: the next sync is full and bridges it |
| NiFi refused the POST | sync fails; the ledger records the bundle | rerun the pipeline (the next sync is full), or `export --sequence N` |
| The high side lost its receipt | `--registry` replays the stream from bundle 1 out of `quay-bundles` (pushes are idempotent, later bundles win); `--inbox` refuses all but a full bundle | nothing with `--registry`; for `--inbox`, `sync --full` on the low side |
| The ledger was deleted | sync starts a new stream | import its first bundle with `--adopt-stream` after review |
| Upstream deleted an old digest | a resend of that image fails | update the catalog; low Quay still holds every image it ever received |

## The scan

`mirror.py pending` lists the catalog lines the ledger has not sent at their current digest
and platform. That is what the next sync sends. `sbom` writes a CycloneDX SBOM of each with
Syft, and `scan` checks those SBOMs with Grype. `sync` waits for both and keeps each SBOM
and Grype report by digest in `quay-mirror-sbom`. Images already sent are not scanned
again. `MIRROR_SCAN=false` turns the scan off.

## One writer at a time

The ledger has no locking of its own. In CI, `sync` and `export` share
`resource_group: quay-mirror`, so GitLab runs one at a time. On the high side, `import` uses
`resource_group: quay-mirror-high`. Do not run either by hand against the same ledger or
receipt while a pipeline might.
