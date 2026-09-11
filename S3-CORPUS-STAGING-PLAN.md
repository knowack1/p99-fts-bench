# S3 corpus staging — eu-north-1

Closes the open item in `ENGINE-PREP-PLAN.md` ("IAM role + S3 bucket"). Turns
the per-stop corpus cost from **36 min** (Swedish mirror) or **2.5–3 h**
(`dumps.wikimedia.org`) into **~2–3 min**.

Status as of **2026-09-11**: **superseded in part.** The S3 path is blocked on
IAM; the corpus now persists on the harness box's root EBS volume instead.
See "What actually happened" below before following any step here.

## What actually happened, 2026-09-11

| Step | Outcome |
|---|---|
| S3 bucket | **DONE** — `knowacki-p99-fts-corpus`, eu-north-1, global namespace, BPA on, versioning off, SSE-S3 |
| IAM policy `fts-bench-corpus-s3` | **BLOCKED** — `DeveloperAccessRole` denied `iam:CreatePolicy` (also denied `access-analyzer:ValidatePolicy`) |
| IAM role + instance profile | **NOT ATTEMPTED** — the same role almost certainly lacks `iam:CreateRole` / `iam:PassRole` |
| Harness instance profile | **none attached** — `i-08d8d2505e16683f7` has an empty IAM role field, so the box has no AWS identity and a bucket policy has no principal to grant to |
| **Root EBS grown** | **DONE** — `vol-0a789d6c4317a2b7d` **8 GiB to 32 GiB**, gp3, 3000 IOPS, 125 MB/s unchanged |

**Decision: the corpus persists on the root EBS volume, not S3.** Root EBS
survives a stop; `/mnt/nvme` does not. This needs no IAM, no public bucket and
no laptop upload. The bucket stays for the day someone grants the role — and
as the home of a presigned-URL fallback, which the console can mint without
any IAM change (12 h expiry, regenerated per session).

Rejected on the way, with reasons, so they are not re-proposed:

- **Borrowing an existing instance profile.** All 17 in the account belong to
  other people or teams (`adaml-s3-sync`, `ernest-s3warm`, `storage-team`, the
  `qa-*` set). Unknown blast radius, and it would attribute this benchmark's
  S3 calls to someone else's identity in CloudTrail.
- **Making the bucket public.** The data is public Wikipedia content, so
  confidentiality is not the issue — unbounded egress billing and corporate
  Block Public Access policy are.
- **Uploading a prepared corpus from the laptop.** 37-54 min at the measured
  uplink, the laptop does not hold the enwiki corpus anyway, and it would
  still leave the box unable to authenticate a download.

### Remaining work on the box, next time it starts

The EBS volume is bigger; **Linux does not notice on its own.**

> **Identify the root device first — do not assume.** On this Nitro box
> `HARNESS-AWS-RUNBOOK.md:232` formats **`/dev/nvme0n1` as the instance
> store**, so the EBS root is a *different* nvme device. Running `growpart`
> against the instance-store device would be destructive. Check with
> `findmnt -no SOURCE /` and `lsblk` before touching anything.

```bash
findmnt -no SOURCE /          # e.g. /dev/nvme1n1p1
lsblk
sudo growpart <root-disk> 1   # the DISK, then the partition number
sudo xfs_growfs /             # AL2023 root is xfs
df -h /                       # expect ~32 GiB
```

Then keep `corpus.jsonl.zst` (~10.2 GB, `pzstd -10`) on the root volume and
decompress to `/mnt/nvme/data/corpus.jsonl` on every start.

**Expected restage: ~1.5-2 min.** The read is bounded by gp3's 125 MB/s
baseline — 10.2 GB is ~82 s — not by `pzstd`, which does ~2.85 GB/s. gp3
throughput is provisionable up to 1,000 MB/s if this ever lands on the
critical path; not worth it at 1.5 min.

---

Original plan follows. Steps 1-5 are the S3 route, still valid if the IAM
block clears.

Status: **drafted, not executed.** Nothing below has been clicked.

## Why this and not a laptop upload

Measured 2026-09-11, on this laptop:

| | value | how |
|---|---|---|
| `corpus.jsonl` compressibility | **2.96x** at `zstd -3`, **3.46x** at `zstd -10` | on the 456,217,584 B simplewiki corpus |
| enwiki 35,448,823,550 B compressed | **~12.0 GB** / **~10.2 GB** | extrapolated at those ratios |
| laptop uplink | **3.72 and 5.40 MB/s** (2 x 50 MB) | `speed.cloudflare.com/__up` |
| → upload of the compressed corpus | **37–54 min** | 12.0 GB ÷ ~4.5 MB/s |

