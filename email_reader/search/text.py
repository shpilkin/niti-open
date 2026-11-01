"""Tokenisation, normalisation and Russian stems — shared by the indexer and the query.

Both sides must agree exactly: a document is indexed with the same tokeniser that turns
what the user typed into a query, or the two never meet.

Why stems at all: people type a word in full and the mail holds another form of it. On
a typical mailbox the stem of «договора» finds several times more letters than the exact form,
and «подписали» too. Why the raw forms stay next to the stems: Snowball also
over-merges (it collapses «подписать/подписание/подпись» into one stem), so the exact
column is what ranks first and the stem column is what widens the net.
"""

from __future__ import annotations

import re
from functools import lru_cache

_TOKEN = re.compile(r"\w+", re.UNICODE)
# Longer than any word worth indexing; guards against base64 blobs in a mail body.
_MAX_TOKEN = 40
_MAX_CHARS = 40_000


def normalize(text: str | None) -> str:
    """Lowercase and fold ё→е.

    FTS5's `remove_diacritics 2` folds Latin accents but leaves ё alone, so «счёт» and
    «счет» were two different words, each finding about half of the letters.
    """
    return (text or "").lower().replace("ё", "е")


def tokens(text: str | None) -> list[str]:
    return [t for t in _TOKEN.findall(normalize(text)) if len(t) <= _MAX_TOKEN]


@lru_cache(maxsize=1)
def _stemmer():
    import snowballstemmer

    return snowballstemmer.stemmer("russian")


@lru_cache(maxsize=100_000)
def stem(word: str) -> str:
    """Snowball stem of an already-normalised word. Latin words pass through unchanged
    (the Russian stemmer leaves them alone), so one column covers both languages."""
    return _stemmer().stemWord(word)


def stems_of(text: str | None) -> str:
    """The stem column of a document: every token stemmed, in order, space separated."""
    return " ".join(stem(t) for t in tokens(text))


def clip(text: str | None) -> str:
    """A very long body is worth indexing only in part — the tail of a 2 MB newsletter
    adds index, not answers."""
    value = (text or "").strip()
    return value[:_MAX_CHARS]
