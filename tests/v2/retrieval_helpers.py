"""Helpers for the retrieval tests: a deterministic fake embedder and store builders."""
from __future__ import annotations

from hashlib import sha256
import re

from recall_v2.store import Store

from .helpers import StepClock, StoreTestCase

_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Hashed bag of words: deterministic, no model. Texts sharing words have positive cosine."""

    model = "fake-hash-v1"
    dim = 64

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += len(texts)
        out = []
        for text in texts:
            vector = [0.0] * self.dim
            for word in _WORD.findall(text.lower()):
                digest = sha256(word.encode()).digest()
                vector[digest[0] % self.dim] += 1.0 + digest[1] / 255.0
            out.append(vector)
        return out


class RetrievalTestCase(StoreTestCase):
    def open_store(self, *, embedder=None, clock=None) -> Store:
        self.clock = clock or StepClock()
        self.path = self.private_dir() / "store.sqlite3"
        store = Store.open(self.path, create=True, clock=self.clock, embedder=embedder)
        self.addCleanup(store.close)
        return store

    def note(self, store, principal, key, title, body, **kwargs):
        return store.save(principal, kind="note", request_id=f"req-{key}-{self.clock.now}", key=key, title=title,
                          body=body, **kwargs)

    def fact(self, store, principal, subject, predicate, value, **kwargs):
        return store.save(principal, kind="fact", request_id=f"req-f-{self.clock.now}",
                          fact={"subject": subject, "predicate": predicate, "value": value}, **kwargs)

    def project_note(self, store, principal, project, key, title, body):
        result = store.save(principal, kind="note", request_id=f"req-{key}-{self.clock.now}", scope="project",
                            project=project, key=key, title=title, body=body)
        store.review_approve(result["item_id"], result["rev"])
        return result
