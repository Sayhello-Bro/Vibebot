"""Per-session sentence CLS memory. This is not a decoder KV cache."""

import math
import threading
import time
import uuid
from array import array
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class EncoderSpec:
    model_id: str
    revision: str
    dimension: int
    pooling: str = "last_hidden_state_cls"

    def __post_init__(self):
        if not self.model_id or not self.revision or self.dimension < 1:
            raise ValueError("Encoder identity, revision and positive dimension are required")


@dataclass(frozen=True)
class MemoryTurn:
    session_id: str
    turn_id: str
    sequence: int
    text: str
    timestamp: float
    cls_bytes: bytes
    norm: float

    @property
    def cls(self):
        # A fresh array prevents callers from modifying the cached vector.
        vector = array("f")
        vector.frombytes(self.cls_bytes)
        return vector


class CLSCache:
    """Keep every accepted turn until explicitly clearing a session.

    Only the immutable records are exposed. Vectors use compact CPU float32
    storage, with no tensor computation graph. One cache has one encoder spec.
    """

    def __init__(self, spec: EncoderSpec):
        self.spec = spec
        self._sessions = {}
        self._ids = {}
        self._lock = threading.RLock()

    def observe(self, session_id, text, cls, spec, turn_id=None):
        """Atomically store a turn and return (current, prior_history).

        Supplying a stable turn_id makes ingestion idempotent. Reusing an ID
        with changed text is rejected rather than silently corrupting history.
        A retry sees only the history preceding its original sequence.
        """
        if spec != self.spec:
            raise ValueError("CLS encoder mismatch; use a separate cache or re-encode")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("A nonempty session_id is required")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Empty input is not a sentence")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise ValueError("turn_id must be a nonempty string")
        vector = array("f", cls)
        if len(vector) != self.spec.dimension:
            raise ValueError("CLS vector dimension mismatch")
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("CLS vector must contain finite values")
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            raise ValueError("CLS vector must not be zero")

        with self._lock:
            rows = self._sessions.setdefault(session_id, [])
            ids = self._ids.setdefault(session_id, {})
            if turn_id in ids:
                existing = ids[turn_id]
                if existing.text != text:
                    raise ValueError("turn_id already exists with different text")
                return existing, tuple(rows[:existing.sequence - 1])
            current = MemoryTurn(
                session_id=session_id,
                turn_id=turn_id or uuid.uuid4().hex,
                sequence=len(rows) + 1,
                text=text,
                timestamp=time.time(),
                cls_bytes=vector.tobytes(),
                norm=norm,
            )
            prior = tuple(rows)
            rows.append(current)
            ids[current.turn_id] = current
            return current, prior

    def turns(self, session_id) -> Tuple[MemoryTurn, ...]:
        with self._lock:
            return tuple(self._sessions.get(session_id, ()))

    def count(self, session_id):
        with self._lock:
            return len(self._sessions.get(session_id, ()))

    def clear_session(self, session_id):
        """Explicit deletion only; starting a new session does not erase the old."""
        with self._lock:
            self._sessions.pop(session_id, None)
            self._ids.pop(session_id, None)