Uploading from the laptop is no faster than the mirror, is re-paid on every
restart, and the laptop does not even hold the enwiki corpus (`bench/data/`
has simplewiki only — enwiki was prepared on `fts-harness` 2026-09-01 and died
with the instance store). S3 in-region is free, fast, and paid once.

## Design decisions

| Decision | Value | Why |
|---|---|---|
| Region | **eu-north-1**, same as the fleet | S3→EC2 in-region transfer is free; cross-region is billed and slow |
| Credentials | **EC2 instance profile**, no access keys on disk | keys on an instance-store box are lost every stop and leak into shell history |
| Stored artifact | prepared **`corpus.jsonl.zst`** only | the 40 GB of bz2 shards are re-derivable from the public dump; the prepared corpus is the expensive part |
| Compression | **`pzstd -10 -p 8`** | 3.44x, and multi-frame so decompression parallelises to ~2.85 GB/s — see below |
| Versioning | **off** | corpus is immutable; integrity is enforced by the sha256 gate below, not by S3 |
| Delete permission | **not granted** | the policy has no `s3:DeleteObject`; removing a frozen corpus requires the console, deliberately |
| Public access | **blocked** (bucket default) | — |
| Encryption | SSE-S3 (bucket default) | free, no key management |

## Compression — measured, 2026-09-11

Decompression must never become the bottleneck, so the codec was chosen on
**decompression throughput**, not ratio. Measured on the 456,217,584 B
simplewiki `corpus.jsonl`; decompression to `/dev/null`, best of 3:

| codec | size | ratio | compress | **decompress out** | decompress in |
|---|---|---|---|---|---|
| `zstd -1` | 176,373,964 | 2.59 | 0.22 s | 1,086 MB/s | 420 MB/s |
| `zstd -3` | 153,991,051 | 2.96 | 0.44 s | 1,014 MB/s | 342 MB/s |
| `zstd -6` | 141,192,812 | 3.23 | 1.02 s | 1,037 MB/s | 321 MB/s |
| `zstd -10` | 131,945,770 | 3.46 | 3.77 s | 845 MB/s | 244 MB/s |
| `pzstd -3 -p 8` | 154,773,922 | 2.95 | 0.63 s | **3,259 MB/s** | 1,106 MB/s |
| **`pzstd -10 -p 8`** | **132,660,587** | **3.44** | 3.69 s | **2,851 MB/s** | 829 MB/s |
| `pzstd -15 -p 8` | 128,096,427 | 3.56 | 25.31 s | 2,535 MB/s | 712 MB/s |

**Single-threaded zstd decompresses at ~1 GB/s of plaintext regardless of
level** — the level buys ratio, not decompression cost. So "use a fast level
to decompress fast" does not hold for zstd: `-1` is only 1.29x faster to
decompress than `-10` while producing a 1.34x larger file. Those cancel.

What actually lifts the ceiling is **parallelism**. `pzstd` writes multiple
frames, so decompression scales across cores: **2,851 MB/s at `-10` on 8
threads, 3.4x plain `zstd -d` at the same ratio**. `zstd -d -T0` does *not*
parallelise (1,037 vs 861 MB/s — that is an I/O thread, not MT decode).

Applied to the 35.4 GB corpus:

| | `zstd -d` | `pzstd -d -p 8` |
|---|---|---|
| decompression floor | ~35 s | **~12 s** |

Against a 20–100 s download, 12 s is never the constraint. That settles the
"decompression could take longer than the download" concern.

**The format is plain zstd either way.** Verified byte-identical both
directions: `zstd -dc` reads a `pzstd` file (serially, ~1 GB/s) and `pzstd -dc`
reads a `zstd` file. So `pzstd` is a speed option on the box, never a hard
dependency — if it is not packaged on Amazon Linux 2023, `zstd -d` still
restages the same object at ~35 s.

`-15` is rejected: 25 s to compress for 3.4% more shrink, and it decompresses
*slower*. `-3` is rejected because decompression is no longer the constraint,
so the 17% smaller `-10` file wins on the side that is.

Caveat: these are x86 numbers from this 22-core laptop with `-p 8`. The fleet
is Graviton4. Treat the ratios as solid and the rates as indicative until the
first restage measures them.

