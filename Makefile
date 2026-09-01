WIKI ?= simplewiki
DUMP_DATE ?= 20260816
DATA_DIR ?= data
CORPUS ?= $(DATA_DIR)/corpus.jsonl
QUERIES ?= $(DATA_DIR)/queries.json
MAX_DOCS ?= 0
OS_URL ?= http://localhost:9200
OS_INDEX ?= wiki-articles
OS_INDEX_RECREATE ?=
SCYLLA_HOSTS ?= 127.0.0.1
SCYLLA_PORT ?= 19042
VS_URL ?= http://localhost:16080
KEYSPACE ?= wiki
VS_INDEX ?= articles_body_fts
SHARD_GLOB ?= $(DATA_DIR)/$(DUMP_DATE)/index_name=$(WIKI)_content/$(WIKI)_content-$(DUMP_DATE)-*.json.bz2

# Prefer the project venv when it exists: matplotlib and scylla-driver are
# installed there, not system-wide. Override with PYTHON=... to use another one.
PYTHON ?= $(if $(wildcard .venv/bin/python3),.venv/bin/python3,python3)

COMPOSE_ENV ?= docker/.env
# OS_RAM_INDEX=1 adds the tmpfs overlay, putting OpenSearch's segment files in
# RAM for the ScyllaDB-parity experiment. It must be set for every target in a
# run: with it on `up` but off `down`, the two act on different definitions.
OS_RAM_INDEX ?=
COMPOSE_OS_OVERLAY = $(if $(strip $(OS_RAM_INDEX)),-f docker/docker-compose.opensearch.ramindex.yml,)
COMPOSE_OS ?= docker compose -f docker/docker-compose.opensearch.yml $(COMPOSE_OS_OVERLAY) --env-file $(COMPOSE_ENV)
COMPOSE_SCYLLA ?= docker compose -f docker/docker-compose.scylla.yml --env-file $(COMPOSE_ENV)

# cqlsh is not on the host PATH and is not a scylla-driver dependency, but the
# compose healthcheck proves it exists inside the container. .cql files live on
# the host, so they are piped in on stdin rather than passed with -f.
CQLSH ?= docker exec -i fts-bench-scylla cqlsh

C1_INTERVAL ?= 1
C1_IDLE_TIMEOUT ?= 30
CACHE_STATE ?= cold
LABEL ?= unlabelled run
REP ?= 1

# The monitor stops on (docs >= this AND index SERVING) instead of waiting out
# IDLE_TIMEOUT after every run. It must match the corpus exactly: a lower value
# would stop the monitor mid-build and a higher one falls back to the timeout.
C1_UNTIL_DOCS ?= 270269

# Both OpenSearch configurations run in the same campaign, and the only
# difference between them is refresh_interval. Every OpenSearch artifact name
# therefore carries the configuration rather than just "opensearch": with a
# shared name the second configuration silently overwrites the first, and the
# chart then draws one configuration twice under two different labels.
OS_CONFIG ?= opensearch
# The ScyllaDB counterpart. The two paths — bootstrap and CDC — are separate
# configurations measured back to back, so anything not named after the path is
# written twice and only the second survives.
SCYLLA_CONFIG ?= scylla-cdc

# One series and one manifest per (config, repetition): repetitions must not
# clobber each other, and ftsbench.plot_c1 globs c1-<config>-*.jsonl.
C1_OS_SERIES ?= $(DATA_DIR)/c1-$(OS_CONFIG)-$(REP).jsonl
C1_SCYLLA_BOOTSTRAP_SERIES ?= $(DATA_DIR)/c1-scylla-bootstrap-$(REP).jsonl
C1_SCYLLA_CDC_SERIES ?= $(DATA_DIR)/c1-scylla-cdc-$(REP).jsonl
C1_OS_MANIFEST ?= $(DATA_DIR)/manifest-$(OS_CONFIG)-$(REP).json
C1_SCYLLA_BOOTSTRAP_MANIFEST ?= $(DATA_DIR)/manifest-scylla-bootstrap-$(REP).json
C1_SCYLLA_CDC_MANIFEST ?= $(DATA_DIR)/manifest-scylla-cdc-$(REP).json

# --- Ingest knobs: equal on both sides by default --------------------------
# The prior C1 probe sent OpenSearch one serial _bulk at a time while ScyllaDB
# ran at concurrency 128, so it compared two clients, not two engines. These
# default to one shared value for that reason. A C3 run in particular MUST pass
# the same batch size to both loaders: batch size sets how many documents one
# recorded latency covers, and comparing a 500-doc p99 against a 2000-doc p99
# is comparing two different quantities.
BATCH_SIZE ?= 500
INGEST_CONCURRENCY ?= 8
OS_CONCURRENCY ?= $(INGEST_CONCURRENCY)
SCYLLA_CONCURRENCY ?= $(INGEST_CONCURRENCY)
# 0 = as fast as the loader can go (C1). C3 wants a fixed, sustainable rate
# instead, so the tail reflects the engine and not the client's own backlog.
TARGET_RATE ?= 0

