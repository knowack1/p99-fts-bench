"""Driver-shaped stand-ins: enough of `Session`, `ResponseFuture` and `Cluster`
to drive the queue, the workers and the topology read without a ScyllaDB node.

Completions are deferred with `call_later` rather than fired inside
`add_callbacks`, so requests genuinely overlap and `max_in_flight` measures the
real in-flight bound.
"""
import asyncio


class FakeResponseFuture:
    def __init__(self, session: "FakeSession", should_fail: bool) -> None:
        self.session = session
        self.should_fail = should_fail

    def add_callbacks(self, callback, errback) -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(self.session.latency_s, self.settle, callback, errback)

    def settle(self, callback, errback) -> None:
        self.session.in_flight -= 1
        if self.should_fail:
            errback(RuntimeError("wire is busy"))
            return
        callback(None)


class FakeRows:
    def __init__(self, release_version: str | None) -> None:
        self.release_version = release_version

    def one(self):
        return None if self.release_version is None else self


class FakeSession:
    def __init__(self, latency_s: float = 0.0,
                 failing_positions: frozenset[int] = frozenset()) -> None:
        self.latency_s = latency_s
        self.failing_positions = failing_positions
        self.release_version = "2026.3.0-rc2"
        self.sent = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.params_seen: list = []

    def execute_async(self, statement, params) -> FakeResponseFuture:
        self.sent += 1
        self.params_seen.append(params)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        return FakeResponseFuture(self, self.sent in self.failing_positions)

    def prepare(self, query: str) -> str:
        self.prepared = query
        return query

    def execute(self, query: str) -> FakeRows:
        self.executed = query
        return FakeRows(self.release_version)


class FakeTablets:
    def __init__(self, answer: bool) -> None:
        self.answer = answer

    def table_has_tablets(self, keyspace: str, table: str) -> bool:
        return self.answer


class FakeMetadata:
    def __init__(self, tablets) -> None:
        self._tablets = tablets


class FakeRoutingPolicy:
    def __init__(self) -> None:
        self._child_policy = object()


class FakeProfileManager:
    def __init__(self, policy) -> None:
        self.default = type("Profile", (), {"load_balancing_policy": policy})()


class FakeCluster:
    connection_class = type("LibevConnection", (), {})
    protocol_version = 5
    compression = False

    def __init__(self, stats: dict | None = None, tablets=None) -> None:
        self.stats = stats
        self.metadata = FakeMetadata(tablets)
        self.profile_manager = FakeProfileManager(FakeRoutingPolicy())

    def is_shard_aware(self) -> bool:
        return bool(self.stats)

    def shard_aware_stats(self) -> dict | None:
        return self.stats
