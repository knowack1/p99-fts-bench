# Fleet environment for the AWS campaign — SOURCE this on the harness box
# before any make target or campaign script:
#
#   source tools/fleet_env.sh
#
# What it does: points every knob the Makefile reads (all `?=`, so plain
# environment variables override them) at the two-box fleet, so the same
# targets that drove the laptop drive the fleet unchanged.
#
#   - DOCKER_HOST=ssh://<sut> makes every docker CLI call — compose up/down,
#     the CQLSH `docker exec`, the OOM-kill and log gates — act on the SUT's
#     daemon transparently. The compose files and env are read LOCALLY (from
#     this checkout); only the containers are remote.
#   - resource_probe is the one thing DOCKER_HOST cannot carry: it reads
#     /sys/fs/cgroup on the machine it runs on, so on the fleet it must run
#     ON the SUT — tools/sut_probe.sh start/stop wraps that.
#   - GEN_CPUSET is emptied: the generator is isolated by being on another
#     machine, which is the whole point of the harness box. The laptop's
#     heterogeneous-core pinning does not transfer.

export SUT_IP="${SUT_IP:-172.31.47.166}"

export DOCKER_HOST="ssh://$SUT_IP"
export COMPOSE_ENV="docker/.env.sut"

export OS_URL="http://$SUT_IP:9200"
export VS_URL="http://$SUT_IP:16080"
export SCYLLA_HOSTS="$SUT_IP"
export SCYLLA_PORT=9042

export GEN_CPUSET=

# enwiki caps (ENGINE-PREP-PLAN.md Phase A item 4). C1_MAX_SECONDS is sized
# for the CDC path (~1.1 h linear estimate) with generous headroom; the
# bootstrap path is out of the campaign (WRITE-PATH-TEST-PLAN.md). The commit
# cadence is a pure 3 s on both sides, so 60 s of idle is already 20 cycles.
export C1_MAX_SECONDS=14400
export C1_IDLE_TIMEOUT=60
export C1_UNTIL_DOCS=8967625

# Docker-over-ssh opens a connection per CLI call; a ControlMaster block for
# the SUT in ~/.ssh/config makes that free:
#   Host 172.31.47.166
#     ControlMaster auto
#     ControlPath ~/.ssh/cm-%r@%h:%p
#     ControlPersist 10m