# The load generator runs on the host and must not share cores with the engine
# containers, which are pinned to ENGINE_CPUSET (0-11) in docker/.env. Unlike a
# container, a host process IS pinned by taskset. Set GEN_CPUSET= to disable.
GEN_CPUSET ?= 12-19
TASKSET ?= $(if $(strip $(GEN_CPUSET)),taskset -c $(GEN_CPUSET),)

# OpenSearch refresh_interval. The campaign runs 1s and 30s as two separate
# configurations: 30s is a real throughput tuning, and choosing it silently
# would be picking the flattering setting for one engine.
OS_REFRESH ?= 1s

# --- Query-side knobs ------------------------------------------------------
QUERY_DURATION ?= 60
QUERY_WARMUP ?= 15
QUERY_CONCURRENCY ?= 16
QUERY_RATE ?= 100
QUERY_LIMIT ?= 10
QUERY_CLASS ?= rare_term
QUERY_SEED ?= 99

# --- Probe knobs -----------------------------------------------------------
PROBE_INTERVAL ?= 1
PROBE_DURATION ?= 0
FRESHNESS_REPS ?= 20
OS_CONTAINER ?= fts-bench-opensearch
SCYLLA_CONTAINER ?= fts-bench-scylla
VS_CONTAINER ?= fts-bench-vector-store

# One loader invocation per engine, appended to rather than copied: C1 (ceiling),
# C3 (paced, latency-logged) and the bare load targets differ only in the
# pacing and logging flags, and a flag that drifted between them would compare
# two different clients again.
OS_LOAD = $(TASKSET) $(PYTHON) -m ftsbench.opensearch_load --corpus $(CORPUS) \
	--url $(OS_URL) --index $(OS_INDEX) --max-docs $(MAX_DOCS) \
	--batch-size $(BATCH_SIZE) --concurrency $(OS_CONCURRENCY) \
	--label "$(LABEL)" --cache-state $(CACHE_STATE)
# Diagnostic only; 0 keeps the per-row prepared-statement path.
SCYLLA_UNLOGGED_BATCH_ROWS ?= 0
SCYLLA_LOAD = $(TASKSET) $(PYTHON) -m ftsbench.scylla_load --corpus $(CORPUS) \
	--hosts $(SCYLLA_HOSTS) --port $(SCYLLA_PORT) --max-docs $(MAX_DOCS) \
	--batch-size $(BATCH_SIZE) --concurrency $(SCYLLA_CONCURRENCY) \
	--unlogged-batch-rows $(SCYLLA_UNLOGGED_BATCH_ROWS) \
	--label "$(LABEL)" --cache-state $(CACHE_STATE)

.PHONY: download corpus queries os-up os-wait os-down os-reset os-index os-reindex os-load \
        os-verify-analyzer os-relax-watermarks scylla-up scylla-wait scylla-down scylla-reset \
        scylla-schema scylla-index scylla-serving scylla-load \
        bench-os bench-scylla c1-os c1-scylla-bootstrap c1-scylla-cdc \
        c1-report c1-plot \
        c3-os c3-scylla-cdc c4-os c4-scylla c5-os c5-scylla c6-os c6-scylla \
        c7-os c7-scylla c8-os c8-scylla-bootstrap c8-scylla-cdc \
        calibrate-os calibrate-scylla campaign-laptop campaign-smoke \
        co-check-os co-check-scylla results

download:
	tools/download_wikipedia.sh $(WIKI) $(DATA_DIR) $(DUMP_DATE)

corpus:
	$(PYTHON) -m ftsbench.prepare_corpus \
		--input '$(SHARD_GLOB)' \
		--output $(CORPUS) --max-docs $(MAX_DOCS)

queries:
	$(PYTHON) -m ftsbench.generate_queries --corpus $(CORPUS) --output $(QUERIES)

os-up:
	$(COMPOSE_OS) up -d

os-wait:
	until curl -fsS $(OS_URL) >/dev/null 2>&1; do echo "waiting for OpenSearch..."; sleep 2; done

os-down:
	$(COMPOSE_OS) down

# `down` keeps the named volume, so an index built by an earlier run — possibly
# by an earlier image tag — survives into the next one and cache_state=cold
# stops being true. Every cold repetition must go through here, not os-down.
os-reset:
	$(COMPOSE_OS) down -v

