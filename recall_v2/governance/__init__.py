"""Governance: near-duplicate flags, the review queue, conflict resolution and the operator capability.

Submodules are imported directly (``from recall_v2.governance import review``); this package imports
nothing eagerly, so the store can use ``governance.dedupe`` without an import cycle.

Principal and operator paths are separate. Principal writes (save, retract) live on the store and are
reached through ``recall_v2.api.Client``. Every operator function here takes an ``OperatorSession``
first and refuses anything else with ``forbidden``; only ``recall_v2.api.Operator`` creates one.
"""
