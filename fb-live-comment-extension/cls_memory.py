"""Bounded, session-isolated CLS cache and deterministic memory retrieval."""

from __future__ import annotations

import heapq
import json
import math
import threading
import time
import uuid
from array import array
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class EncoderSpec:
    model_id: str
    revision: str
    dimension: int
    pooling: str = "last_hidden_state_cls"

    def __post_init__(self) -> None:
        if not self.model_id or not self.revision or self.dimension < 1:
            raise ValueError("encoder model, revision and dimension are required")


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
    def cls(self) -> array:
        vector = array("f")
        vector.frombytes(self.cls_bytes)
        return vector


class CLSCache:
    """Store immutable float32 CLS vectors separately for each live stream."""

    def __init__(self, spec: EncoderSpec, max_turns: int = 500) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self.spec = spec
        self.max_turns = max_turns
        self._sessions: dict[str, list[MemoryTurn]] = {}
        self._ids: dict[str, dict[str, MemoryTurn]] = {}
        self._sequences: dict[str, int] = {}
        self._lock = threading.RLock()

    def lookup(
        self, session_id: str, turn_id: str, text: str
    ) -> tuple[MemoryTurn, tuple[MemoryTurn, ...]] | None:
        with self._lock:
            current = self._ids.get(session_id, {}).get(turn_id)
            if current is None:
                return None
            if current.text != text:
                raise ValueError("turn_id already exists with different text")
            history = tuple(
                row
                for row in self._sessions.get(session_id, ())
                if row.sequence < current.sequence
            )
            return current, history

    def observe(
        self,
        session_id: str,
        text: str,
        cls: Iterable[float],
        spec: EncoderSpec,
        turn_id: str | None = None,
    ) -> tuple[MemoryTurn, tuple[MemoryTurn, ...], bool]:
        if spec != self.spec:
            raise ValueError("CLS encoder mismatch")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id.strip()):
            raise ValueError("turn_id must be a nonempty string")
        vector = array("f", cls)
        if len(vector) != self.spec.dimension:
            raise ValueError("CLS vector dimension mismatch")
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("CLS vector contains a non-finite value")
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            raise ValueError("CLS vector must not be zero")

        with self._lock:
            rows = self._sessions.setdefault(session_id, [])
            ids = self._ids.setdefault(session_id, {})
            if turn_id and turn_id in ids:
                existing = ids[turn_id]
                if existing.text != text:
                    raise ValueError("turn_id already exists with different text")
                history = tuple(row for row in rows if row.sequence < existing.sequence)
                return existing, history, False

            sequence = self._sequences.get(session_id, 0) + 1
            self._sequences[session_id] = sequence
            current = MemoryTurn(
                session_id=session_id,
                turn_id=turn_id or uuid.uuid4().hex,
                sequence=sequence,
                text=text,
                timestamp=time.time(),
                cls_bytes=vector.tobytes(),
                norm=norm,
            )
            history = tuple(rows)
            rows.append(current)
            ids[current.turn_id] = current
            while len(rows) > self.max_turns:
                removed = rows.pop(0)
                ids.pop(removed.turn_id, None)
            return current, history, True

    def count(self, session_id: str) -> int:
        with self._lock:
            return len(self._sessions.get(session_id, ()))

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def clear_session(self, session_id: str) -> int:
        with self._lock:
            removed = len(self._sessions.pop(session_id, ()))
            self._ids.pop(session_id, None)
            self._sequences.pop(session_id, None)
            return removed


class MemoryReader:
    def __init__(self, top_k: int = 2, recent_k: int = 2, max_chars: int = 400) -> None:
        if top_k < 0 or recent_k < 0 or max_chars < 1:
            raise ValueError("invalid CLS retrieval limits")
        self.top_k = top_k
        self.recent_k = recent_k
        self.max_chars = max_chars

    def read(
        self, current: MemoryTurn, history: tuple[MemoryTurn, ...]
    ) -> list[dict[str, object]]:
        query = current.cls
        scores: dict[str, float] = {}
        for turn in history:
            if turn.session_id != current.session_id or turn.sequence >= current.sequence:
                raise ValueError("history must precede the current turn in one session")
            vector = turn.cls
            if len(vector) != len(query):
                raise ValueError("cannot compare different CLS dimensions")
            score = sum(a * b for a, b in zip(query, vector)) / (
                current.norm * turn.norm
            )
            scores[turn.turn_id] = max(-1.0, min(1.0, score))

        ranked = heapq.nlargest(
            self.top_k,
            history,
            key=lambda turn: (scores[turn.turn_id], turn.sequence),
        )
        recent = list(history[-self.recent_k :]) if self.recent_k else []
        recent_ids = {turn.turn_id for turn in recent}
        semantic_ids = {turn.turn_id for turn in ranked}
        chosen: dict[str, dict[str, object]] = {}
        remaining = self.max_chars
        for turn in list(reversed(recent)) + ranked:
            if turn.turn_id in chosen or remaining <= 0:
                continue
            excerpt = turn.text[:remaining]
            remaining -= len(excerpt)
            chosen[turn.turn_id] = {
                "turn_id": turn.turn_id,
                "sequence": turn.sequence,
                "text": excerpt,
                "truncated": len(excerpt) < len(turn.text),
                "similarity": round(scores[turn.turn_id], 6),
                "selection": [
                    name
                    for name, ids in (
                        ("recent", recent_ids),
                        ("semantic", semantic_ids),
                    )
                    if turn.turn_id in ids
                ],
            }
        return sorted(chosen.values(), key=lambda row: int(row["sequence"]))


def format_live_memory(records: list[dict[str, object]]) -> str:
    if not records:
        return ""
    rows = [
        json.dumps(
            {"sequence": item["sequence"], "text": item["text"]},
            ensure_ascii=False,
        )
        for item in records
    ]
    return "\n".join(rows)