# create_index.sh refuses to clobber an existing index, so repeated C1
# repetitions go through os-reindex rather than dropping it by hand.
OS_INDEX_CONFIG ?= index-config.json

# The analyzer gate runs here, not as an optional follow-up. An index whose
# analyzer does not match the vector-store's is not a comparison, and an
# analyzer cannot be changed on a live index — so the only useful moment to
# fail is immediately after creation, before anything is loaded into it.
os-index:
	OS_URL=$(OS_URL) OS_REFRESH_INTERVAL=$(OS_REFRESH) \
		OS_INDEX_CONFIG=$(OS_INDEX_CONFIG) \
		opensearch/create_index.sh $(OS_INDEX) $(OS_INDEX_RECREATE)
	$(MAKE) os-verify-analyzer

os-reindex: OS_INDEX_RECREATE = --recreate
os-reindex: os-index

os-load:
	$(OS_LOAD) --target-rate $(TARGET_RATE)

os-verify-analyzer:
	OS_URL=$(OS_URL) opensearch/verify_analyzer.sh $(OS_INDEX)

# This host keeps Docker's data root on a filesystem that sits above the 90%
# high watermark with tens of GB still free, and OpenSearch responds by setting
# cluster.blocks.create_index, which fails every os-index with a bare 403. The
# percentage heuristic is what is wrong at this disk size, not the free space,
# so the thresholds are moved rather than the data. Recorded in TUNING.md: it is
# a deviation from stock, and any run that needed it must say so.
OS_WATERMARK_LOW ?= 97%
OS_WATERMARK_HIGH ?= 98%
OS_WATERMARK_FLOOD ?= 99%

os-relax-watermarks:
	OS_URL=$(OS_URL) OS_WATERMARK_LOW=$(OS_WATERMARK_LOW) \
		OS_WATERMARK_HIGH=$(OS_WATERMARK_HIGH) \
		OS_WATERMARK_FLOOD=$(OS_WATERMARK_FLOOD) \
		opensearch/relax_watermarks.sh

scylla-up:
	$(COMPOSE_SCYLLA) up -d

# Both halves must answer before the stack is usable: CQL alone is not enough,
# because fulltext DDL is served by the vector-store and CREATE CUSTOM INDEX
# fails if it is not yet up.
scylla-wait:
	until $(CQLSH) -e 'SELECT now() FROM system.local' >/dev/null 2>&1; do \
		echo "waiting for ScyllaDB..."; sleep 2; done
	until curl -fsS $(VS_URL)/api/v1/status >/dev/null 2>&1; do \
		echo "waiting for vector-store..."; sleep 2; done

scylla-down:
	$(COMPOSE_SCYLLA) down

# The vector-store index is in RAM and dies with the container, but the base
# table survives in the scylla-data volume. A cold repetition needs both gone,
# otherwise the next load writes into a table that already holds the corpus.
scylla-reset:
	$(COMPOSE_SCYLLA) down -v

scylla-schema:
	$(CQLSH) < scylladb/schema.cql

scylla-index:
	$(CQLSH) < scylladb/index.cql

# CREATE CUSTOM INDEX returns before the index is usable, so the CDC run has to
# gate on SERVING or it would measure the tail of the bootstrap scan instead.
scylla-serving:
	until [ "$$(curl -fsS $(VS_URL)/api/v1/indexes/$(KEYSPACE)/$(VS_INDEX)/status \
		| python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' \
		2>/dev/null)" = "SERVING" ]; do \
		echo "waiting for $(VS_INDEX) to reach SERVING..."; sleep 2; done

scylla-load:
	$(SCYLLA_LOAD) --target-rate $(TARGET_RATE)

bench-os:
	$(PYTHON) -m ftsbench.query_bench --engine opensearch --queries $(QUERIES) \
		--url $(OS_URL) --index $(OS_INDEX) \
		--output $(DATA_DIR)/results-opensearch.json

bench-scylla:
	$(PYTHON) -m ftsbench.query_bench --engine scylladb --queries $(QUERIES) \
		--hosts $(SCYLLA_HOSTS) --port $(SCYLLA_PORT) \
		--output $(DATA_DIR)/results-scylladb.json

# --- C1: index build throughput (docs/s vs wall time) ---------------------
# Each config runs the sampler in the background and the measured work in the
# foreground; the sampler, not the work, decides when the run is over — index
# DDL returns long before the index finishes building. CACHE_STATE and LABEL
# land in the series header and the chart footer.

