# How bundles are built, tracked and imported

This page explains the state behind `mirror.py sync`, `resend` and `import`: what is
recorded, where, and how the two sides stay in step over a one-way link. The README covers
the inputs and commands.

## The pieces

| Term | Meaning |
|---|---|
| Catalog | `images.txt`: one pinned source reference (`repo:tag@sha256:...`) and one destination (`org/repo`) per line. Changing it is the only way to approve an image. |
| Low registry, high registry | The `TARGET_REGISTRY` on each side. The low registry holds every approved image; the high registry receives them through bundles. |
| Bundle | One tar file plus its `.sha256` checksum. It carries `images.json` (what is inside) and a `dir:` copy of each image: every manifest, config and layer, byte for byte. |
| Stream | A random id chosen once by the sender. Every bundle from that sender carries it, so the high side can tell one sender's history from another's. |
| Sequence | The bundle's number within the stream: 1, 2, 3 and so on, with no reuse. |
| Ledger | The sender's record: the last digest sent for each destination tag, and every bundle written. |
| Receipt | The high side's record: the last bundle it imported. |

## Where the state lives

Nothing the mirror needs survives on a runner. In CI each job's work directory is removed
when it ends.

| What | Where (CI, `MIRROR_LEDGER=true`) | Without the ledger |
|---|---|---|
| Sender state | low project, generic package `registry-mirror-ledger`, `head/state.json` | `sent.json` in `MIRROR_STATE_DIR` |
| One record per bundle | low project, `registry-mirror-ledger/<sequence>/images.json` | none |
| Images | low registry, then high registry | same |
| SBOM and Grype report per sent image | low project, `registry-mirror-sbom/sha256-<hex>/` | none |
| Bundles in transit | NiFi, then `registry-mirror-bundles/<bundle name>/` in `IMPORT_STORE`: the high project's package registry, or the S3 bucket | `MIRROR_BUNDLE_DIR`, carried by hand |
| Import receipt | `registry-mirror-receipt/head/received.json` in the same store | `received.json` in `IMPORT_STATE_DIR` |

GitLab keeps every upload of a generic package file and serves the newest. So `head/`
always reads as the latest state, and the older uploads remain as history.

Bundles are the bulk of the high side's storage. `IMPORT_DELETE_BUNDLES=true` deletes each
one after its images are in the high registry, or an S3 lifecycle rule expires
`registry-mirror-bundles/` after a few days. Never expire `registry-mirror-receipt/`: the receipt is a few
hundred bytes, and it is what refuses a replayed old bundle that would roll a moved tag such
as `latest` back, and what notices a lost one. The registry itself cannot tell you either:
it shows which digests exist, not the order they arrived in.

To move the high side between stores, copy `registry-mirror-receipt/head/received.json` to the
new store first, then switch `IMPORT_STORE` and the NiFi upload processor.

## The sender state (`state.json`)

| Field | Meaning |
|---|---|
| `stream` | This sender's stream id. |
| `sequence` | The last sequence number used. |
| `registry` | The low registry this state belongs to. A state for another low registry is refused. |
| `sent` | For each destination tag (`org/repo:tag`), the digest and platform last sent, for example `"sha256:ab... linux/amd64"`. |
| `aliases` | Version tags sent with a `latest` entry, such as `["8.10.2"]` for `bitnami/redis:latest`. |
| `pinned` | Every destination tag the catalog pins or once pinned. A version tag never takes one of these over. |
| `platforms` | A cache of which platform image and tags each entry resolved to, so an unchanged entry needs no source request. |
| `pending` | True while a bundle is being written. If a run dies part way, the next run notices and sends a full bundle. |

## What `sync` does

1. Reads the state from the ledger, or starts a new stream if there is none.
2. For each catalog line:
   - copies the image to the low registry if it lacks it or holds another digest;
   - decides whether it needs sending: its digest, platform or version tags differ from
     what `sent` records. A full sync sends everything.
3. If nothing needs sending, prints `nothing to send` and stops. No bundle, no new sequence.
   If something does but neither `NIFI_URL` nor `MIRROR_BUNDLE_DIR` is set, it prints
   `not sent to the high side` and stops the same way. The ledger records only bundles that
   left, so the next run with a destination sends them.
4. Otherwise reserves the next sequence. It writes `pending: true` to the ledger first, so a
   crash cannot reuse the number.
5. Copies each changed image out of the low registry into the bundle, checks every file against its
   digest, and writes `images.json` and the `.sha256`.
6. Uploads `<sequence>/images.json` to the ledger with its creation time.
7. POSTs the checksum, then the bundle, to `NIFI_URL`, and deletes both once NiFi accepts them.
8. Updates `sent`, `aliases` and `pending: false` in the ledger.

The bundle is named `mirror-<stream>-<sequence>.tar`, for example
`mirror-1039946406c84e0ea1b2da9b9f231775-000000000004.tar`.

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

