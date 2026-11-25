"""memory_lookup (section 4.2): fact mode and text mode, with filters, as_of / known_at and limits."""
from __future__ import annotations

import sqlite3
from typing import Optional

from .. import SCHEMA_VERSION
from ..core import model, normalise, timefmt
from ..core.errors import RecallError, invalid
from ..store import embeddings
from ..store.connection import read_transaction
from ..store.repository import Principal, Store
from . import fusion, lexical
from .candidates import COLUMNS, CURRENT, FILTERS, REVISION_FROM, enrich, is_member
from .envelope import NOTICE, error_envelope

DEFAULT_LIMIT = 10
MAX_LIMIT = 20
QUERY_MAX_BYTES = 256
LEXICAL = "lexical"
VECTOR = "vector"


def _check_args(query, subject, predicate, kind, scope, project, limit, explain) -> str:
    fact_mode = subject is not None or predicate is not None
    if (query is not None) == fact_mode:
        raise invalid("Give exactly one mode: query, or subject and/or predicate.")
    if query is not None:
        if not isinstance(query, str) or "\x00" in query or not query.strip():
            raise invalid("query must be non-empty text.")
        if len(query.encode("utf-8")) > QUERY_MAX_BYTES:
            raise invalid("query exceeds 256 bytes.")
    if kind is not None and kind not in model.KINDS:
        raise invalid("Unknown kind.")
    if scope is not None and scope not in model.SCOPES:
        raise invalid("scope must be private or project.")
    if project is not None:
        normalise.check_id(project, normalise.PROJECT_ID, "project")
        if scope == "private":
            raise invalid("project cannot be combined with private scope.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise invalid("limit must be an integer from 1 to 20.")
    if not isinstance(explain, bool):
        raise invalid("explain must be a boolean.")
    return "text" if query is not None else "fact"


def lookup(store: Store, principal: Principal, *, query=None, subject=None, predicate=None, kind=None,
           scope=None, project=None, as_of=None, known_at=None, limit=DEFAULT_LIMIT, explain=False) -> dict:
    """Return the lookup envelope (never raises for domain failures)."""
    try:
        mode = _check_args(query, subject, predicate, kind, scope, project, limit, explain)
        now = store.clock()
        tv = timefmt.coerce(as_of)
        tk = timefmt.coerce(known_at)
        tv = now if tv is None else tv
        tk = now if tk is None else tk
        params = {"tv": tv, "tk": tk, "kind": kind, "scope": scope, "project": project}
        if mode == "fact":
            key_sql = _fact_key_clause(subject, predicate, params)
            with read_transaction(store.db) as db:
                caller = params["caller"] = store._caller(db, principal.token_digest)
                _check_project(db, caller, project)
                results, total = _fact_mode(db, caller, key_sql, params, limit)
        else:
            terms = lexical.query_terms(store.db, query)
            query_vector = None
            if store.embedder is not None:
                [query_vector] = store.embedder.embed([query])
            with read_transaction(store.db) as db:
                caller = params["caller"] = store._caller(db, principal.token_digest)
                _check_project(db, caller, project)
                results, total = _text_mode(db, caller, terms, query_vector, store.embedder, params, limit, explain)
        return {"ok": True, "schema_version": SCHEMA_VERSION, "op": "lookup",
                "as_of": timefmt.format_rfc3339(tv), "known_at": timefmt.format_rfc3339(tk), "notice": NOTICE,
                "count": len(results), "truncated": total > len(results), "results": results}
    except RecallError as error:
        return error_envelope("lookup", error)


def _check_project(db: sqlite3.Connection, caller: str, project: Optional[str]) -> None:
    if project is not None and not is_member(db, caller, project):
        raise RecallError("not_found", "No such project.")


# -- fact mode ---------------------------------------------------------------------------------------

def _fact_key_clause(subject, predicate, params: dict) -> str:
    for value in (subject, predicate):
        if value is not None and not isinstance(value, str):
            raise invalid("subject and predicate must be text.")
    sep = normalise.FACT_KEY_SEPARATOR
    if subject is not None and predicate is not None:
        params["key"] = normalise.norm(subject) + sep + normalise.norm(predicate)
        return "i.key = :key"
    if subject is not None:
        # key = norm(subject) U+001F norm(predicate); normalised text never holds control characters,
        # so the keys with this subject are exactly the range [subject U+001F, subject U+0020).
        prefix = normalise.norm(subject)
        params["key_lo"], params["key_hi"] = prefix + sep, prefix + " "
        return "i.key >= :key_lo AND i.key < :key_hi"
    params["pred"] = normalise.norm(predicate)
    return "substr(i.key, instr(i.key, char(31)) + 1) = :pred"


def _fact_mode(db, caller, key_sql, params, limit) -> tuple[list[dict], int]:
    if params["kind"] not in (None, "fact"):
        return [], 0
    rows = db.execute(
        f"SELECT {COLUMNS} FROM {REVISION_FROM} WHERE i.kind = 'fact' AND {key_sql} AND {CURRENT}{FILTERS}"
        " ORDER BY (i.scope_kind = 'project'), i.project_id, i.key, i.id", params).fetchall()
    return [enrich(db, caller, row) for row in rows[:limit]], len(rows)


# -- text mode ---------------------------------------------------------------------------------------

def _lexical_leg(db, terms, params) -> tuple[list[tuple[str, int]], dict, dict]:
    """Returns ([(item_id, accepted_at)] in rank order, rows by item ID, bm25 by item ID)."""
    if not terms:
        return [], {}, {}
    params = dict(params, match=lexical.match_expression(terms))
    rows = db.execute(
        f"SELECT {COLUMNS}, d.sz AS doc_size,"
        " (r.rowid IN (SELECT rowid FROM fts_items WHERE fts_items MATCH :match)) AS hit"
        f" FROM {REVISION_FROM} JOIN fts_items_docsize AS d ON d.id = r.rowid"
        f" WHERE {CURRENT}{FILTERS}", params).fetchall()
    corpus = {row["rid"]: lexical.doc_length(row["doc_size"]) for row in rows}
    hits = {row["rid"] for row in rows if row["hit"]}
    scores = lexical.bm25(db, terms, corpus, hits)
    hit_rows = [row for row in rows if row["rid"] in hits]
    hit_rows.sort(key=lambda row: (-scores[row["rid"]], *fusion.tie_key(row["accepted_at"], row["item_id"])))
    hit_rows = hit_rows[:lexical.LEG_DEPTH]
    return ([(row["item_id"], row["accepted_at"]) for row in hit_rows],
            {row["item_id"]: row for row in hit_rows},
            {row["item_id"]: scores[row["rid"]] for row in hit_rows})


def _vector_leg(db, query_vector, embedder, params) -> tuple[list[tuple[str, int]], dict, dict]:
    """Exact cosine over the caller's visible, current revisions that have an embedding; top 100."""
    params = dict(params, model=embedder.model)
    rows = db.execute(
        f"SELECT {COLUMNS}, e.vector AS vector FROM {REVISION_FROM}"
        " JOIN embeddings AS e ON e.item_id = i.id AND e.rev = r.rev AND e.model = :model"
        f" WHERE {CURRENT}{FILTERS}", params).fetchall()
    scored = []
    for row in rows:
        similarity = embeddings.cosine(query_vector, embeddings.decode(row["vector"]))
        if similarity > 0:
            scored.append((similarity, row))
    scored.sort(key=lambda pair: (-pair[0], *fusion.tie_key(pair[1]["accepted_at"], pair[1]["item_id"])))
    scored = scored[:lexical.LEG_DEPTH]
    return ([(row["item_id"], row["accepted_at"]) for _, row in scored],
            {row["item_id"]: row for _, row in scored},
            {row["item_id"]: similarity for similarity, row in scored})


def _text_mode(db, caller, terms, query_vector, embedder, params, limit, explain) -> tuple[list[dict], int]:
    legs, rows, raw = {}, {}, {}
    ranked, leg_rows, leg_scores = _lexical_leg(db, terms, params)
    legs[LEXICAL], raw[LEXICAL] = ranked, leg_scores
    rows.update(leg_rows)
    if embedder is not None and query_vector is not None:
        ranked, leg_rows, leg_scores = _vector_leg(db, query_vector, embedder, params)
        legs[VECTOR], raw[VECTOR] = ranked, leg_scores
        rows.update(leg_rows)
    fused = fusion.rrf(legs)
    results = []
    for position, entry in enumerate(fused[:limit], start=1):
        item = enrich(db, caller, rows[entry.item_id])
        item["rank"] = position
        item["score"] = float(entry.score)
        if explain:
            item["explain"] = {
                leg: ({"rank": entry.ranks[leg], "contribution": float(fusion.contribution(entry.ranks[leg])),
                       "score": raw[leg][entry.item_id]} if leg in entry.ranks else None)
                for leg in legs}
        results.append(item)
    return results, len(fused)