C1_MONITOR_OS = --engine opensearch --url $(OS_URL) --index $(OS_INDEX)
C1_MONITOR_SCYLLA = --engine scylladb --vs-url $(VS_URL) \
	--keyspace $(KEYSPACE) --vs-index $(VS_INDEX)
# The idle timeout only fires once the doc count has moved, so a load that dies
# before indexing anything leaves the monitor sampling zeroes forever. The
# wall-clock cap bounds that; it must stay well above a healthy build.
C1_MAX_SECONDS ?= 600

C1_MONITOR_COMMON = --interval $(C1_INTERVAL) \
	--idle-timeout $(C1_IDLE_TIMEOUT) --until-docs $(C1_UNTIL_DOCS) \
	--max-seconds $(C1_MAX_SECONDS) --corpus $(CORPUS) \
	--label "$(LABEL)" --cache-state $(CACHE_STATE)

# C1 measures the ceiling, so no --target-rate: the loader runs unpaced.
C1_LOAD_OS = $(OS_LOAD)
C1_LOAD_SCYLLA = $(SCYLLA_LOAD)
C1_CREATE_INDEX = $(CQLSH) < scylladb/index.cql

# $(1) manifest path  $(2) config name  $(3) series path  $(4) --command flags
# Recorded before the measured work so the version probes hit a live stack; the
# image tags and engine versions are unrecoverable once the stack is torn down.
define manifest
@$(PYTHON) -m ftsbench.run_manifest --output $(1) --config $(2) --rep $(REP) \
	--label "$(LABEL)" --cache-state $(CACHE_STATE) \
	--series $(3) --corpus $(CORPUS) --max-docs $(MAX_DOCS) \
	--env-file $(COMPOSE_ENV) --os-url $(OS_URL) --vs-url $(VS_URL) \
	--scylla-hosts $(SCYLLA_HOSTS) --scylla-port $(SCYLLA_PORT) $(4)
endef

# $(1) engine-specific monitor flags  $(2) series path  $(3) measured command
define c1_run
@$(PYTHON) -m ftsbench.build_monitor $(1) --output $(2) $(C1_MONITOR_COMMON) & \
	MON=$$!; \
	sleep 2; \
	$(3); \
	LOAD_RC=$$?; \
	wait $$MON; \
	exit $$LOAD_RC
@$(PYTHON) -m ftsbench.build_report $(2)
endef

c1-os:
	$(call manifest,$(C1_OS_MANIFEST),$(OS_CONFIG),$(C1_OS_SERIES),--command "make os-index" --command "make c1-os")
	$(call c1_run,$(C1_MONITOR_OS),$(C1_OS_SERIES),$(C1_LOAD_OS))

# Bootstrap path: the base table is already loaded, so the measured work is the
# base-table scan that CREATE CUSTOM INDEX kicks off. Run `make scylla-schema
# scylla-load` first — that write is setup, not the measurement.
c1-scylla-bootstrap:
	$(call manifest,$(C1_SCYLLA_BOOTSTRAP_MANIFEST),scylla-bootstrap,$(C1_SCYLLA_BOOTSTRAP_SERIES),--command "make scylla-schema" --command "make scylla-load" --command "make c1-scylla-bootstrap")
	$(call c1_run,$(C1_MONITOR_SCYLLA),$(C1_SCYLLA_BOOTSTRAP_SERIES),$(C1_CREATE_INDEX))

# CDC path: the index already exists and is SERVING, so the measured work is the
# base-table write plus the CDC hop. Run `make scylla-schema scylla-index
# scylla-serving` first.
c1-scylla-cdc:
	$(call manifest,$(C1_SCYLLA_CDC_MANIFEST),scylla-cdc,$(C1_SCYLLA_CDC_SERIES),--command "make scylla-schema" --command "make scylla-index" --command "make c1-scylla-cdc")
	$(call c1_run,$(C1_MONITOR_SCYLLA),$(C1_SCYLLA_CDC_SERIES),$(C1_LOAD_SCYLLA))

# c1-opensearch-[0-9]* rather than c1-opensearch-*: the plain glob also matches
# every c1-opensearch-refresh30-* file, which reported each refresh=30s
# repetition twice.
C1_SERIES_GLOB = $(DATA_DIR)/c1-opensearch-[0-9]*.jsonl \
	$(DATA_DIR)/c1-opensearch-refresh30-*.jsonl \
	$(DATA_DIR)/c1-scylla-bootstrap-*.jsonl \
	$(DATA_DIR)/c1-scylla-cdc-*.jsonl

