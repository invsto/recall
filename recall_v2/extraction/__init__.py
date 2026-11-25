"""Opt-in extraction (section 3.2): sources, disclosure manifests, redaction, budgets and candidate output.

Extraction is off per source until the operator enables it, sends only redacted chunks, needs an
acknowledged manifest for every run, stops before a call that would exceed a token budget, and writes
candidates only (never accepted content), each with provenance (source, chunk hash, run, model through
the run, confidence). Only a deterministic in-process extractor and a recording test double exist; no
network or command-line model client is part of this build.

Modules are imported directly (``from recall_v2.extraction import runs``).
"""
