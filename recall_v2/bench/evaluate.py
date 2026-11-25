"""Fact-evolution benchmark: build a store from the generated chains, query it, compare with the reference.

Metrics: current-truth accuracy, stale-current rate, scope leaks, text-mode hit@5 and MRR, bundle budget
violations and bundle determinism. Gates: stale-current = 0, accuracy = 1.00, scope leaks = 0, bundle
budget violations = 0, bundle determinism = 100%. Text-mode hit@5 has no gate here: its threshold is
defined on the scenario set (section 9.2, dataset 2), which this harness does not provide.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Optional

from ..core.timefmt import format_rfc3339
from ..retrieval import bundle, canonical_bytes, lookup
from ..retrieval.bundle import cost
from ..store import Store
from . import reference
from .generator import DEFAULT_CHAINS, DEFAULT_SEED, Dataset, generate

ALPHA, OUTSIDER = "alpha", "outsider"
PROJECT, OTHER_PROJECT = "proj-a", "proj-b"
TEXT_QUERIES = 400
METAMORPHIC_QUERIES = 40
OUTSIDER_SHARE = 0.25
BUNDLE_BUDGETS = (400, 1500, 8000, 32000)


class SetClock:
    def __init__(self, now: int):
        self.now = now

    def __call__(self) -> int:
        return self.now


@dataclass
class Built:
    item_ids: dict[int, str]
    refs: dict[int, reference.RefChain]


def _apply(store: Store, clock: SetClock, principal, dataset: Dataset) -> Built:
    writes = sorted(((event.t_write, chain.index, n) for chain in dataset.chains
                     for n, event in enumerate(chain.events)))
    by_index = {chain.index: chain for chain in dataset.chains}
    item_ids: dict[int, str] = {}
    rev_of_event: dict[tuple[int, int], int] = {}
    for t_write, index, n in writes:
        chain, event = by_index[index], by_index[index].events[n]
        clock.now = t_write
        request_id = f"c{index}-e{n}"
        if event.op == "assert":
            result = store.save(principal, kind="fact", request_id=request_id, scope=chain.scope,
                                project=PROJECT if chain.scope == "project" else None,
                                fact={"subject": chain.subject, "predicate": chain.predicate, "value": event.value},
                                valid_from=event.valid_from, valid_to=event.valid_to)
        elif event.op == "retract":
            result = store.retract(principal, item_id=item_ids[index], rev=rev_of_event[(index, event.target)],
                                   request_id=request_id)
        else:
            store.delete_item(item_ids[index])
            continue
        if chain.scope == "project":
            store.review_approve(result["item_id"], result["rev"])
        item_ids[index] = result["item_id"]
        rev_of_event[(index, n)] = result["rev"]
    return Built(item_ids, {chain.index: reference.build(chain) for chain in dataset.chains})


def _add_outsider_data(store: Store, clock: SetClock, outsider, dataset: Dataset, rng: random.Random) -> None:
    """Same subjects and predicates, other values, in the outsider's private scope and another project."""
    for chain in dataset.chains:
        if rng.random() >= OUTSIDER_SHARE:
            continue
        clock.now = chain.events[0].t_write
        scope = "private" if chain.index % 2 else "project"
        result = store.save(outsider, kind="fact", request_id=f"o{chain.index}", scope=scope,
                            project=OTHER_PROJECT if scope == "project" else None,
                            fact={"subject": chain.subject, "predicate": chain.predicate, "value": "outsider-value"},
                            valid_from=chain.events[0].valid_from)
        if scope == "project":
            store.review_approve(result["item_id"], result["rev"])
        store.save(outsider, kind="note", request_id=f"on{chain.index}", key=f"note-{chain.index}",
                   title=f"{chain.subject} {chain.predicate}", body=f"{chain.subject} {chain.predicate} notes")


def _visible_to_alpha(item: dict) -> bool:
    scope = item["scope"]
    return (scope["type"] == "private" and scope["id"] == ALPHA) or (scope["type"] == "project" and scope["id"] == PROJECT)