c1-report:
	@test -n "$(strip $(wildcard $(C1_SERIES_GLOB)))" \
		|| { echo "no C1 series in $(DATA_DIR) — run c1-os / c1-scylla-* first"; exit 1; }
	$(PYTHON) -m ftsbench.build_report $(wildcard $(C1_SERIES_GLOB))

C1_PLOT_OUTPUT ?= results/c1.png
C1_PLOT_TITLE ?= Index build throughput
C1_PLOT_FOOTER ?=

# The globs are passed through unexpanded: plot_c1 expands them itself and
# warns about a config with no series instead of failing the whole chart.
c1-plot:
	$(PYTHON) -m ftsbench.plot_c1 \
		--config 'opensearch:$(DATA_DIR)/c1-opensearch-[0-9]*.jsonl' \
		--config 'opensearch-refresh30:$(DATA_DIR)/c1-opensearch-refresh30-*.jsonl' \
		--config 'scylla-bootstrap:$(DATA_DIR)/c1-scylla-bootstrap-*.jsonl' \
		--config 'scylla-cdc:$(DATA_DIR)/c1-scylla-cdc-*.jsonl' \
		--output $(C1_PLOT_OUTPUT) --title "$(C1_PLOT_TITLE)" \
		--footer-extra "$(C1_PLOT_FOOTER)"

# --- C3: write latency during ingest (p99 / p999) --------------------------
# The most important chart in the talk. Paced on purpose: a loader running flat
# out queues work inside itself, and its own backlog then appears in the tail as
# if the engine had produced it. C3_RATE must be a rate BOTH engines sustain, so
# it is chosen from the C1 result of the slower side, not from either ceiling.
# Both loaders get the same BATCH_SIZE: batch size decides how many documents one
# recorded latency covers, and a 500-doc p99 is not comparable to a 2000-doc p99.
C3_RATE ?= 2000
# C3 needs its own batch size, smaller than the global BATCH_SIZE that C1 uses
# for throughput. One recorded latency covers one batch, so operations per bucket
# is C3_RATE / C3_BATCH * bucket_s -- and that does NOT grow with the corpus. At
# the global 500 it was 2000/500*5 = 20 operations per 5 s bucket against a floor
# of 100 for p99 and 1000 for p999, so every bucket was refused and the laptop
# chart came out blank. At 10 it is 1000 per bucket: p99 with ten times its floor,
# p999 exactly at its floor. Raising the corpus 73x would not have fixed this.
C3_BATCH ?= 10
C3_OS_LOG ?= $(DATA_DIR)/c3-$(OS_CONFIG)-$(REP).jsonl
C3_SCYLLA_CDC_LOG ?= $(DATA_DIR)/c3-scylla-cdc-$(REP).jsonl
C3_OS_MANIFEST ?= $(DATA_DIR)/manifest-c3-$(OS_CONFIG)-$(REP).json
C3_SCYLLA_CDC_MANIFEST ?= $(DATA_DIR)/manifest-c3-scylla-cdc-$(REP).json

# C3 was the only phase measured without a resource probe, and that is why the
# 1.9x step in ScyllaDB's p50 between repetitions could not be diagnosed on the
# laptop (results/laptop-simplewiki-2026-08/FINDINGS.md section 10). The leading
# hypothesis is contention inside the engine cpuset: SCYLLA_SMP gives ScyllaDB a
# fixed shard count while the vector-store shares the whole cpuset unrestricted,
# and under CDC it is indexing for the entire C3 window. Only a per-container CPU
# series taken DURING C3 can confirm or kill that, so the probe runs beside the
# paced load rather than in a phase of its own.
C3_PROBE_OS_OUT ?= $(DATA_DIR)/c3-resource-$(OS_CONFIG)-$(REP).jsonl
C3_PROBE_SCYLLA_CDC_OUT ?= $(DATA_DIR)/c3-resource-scylla-cdc-$(REP).jsonl
C3_PROBE_COMMON = --interval $(PROBE_INTERVAL) --duration 0 \
	--label "$(LABEL)" --cache-state $(CACHE_STATE) --corpus $(CORPUS)

# Unlike c1_run, the probe never stops on its own: --duration 0 means "until
# signalled", which is deliberate because the C3 window is set by the loader and
# a guessed duration would either truncate the series or outlive the run. The
# loader's status is captured before the wait, because `wait` overwrites $?.
# A dead probe does not invalidate the repetition - C3's own metric comes from
# the loader's latency log - so it warns rather than aborting, but it must warn:
# silently collecting nothing is how this phase came to be unmeasurable once.
define c3_run
@$(TASKSET) $(PYTHON) -m ftsbench.resource_probe $(1) --output $(2) \
	$(C3_PROBE_COMMON) & \
	PROBE=$$!; \
	sleep 1; \
	$(3); \
	LOAD_RC=$$?; \
	kill -TERM $$PROBE 2>/dev/null || true; \
	wait $$PROBE 2>/dev/null || true; \
	if [ ! -s $(2) ]; then \
		echo "WARNING: C3 resource probe wrote nothing to $(2) - the p50 step this probe exists to diagnose will be undiagnosable for this repetition" >&2; \
	fi; \
	exit $$LOAD_RC
