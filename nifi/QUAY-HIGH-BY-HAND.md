# Build the quay high-side flow by hand

The high half of `nifi/flow.py --side high`, for a NiFi where you cannot run scripts.
Anything not listed stays at its default. Written for the NiFi 2.x UI. Property names,
parameter names, attribute names and relationship names are case-sensitive: type them
exactly as written.

NiFi does not push images. It files each bundle in the high GitLab project's generic
package registry and starts that project's pipeline, and the pipeline's `import` job
pushes the images with `skopeo copy`. That job runs the reviewed `mirror.py` from the
project, the same way every other CI job runs, so nobody runs a script by hand.

## Why not `docker load`, `docker tag`, `docker push`

A bundle is a tar of `images.json` plus one `images/<digest>/` folder per image, in
skopeo's `dir:` layout: every manifest, config and layer, byte for byte. It is not a
`docker save` archive, so `docker load` cannot read it. Going through `docker` would
also rebuild the manifest and change the digest, so the high side would no longer hold
the digest the low side approved. `skopeo copy --preserve-digests` keeps it, and
`import` checks the digest after every push.

## Before you start

- The high registry. A plain `registry:2` works: `import` only pushes and inspects over
  the registry API, and it never calls a Quay API.
- A high GitLab project holding this repository, with **Settings > CI/CD > General
  pipelines > CI/CD configuration file** set to `.gitlab-ci-high.yml`.
- A runner that takes untagged jobs (or the one `MIRROR_JOB_TAG` names) with `python3` (3.9 or later), `skopeo`
  and `curl`.
- Project CI/CD variables, protected and masked, scoped to environment `mirror-high`:

  | Variable | Value |
  |---|---|
  | `HIGH_QUAY_HOST` | the registry as `host[:port]` |
  | `HIGH_QUAY_USERNAME`, `HIGH_QUAY_PASSWORD` | an account that can push. A `registry:2` without auth accepts any value |
  | `HIGH_QUAY_TLS_VERIFY` | `false` only for a plain-HTTP registry |

- A project access token for NiFi: role **Developer**, scope **api**.
- **Settings > Repository > Protected branches**: allow **Developers + Maintainers** to
  merge to the default branch. GitLab runs a pipeline on a protected branch only for a
  role that may merge to it, and refuses the token's pipeline request otherwise.
- The project's numeric ID (**Settings > General**).

## What arrives

The low side POSTs two files per bundle to NiFi, each with these attributes:

| Attribute | Value |
|---|---|
| `filename` | `quay-<stream>-<sequence>.tar` or the same name plus `.sha256` |
| `X-Sha256` | SHA-256 of this file |
| `X-Artifact-Type` | `container-images` |
| `X-Artifact-Format` | `tar` or `sha256` |
| `X-Artifact-Action` | `mirror` |
| `X-Bundle-Kind` | `delta`, `full` or `resend` |

The flow below starts where your pypi flow starts: at the RouteOnAttribute that sees
these attributes. If your link delivers bare files and the attributes travel inside a
FlowFile package, put the same unpack step in front that the pypi feed uses.

Both files of a bundle go to GitLab unchanged. Unlike the pypi flow there is no unpack
of the content and no UpdateAttribute for names: the file name already carries the
package version.

## Part 1: parameter context

1. Top-right menu (**☰**) > **Parameter Contexts** > **+**.
2. **Settings** tab: Name `quay-to-gitlab`.
3. **Parameters** tab, add four parameters with **+**. Choose **Sensitive: Yes** for
   `gitlab.token` when you create it: it cannot be changed afterwards.

   | Name | Value | Sensitive |
   |---|---|---|
   | `gitlab.api` | `https://<gitlab>/api/v4` | No |
   | `gitlab.project` | the numeric project ID | No |
   | `gitlab.ref` | the default branch, for example `main` | No |
   | `gitlab.token` | the access token | **Yes** |

4. **Apply**.

## Part 2: process group

1. Drag the **Process Group** icon onto the canvas, name it `quay-to-gitlab`, click
   **Add**.
2. Right-click it > **Configure** > **Settings**: set **Parameter Context** to
   `quay-to-gitlab`, click **Apply**.