Key layout — dump date in the prefix, so a future re-freeze coexists instead
of overwriting:

```
s3://<BUCKET>/enwiki-20260816/corpus.jsonl.zst
s3://<BUCKET>/enwiki-20260816/corpus.sha256      # sha256 of the DECOMPRESSED jsonl
s3://<BUCKET>/simplewiki-20260816/corpus.jsonl.zst
s3://<BUCKET>/simplewiki-20260816/corpus.sha256
```

`corpus.sha256` must equal `FREEZE.md`'s value. For enwiki that is
`1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432`. That
check is what makes an S3 restage as trustworthy as a mirror download.

## Step 1 — bucket (console, S3)

There is no AWS CLI credential on the laptop (`HARNESS-AWS-RUNBOOK.md:64`), so
creation is console-only.

1. S3 → **Create bucket**
2. Region **Europe (Stockholm) eu-north-1** — verify, the console may default elsewhere
3. Name: globally unique, DNS-safe. Suggest `scylla-p99-fts-corpus-<account-id>`
4. Block **all** public access: leave ON
5. Versioning: **Disable**
6. Encryption: SSE-S3, bucket key enabled
7. Create

Record the final name; it becomes `CORPUS_BUCKET` everywhere below.

## Step 2 — IAM policy (console, IAM)

IAM → Policies → **Create policy** → JSON. Replace `<BUCKET>`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListOnlyThisBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<BUCKET>"
    },
    {
      "Sid": "ReadWriteCorpusObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::<BUCKET>/*"
    }
  ]
}
```

Name: `fts-bench-corpus-s3`. The multipart actions are required — a ~10 GB
object exceeds the 5 GB single-PUT limit and `aws s3 cp` will shard it.

## Step 3 — role + instance profile (console, IAM)

1. IAM → Roles → **Create role**
2. Trusted entity: **AWS service** → **EC2**
3. Attach `fts-bench-corpus-s3`. Nothing else — no `AmazonS3FullAccess`
4. Name: `fts-bench-corpus-s3-role`

The console creates the matching instance profile automatically.

## Step 4 — attach to the boxes (console, EC2)

Both boxes are currently **stopped**, which is the cheapest moment to do this.
Attaching works on a stopped instance and takes effect on next boot.

EC2 → Instances → select → **Actions → Security → Modify IAM role** →
`fts-bench-corpus-s3-role` → Update.

- `fts-harness` — **required**, it holds the corpus and runs the loaders
- `fts-sut` — optional; it never reads the corpus. Attach only if a future
  arm needs it

## Step 5 — VPC gateway endpoint (optional, free)

VPC → Endpoints → Create → `com.amazonaws.eu-north-1.s3`, type **Gateway**,
default VPC, tick the route tables the boxes' subnet uses.

Costs nothing and keeps S3 traffic off the internet gateway. Not required —
in-region S3 transfer is already free over the public path — but it removes
the public path as a variable.

## Step 6 — seed the bucket, once, from the box

After the next normal mirror download + prepare on `fts-harness`:

```bash
export CORPUS_BUCKET=<bucket>
export WIKI=enwiki DUMP_DATE=20260816
C=/mnt/nvme/data/corpus.jsonl

sha256sum "$C" | awk '{print $1}' > "$C.sha256"
grep -q 1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432 "$C.sha256" \
  || { echo "CORPUS DOES NOT MATCH FREEZE.md — do not upload"; exit 1; }

pzstd -10 -p "$(nproc)" -f -o "$C.zst" "$C"
aws s3 cp "$C.zst"     "s3://$CORPUS_BUCKET/$WIKI-$DUMP_DATE/corpus.jsonl.zst" --region eu-north-1
aws s3 cp "$C.sha256"  "s3://$CORPUS_BUCKET/$WIKI-$DUMP_DATE/corpus.sha256"   --region eu-north-1
```

The freeze check runs **before** the upload deliberately: seeding S3 from a
corpus that does not match `FREEZE.md` would launder a bad corpus into every
later run.

Do the same for simplewiki (`c1be2adb…91c5149`) so a smoke run needs no
download either.

## Step 7 — restage, on every start

New script, `tools/stage_corpus_s3.sh`:

```bash
#!/bin/bash
set -euo pipefail
BUCKET="${CORPUS_BUCKET:?set CORPUS_BUCKET}"
WIKI="${1:-enwiki}"; DATE="${2:-20260816}"; DEST="${3:-/mnt/nvme/data/corpus.jsonl}"
PREFIX="s3://$BUCKET/$WIKI-$DATE"