endef

c3-os:
	$(call manifest,$(C3_OS_MANIFEST),$(OS_CONFIG),$(C3_OS_LOG),--command "make os-index" --command "make c3-os")
	$(call c3_run,--engine opensearch --containers $(OS_CONTAINER):opensearch --os-url $(OS_URL) --os-index $(OS_INDEX),$(C3_PROBE_OS_OUT),$(OS_LOAD) --target-rate $(C3_RATE) --batch-size $(C3_BATCH) --latency-log $(C3_OS_LOG))

c3-scylla-cdc:
	$(call manifest,$(C3_SCYLLA_CDC_MANIFEST),scylla-cdc,$(C3_SCYLLA_CDC_LOG),--command "make scylla-schema" --command "make scylla-index" --command "make c3-scylla-cdc")
	$(call c3_run,--engine scylladb --containers $(SCYLLA_CONTAINER):scylladb --containers $(VS_CONTAINER):vector-store --vs-url $(VS_URL) --keyspace $(KEYSPACE) --vs-index $(VS_INDEX),$(C3_PROBE_SCYLLA_CDC_OUT),$(SCYLLA_LOAD) --target-rate $(C3_RATE) --batch-size $(C3_BATCH) --latency-log $(C3_SCYLLA_CDC_LOG))

# --- C4: resource usage (RSS, CPU, index size) ----------------------------
# The ScyllaDB side is sampled as two containers because it IS two services; a
# probe that watched only fts-bench-scylla would understate the side by exactly
# the size of the index. resource_probe exits non-zero if the vector-store is
# omitted, so that understatement cannot happen by accident.
C4_OS_OUT ?= $(DATA_DIR)/c4-$(OS_CONFIG)-$(REP).jsonl
C4_SCYLLA_OUT ?= $(DATA_DIR)/c4-$(SCYLLA_CONFIG)-$(REP).jsonl
C4_PROBE_COMMON = --interval $(PROBE_INTERVAL) --duration $(PROBE_DURATION) \
	--label "$(LABEL)" --cache-state $(CACHE_STATE) --corpus $(CORPUS)

c4-os:
	$(TASKSET) $(PYTHON) -m ftsbench.resource_probe --engine opensearch \
		--containers $(OS_CONTAINER):opensearch \
		--os-url $(OS_URL) --os-index $(OS_INDEX) \
		--output $(C4_OS_OUT) $(C4_PROBE_COMMON)

c4-scylla:
	$(TASKSET) $(PYTHON) -m ftsbench.resource_probe --engine scylladb \
		--containers $(SCYLLA_CONTAINER):scylladb \
		--containers $(VS_CONTAINER):vector-store \
		--vs-url $(VS_URL) --keyspace $(KEYSPACE) --vs-index $(VS_INDEX) \
		--output $(C4_SCYLLA_OUT) $(C4_PROBE_COMMON)

# --- C5 / C6: query latency, single class and per class --------------------
# load_gen is open-loop: it dispatches on an absolute schedule and reports
# latency from the INTENDED start, so a slow engine cannot hide behind a client
# that waited for it. queue_ms in the artifact is what makes coordinated
# omission a measured number rather than a claim about the harness.
QUERY_COMMON = --queries $(QUERIES) --duration $(QUERY_DURATION) \
	--warmup $(QUERY_WARMUP) --concurrency $(QUERY_CONCURRENCY) \
	--limit $(QUERY_LIMIT) --seed $(QUERY_SEED) \
	--label "$(LABEL)" --cache-state $(CACHE_STATE)
QUERY_CONN_OS = --url $(OS_URL) --index $(OS_INDEX)
QUERY_CONN_SCYLLA = --hosts $(SCYLLA_HOSTS) --port $(SCYLLA_PORT) \
	--keyspace $(KEYSPACE)

C5_OS_LOG ?= $(DATA_DIR)/c5-$(OS_CONFIG)-$(QUERY_CLASS)-$(REP).jsonl
C5_SCYLLA_LOG ?= $(DATA_DIR)/c5-$(SCYLLA_CONFIG)-$(QUERY_CLASS)-$(REP).jsonl

