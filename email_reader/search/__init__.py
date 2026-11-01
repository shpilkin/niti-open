"""Cross-cutting search: mail, commitments, meetings and their moments, calendar,
people, projects — one FTS5 index, one query language, one result list.

`index` writes it, `query` reads it, `text` is the tokeniser both sides share.
"""

from email_reader.search.index import DDL, KINDS, reconcile, reindex

__all__ = ["KINDS", "DDL", "reconcile", "reindex"]
