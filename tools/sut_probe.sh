#!/usr/bin/env bash
# Run ftsbench.resource_probe ON the SUT box, controlled from the harness.
#
# The probe reads /sys/fs/cgroup locally, which DOCKER_HOST=ssh:// cannot
# carry across, so it is the one campaign component that must execute on the
# SUT itself. Engine URLs it polls for index size are localhost there.
#
#   tools/sut_probe.sh start <local-output.jsonl> <probe args...>
#   tools/sut_probe.sh stop  <local-output.jsonl>
#
# `start` launches the probe detached on the SUT writing to a scratch file;
# `stop` TERMs it (the probe flushes on TERM), waits for exit, and copies the
# series back to <local-output.jsonl> so gates and plots read it exactly as
# they would a local probe's file.
set -euo pipefail

SUT="${SUT_IP:?source tools/fleet_env.sh first}"
BENCH_ON_SUT="${SUT_BENCH_DIR:-p99/bench}"
PYTHON_ON_SUT="${SUT_PYTHON:-\$HOME/venv/bin/python3}"

CMD="${1:?usage: sut_probe.sh <start|stop> <local-output.jsonl> [probe args...]}"
LOCAL_OUT="${2:?usage: sut_probe.sh <start|stop> <local-output.jsonl> [probe args...]}"
shift 2

# %q-quote every probe argument so labels with spaces survive the remote
# shell; the ssh command string is evaluated twice otherwise.
ARGS="$(printf '%q ' "$@")"

# One probe per output file, so parallel probes (never wanted) fail loudly
# instead of fighting over one pidfile.
remote_name="fts-probe-$(basename "$LOCAL_OUT" .jsonl)"
REMOTE_OUT="/tmp/$remote_name.jsonl"
PIDFILE="/tmp/$remote_name.pid"

case "$CMD" in
  start)
    ssh "$SUT" "cd $BENCH_ON_SUT && \
      if [ -f $PIDFILE ] && kill -0 \$(cat $PIDFILE) 2>/dev/null; then \
        echo 'probe already running' >&2; exit 1; fi && \
      rm -f $REMOTE_OUT && \
      nohup $PYTHON_ON_SUT -m ftsbench.resource_probe $ARGS \
        --output $REMOTE_OUT >/tmp/$remote_name.log 2>&1 & \
      echo \$! > $PIDFILE"
    ;;
  stop)
    ssh "$SUT" "if [ -f $PIDFILE ]; then \
        pid=\$(cat $PIDFILE); \
        kill -TERM \$pid 2>/dev/null || true; \
        for i in \$(seq 1 20); do kill -0 \$pid 2>/dev/null || break; sleep 0.5; done; \
        rm -f $PIDFILE; fi"
    scp -q "$SUT:$REMOTE_OUT" "$LOCAL_OUT"
    ssh "$SUT" "rm -f $REMOTE_OUT"
    ;;
  *) echo "unknown command: $CMD" >&2; exit 2 ;;
esac
