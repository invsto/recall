"""Intervals, default validity of new fact revisions, and the current-truth rule.

Intervals are closed-open [valid_from, valid_to); valid_to None means open-ended (+infinity).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .errors import invalid

ASSERT = "assert"
RETRACT = "retract"


@dataclass(frozen=True)
class FactRevision:
    """The temporal part of one fact revision."""
    rev: int
    op: str
    valid_from: int
    valid_to: Optional[int]
    state: str = "accepted"
    accepted_at: Optional[int] = None


@dataclass(frozen=True)
class Revision:
    """The temporal part of a non-fact revision."""
    rev: int
    valid_from: int
    valid_to: Optional[int]
    state: str = "accepted"
    accepted_at: Optional[int] = None


def check_interval(valid_from: int, valid_to: Optional[int]) -> None:
    if valid_to is not None and valid_to <= valid_from:
        raise invalid("valid_to must be later than valid_from.")


def contains(valid_from: int, valid_to: Optional[int], instant: int) -> bool:
    return valid_from <= instant and (valid_to is None or instant < valid_to)


def overlaps(a_from: int, a_to: Optional[int], b_from: int, b_to: Optional[int]) -> bool:
    """[a1,b1) and [a2,b2) overlap iff a1 < b2 and a2 < b1 (None = +infinity). Touching does not overlap."""
    return (b_to is None or a_from < b_to) and (a_to is None or b_from < a_to)


def _accepted_asserts(revisions: Iterable[FactRevision]) -> list[FactRevision]:
    return [r for r in revisions if r.state == "accepted" and r.op == ASSERT]


def default_valid_to(valid_from: int, revisions: Iterable[FactRevision]) -> Optional[int]:
    """Smallest valid_from among accepted asserts that is greater than valid_from, else None."""
    later = [r.valid_from for r in _accepted_asserts(revisions) if r.valid_from > valid_from]
    return min(later) if later else None


def effective_interval(target: FactRevision, revisions: Iterable[FactRevision]) -> tuple[int, Optional[int]]:
    """target.valid_from to the earliest of target.valid_to and the next later-starting accepted assert."""
    ends = [r.valid_from for r in _accepted_asserts(revisions) if r.valid_from > target.valid_from]
    if target.valid_to is not None:
        ends.append(target.valid_to)
    return target.valid_from, (min(ends) if ends else None)


def current_fact(revisions: Iterable[FactRevision], valid_time: int, known_time: int) -> Optional[FactRevision]:
    """Section 2.5: the winner of S by (accepted_at, rev); None if S is empty or the winner is a retraction."""
    covering = [
        r for r in revisions
        if r.state == "accepted" and r.accepted_at is not None and r.accepted_at <= known_time
        and contains(r.valid_from, r.valid_to, valid_time)
    ]
    if not covering:
        return None
    winner = max(covering, key=lambda r: (r.accepted_at, r.rev))
    return None if winner.op == RETRACT else winner


def current_other(revisions: Sequence[Revision], valid_time: int, known_time: int) -> Optional[Revision]:
    """Non-fact kinds: the accepted revision with the greatest rev known by known_time, if it covers valid_time."""
    known = [r for r in revisions if r.state == "accepted" and r.accepted_at is not None
             and r.accepted_at <= known_time]
    if not known:
        return None
    latest = max(known, key=lambda r: r.rev)
    return latest if contains(latest.valid_from, latest.valid_to, valid_time) else None


def conflicts_with(new_from: int, new_to: Optional[int], new_value: str,
                   existing: FactRevision, existing_value: str,
                   revisions: Iterable[FactRevision]) -> bool:
    """Section 3.4: values differ, effective intervals overlap, and new.valid_from <= existing.valid_from."""
    if existing.state != "accepted" or existing.op != ASSERT or new_value == existing_value:
        return False
    e_from, e_to = effective_interval(existing, revisions)
    return new_from <= e_from and overlaps(new_from, new_to, e_from, e_to)
