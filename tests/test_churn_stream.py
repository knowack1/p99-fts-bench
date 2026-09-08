"""The churn stream is built once and shared, so both engines see one mix.

Two per-engine streams could differ in their add/delete ratio or in which ids
they recycled, and that difference would arrive on the S28 chart as an engine
property rather than as a harness one.
"""
import pytest

from ftsbench import churn_stream

DOCUMENTS = [{"title": f"t{i}", "body": f"body {i}"} for i in range(4)]


def stream(ring_size: int = 4, batch_size: int = 4) -> churn_stream.ChurnStream:
    return churn_stream.ChurnStream(DOCUMENTS, ring_size, batch_size)


def drain(source: churn_stream.ChurnStream, batches: int) -> list:
    return [item for _ in range(batches) for item in source.next_batch().items]


def test_every_batch_is_exactly_the_batch_size():
    """The driver paces at rate / batch_size operations per second, so a short
    batch would make the offered item rate silently lower than its label."""
    items = stream(batch_size=7).next_batch().items
    assert len(items) == 7


def test_no_delete_is_issued_before_the_ring_fills():
    """Deleting an id that was never added measures a tombstone write against
    nothing, and on OpenSearch it is a 404 the gate would have to forgive."""
    items = drain(stream(ring_size=8, batch_size=4), batches=2)
    assert all(item.kind == churn_stream.ADD for item in items)


def test_the_steady_state_deletes_once_per_add():
    """Index size constant to within the ring is what makes the row a churn
    measurement rather than a slow build."""
    source = stream(ring_size=4, batch_size=4)
    drain(source, batches=1)
    before_adds, before_deletes = source.adds, source.deletes
    drain(source, batches=10)
    assert source.adds - before_adds == source.deletes - before_deletes


def test_the_ring_stays_at_its_size_once_full():
    source = stream(ring_size=4, batch_size=4)
    drain(source, batches=12)
    assert source.ring_outstanding == 4


def test_the_op_kind_separates_the_warm_in_from_the_steady_state():
    """The warm-in really is a different operation — no deletes exist yet — and
    a single per-loader op_kind would label the two alike, which is why the
    batch carries its own."""
    source = stream(ring_size=4, batch_size=4)
    assert source.next_batch().op_kind == churn_stream.OP_WARM_IN
    assert source.next_batch().op_kind == churn_stream.OP_STEADY


def test_the_sequence_is_deterministic_across_two_constructions():
    first = [(item.kind, item.doc_id) for item in drain(stream(), 6)]
    second = [(item.kind, item.doc_id) for item in drain(stream(), 6)]
    assert first == second


def test_the_batch_size_does_not_change_the_item_sequence():
    """The engines are offered the same items in the same order whatever the
    batch size, so a batch-size sensitivity run stays comparable."""
    wide = [(item.kind, item.doc_id) for item in drain(stream(batch_size=8), 3)]
    narrow = [(item.kind, item.doc_id) for item in drain(stream(batch_size=4), 6)]
    assert wide == narrow


def test_deleted_ids_are_the_oldest_first():
    source = stream(ring_size=3, batch_size=3)
    added = [item.doc_id for item in drain(source, 1)]
    deleted = [item.doc_id for item in drain(source, 3)
               if item.kind == churn_stream.DELETE]
    assert deleted[:len(added)] == added


def test_every_add_carries_a_real_corpus_body():
    for item in drain(stream(), 4):
        if item.kind == churn_stream.ADD:
            assert item.document["body"] in {doc["body"] for doc in DOCUMENTS}


def test_a_delete_carries_no_document():
    source = stream(ring_size=2, batch_size=4)
    deletes = [item for item in drain(source, 3)
               if item.kind == churn_stream.DELETE]
    assert deletes and all(item.document is None for item in deletes)


def test_ids_restart_from_zero_so_later_rows_overwrite_earlier_ones():
    """Pre-existing behaviour of every S28 artifact on disk: from the second
    row onward the adds overwrite ids an earlier row created and deleted, so
    the engine sees a different tombstone and merge load than on row one.
    Asserted rather than fixed, so it cannot change by accident and make new
    rows incomparable with old ones.
    """
    assert churn_stream.churn_id(0) == churn_stream.churn_id(0)
    assert drain(stream(), 1)[0].doc_id == churn_stream.churn_id(0)


@pytest.mark.parametrize("batches", [1, 5, 20])
def test_the_counters_match_the_items_emitted(batches):
    source = stream(ring_size=4, batch_size=4)
    items = drain(source, batches)
    assert source.adds == sum(1 for item in items if item.kind == churn_stream.ADD)
    assert source.deletes == sum(1 for item in items
                                 if item.kind == churn_stream.DELETE)
