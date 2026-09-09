"""An annotation may not cost the series it annotates.

The write-pool reading founded this campaign — a pool sitting at 0.77 of 3
active with an empty queue is what told a starved engine from a saturated one
— but it is a second, node-scoped REST call to the system under test on every
tick, and two producers never asked for it: `build_monitor` (C1) is where the
fields are wanted, while `resource_probe` (C4) calls `sample()` only for
`store_size_bytes` and discards the rest.

Two properties matter beyond "the numbers are right":

- **Opt-in.** A producer that did not ask must issue no extra request, and the
  fields must then be absent rather than null — every consumer reads them
  through `.get()`, and a null there would claim the endpoint was read and
  found empty.
- **One timeout, not one per tick.** `POOL_STATS_TIMEOUT_S` is 5 s against a
  1 s cadence, so an endpoint that stops answering would stretch the sampling
  interval to 6 s — and `build_report.summarize` derives `docs_per_s_median`,
  p10, p90, `throughput_variability` and `stall_fraction` from that cadence.
"""
import requests

from ftsbench import build_monitor, null_sink_http, resource_probe, samplers

URL = "http://sut:9200"
INDEX = "wiki-articles"
INDEX_STATS = f"/{INDEX}/_stats"
POOL_STATS = "/_nodes/stats/thread_pool"
POOL_SIZE = "/_nodes/thread_pool"
POOL_PATHS = (POOL_STATS, POOL_SIZE)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Records every path asked for, so an extra node-scoped call is visible as
    a call and not only as a field in the sample."""

    def __init__(self, dead: tuple[str, ...] = ()) -> None:
        self.paths: list[str] = []
        self._dead = dead

    def get(self, url: str, timeout: float | None = None) -> FakeResponse:
        path = url[len(URL):]
        self.paths.append(path)
        if path in self._dead:
            raise requests.exceptions.ReadTimeout("read timed out")
        if path == INDEX_STATS:
            return FakeResponse(null_sink_http.index_stats(7))
        if path == POOL_STATS:
            return FakeResponse(null_sink_http.node_thread_pool_stats())
        if path == POOL_SIZE:
            return FakeResponse(null_sink_http.node_thread_pool_info())
        raise AssertionError(f"the sampler asked for {path}")

    @property
    def pool_reads(self) -> list[str]:
        return [path for path in self.paths if path.startswith("/_nodes")]


def install(monkeypatch, dead: tuple[str, ...] = ()) -> FakeSession:
    session = FakeSession(dead)
    monkeypatch.setattr(samplers.requests, "Session", lambda: session)
    return session


def monitor_args(*flags: str):
    return build_monitor.parse_args_from(
        ["--engine", "opensearch", "--output", "-", "--url", URL,
         "--index", INDEX, *flags])


def test_a_producer_that_did_not_ask_reads_only_the_index(monkeypatch):
    """The default is off, so the two pre-existing producers keep the request
    count they were written with."""
    session = install(monkeypatch)
    sample = samplers.OpenSearchSampler(URL, INDEX).sample()
    assert session.paths == [INDEX_STATS]
    assert [field for field in samplers.WRITE_POOL_FIELDS
            if field in sample] == [], \
        "an unread pool must be absent from the sample, not null in it"


def test_the_c4_resource_probe_never_reaches_the_node_endpoint(monkeypatch):
    """C4 samples the engine once a second for `store_size_bytes` alone. A
    node-scoped pool read beside it is load on the box under measurement, added
    by a chart that does not use the answer."""
    session = install(monkeypatch)
    probes = resource_probe.EngineProbes(
        os_sampler=samplers.OpenSearchSampler(URL, INDEX), vs_sampler=None)
    assert probes.index_size_for("opensearch") == 0
    assert session.pool_reads == []


def test_the_c1_monitor_asks_for_the_write_pool(monkeypatch):
    """C1 is the series the fields exist for, so the monitor's own sampler
    still carries them."""
    session = install(monkeypatch)
    sample = samplers.build_sampler(monitor_args()).sample()
    assert POOL_STATS in session.pool_reads
    assert sample["write_pool_size"] == null_sink_http.WRITE_POOL_SIZE
    assert sample["write_queue"] == 0


def test_an_unresponsive_pool_costs_one_timeout_not_one_per_tick(monkeypatch):
    """The warning was already once per sampler; the request was not. At the
    5 s pool budget against a 1 s interval, retrying every tick collapses the
    monitor's cadence to 6 s — which is the cadence C1's p10, p90 and stall
    fraction are computed from."""
    session = install(monkeypatch, dead=POOL_PATHS)
    sampler = samplers.OpenSearchSampler(URL, INDEX, read_write_pool=True)
    samples = [sampler.sample() for _ in range(3)]
    assert len(session.pool_reads) == 1, \
        f"the dead endpoint was asked {len(session.pool_reads)} times"
    for sample in samples:
        assert sample["write_active"] is None
        assert sample["write_pool_size"] is None


def test_an_unreadable_pool_still_records_null_rather_than_zero(monkeypatch):
    """An idle pool beside an empty queue is exactly what a starved engine
    looks like, so a synthesised 0 would read as the founding finding
    itself."""
    install(monkeypatch, dead=POOL_PATHS)
    sample = samplers.OpenSearchSampler(URL, INDEX,
                                        read_write_pool=True).sample()
    assert all(sample[field] is None for field in samplers.WRITE_POOL_FIELDS)
    assert sample["docs_indexed"] == 7, \
        "the index reading must survive an unreadable pool"


def test_a_dead_pool_size_endpoint_does_not_blind_the_live_one(monkeypatch):
    """The two node endpoints fail independently: the configured pool size
    comes from /_nodes/thread_pool and the levels from
    /_nodes/stats/thread_pool.
    Latching both on one failure would report null levels out of an endpoint
    that is still answering, which is the starved-engine picture again."""
    session = install(monkeypatch, dead=(POOL_SIZE,))
    sampler = samplers.OpenSearchSampler(URL, INDEX, read_write_pool=True)
    samples = [sampler.sample() for _ in range(3)]
    assert session.paths.count(POOL_SIZE) == 1, \
        "the dead endpoint was asked more than once"
    assert session.paths.count(POOL_STATS) == 3, \
        "a live endpoint stopped being read because another one died"
    for sample in samples:
        assert sample["write_active"] == 0
        assert sample["write_pool_size"] is None
