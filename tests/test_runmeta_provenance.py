"""The commit stamped on an artifact must not name code that did not run.

Every artifact of the first laptop campaign carries `39eae6e`, a commit made
before the harness that produced them existed, because the whole repair was
uncommitted at run time. A bare hash is worse than no hash there: it invites a
reader to check that revision out and conclude the numbers are reproducible.
"""
from ftsbench import runmeta


def test_a_clean_tree_stamps_the_bare_commit(monkeypatch):
    monkeypatch.setattr(runmeta, "git_output", lambda *args: "abc1234\n")
    monkeypatch.setattr(runmeta, "working_tree_is_dirty", lambda: False)
    assert runmeta.git_commit() == "abc1234"


def test_a_dirty_tree_says_so(monkeypatch):
    monkeypatch.setattr(runmeta, "git_output", lambda *args: "abc1234\n")
    monkeypatch.setattr(runmeta, "working_tree_is_dirty", lambda: True)
    assert runmeta.git_commit() == "abc1234-dirty"


def test_no_git_is_unknown_rather_than_a_guess(monkeypatch):
    monkeypatch.setattr(runmeta, "git_output", lambda *args: None)
    assert runmeta.git_commit() == "unknown"


def test_untracked_files_alone_do_not_mean_dirty(monkeypatch):
    """A new plot script sitting unstaged beside the harness did not run; an
    empty porcelain status under --untracked-files=no is the clean signal."""
    monkeypatch.setattr(runmeta, "git_output", lambda *args: "")
    assert runmeta.working_tree_is_dirty() is False


def test_a_modified_tracked_file_means_dirty(monkeypatch):
    monkeypatch.setattr(runmeta, "git_output",
                        lambda *args: " M bench/ftsbench/load_gen.py\n")
    assert runmeta.working_tree_is_dirty() is True


def test_the_header_carries_the_stamp():
    header = runmeta.header(producer="test", engine="opensearch",
                            engine_version="3.8.0", label="l",
                            cache_state="cold", corpus="c", max_docs=1)
    assert header["git_commit"].endswith("-dirty") or \
        header["git_commit"] not in ("", None)
