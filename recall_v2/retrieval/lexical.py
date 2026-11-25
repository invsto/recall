"""Lexical leg: FTS5 MATCH generates candidates inside the visible set; BM25 is computed here.

BM25 follows the FTS5 bm25() formula with k1 = 1.2, b = 0.75 and column weights title 5.0, body 1.0:

    idf(t)   = ln((N - n(t) + 0.5) / (n(t) + 0.5)), or 1e-6 if that is not positive
    w(t, d)  = 5.0 * tf(t, title) + 1.0 * tf(t, body)
    score(d) = sum over query terms t of idf(t) * w * (k1 + 1) / (w + k1 * (1 - b + b * |d| / avgdl))

N, n(t) and avgdl are taken over the caller's corpus only: the visible, current revisions that pass
the lookup filters and have an FTS row. Data the caller cannot see therefore cannot shift a score.
Term frequencies come from the fts_items_vocab instance table; document lengths from fts_items_docsize,
so tokenisation is always FTS5's own.
"""
from __future__ import annotations

import math
import sqlite3

K1 = 1.2
B = 0.75
TITLE_WEIGHT = 5.0
BODY_WEIGHT = 1.0
COLUMN_WEIGHTS = {"title": TITLE_WEIGHT, "body": BODY_WEIGHT}
LEG_DEPTH = 100

_TOKENIZER_READY = "recall_query_tok"


def _ensure_tokenizer(db: sqlite3.Connection) -> None:
    """A per-connection TEMP FTS5 table with the store's tokenizer, used only to tokenise queries."""
    if db.execute("SELECT 1 FROM temp.sqlite_schema WHERE name = ?", (_TOKENIZER_READY,)).fetchone():
        return
    db.execute("CREATE VIRTUAL TABLE temp.recall_query_tok USING fts5(q, tokenize = 'unicode61 remove_diacritics 2')")
    db.execute("CREATE VIRTUAL TABLE temp.recall_query_vocab USING fts5vocab(temp, recall_query_tok, instance)")


def query_terms(db: sqlite3.Connection, query: str) -> list[str]:
    """Distinct FTS tokens of the query, in order of first appearance. Uses only TEMP storage."""
    _ensure_tokenizer(db)
    db.execute("DELETE FROM temp.recall_query_tok")
    db.execute("INSERT INTO temp.recall_query_tok (rowid, q) VALUES (1, ?)", (query,))
    terms: list[str] = []
    for (term,) in db.execute("SELECT term FROM temp.recall_query_vocab ORDER BY offset"):
        if term not in terms:
            terms.append(term)
    db.execute("DELETE FROM temp.recall_query_tok")
    return terms


def match_expression(terms: list[str]) -> str:
    """Each term quoted, OR-joined: FTS syntax in user input is inert."""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _varints(blob: bytes) -> list[int]:
    out, value = [], 0
    for byte in blob:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            out.append(value)
            value = 0
    return out


def doc_length(size_blob: bytes) -> int:
    """Total token count of one FTS row (sum over columns) from its fts_items_docsize blob."""
    return sum(_varints(size_blob))


def bm25(db: sqlite3.Connection, terms: list[str], corpus: dict[int, int], hits: set[int]) -> dict[int, float]:
    """BM25 for each hit, with statistics over `corpus` (rowid -> document length) only."""
    n_docs = len(corpus)
    if not n_docs or not hits:
        return {}
    avgdl = sum(corpus.values()) / n_docs
    scores = {rid: 0.0 for rid in hits}
    for term in terms:
        weighted: dict[int, float] = {}
        for doc, col, count in db.execute(
                "SELECT doc, col, COUNT(*) FROM fts_items_vocab WHERE term = ? GROUP BY doc, col", (term,)):
            if doc in corpus:
                weighted[doc] = weighted.get(doc, 0.0) + COLUMN_WEIGHTS[col] * count
        df = len(weighted)
        idf = math.log((n_docs - df + 0.5) / (df + 0.5))
        if idf <= 0:
            idf = 1e-6
        for rid in hits:
            w = weighted.get(rid, 0.0)
            if w:
                norm = K1 * (1 - B + B * (corpus[rid] / avgdl if avgdl else 0.0))
                scores[rid] += idf * (w * (K1 + 1)) / (w + norm)
    return scores
