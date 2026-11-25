"""Library interfaces: Client (a principal, identified by its token) and Operator (the store's owner).

The write path of this milestone lives here (Client.save, Client.retract) on top of the store's
transactional writes; governance (review, conflicts, near-duplicate flags) lives in recall_v2.governance.
"""
from .client import Client
from .operator import Operator

__all__ = ["Client", "Operator"]
