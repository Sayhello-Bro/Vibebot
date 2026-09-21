import math
from typing import Iterable, List, Sequence


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0

    for av, bv in zip(a, b):
        av = float(av)
        bv = float(bv)
        dot += av * bv
        norm_a += av * av
        norm_b += bv * bv

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def top_k_by_similarity(query_vector: Sequence[float], items: Iterable[dict], k: int = 5):
    scored = []
    for item in items:
        score = cosine_similarity(query_vector, item.get("embedding", []))
        scored.append({**item, "similarity": score})

    scored.sort(key=lambda row: row["similarity"], reverse=True)
    return scored[:k]