3. Double-click the group to go inside it. Add an **Input Port** named `in` if the
   bundles come from your shared feed router, and connect your router's
   container-images output to it.

## Part 3: processors

For each one: drag the **Processor** icon onto the canvas, filter by type, **Add**,
then right-click > **Configure**. Set the Name on **Settings**, the listed properties
on **Properties** (for a row marked *(add)*, click **+**, enter the name, **OK**, then
the value), tick **terminate** for each relationship under Auto-terminate on
**Relationships**, and **Apply**.

### 1. Container bundles only

Type **RouteOnAttribute**.

| Property | Value |
|---|---|
| Routing Strategy | `Route to Property name` (default) |
| `container-images` *(add)* | `${X-Artifact-Type:equals('container-images'):and(${X-Artifact-Action:equals('mirror')}):and(${filename:matches('quay-[0-9a-f]{32}-[0-9]{12}[.]tar([.]sha256)?')})}` |

Auto-terminate: `unmatched`, or wire it to your other feeds.

### 2. Hash file

Type **CryptographicHashContent**.

| Property | Value |
|---|---|
| Hash Algorithm | `SHA-256` (default) |

Auto-terminate: none.

### 3. File matches X-Sha256

Type **RouteOnAttribute**.

| Property | Value |
|---|---|
| `verified` *(add)* | `${content_SHA-256:equals(${X-Sha256})}` |

Auto-terminate: none.

### 4. Upload to GitLab

Type **InvokeHTTP**.

| Property | Value |
|---|---|
| HTTP Method | `PUT` |
| HTTP URL | `#{gitlab.api}/projects/#{gitlab.project}/packages/generic/quay-bundles/${filename:substringBefore('.tar')}/${filename}` |
| Request Body Enabled | `true` |
| Response Body Attribute Name | `gitlab.response` |
| `PRIVATE-TOKEN` *(add, sensitive)* | `#{gitlab.token}` |

Auto-terminate: `Response`. For `PRIVATE-TOKEN`, tick **Sensitive** in the add dialog;
a dynamic property on InvokeHTTP is sent as a request header.

If GitLab uses an internal CA, set **SSL Context Service** to a
StandardSSLContextService whose truststore holds it. For bundles of several GB, raise
**Socket Write Timeout** and **Socket Read Timeout**, for example to `30 mins`.

### 4 (S3 instead). Upload to S3

With `IMPORT_STORE=s3` on the high project, build this in place of **Upload to GitLab**.
Add three parameters to the context first: `s3.access_key` and `s3.secret_key`, both
**Sensitive: Yes**, and `s3.ca`, the store's CA certificate in PEM, pasted whole.

Two controller services, from the process group's **Configure > Controller Services > +**.
Enable each with the lightning icon once set.

| Service | Property | Value |
|---|---|---|
| AWSCredentialsProviderControllerService, named `S3 credentials` | Access Key ID | `#{s3.access_key}` |
| | Secret Access Key | `#{s3.secret_key}` |
| PEMEncodedSSLContextProvider, named `S3 CA` | Private Key Source | `UNDEFINED` |
| | Certificate Authorities Source | `PROPERTIES` |
| | Certificate Authorities | `#{s3.ca}` |

Then the processor. Type **PutS3Object**.

| Property | Value |
|---|---|
| Bucket | the bucket name |
| Object Key | `quay-bundles/${filename:substringBefore('.tar')}/${filename}`, with the project's `S3_PREFIX` and a `/` in front when it sets one |
| Region | `us-east-1`, or the store's region |
| AWS Credentials Provider Service | `S3 credentials` |
| SSL Context Service | `S3 CA` |
| Endpoint Override URL | `https://<store>:<port>` |
| Use Path Style Access | `true` |

Auto-terminate: none. In Part 4, connect `success` to **Start the import pipeline** and
`failure` to **Rejected (inspect queue)**, in place of rows 7 to 9.

Give the bucket a lifecycle rule that expires `quay-bundles/` after a few days, or set
`IMPORT_DELETE_BUNDLES=true` on the high project. Never expire `quay-import-receipt/`.

### 5. Start the import pipeline

Type **InvokeHTTP**.

