"""rare_term must be an absolute low-document-frequency band, not one that
scales with corpus size. The earlier band (doc_count // 1000 to
doc_count // 100) matched 9,000-90,000 documents on the enwiki corpus, which
is not rare by any reading of the class name — these tests pin the fixed
replacement and the safeguards added alongside it.

phrase had a matching problem: punctuation-stripped abbreviations like
"U.S." or "e.g." tokenize into adjacent single-letter fragments ("u", "s"),
which used to have no minimum length and could out-rank real phrases purely
from abbreviation frequency. These tests pin the length filter and the
extension to 3-word phrases.
"""
import collections
import json
import os
import pathlib
import random
import string
import subprocess
import sys

import pytest

from ftsbench import generate_queries as gq

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _alpha_word(i: int) -> str:
    letters = string.ascii_lowercase
    a, b = divmod(i, 26)
    return f"term{letters[a % 26]}{letters[b]}"


def term_df_with(counts: dict[str, int]) -> collections.Counter:
    return collections.Counter(counts)


def write_min_corpus(path, texts) -> str:
    with open(path, "w", encoding="utf-8") as handle:
        for i, text in enumerate(texts):
            handle.write(json.dumps(
                {"id": i, "uuid": f"u{i}", "title": f"t{i}", "text": text}) + "\n")
    return str(path)


def run_main(argv: list[str]) -> int:
    old_argv = sys.argv
    sys.argv = ["generate_queries", *argv]
    try:
        return gq.main()
    finally:
        sys.argv = old_argv


def test_rare_terms_stay_within_the_configured_absolute_band():
    """A large corpus's term_df includes terms far above the rare band — the
    old proportional band would have selected them (doc_count // 100 is
    90,000 on a 9,000,000-doc corpus) — so this pins the fixed cutoff instead."""
    term_df = term_df_with(
        {_alpha_word(i): 10 + i for i in range(50)}
        | {f"common{_alpha_word(i)}": 50_000 + i for i in range(10)}
    )
    picked = gq.pick_rare_terms(term_df, count=20, rng=random.Random(1),
                                min_df=5, max_df=100)
    assert len(picked) == 20
    for term in picked:
        assert 5 <= term_df[term] <= 100


def test_pick_rare_terms_raises_when_the_band_has_too_few_candidates():
    """Silently returning fewer than requested is how the boolean classes'
    199/200-distinct undercount slipped through unnoticed; rare_term should
    fail loudly instead."""
    term_df = term_df_with({_alpha_word(i): 10 for i in range(3)})
    with pytest.raises(ValueError, match="only 3 candidate"):
        gq.pick_rare_terms(term_df, count=20, rng=random.Random(1),
                           min_df=5, max_df=100)


def test_pick_rare_terms_is_deterministic_for_the_same_seed():
    term_df = term_df_with({_alpha_word(i): 10 + (i % 50) for i in range(200)})
    first = gq.pick_rare_terms(term_df, count=20, rng=random.Random(99),
                               min_df=5, max_df=100)
    second = gq.pick_rare_terms(term_df, count=20, rng=random.Random(99),
                                min_df=5, max_df=100)
    assert first == second


def test_main_refuses_to_overwrite_an_existing_output_without_force(tmp_path, capsys):
    corpus = write_min_corpus(
        tmp_path / "c.jsonl",
        [f"alpha beta gamma {_alpha_word(i)}" for i in range(30)],
    )
    output = tmp_path / "queries.json"
    output.write_text("{}")
    rc = run_main([
        "--corpus", corpus, "--output", str(output),
        "--per-class", "2", "--common-pool-size", "2",
        "--rare-min-df", "1", "--rare-max-df", "1000",
    ])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_main_writes_when_forced_and_records_the_corpus_sha256(tmp_path):
    corpus = write_min_corpus(
        tmp_path / "c.jsonl",
        [f"alpha beta gamma {_alpha_word(i)}" for i in range(30)],
    )
    output = tmp_path / "queries.json"
    output.write_text("{}")
    rc = run_main([
        "--corpus", corpus, "--output", str(output),
        "--per-class", "2", "--common-pool-size", "2",
        "--rare-min-df", "1", "--rare-max-df", "1000", "--force",
    ])
    assert rc == 0
    written = json.loads(output.read_text())
    assert written["corpus_sha256"] == gq.sha256_file(corpus)
    assert written["corpus_bytes"] == os.path.getsize(corpus)
    assert written["rare_min_df"] == 1
    assert written["rare_max_df"] == 1000


def test_output_is_identical_across_different_process_hash_seeds(tmp_path):
    """CPython randomizes str-hash seeds per process by default, which drives
    set iteration order. term_df/phrase_df used to be filled from set
    comprehensions, so which of several equal-frequency terms landed in a
    class could silently change between two runs of the same corpus and seed
    — e.g. across a fleet restart, which starts a fresh process. This proves
    the fix (sorting before Counter.update) makes class membership a function
    of corpus content alone."""
    texts = []
    for pair in range(30):
        term = _alpha_word(pair)
        texts += [f"alpha beta gamma {term}"] * 2
    corpus = write_min_corpus(tmp_path / "c.jsonl", texts)

    outputs = []
    for hash_seed in ("0", "1"):
        output = tmp_path / f"queries-{hash_seed}.json"
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        subprocess.run(
            [sys.executable, "-m", "ftsbench.generate_queries",
             "--corpus", corpus, "--output", str(output),
             "--per-class", "10", "--common-pool-size", "10",
             "--rare-min-df", "1", "--rare-max-df", "1000"],
            cwd=REPO_ROOT, env=env, check=True, capture_output=True, text=True,
        )
        outputs.append(json.loads(output.read_text())["classes"])

    assert outputs[0] == outputs[1]


def test_phrase_words_shorter_than_the_minimum_are_excluded():
    """"U.S." tokenizes to the adjacent single-letter tokens "u" and "s" once
    punctuation is stripped; without a length filter these could dominate the
    phrase class purely from abbreviation frequency, not real phrases."""
    raw_tokens = ["the", "u", "s", "government", "announced", "today"]
    ngrams = gq.adjacent_content_ngrams(raw_tokens)
    assert ("u", "s") not in ngrams


def test_adjacent_content_ngrams_includes_both_two_and_three_word_windows():
    raw_tokens = ["global", "search", "engine", "benchmark"]
    ngrams = gq.adjacent_content_ngrams(raw_tokens)
    assert ("global", "search") in ngrams
    assert ("global", "search", "engine") in ngrams
    assert ("search", "engine", "benchmark") in ngrams


def test_a_stop_word_inside_a_window_excludes_the_whole_window():
    """A window must be a literal adjacent substring valid for a positional
    index; skipping over the stop word to bridge the two content words either
    side of it would produce a quoted phrase that never actually occurs."""
    raw_tokens = ["state", "of", "emergency", "declared"]
    ngrams = gq.adjacent_content_ngrams(raw_tokens)
    assert ("state", "of", "emergency") not in ngrams
    assert ("of", "emergency", "declared") not in ngrams
    assert ("emergency", "declared") in ngrams


def test_pick_phrases_formats_windows_of_either_length():
    phrase_df = collections.Counter({
        ("alpha", "beta"): 5,
        ("alpha", "beta", "gamma"): 3,
    })
    picked = gq.pick_phrases(phrase_df, count=2)
    assert picked == ['"alpha beta"', '"alpha beta gamma"']
