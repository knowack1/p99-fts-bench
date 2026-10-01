# enwiki K-sweep validation — runbook for a delegated agent

Purpose: prove on **enwiki** what was proved on simplewiki — that phrase
latency is flat in `LIMIT` before the patch and scales with it after, and
where OpenSearch sits. This validates the *mechanism* at the target corpus.
It does **not** reproduce the deck's headline 320.6 ms, which needs the full
corpus and the tuned `.env.sut` protocol.

## Preconditions (a human must do these first)

1. Start both `-priv` instances (AWS console — no credentials on the laptop).
2. Public IPs change on every start. Update `~/.ssh/config` for
   `fts-harness-priv` and `fts-sut-priv`.
3. Verify: `ssh fts-harness-priv true && ssh fts-sut-priv true`.

Private IPs never change: harness `172.31.8.140`, SUT `172.31.13.225`.

## What survives a stop, and what does not

Instance store (`/mnt/nvme`) is **wiped** on every stop. Root EBS persists:
`~/corpus.jsonl.zst` (10.9 GB), `~/bench`, `~/.cargo` + `~/.rustup`, and the
docker image cache in `/var/lib/docker`.

## Steps

### 1. Re-provision both boxes
```bash
# both boxes
sudo mkfs.xfs -f -q /dev/nvme0n1 && sudo mkdir -p /mnt/nvme \
  && sudo mount /dev/nvme0n1 /mnt/nvme && sudo chown ec2-user:ec2-user /mnt/nvme
# SUT only — root volume is 8 GB, far too small for an index
sudo systemctl stop docker docker.socket
echo '{"data-root": "/mnt/nvme/docker"}' | sudo tee /etc/docker/daemon.json
sudo systemctl start docker
```

### 2. Corpus and venv on the harness
```bash
mkdir -p /mnt/nvme/data /mnt/nvme/work
pzstd -d -p 8 -f -o /mnt/nvme/data/corpus.jsonl ~/corpus.jsonl.zst   # ~1m22s
python3.12 -m venv /mnt/nvme/work/venv
/mnt/nvme/work/venv/bin/pip install -q -r /mnt/nvme/work/bench/requirements.txt
```
Expected: 8,967,625 lines, sha256
`1700bb6c9b2652cf7b248e8caff7bfecc54fd2376e9a75e43379aaa79c50c432`.

### 3. Sync bench + the patched sources from the laptop
```bash
rsync -az --delete --exclude='.venv' --exclude='data' --exclude='results' \
  --exclude='.git' --exclude='target' --exclude='__pycache__' \
  ~/Projects/Scylla/p99/bench/ fts-harness-priv:/mnt/nvme/work/bench/
rsync -az --exclude='target' ~/Projects/Scylla/tantivy-p99/ \
  fts-harness-priv:/mnt/nvme/tantivy-p99/
rsync -az --exclude='target' ~/Projects/Scylla/vector-store-p99-phrase-prune/ \
  fts-harness-priv:/mnt/nvme/vector-store-p99/
```
`Cargo.toml` already carries `tantivy = { path = "../tantivy-p99" }`, and the
relative path resolves correctly with this layout.

### 4. Build both arm64 binaries natively on the harness
Build **natively with cargo** — do not use `scripts/build-dockers`, which
defaults to `--platform linux/amd64` and dies with `exec format error` on
Graviton.

```bash
. "$HOME/.cargo/env"; cd /mnt/nvme/vector-store-p99
CARGO_TARGET_DIR=/mnt/nvme/vs-target cargo build --release --bin vector-store
cp /mnt/nvme/vs-target/release/vector-store /mnt/nvme/bins/vector-store-v1-prune

# control: same commit, stock tantivy, pinned to the SAME version
sed -i 's|^tantivy = { path = "../tantivy-p99" }|# &|' Cargo.toml
cargo update -p tantivy --precise 0.26.1      # else Cargo resolves UP to 0.26.2
CARGO_TARGET_DIR=/mnt/nvme/vs-target cargo build --release --bin vector-store
cp /mnt/nvme/vs-target/release/vector-store /mnt/nvme/bins/vector-store-v0-control
sed -i 's|^# tantivy = { path = "../tantivy-p99" }|tantivy = { path = "../tantivy-p99" }|' Cargo.toml
```

Images: base them on a runtime whose glibc is **not older than the build
host's** (ubi9-minimal failed against Fedora 44 with `GLIBC_2.38 not found`;
on AL2023 build hosts ubi9 may still be wrong — check `ldd --version` on the
harness and pick a matching base). Then `docker save | ssh fts-sut-priv docker load`.

### 5. Measure — one engine at a time, never both
Both stacks on one 8-vCPU box contend for CPU and the numbers stop being
quotable. Bring one up, measure, tear it down, bring the next up.

Use `bench/docker/.env.recall-check` sizing (lightweight) but **run the arms
sequentially**. Required `-priv` overrides on the ScyllaDB stack:
```bash
export SCYLLA_BROADCAST_RPC=172.31.13.225
export SCYLLA_VS_URI=http://172.31.13.225:16080
export VECTOR_STORE_IMAGE=<the tag being measured>
```

Load **the same 500,000-document slice** into each engine
(`MAX_DOCS=500000`, `CORPUS=/mnt/nvme/data/corpus.jsonl`), from the harness,
pointing at `172.31.13.225`. Use `GEN_CPUSET=` (empty) — the Makefile default
`12-19` does not exist on an 8-core box.

**Warm OpenSearch before recording.** Run the probe once and discard it; three
warm-up queries per query do not JIT-warm a JVM, and a cold first pass reads
~44% slow.

Three arms:
1. OpenSearch alone
2. ScyllaDB + vector-store, control image
3. ScyllaDB + vector-store, patched image (base table persists in the volume —
   only the in-RAM index rebuilds, so no reload needed)

```bash
/mnt/nvme/work/venv/bin/python3 optimization/phrase/probe_phrase.py 15 out.json
```

Readiness gate: poll the **bm25 endpoint**, not the status endpoint — after a
restart the status endpoint still reports the previous index as `SERVING` at
full count while the new container is scanning, and the benchmark then dies on
`503`. See `run_ab.sh:wait_ready`.

## Deliverable

`results/enwiki-phrase-validation-<date>/` with the three probe JSONs, the
segment count per arm, and a short README stating: whether the control is flat
in K, whether the patched arm scales with K, and the three-way ratios at
K = 1 / 10 / 100 / 1000.

## Expected shapes (this is what would falsify the diagnosis)

| arm | expected |
|---|---|
| OpenSearch | rises with K — it prunes |
| control | **flat** — the whole claim rests on this |
| patched | rises with K, below the control everywhere |

If the control is **not** flat on enwiki, the simplewiki diagnosis does not
transfer and the chapter's framing must change. Report that loudly rather than
explaining it away.

## Cost

~60 min of fleet time, two i8g.2xlarge. Stop the instances afterwards — and
note the console needs a click every few minutes or the session expires before
you can stop them.