`import --registry` reads bundles from the high project's `registry-mirror-bundles` package. It
takes the one named by `BUNDLE`, and before that any later sequence the receipt points to
whose pipeline never started. A bundle missing either file waits for it.

## Resending

`resend` builds the next bundle from the ledger's records, without changing the catalog:

- `--sequence N..` resends everything recorded from bundle N to the newest;
- `--since YYYY-MM-DD` does the same from a date;
- `--image org/repo[:tag]` resends one image.

Each destination tag goes in at the **newest** digest recorded for it, never an older one,
so a resend cannot roll a moved tag such as `latest` back on the high side. Images come from
the low registry; one it no longer holds is copied from the source by the same digest.

A resend gets a new sequence number, because the high side refuses a second bundle with an
old number and different content. When it covers every bundle from N to the newest, it
carries `"from": N`. The high side then accepts it over a gap that starts at N.

Worked example: the high side holds bundle 6, bundles 7 and 8 are lost in transit, and
bundle 9 arrives.

1. Import refuses 9: `missing earlier bundle 7 to 8 (imported up to 6, received 9); on the low side, Run pipeline with RESEND_SEQUENCE=7..`.
2. On the low side, Run pipeline with `RESEND_SEQUENCE=7..`. Resend writes bundle 10,
   holding the newest digest of every tag recorded in 7, 8 and 9, with `"from": 7`.
3. The high side imports 10 over the gap (7 <= 6 + 1). Bundle 9 is now older than the
   receipt and is reported `superseded`.

## What the high side fixes by itself

- A missed pipeline trigger: the next import fetches every later bundle the receipt points
  to, in order.
- A bundle whose parts arrive apart, or bundles out of order: import prints `waiting` (the
  pipeline shows a warning) and the trigger that brings the missing file imports them in order.
  A gap that lasts longer than `IMPORT_GAP_GRACE` hours fails with the resend instruction.
- A duplicate or late trigger: `already imported` or `superseded`, nothing pushed.

A bundle lost on the way cannot heal on the high side: the link is one-way, so it cannot ask
for it. The next bundle's import fails and names the missing range and the exact low-side
action, `Run pipeline with RESEND_SEQUENCE=N..`.

## Failures and recovery

| What happened | What you see | What to do |
|---|---|---|
| A bundle never reached the high side | the next import fails: `missing earlier bundle N to M (imported up to N-1, received M+1); on the low side, Run pipeline with RESEND_SEQUENCE=N..` | exactly that |
| A run died after reserving a sequence | `pending: true` in the ledger | nothing: the next sync is full and bridges it |
| NiFi refused the POST | sync fails; the ledger records the bundle | rerun the pipeline (the next sync is full), or `RESEND_SEQUENCE=N` |
| The high side lost its receipt | `--registry` replays the stream from bundle 1 out of `registry-mirror-bundles` (pushes are idempotent, later bundles win); with deleted or expired bundles, and with `--inbox`, it refuses all but a full bundle | with every bundle kept, nothing; otherwise Run pipeline on the low side with `RESEND_ALL=true` |
| The ledger is deleted | sync starts a new stream | import its first bundle with `--adopt-stream` after review |
| The source deleted an old digest | a resend of that image fails | update the catalog; the low registry still holds every image it ever received |

## The scan

`mirror.py pending` lists the catalog lines the ledger has not sent at their current digest
and platform. That is what the next sync sends. `sbom` writes a CycloneDX SBOM of each with
Syft, and `scan` checks those SBOMs with Grype. `sync` waits for both and keeps each SBOM
and Grype report by digest in `registry-mirror-sbom`. Images already sent are not scanned
again. `MIRROR_SCAN=false` turns the scan off.

## One writer at a time

The ledger has no locking of its own. In CI, `sync` and `resend` share
`resource_group: registry-mirror`, so GitLab runs one at a time. On the high side, `import` uses
`resource_group: registry-mirror-high`. Do not run either by hand against the same ledger or
receipt while a pipeline might.

## Upgrading from the Quay-named version

A project on the Quay-named version keeps its state under the old names, and this version
does not read them:

- Rename the CI variables; a job with an old name set fails and names the new one.
- Both sides start over. The first `sync` finds no `registry-mirror-ledger` and sends a new
  stream's full bundle; the high side finds no `registry-mirror-receipt` and imports it as a
  first import. Images already on the high side are pushed again with the same digests. The
  old `quay-*` packages can then be deleted.
- Let NiFi drain any `quay-*.tar` in transit before switching: the new flow and import only
  take `mirror-*.tar`. `nifi/flow.py` replaces the old `quay <side> side` group itself.
- A catalog source counts as being in `TARGET_REGISTRY` only when its host matches the
  variable exactly, port included.
