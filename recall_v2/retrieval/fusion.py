"""Reciprocal rank fusion (section 4.3).

score(d) = sum over legs of 1 / (K + rank_leg(d)), ranks from 1, K = 60; a leg that did not return d
contributes nothing. Ties: accepted_at descending, then item ID ascending. Scores are compared as exact
fractions, so equal sums are equal ties regardless of floating-point rounding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Mapping, Sequence

K = 60


@dataclass
class Fused:
    item_id: str
    accepted_at: int
    score: Fraction = Fraction(0)
    ranks: dict = field(default_factory=dict)

    def contributions(self) -> dict:
        return {leg: Fraction(1, K + rank) for leg, rank in self.ranks.items()}


def contribution(rank: int) -> Fraction:
    return Fraction(1, K + rank)


def tie_key(accepted_at: int, item_id: str):
    return (-accepted_at, item_id)


def rrf(legs: Mapping[str, Sequence[tuple[str, int]]]) -> list[Fused]:
    """legs: name -> [(item_id, accepted_at), ...] in that leg's rank order (rank 1 first)."""
    fused: dict[str, Fused] = {}
    for leg, ranked in legs.items():
        for position, (item_id, accepted_at) in enumerate(ranked, start=1):
            entry = fused.setdefault(item_id, Fused(item_id, accepted_at))
            entry.ranks[leg] = position
            entry.score += contribution(position)
    return sorted(fused.values(), key=lambda e: (-e.score, *tie_key(e.accepted_at, e.item_id)))