| Property | Value |
|---|---|
| HTTP Method | `POST` |
| HTTP URL | `#{gitlab.api}/projects/#{gitlab.project}/pipeline?ref=#{gitlab.ref}&variables%5B%5D%5Bkey%5D=BUNDLE&variables%5B%5D%5Bvalue%5D=${filename:urlEncode()}` |
| Request Body Enabled | `false` |
| Response Body Attribute Name | `gitlab.response` |
| `PRIVATE-TOKEN` *(add, sensitive)* | `#{gitlab.token}` |

Auto-terminate: `Response`. Type the `%5B%5D` sequences as shown: they are `[]`,
encoded.

Each of a bundle's two files starts one pipeline. Whichever runs first while the other
file is missing prints `waiting for` and exits cleanly. The second imports the bundle,
or reports `already imported`.

### 6. Delivered

Type **UpdateAttribute**, no properties. Auto-terminate: `success`.

### 7. Rejected (inspect queue)

Type **UpdateAttribute**, no properties. Auto-terminate: `success`. Keep it
**stopped**, so anything that failed waits in its input queue for you.

## Part 4: connections

Hover the source, drag the arrow to the destination, tick exactly the listed
relationship(s), **Add**. For a row whose destination is the source itself, drag the
arrow away and back onto the same processor.

| # | From | Relationship | To |
|---|---|---|---|
| 1 | your feed (or `in`) | | Container bundles only |
| 2 | Container bundles only | `container-images` | Hash file |
| 3 | Hash file | `success` | File matches X-Sha256 |
| 4 | Hash file | `failure` | Rejected (inspect queue) |
| 5 | File matches X-Sha256 | `verified` | Upload to GitLab |
| 6 | File matches X-Sha256 | `unmatched` | Rejected (inspect queue) |
| 7 | Upload to GitLab | `Original` | Start the import pipeline |
| 8 | Upload to GitLab | `Retry` | Upload to GitLab (itself) |
| 9 | Upload to GitLab | `No Retry`, `Failure` | Rejected (inspect queue) |
| 10 | Start the import pipeline | `Original` | Delivered |
| 11 | Start the import pipeline | `Retry` | Start the import pipeline (itself) |
| 12 | Start the import pipeline | `No Retry`, `Failure` | Rejected (inspect queue) |

## Part 5: check and start

1. Every processor except the two end points shows a stopped square, not a warning
   triangle. Hover a triangle to read what is missing.
2. On both InvokeHTTP processors, the only deletable property row is `PRIVATE-TOKEN`.
   A mistyped built-in property also shows as a deletable row.
3. Select everything except **Rejected (inspect queue)**, right-click > **Start**.
4. On the low side, run the low project's pipeline. Its `sync` log names what it sent:
   `posted to NiFi: quay-<stream>-<n>.tar -> <url> (HTTP 200, ...)`.
5. Watch the queues. Two flowfiles reach **Delivered**, nothing reaches **Rejected**.
6. In the high project, **Deploy > Package registry** shows `quay-bundles` with that
   version, and **Build > Pipelines** shows two pipelines. The `import` log names each
   image as `pushed to high registry: <host>/<repo>:<tag>@sha256:...`.
7. Pull that exact path to prove it: `podman pull <host>/<repo>:<tag>@sha256:...`

If anything reaches **Rejected (inspect queue)**, right-click the queue > **List queue**
and open the flowfile's attributes:
- `invokehttp.status.code` and `gitlab.response` explain a GitLab refusal: 401 means the
  token, 404 the project ID or `gitlab.api`, 400 with "insufficient permission to run a
  pipeline" means Developers may not merge to `gitlab.ref`.
- A file rejected at **File matches X-Sha256** was damaged in transit. On the low side,
  run the pipeline with `EXPORT_SEQUENCE=<n>..`.

## Without a pipeline

If the high GitLab cannot run the import, an operator can push a bundle with skopeo
alone. Check the tar against its `.sha256` file, extract it, then read `images.json`.
Each entry gives `target`, `tags` and `transfer`. For each tag:

```sh
skopeo copy --all --preserve-digests \
  dir:images/<transfer with ':' replaced by '-'> docker://<registry>/<target>:<tag>
skopeo inspect --raw docker://<registry>/<target>:<tag> | sha256sum   # must equal <transfer>
```

This skips the receipt, so the order and gap checks in `docs/ledger.md` are yours to
make.
