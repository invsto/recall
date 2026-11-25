"""Independent reference for the benchmark ground truth: sections 2.4 and 2.5 written from the spec text.

Deliberately shares no code with recall_v2.core or recall_v2.store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .generator import Chain

INF = float("inf")


@dataclass(frozen=True)
class RefRevision:
    rev: int
    op: str
    valid_from: int
    valid_to: Optional[int]
    accepted_at: int
    value: Optional[str]


@dataclass
class RefChain:
    revisions: list[RefRevision]
    deleted: bool


def build(chain: Chain) -> RefChain:
    """Apply the chain's events in write order; every write is accepted at its write time."""
    revs: list[RefRevision] = []
    by_event: dict[int, RefRevision] = {}
    deleted = False
    for index, event in enumerate(chain.events):
        if event.op == "delete":
            deleted = True
            continue
        asserts = [r for r in revs if r.op == "assert"]
        if event.op == "assert":
            valid_to = event.valid_to
            if valid_to is None:
                later = [r.valid_from for r in asserts if r.valid_from > event.valid_from]
                valid_to = min(later) if later else None
            rev = RefRevision(len(revs) + 1, "assert", event.valid_from, valid_to, event.t_write, event.value)
        else:
            target = by_event[event.target]
            ends = [r.valid_from for r in asserts if r.valid_from > target.valid_from]
            if target.valid_to is not None:
                ends.append(target.valid_to)
            rev = RefRevision(len(revs) + 1, "retract", target.valid_from, min(ends) if ends else None,
                              event.t_write, target.value)
        revs.append(rev)
        by_event[index] = rev
    return RefChain(revs, deleted)


def current(ref: RefChain, tv: int, tk: int) -> Optional[RefRevision]:
    """Section 2.5 at valid time tv and known time tk."""
    if ref.deleted:
        return None
    covering = [r for r in ref.revisions
                if r.accepted_at <= tk and r.valid_from <= tv < (INF if r.valid_to is None else r.valid_to)]
    if not covering:
        return None
    winner = max(covering, key=lambda r: (r.accepted_at, r.rev))
    return None if winner.op == "retract" else winner