c5-os:
	$(TASKSET) $(PYTHON) -m ftsbench.load_gen --engine opensearch \
		$(QUERY_COMMON) $(QUERY_CONN_OS) --rate $(QUERY_RATE) \
		--class $(QUERY_CLASS) --latency-log $(C5_OS_LOG)

c5-scylla:
	$(TASKSET) $(PYTHON) -m ftsbench.load_gen --engine scylladb \
		$(QUERY_COMMON) $(QUERY_CONN_SCYLLA) --rate $(QUERY_RATE) \
		--class $(QUERY_CLASS) --latency-log $(C5_SCYLLA_LOG)

# C6 is C5 repeated once per class. The classes are equal in cardinality by
# construction (QUERY-SET.md): in a fixed-duration run a class with fewer
# distinct queries gets more repeats each, so its cache is warmer and it would
# look faster for a reason that has nothing to do with the class.
QUERY_CLASSES ?= rare_term common_term phrase bool_and bool_not bool_mixed

# The six per-class logs are one repetition of C6, but plotlib reads one file as
# one repetition, so they are concatenated into a single artifact per repetition.
# The class list is passed explicitly and never globbed: data/c5-opensearch-*
# also matches every c5-opensearch-refresh30-* file.
C6_OS_LOG ?= $(DATA_DIR)/c6-$(OS_CONFIG)-$(REP).jsonl
C6_SCYLLA_LOG ?= $(DATA_DIR)/c6-$(SCYLLA_CONFIG)-$(REP).jsonl

c6-os:
	@for class in $(QUERY_CLASSES); do \
		echo "=== C6 $(OS_CONFIG) rep $(REP): $$class ==="; \
		$(MAKE) c5-os QUERY_CLASS=$$class REP=$(REP) LABEL="$(LABEL)" \
			CACHE_STATE=$(CACHE_STATE) QUERY_RATE=$(QUERY_RATE) || exit 1; \
	done
	$(TASKSET) $(PYTHON) -m ftsbench.c6_merge --source $(DATA_DIR) \
		--config $(OS_CONFIG) --rep $(REP) --output $(C6_OS_LOG)

c6-scylla:
	@for class in $(QUERY_CLASSES); do \
		echo "=== C6 $(SCYLLA_CONFIG) rep $(REP): $$class ==="; \
		$(MAKE) c5-scylla QUERY_CLASS=$$class REP=$(REP) LABEL="$(LABEL)" \
			CACHE_STATE=$(CACHE_STATE) QUERY_RATE=$(QUERY_RATE) || exit 1; \
	done
	$(TASKSET) $(PYTHON) -m ftsbench.c6_merge --source $(DATA_DIR) \
		--config $(SCYLLA_CONFIG) --rep $(REP) --output $(C6_SCYLLA_LOG)

# --- C7: QPS vs p99 sweep (the SLA knee) ----------------------------------
# --stop-when-saturated ends the sweep when the generator, not the engine, is the
# limit. Without it the curve past that point is a picture of this laptop's
# client. Run calibrate-* first and pass the result as CEILING_QPS so the sweep
# knows its own ceiling instead of discovering it as a fake knee.
C7_OS_OUT ?= $(DATA_DIR)/c7-$(OS_CONFIG)-$(REP).json
C7_SCYLLA_OUT ?= $(DATA_DIR)/c7-$(SCYLLA_CONFIG)-$(REP).json
C7_RATE_START ?= 25
C7_RATE_FACTOR ?= 2
C7_RATE_MAX ?= 3200
CEILING_QPS ?=
C7_COMMON = --duration $(QUERY_DURATION) --warmup $(QUERY_WARMUP) \
	--concurrency $(QUERY_CONCURRENCY) --limit $(QUERY_LIMIT) \
	--seed $(QUERY_SEED) --queries $(QUERIES) --class $(QUERY_CLASS) \
	--rate-start $(C7_RATE_START) --rate-factor $(C7_RATE_FACTOR) \
	--rate-max $(C7_RATE_MAX) --stop-when-saturated \
	$(if $(strip $(CEILING_QPS)),--ceiling-qps $(CEILING_QPS),) \
	--label "$(LABEL)" --cache-state $(CACHE_STATE)

c7-os:
	$(TASKSET) $(PYTHON) -m ftsbench.sweep --engine opensearch \
		$(C7_COMMON) $(QUERY_CONN_OS) --output $(C7_OS_OUT)

c7-scylla:
	$(TASKSET) $(PYTHON) -m ftsbench.sweep --engine scylladb \
		$(C7_COMMON) $(QUERY_CONN_SCYLLA) --output $(C7_SCYLLA_OUT)

