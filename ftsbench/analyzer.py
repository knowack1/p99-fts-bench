r"""The ScyllaDB FTS M1 analyzer: split on non-alphanumeric, lowercase, drop
English stop words, no stemming.

Tantivy's `SimpleTokenizer` breaks on any rune where Rust's
`char::is_alphanumeric()` is false, i.e. it keeps runs of
`Alphabetic | N`. `Alphabetic` is the Unicode *derived* property, which
includes Other_Alphabetic combining marks — the vowel signs of Bengali,
Thaana, Hebrew, Tamil and friends. The stdlib `\w` class does not: it is
category-L based, so it split `মিত` into `ম` + `ত` and diverged from the
engine on 13 of 3,000 simplewiki documents (~2,400 tokens). Hence `regex`
and an explicit `\p{Alphabetic}`.

OpenSearch is held to the same rule by a `pattern` tokenizer over
`[^\p{IsAlphabetic}\p{N}]+`; `opensearch/verify_analyzer.sh` asserts it.
"""
import regex

TOKEN_PATTERN = regex.compile(r"[\p{Alphabetic}\p{N}]+")

LUCENE_ENGLISH_STOP_WORDS = frozenset(
    """
    a an and are as at be but by for if in into is it
    no not of on or such that the their then there these
    they this to was will with
    """.split()
)


def tokenize(text: str, keep_stop_words: bool = False) -> list[str]:
    tokens = [match.group(0).lower() for match in TOKEN_PATTERN.finditer(text)]
    if keep_stop_words:
        return tokens
    return [token for token in tokens if token not in LUCENE_ENGLISH_STOP_WORDS]


def is_stop_word(token: str) -> bool:
    return token in LUCENE_ENGLISH_STOP_WORDS
