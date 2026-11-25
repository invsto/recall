"""Shared helpers for the v2 tests: private temp directories and a deterministic clock."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from recall_v2.store import Store

T0 = 1_795_000_000_000_000  # 2026-11-17T..Z in microseconds


class StepClock:
    """Returns now and then advances by step microseconds on every call."""

    def __init__(self, start: int = T0, step: int = 1_000):
        self.now = start
        self.step = step

    def __call__(self) -> int:
        value = self.now
        self.now += self.step
        return value


class StoreTestCase(unittest.TestCase):
    def private_dir(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        os.chmod(path, 0o700)
        return path

    def new_store(self, clock=None) -> Store:
        self.clock = clock or StepClock()
        store = Store.open(self.private_dir() / "store.sqlite3", create=True, clock=self.clock)
        self.addCleanup(store.close)
        return store

    def add_principal(self, store: Store, name: str):
        return store.authenticate(store.principal_add(name))

    def assertCode(self, code: str, func, *args, **kwargs):
        from recall_v2.core.errors import RecallError
        with self.assertRaises(RecallError) as raised:
            func(*args, **kwargs)
        self.assertEqual(raised.exception.code, code, raised.exception.message)
        return raised.exception