ZST="$(dirname "$DEST")/corpus.jsonl.zst"
mkdir -p "$(dirname "$DEST")"

aws s3 cp "$PREFIX/corpus.jsonl.zst" "$ZST" --region eu-north-1
if command -v pzstd >/dev/null; then pzstd -d -p "$(nproc)" -f -o "$DEST" "$ZST"
else                                 zstd  -d          -f -o "$DEST" "$ZST"; fi

aws s3 cp "$PREFIX/corpus.sha256" /tmp/corpus.sha256 --region eu-north-1
echo "$(cat /tmp/corpus.sha256)  $DEST" | sha256sum -c -
rm -f "$ZST"
```

**Download to a file, not through a pipe.** `aws s3 cp` to a path issues
concurrent ranged GETs (10 by default) and retries per part; streaming to
stdout forces one sequential GET and gives up most of the available
bandwidth. Since `pzstd` removed the decompression bottleneck, there is
nothing left to gain from overlapping the two stages. Costs 45.7 GB of
transient NVMe out of 1.9 TB.

The `sha256sum -c` is a **gate, not a log line** — a failed restage must stop
the session, not produce a run against an unknown corpus.

Throughput to expect: ~10.3 GB at an assumed 300–800 MB/s is **13–34 s**,
plus **~12 s** of `pzstd -d`, plus the NVMe write. Call it **~1 min, to be
confirmed on the first real restage** — the network figure is the one number
here that is still assumed. Raise
`aws configure set default.s3.max_concurrent_requests 20` if it underperforms.

## Cost

| Item | Monthly |
|---|---|
| ~10.3 GB enwiki + ~0.15 GB simplewiki, S3 Standard eu-north-1 (~$0.023/GB) | **~$0.24** |
| GET requests, in-region transfer to EC2 | $0 |

`HARDWARE.md:177` budgets **$1.73/month for ~75 GB uncompressed**. Storing only
the compressed prepared corpus is ~7x cheaper; that line needs updating.

## Docs to update once this is live

- `ENGINE-PREP-PLAN.md:119` — open item closes; the "~3 h re-download+prepare"
  warning becomes "~2–3 min restage"
- `ENGINE-PREP-PLAN.md:120` — cites `tools/parallel_fetch_remaining.sh`, which
  **does not exist in `tools/`**; fix or drop the reference
- `HARDWARE.md:177` — $1.73/75 GB → ~$0.24/10 GB compressed
- `AWS-RUN-PLAN.md:93` — uncompressed `aws s3 cp` line → the key layout above
- `HARNESS-AWS-RUNBOOK.md` Phase 2 (fleet re-entry) — add the restage step
- `BUILD-RATE-MATRIX-PLAN.md:423` ("Fleet re-entry, every time") — same
- `FREEZE.md` — add the S3 URIs as a second retrieval path beside the mirror

## Related, not blocked by this

`tools/download_wikipedia.sh:20` still hardcodes
`BASE_URL="https://dumps.wikimedia.org/other/cirrus_search_index"` (~5 MB/s).
The 36-min figure came from an ad-hoc fetch of
`https://mirror.accum.se/mirror/wikimedia.org` (~215 MB/s), recorded only in
prose at `BUILD-RATE-MATRIX-PLAN.md:431`. S3 makes that path rare but not
dead — it is still the seed path and the fallback if the bucket is lost — so
`BASE_URL` should become an overridable variable regardless.

## Things not to get wrong

- **Bucket region must be eu-north-1.** A bucket in another region silently
  converts a free in-region transfer into a billed, slow one.
- **No access keys.** If `aws` on the box asks for credentials, the instance
  profile is not attached or the box booted before Step 4 — fix the role, do
  not create a key.
- **Same AWS account** as the instances, or the policy needs a cross-account
  bucket policy too.
- **Check `pzstd` is packaged on Amazon Linux 2023** during bring-up
  (`dnf install -y zstd; command -v pzstd`). If absent, the restage still
  works via `zstd -d` at ~35 s; do not block on it.
- **Instance store is still wiped.** This plan removes the download, not the
  `mkfs.xfs` / mount / docker-image re-pull half of re-entry. Baking an AMI
  with the images is the separate fix for that.
- The corpus in S3 is **not** a substitute for `FREEZE.md`'s public
  provenance. The public dump plus the recorded sha256 remains the
  reproducibility story; S3 is a private cache of the same bytes.