# What this generator can offer on this machine. On a laptop that also hosts the
# engine, this is the number that decides whether a C7 knee is a finding.
calibrate-os:
	$(TASKSET) $(PYTHON) -m ftsbench.load_gen --engine opensearch \
		--queries $(QUERIES) --calibrate --concurrency $(QUERY_CONCURRENCY) \
		$(QUERY_CONN_OS)

calibrate-scylla:
	$(TASKSET) $(PYTHON) -m ftsbench.load_gen --engine scylladb \
		--queries $(QUERIES) --calibrate --concurrency $(QUERY_CONCURRENCY) \
		$(QUERY_CONN_SCYLLA)

# --- C8: freshness (write ack -> searchable) -------------------------------
# The probe leaves FRESHNESS_REPS documents behind, recorded as docs_added in its
# header. Run it AFTER any corpus-count assertion, or that count is ambiguous.
# The two OpenSearch configs differ only by refresh_interval, which the probe
# reads back from _settings and warns about on a mismatch.
C8_REPS ?= $(FRESHNESS_REPS)
C8_OS_OUT ?= $(DATA_DIR)/c8-$(OS_CONFIG)-$(REP).jsonl
C8_SCYLLA_BOOTSTRAP_OUT ?= $(DATA_DIR)/c8-scylla-bootstrap-$(REP).jsonl
C8_SCYLLA_CDC_OUT ?= $(DATA_DIR)/c8-scylla-cdc-$(REP).jsonl
C8_COMMON = --reps $(C8_REPS) --seed $(QUERY_SEED) --limit $(QUERY_LIMIT) \
	--label "$(LABEL)" --cache-state $(CACHE_STATE) --corpus $(CORPUS)

c8-os:
	$(TASKSET) $(PYTHON) -m ftsbench.freshness_probe --engine opensearch \
		$(C8_COMMON) $(QUERY_CONN_OS) --refresh-interval $(OS_REFRESH) \
		--output $(C8_OS_OUT)

c8-scylla-bootstrap:
	$(TASKSET) $(PYTHON) -m ftsbench.freshness_probe --engine scylladb \
		$(C8_COMMON) $(QUERY_CONN_SCYLLA) --vs-url $(VS_URL) \
		--vs-index $(VS_INDEX) --path bootstrap \
		--output $(C8_SCYLLA_BOOTSTRAP_OUT)

c8-scylla-cdc:
	$(TASKSET) $(PYTHON) -m ftsbench.freshness_probe --engine scylladb \
		$(C8_COMMON) $(QUERY_CONN_SCYLLA) --vs-url $(VS_URL) \
		--vs-index $(VS_INDEX) --path cdc \
		--output $(C8_SCYLLA_CDC_OUT)

# --- The whole campaign ----------------------------------------------------
# Serialized: four configurations, N repetitions, one engine up at a time. Hours,
# not minutes. --smoke is the end-to-end gate at 20k docs before the real run.
CAMPAIGN_ARGS ?=

campaign-laptop:
	tools/campaign_laptop.sh $(CAMPAIGN_ARGS)

campaign-smoke:
	tools/campaign_laptop.sh --smoke

# --- Verification gates ----------------------------------------------------
# The pacer must be open-loop before C5 or C7 mean anything. Run against a live,
# populated index at a rate deliberately above capacity.
CO_CHECK_RATE ?= 5000

co-check-os:
	$(TASKSET) $(PYTHON) tools/co_check.py --engine opensearch \
		--rate $(CO_CHECK_RATE) --concurrency $(QUERY_CONCURRENCY) \
		--queries $(QUERIES) --python $(PYTHON)

co-check-scylla:
	$(TASKSET) $(PYTHON) tools/co_check.py --engine scylladb \
		--rate $(CO_CHECK_RATE) --concurrency $(QUERY_CONCURRENCY) \
		--queries $(QUERIES) --python $(PYTHON)

# --- Charts and the results tree -------------------------------------------
# Every glob lives in ftsbench.render_results, once. Eight plot invocations and
# the tree that reads their sidecars must agree on which files belong to which
# configuration, and `c1-opensearch-*` also matching every
# `c1-opensearch-refresh30-*` file has already cost this campaign one chart.
# `results` must stay in .PHONY: the results/ directory exists, so make would
# otherwise consider the target already made and render nothing.
RESULTS_DIR ?= results
RUN_NAME ?= laptop-simplewiki-2026-08
RENDER_ARGS ?=

results:
	$(PYTHON) -m ftsbench.render_results --run-name $(RUN_NAME) \
		--results-root $(RESULTS_DIR) --data-dir $(DATA_DIR) $(RENDER_ARGS)