def run(seed: int = DEFAULT_SEED, chains: int = DEFAULT_CHAINS, text_queries: int = TEXT_QUERIES,
        workdir: Optional[Path] = None) -> dict:
    started = time.monotonic()
    dataset = generate(seed, chains)
    rng = random.Random(seed ^ 0x5EED)
    with tempfile.TemporaryDirectory(dir=workdir) as temp:
        os.chmod(temp, 0o700)
        clock = SetClock(dataset.chains[0].events[0].t_write)
        store = Store.open(Path(temp) / "bench.sqlite3", create=True, clock=clock)
        try:
            return _run(store, clock, dataset, rng, text_queries, started)
        finally:
            store.close()


def _run(store: Store, clock: SetClock, dataset: Dataset, rng: random.Random, text_queries: int,
         started: float) -> dict:
    t_end = dataset.t_end
    clock.now = dataset.chains[0].events[0].t_write - 1
    alpha = store.authenticate(store.principal_add(ALPHA))
    outsider = store.authenticate(store.principal_add(OUTSIDER))
    store.project_add(PROJECT)
    store.project_add(OTHER_PROJECT)
    store.member_add(ALPHA, PROJECT)
    store.member_add(OUTSIDER, OTHER_PROJECT)
    built = _apply(store, clock, alpha, dataset)
    revisions = sum(len(ref.revisions) for ref in built.refs.values())

    # Metamorphic baseline: alpha's text lookups and bundles before any outsider data exists.
    sample = rng.sample(dataset.chains, min(METAMORPHIC_QUERIES, len(dataset.chains)))
    probe_args = [dict(query=f"{c.subject} {c.predicate}", as_of=t_end, known_at=t_end, explain=True, limit=20)
                  for c in sample]
    before = [canonical_bytes(lookup(store, alpha, **args)) for args in probe_args]
    bundles_before = [canonical_bytes(bundle(store, alpha, project=PROJECT, budget_tokens=b, as_of=t_end))
                      for b in BUNDLE_BUDGETS]
    _add_outsider_data(store, clock, outsider, dataset, rng)
    clock.now = t_end + 1
    leaks = sum(a != canonical_bytes(lookup(store, alpha, **args)) for a, args in zip(before, probe_args))
    leaks += sum(a != canonical_bytes(bundle(store, alpha, project=PROJECT, budget_tokens=b, as_of=t_end))
                 for a, b in zip(bundles_before, BUNDLE_BUDGETS))

    # Current truth, fact mode.
    queries = correct = stale = 0
    errors = 0
    for chain in dataset.chains:
        ref = built.refs[chain.index]
        for tv, tk in chain.queries:
            queries += 1
            env = lookup(store, alpha, subject=chain.subject, predicate=chain.predicate,
                         as_of=format_rfc3339(tv), known_at=format_rfc3339(tk))
            if not env["ok"]:
                errors += 1
                continue
            leaks += sum(not _visible_to_alpha(item) for item in env["results"])
            mine = [item for item in env["results"] if _visible_to_alpha(item)]
            expected = reference.current(ref, tv, tk)
            got = [(item["id"], item["rev"]) for item in mine]
            want = [] if expected is None else [(built.item_ids[chain.index], expected.rev)]
            if got == want:
                correct += 1
            stale += sum(1 for pair in got if pair not in want)

    # Outsider probes: fact mode on alpha's keys returns only the outsider's own items.
    for chain in rng.sample(dataset.chains, min(200, len(dataset.chains))):
        env = lookup(store, outsider, subject=chain.subject, predicate=chain.predicate, as_of=t_end, known_at=t_end)
        leaks += sum(_visible_to_alpha(item) for item in env.get("results", []))
        env = lookup(store, outsider, query=f"{chain.subject} {chain.predicate}", as_of=t_end, known_at=t_end)
        leaks += sum(_visible_to_alpha(item) for item in env.get("results", []))

    # Text mode at t_end: hit@5 and MRR of the chain's item; every returned fact must be current.
    hits = reciprocal = judged = 0
    text_sample = rng.sample(dataset.chains, min(text_queries, len(dataset.chains)))
    current_at_end = {c.index: reference.current(built.refs[c.index], t_end, t_end) for c in dataset.chains}
    by_item = {item_id: index for index, item_id in built.item_ids.items()}
    for chain in text_sample:
        env = lookup(store, alpha, query=f"{chain.subject} {chain.predicate}", as_of=t_end, known_at=t_end,
                     limit=20)
        if not env["ok"]:
            errors += 1
            continue
        ids = []
        for item in env["results"]:
            if not _visible_to_alpha(item):
                leaks += 1
                continue
            ids.append(item["id"])
            index = by_item.get(item["id"])
            truth = current_at_end.get(index)
            if index is not None and (truth is None or truth.rev != item["rev"]):
                stale += 1
        if current_at_end[chain.index] is not None:
            judged += 1
            target = built.item_ids[chain.index]
            if target in ids[:5]:
                hits += 1
            if target in ids:
                reciprocal += 1 / (ids.index(target) + 1)

    # Bundles: budget invariant and determinism.
    violations = nondeterministic = 0
    for budget in BUNDLE_BUDGETS:
        first = bundle(store, alpha, project=PROJECT, budget_tokens=budget, as_of=t_end)
        second = bundle(store, alpha, project=PROJECT, budget_tokens=budget, as_of=t_end)
        if not first["ok"] or first["used"] > budget or cost(first["text"]) > first["used"]:
            violations += 1
        if canonical_bytes(first) != canonical_bytes(second):
            nondeterministic += 1

    accuracy = correct / queries if queries else 0.0
    metrics = {
        "generator_version": dataset.version, "seed": dataset.seed, "chains": len(dataset.chains),
        "revisions": revisions, "events": _event_counts(dataset), "fact_queries": queries, "text_queries": len(text_sample), "text_judged": judged,
        "current_truth_accuracy": accuracy, "stale_current": stale,
        "stale_current_rate": stale / queries if queries else 0.0,
        "scope_leaks": leaks, "errors": errors,
        "hit_at_5": hits / judged if judged else None, "mrr": reciprocal / judged if judged else None,
        "bundle_budget_violations": violations, "bundle_nondeterministic": nondeterministic,
    }
    metrics["gates"] = {
        "stale_current_rate == 0": stale == 0,
        "current_truth_accuracy == 1.00": correct == queries and errors == 0,
        "scope_leaks == 0": leaks == 0,
        "bundle_budget_violations == 0": violations == 0,
        "bundle_determinism == 100%": nondeterministic == 0,
    }
    metrics["passed"] = all(metrics["gates"].values())
    metrics["scenario_set"] = "not built: hit@5 thresholds on the scenario set are not evaluated"
    metrics["elapsed_s"] = round(time.monotonic() - started, 2)
    return metrics


def _event_counts(dataset: Dataset) -> dict:
    counts = {"assert": 0, "backdated": 0, "retract": 0, "expiring": 0, "delete": 0}
    for chain in dataset.chains:
        latest_from = None
        for event in chain.events:
            counts[event.op] += 1
            if event.op == "assert":
                if latest_from is not None and event.valid_from < latest_from:
                    counts["backdated"] += 1
                latest_from = event.valid_from if latest_from is None else max(latest_from, event.valid_from)
                counts["expiring"] += event.valid_to is not None
    return counts


def summary_line(m: dict) -> str:
    return (f"fact-evolution {m['generator_version']} seed={m['seed']} chains={m['chains']} "
            f"revisions={m['revisions']} fact_queries={m['fact_queries']} "
            f"accuracy={m['current_truth_accuracy']:.4f} stale_current={m['stale_current']} "
            f"(rate {m['stale_current_rate']:.4f}) scope_leaks={m['scope_leaks']} "
            f"text_queries={m['text_queries']} hit@5={m['hit_at_5']:.4f} mrr={m['mrr']:.4f} "
            f"bundle_violations={m['bundle_budget_violations']} elapsed={m['elapsed_s']}s "
            f"{'PASS' if m['passed'] else 'FAIL'}")
