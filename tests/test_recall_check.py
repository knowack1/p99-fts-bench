"""jaccard/recall_at_k are the whole comparison; get their edge cases pinned
before trusting the numbers a run of the script prints."""
from ftsbench import recall_check as rc


def test_jaccard_is_one_for_identical_sets():
    assert rc.jaccard(["1", "2", "3"], ["3", "2", "1"]) == 1.0


def test_jaccard_is_zero_for_disjoint_sets():
    assert rc.jaccard(["1", "2"], ["3", "4"]) == 0.0


def test_jaccard_is_one_when_both_sides_are_empty():
    assert rc.jaccard([], []) == 1.0


def test_jaccard_ignores_duplicate_ids_within_a_side():
    assert rc.jaccard(["1", "1", "2"], ["2", "3"]) == 1 / 3


def test_recall_at_k_is_full_when_every_reference_id_is_present():
    assert rc.recall_at_k(["1", "2"], ["2", "1", "3"]) == 1.0


def test_recall_at_k_is_partial_when_some_reference_ids_are_missing():
    assert rc.recall_at_k(["1", "2", "3", "4"], ["1", "3"]) == 0.5


def test_recall_at_k_is_one_when_reference_is_empty():
    assert rc.recall_at_k([], ["1", "2"]) == 1.0


def test_recall_at_k_is_zero_when_other_found_nothing_reference_did():
    assert rc.recall_at_k(["1"], []) == 0.0


def test_summarize_class_flags_only_nonempty_zero_overlap_queries():
    per_query = [
        {"query": "both empty", "opensearch_hits": 0, "scylladb_hits": 0,
         "jaccard_at_k": 1.0, "recall_at_k_scylladb_vs_opensearch": 1.0,
         "recall_at_k_opensearch_vs_scylladb": 1.0},
        {"query": "disagree", "opensearch_hits": 3, "scylladb_hits": 2,
         "jaccard_at_k": 0.0, "recall_at_k_scylladb_vs_opensearch": 0.0,
         "recall_at_k_opensearch_vs_scylladb": 0.0},
    ]
    summary = rc.summarize_class(per_query)
    assert summary["zero_overlap_queries"] == ["disagree"]
    assert summary["mean_jaccard_at_k"] == 0.5


def test_sampled_classes_takes_the_first_n_queries_per_class():
    query_set = {"classes": {"rare_term": ["a", "b", "c"], "phrase": ["x", "y"]}}
    sampled = rc.sampled_classes(query_set, None, sample_per_class=2)
    assert sampled == {"rare_term": ["a", "b"], "phrase": ["x", "y"]}


def test_sampled_classes_restricts_to_the_requested_classes():
    query_set = {"classes": {"rare_term": ["a"], "phrase": ["x"]}}
    sampled = rc.sampled_classes(query_set, ["phrase"], sample_per_class=5)
    assert sampled == {"phrase": ["x"]}
