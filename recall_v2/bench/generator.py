"""Seeded fact-evolution generator (section 9.2, dataset 1).

Each chain is one fact key with 1 to 5 forward changes, plus, per chain and independently, a 10% chance
of a back-dated insert, a 10% chance of a retraction and a 10% chance that the last change expires
(explicit valid_to); 3% of chains are deleted at the end. Query points (Tv, Tk) are random.
Ground truth is produced with the data by bench.reference, an implementation of section 2.5 that
shares no code with the store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import random
from typing import Optional

GENERATOR_VERSION = "fact-evolution-v1"
DEFAULT_SEED = 20261125
DEFAULT_CHAINS = 2000

DAY = 86_400 * 1_000_000
BASE = 1_767_225_600_000_000  # 2026-01-01T00:00:00Z
PREDICATES = ("owner", "region", "status", "version", "endpoint", "maintainer", "budget", "deadline", "tier",
              "language", "database", "oncall")
_ONSETS = ("b", "d", "f", "g", "k", "l", "m", "n", "p", "r", "s", "t", "v", "z", "br", "kr", "st", "tr")
_VOWELS = ("a", "e", "i", "o", "u")
_CODAS = ("", "n", "r", "l", "s", "x")

BACKDATE_P = 0.10
RETRACT_P = 0.10
EXPIRE_P = 0.10
DELETE_P = 0.03
PROJECT_P = 0.20
QUERIES_PER_CHAIN = 3


@dataclass
class Event:
    """One write. `op` is assert, retract or delete. `target` is the event index a retract retracts."""
    op: str
    t_write: int
    valid_from: Optional[int] = None
    valid_to: Optional[int] = None  # explicit; None means the spec default
    value: Optional[str] = None
    target: Optional[int] = None


@dataclass
class Chain:
    index: int
    scope: str  # private or project
    subject: str
    predicate: str
    events: list[Event] = field(default_factory=list)
    queries: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Dataset:
    seed: int
    version: str
    chains: list[Chain]
    t_end: int


def _word(rng: random.Random) -> str:
    return "".join(rng.choice(_ONSETS) + rng.choice(_VOWELS) for _ in range(rng.randint(2, 3))) + rng.choice(_CODAS)


def subjects(rng: random.Random, count: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    while len(out) < count:
        word = _word(rng)
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


def generate(seed: int = DEFAULT_SEED, chains: int = DEFAULT_CHAINS) -> Dataset:
    rng = random.Random(seed)
    names = subjects(rng, (chains + 1) // 2 + 1)
    keys = [(names[i // 2], PREDICATES[(i * 7 + i // 2) % len(PREDICATES)]) for i in range(chains)]
    out: list[Chain] = []
    t_end = BASE
    for index, (subject, predicate) in enumerate(keys):
        chain = Chain(index, "project" if rng.random() < PROJECT_P else "private", subject, predicate)
        start = BASE + rng.randint(0, 300) * DAY + rng.randint(0, DAY - 1)
        valid_from = start
        asserts: list[int] = []
        for change in range(rng.randint(1, 5)):
            if change:
                valid_from += rng.randint(1, 30) * DAY + rng.randint(0, DAY - 1)
            t_write = valid_from + rng.randint(0, 2 * DAY)
            chain.events.append(Event("assert", t_write, valid_from, None, f"val-{rng.randint(0, 99999)}"))
            asserts.append(len(chain.events) - 1)
        # keep writes in time order within the chain
        for previous, event in zip(chain.events, chain.events[1:]):
            if event.t_write <= previous.t_write:
                event.t_write = previous.t_write + 1
        last_write = chain.events[-1].t_write
        if rng.random() < EXPIRE_P:
            last = chain.events[asserts[-1]]
            last.valid_to = last.valid_from + rng.randint(1, 20) * DAY
        if rng.random() < BACKDATE_P:
            first_from = chain.events[0].valid_from
            back_from = first_from - rng.randint(1, 10) * DAY if rng.random() < 0.3 else \
                rng.randint(first_from + 1, max(first_from + 2, chain.events[asserts[-1]].valid_from))
            last_write += rng.randint(1, 5) * DAY
            chain.events.append(Event("assert", last_write, back_from, None, f"val-{rng.randint(0, 99999)}"))
            asserts.append(len(chain.events) - 1)
        if rng.random() < RETRACT_P:
            last_write += rng.randint(1, 5) * DAY
            chain.events.append(Event("retract", last_write, target=rng.choice(asserts)))
        if rng.random() < DELETE_P:
            last_write += rng.randint(1, 5) * DAY
            chain.events.append(Event("delete", last_write))
        t_end = max(t_end, last_write)
        out.append(chain)
    t_end += DAY
    for chain in out:
        lo = chain.events[0].valid_from - 10 * DAY
        for _ in range(QUERIES_PER_CHAIN):
            chain.queries.append((rng.randint(lo, t_end), rng.randint(lo, t_end)))
        chain.queries.append((t_end, t_end))
    return Dataset(seed, GENERATOR_VERSION, out, t_end)
