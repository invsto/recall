"""Optional embeddings: the Embedder protocol and the stored vector format.

No embedding model ships with Recall. A store gets vectors only when the caller passes an object that
implements Embedder; without one, the vector leg is off and nothing is written to `embeddings`.
"""
from __future__ import annotations

from array import array
import math
import sys
from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """A local embedding function. `model` names the model and version; `dim` is the vector length."""

    model: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[Sequence[float]]:
        ...


def document_text(title: str, body: str) -> str:
    """The text embedded for one revision: the same title and body the FTS row holds."""
    return f"{title}\n{body}"


def encode(vector: Sequence[float], dim: int) -> bytes:
    """Little-endian float32, L2-normalised so cosine is a dot product."""
    if len(vector) != dim:
        raise ValueError("embedding has the wrong dimension")
    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    values = array("f", [(float(x) / norm) if norm else 0.0 for x in vector])
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def decode(blob: bytes) -> array:
    values = array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Exact cosine similarity of two vectors (0.0 if either is zero)."""
    dot = sum(a * b for a, b in zip(left, right))
    nl = math.sqrt(sum(a * a for a in left))
    nr = math.sqrt(sum(b * b for b in right))
    return dot / (nl * nr) if nl and nr else 0.0
