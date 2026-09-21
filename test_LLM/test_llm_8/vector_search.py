"""MongoDB candidate loading, embedding and vector ranking.

This module deliberately owns every vector-related responsibility so the Flask
orchestration layer does not know how embeddings or cosine similarity work.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

import ollama
from pymongo import MongoClient


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_DB", "live_stream_db")
DEFAULT_COLLECTION_NAME = os.environ.get(
    "MONGODB_DEFAULT_COLLECTION", "default_replies"
)
USER_COLLECTION_NAME = os.environ.get("MONGODB_USER_COLLECTION", "user_replies")
CONFIG_COLLECTION_NAME = os.environ.get("MONGODB_CONFIG_COLLECTION", "reply_config")
CROWD_DATABASE_NAME = os.environ.get("MONGODB_CROWD_DB", "live_stream_crowd_db")
CROWD_COLLECTION_NAME = os.environ.get("MONGODB_CROWD_COLLECTION", "crowd_slogans")
CROWD_CONFIG_COLLECTION_NAME = os.environ.get(
    "MONGODB_CROWD_CONFIG_COLLECTION", "crowd_config"
)
STREAM_COLLECTION_NAME = os.environ.get(
    "MONGODB_STREAM_COLLECTION", "stream_profiles"
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.8"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "8"))
MAX_LEARNED_EXAMPLES = int(os.environ.get("MAX_LEARNED_EXAMPLES", "500"))
LEARNED_EXAMPLE_WEIGHT = float(os.environ.get("LEARNED_EXAMPLE_WEIGHT", "1.0"))
LIVE_DATABASE_PREFIX = os.environ.get("LIVE_DATABASE_PREFIX", "live_reply")
LIVE_EXAMPLE_COLLECTION = os.environ.get(
    "LIVE_EXAMPLE_COLLECTION", "reply_examples"
)
MIN_VECTOR_EXAMPLES = int(os.environ.get("MIN_VECTOR_EXAMPLES", "20"))
MAX_QUERY_FRAGMENTS = int(os.environ.get("MAX_QUERY_FRAGMENTS", "12"))
REVISION_CHECK_SECONDS = float(
    os.environ.get("CANDIDATE_REVISION_CHECK_SECONDS", "0")
)


def get_embedding(text: str) -> list[float]:
    """Create an embedding while supporting old and new Ollama clients."""

    try:
        response = ollama.embeddings(model=EMBEDDING_MODEL, prompt=text)
        return [float(value) for value in response["embedding"]]
    except Exception:
        response = ollama.embed(model=EMBEDDING_MODEL, input=text)
        return [float(value) for value in response["embeddings"][0]]


def get_embeddings(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    try:
        response = ollama.embed(model=EMBEDDING_MODEL, input=texts)
        return [
            [float(value) for value in embedding]
            for embedding in response["embeddings"]
        ]
    except Exception:
        return [get_embedding(text) for text in texts]


def split_speaker_fragments(text: str) -> list[str]:
    fragments = [
        " ".join(part.split()).strip()
        for part in re.split(r"[，。！？；!?;\n]+", str(text or ""))
    ]
    fragments = [fragment for fragment in fragments if len(fragment) >= 4]
    if not fragments and str(text or "").strip():
        fragments = [str(text).strip()]
    return fragments[-MAX_QUERY_FRAGMENTS:]


def cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    if not vector_a or not vector_b or len(vector_a) != len(vector_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = math.sqrt(sum(value * value for value in vector_a))
    norm_b = math.sqrt(sum(value * value for value in vector_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def serialize_candidate(
    source: str, document: dict[str, Any]
) -> dict[str, Any] | None:
    text = document.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if not document.get("enabled", True):
        return None
    embedding = document.get("embedding")
    return {
        "key": f"{source}:{document['_id']}",
        "source": source,
        "id": str(document["_id"]),
        "text": text.strip(),
        "embedding": [float(value) for value in embedding]
        if isinstance(embedding, list)
        else [],
        "weight": float(document.get("weight", 1.0)),
        "multi_output": bool(document.get("multi_output", False)),
        "candidate_type": str(document.get("candidate_type") or "custom_phrase"),
        "meaning": str(document.get("meaning") or "").strip(),
        "response_mode": str(document.get("response_mode") or "same").strip(),
        "account_ids": [
            str(value).strip()
            for value in document.get("account_ids", [])
            if str(value).strip()
        ] if isinstance(document.get("account_ids"), list) else [],
    }


class CandidateVectorSearch:
    """Thread-safe candidate cache that follows MongoDB revision changes."""

    def __init__(self, mongo_client: MongoClient | None = None) -> None:
        self.mongo_client = mongo_client or MongoClient(
            MONGODB_URI, serverSelectionTimeoutMS=5000
        )
        self.db = self.mongo_client[DATABASE_NAME]
        self.default_collection = self.db[DEFAULT_COLLECTION_NAME]
        self.user_collection = self.db[USER_COLLECTION_NAME]
        self.config_collection = self.db[CONFIG_COLLECTION_NAME]
        self.crowd_db = self.mongo_client[CROWD_DATABASE_NAME]
        self.crowd_collection = self.crowd_db[CROWD_COLLECTION_NAME]
        self.crowd_config_collection = self.crowd_db[
            CROWD_CONFIG_COLLECTION_NAME
        ]
        self.stream_collection = self.db[STREAM_COLLECTION_NAME]
        self._lock = threading.RLock()
        self._cache: list[dict[str, Any]] = []
        self._revision: int | None = None
        self._last_revision_check = 0.0
        self._learning_indexes_ready: set[str] = set()

    def _live_database_name(self, stream_id: str) -> str:
        safe_stream_id = re.sub(r"[^A-Za-z0-9_]", "_", stream_id).strip("_")
        safe_stream_id = (safe_stream_id or "stream")[:40]
        suffix = sha256(stream_id.encode("utf-8")).hexdigest()[:8]
        return f"{LIVE_DATABASE_PREFIX}_{safe_stream_id}_{suffix}"

    def _live_collections(self, stream_id: str):
        database_name = self._live_database_name(stream_id)
        database = self.mongo_client[database_name]
        return database_name, database[LIVE_EXAMPLE_COLLECTION], database["stream_meta"]

    def _ensure_learning_indexes(self, stream_id: str) -> None:
        database_name, examples, _ = self._live_collections(stream_id)
        with self._lock:
            if database_name in self._learning_indexes_ready:
                return
            examples.create_index([("product_type", 1), ("created_at", -1)])
            examples.create_index("example_key", unique=True)
            self.stream_collection.create_index("product_type")
            self._learning_indexes_ready.add(database_name)

    def delete_live_database(self, stream_id: str) -> dict[str, Any]:
        """Delete only the database derived from one explicit live-session ID."""

        stream_id = str(stream_id or "").strip()
        if not stream_id:
            raise ValueError("stream_id is required")
        database_name = self._live_database_name(stream_id)
        existed = database_name in self.mongo_client.list_database_names()
        self.mongo_client.drop_database(database_name)
        self.stream_collection.delete_one({"_id": stream_id})
        with self._lock:
            self._learning_indexes_ready.discard(database_name)
        return {
            "stream_id": stream_id,
            "database_name": database_name,
            "database_existed": existed,
            "deleted": True,
        }

    def set_stream_product_type(self, stream_id: str, product_type: str) -> str:
        stream_id = str(stream_id or "").strip()
        product_type = str(product_type or "").strip()
        if not stream_id or not product_type:
            raise ValueError("stream_id and product_type are required")
        self._ensure_learning_indexes(stream_id)
        self.stream_collection.update_one(
            {"_id": stream_id},
            {
                "$set": {
                    "product_type": product_type,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
            },
            upsert=True,
        )
        database_name, _, meta_collection = self._live_collections(stream_id)
        meta_collection.update_one(
            {"_id": "stream"},
            {
                "$set": {
                    "stream_id": stream_id,
                    "product_type": product_type,
                    "database_name": database_name,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
            },
            upsert=True,
        )
        return product_type

    def get_stream_product_type(self, stream_id: str) -> str | None:
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return None
        document = self.stream_collection.find_one({"_id": stream_id})
        value = document.get("product_type") if document else None
        return str(value).strip() if value else None

    def resolve_stream_product_type(
        self, stream_id: str, supplied_product_type: str | None = None
    ) -> str | None:
        supplied = str(supplied_product_type or "").strip()
        if supplied:
            return self.set_stream_product_type(stream_id, supplied)
        return self.get_stream_product_type(stream_id)

    def get_learning_status(
        self, stream_id: str, product_type: str | None
    ) -> dict[str, Any]:
        database_name, learned_collection, _ = self._live_collections(stream_id)
        query = {"product_type": str(product_type)} if product_type else {}
        example_count = learned_collection.count_documents(query)
        return {
            "live_database": database_name,
            "example_count": example_count,
            "minimum_vector_examples": MIN_VECTOR_EXAMPLES,
            "learning_ready": example_count >= MIN_VECTOR_EXAMPLES,
        }

    def store_generated_examples(
        self,
        stream_id: str,
        product_type: str,
        speaker_text: str,
        replies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Store validated Qwen replies with their quoted speaker fragments."""

        stream_id = str(stream_id or "").strip()
        product_type = str(product_type or "").strip()
        speaker_text = str(speaker_text or "").strip()
        usable = [
            item
            for item in replies
            if item.get("reply_source") == "qwen"
            and item.get("has_reply")
            and str(item.get("evidence_text") or "").strip()
            and str(item.get("reply") or "").strip().casefold() != "ignore"
        ]
        if not stream_id or not product_type or not speaker_text or not usable:
            return {"stored_count": 0, "matched_count": len(usable)}

        self._ensure_learning_indexes(stream_id)
        database_name, learned_collection, _ = self._live_collections(stream_id)
        stored_count = 0
        evidence_embeddings = get_embeddings(
            [str(item["evidence_text"]).strip() for item in usable]
        )
        for item, evidence_embedding in zip(usable, evidence_embeddings):
            reply_text = str(item["reply"]).strip()
            evidence_text = str(item["evidence_text"]).strip()
            example_key = sha256(
                f"{product_type}\0{evidence_text}\0{reply_text}".encode(
                    "utf-8"
                )
            ).hexdigest()
            result = learned_collection.update_one(
                {"example_key": example_key},
                {
                    "$setOnInsert": {
                        "example_key": example_key,
                        "stream_id": stream_id,
                        "product_type": product_type,
                        "speaker_text": speaker_text,
                        "speaker_fragment": evidence_text,
                        "reply_text": reply_text,
                        "speaker_embedding": evidence_embedding,
                        "embedding_model": EMBEDDING_MODEL,
                        "account_id": str(item.get("account_id") or ""),
                        "style": str(item.get("style") or ""),
                        "source": "qwen_generated",
                        "created_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
            if result.upserted_id is not None:
                stored_count += 1
        return {
            "stored_count": stored_count,
            "matched_count": len(usable),
            "database_name": database_name,
            "example_count": learned_collection.count_documents({}),
        }

    @property
    def revision(self) -> int | None:
        return self._revision

    @property
    def cached_count(self) -> int:
        with self._lock:
            return len(self._cache)

    def _read_revision(self) -> int:
        document = self.config_collection.find_one({"_id": "candidate_revision"})
        crowd_document = self.crowd_config_collection.find_one(
            {"_id": "crowd_revision"}
        )
        custom_revision = int(document.get("revision", 0)) if document else 0
        crowd_revision = (
            int(crowd_document.get("revision", 0)) if crowd_document else 0
        )
        return custom_revision + crowd_revision

    def _load_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        collections = (
            ("default", self.default_collection),
            ("user", self.user_collection),
            ("crowd", self.crowd_collection),
        )
        for source, collection in collections:
            for document in collection.find({"enabled": True}):
                candidate = serialize_candidate(source, document)
                if candidate:
                    candidates.append(candidate)
        return candidates

    def refresh(self, force: bool = False) -> dict[str, Any]:
        """Reload candidates when candidate_revision has changed."""

        with self._lock:
            now = time.monotonic()
            check_due = (
                force
                or self._revision is None
                or REVISION_CHECK_SECONDS <= 0
                or now - self._last_revision_check >= REVISION_CHECK_SECONDS
            )
            if not check_due:
                return self.cache_status(refreshed=False)

            current_revision = self._read_revision()
            self._last_revision_check = now
            if force or self._revision != current_revision:
                self._cache = self._load_candidates()
                self._revision = current_revision
                return self.cache_status(refreshed=True)
            return self.cache_status(refreshed=False)

    def cache_status(self, refreshed: bool = False) -> dict[str, Any]:
        return {
            "cached_reply_count": len(self._cache),
            "candidate_revision": self._revision,
            "refreshed": refreshed,
        }

    def list_cached(self, include_embedding: bool = False) -> list[dict[str, Any]]:
        self.refresh()
        with self._lock:
            items = []
            for candidate in self._cache:
                item = dict(candidate)
                embedding = item.pop("embedding", [])
                item["embedding_dimensions"] = len(embedding)
                if include_embedding:
                    item["embedding"] = embedding
                items.append(item)
            return items

    def list_custom_phrases(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.list_cached()
            if item.get("source") == "user"
            and item.get("candidate_type") == "custom_phrase"
        ]

    def list_crowd_slogans(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.list_cached()
            if item.get("candidate_type") == "crowd_slogan"
        ]

    def search_fixed_candidates(
        self,
        raw_text: str,
        allowed_keys: set[str] | None = None,
        limit: int = MAX_CANDIDATES,
        threshold: float = SIMILARITY_THRESHOLD,
    ) -> dict[str, Any]:
        """Rank enabled default, custom and crowd rows without generated examples."""

        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("raw_text must not be blank")
        if limit < 1:
            raise ValueError("limit must be greater than 0")

        started = time.perf_counter()
        cache_status = self.refresh()
        query_fragments = split_speaker_fragments(raw_text)
        query_embeddings = get_embeddings(query_fragments)
        with self._lock:
            candidates = [
                dict(item)
                for item in self._cache
                if allowed_keys is None or str(item.get("key")) in allowed_keys
            ]

        missing_embeddings = [
            candidate for candidate in candidates if not candidate.get("embedding")
        ]
        if missing_embeddings:
            generated_embeddings = get_embeddings(
                [str(candidate["text"]) for candidate in missing_embeddings]
            )
            generated_by_key = {}
            for candidate, embedding in zip(
                missing_embeddings, generated_embeddings
            ):
                candidate["embedding"] = embedding
                generated_by_key[str(candidate["key"])] = embedding
            with self._lock:
                for cached_candidate in self._cache:
                    generated = generated_by_key.get(str(cached_candidate.get("key")))
                    if generated:
                        cached_candidate["embedding"] = generated

        ranked: list[dict[str, Any]] = []
        compared = 0
        for candidate in candidates:
            embedding = candidate.pop("embedding", [])
            if not embedding:
                continue
            compared += 1
            fragment_scores = [
                cosine_similarity(query_embedding, embedding)
                for query_embedding in query_embeddings
            ]
            similarity = max(fragment_scores, default=0.0)
            if similarity < threshold:
                continue
            best_index = fragment_scores.index(similarity)
            candidate.update(
                {
                    "similarity": similarity,
                    "score": similarity * float(candidate.get("weight", 1.0)),
                    "matched_current_fragment": query_fragments[best_index],
                }
            )
            ranked.append(candidate)

        ranked.sort(key=lambda item: item["score"], reverse=True)
        selected = ranked[:limit]
        return {
            "candidates": selected,
            "candidate_count": len(selected),
            "best_similarity": max(
                (item["similarity"] for item in selected), default=0.0
            ),
            "threshold": threshold,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimensions": len(query_embeddings[0])
            if query_embeddings
            else 0,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "query_fragment_count": len(query_fragments),
            "static_candidates_compared": compared,
            "learned_examples_considered": 0,
            "learned_candidates_selected": 0,
            **cache_status,
        }

    def search(
        self,
        raw_text: str,
        limit: int = MAX_CANDIDATES,
        threshold: float = SIMILARITY_THRESHOLD,
        stream_id: str | None = None,
        product_type: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError("raw_text must not be blank")
        if limit < 1:
            raise ValueError("limit must be greater than 0")

        started = time.perf_counter()
        cache_status = self.refresh()
        ranked: list[dict[str, Any]] = []
        learned_considered = 0
        example_count = 0
        learning_ready = False
        database_name = None
        query_fragments: list[str] = []
        if stream_id and product_type:
            database_name, learned_collection, _ = self._live_collections(
                str(stream_id)
            )
            query = {
                "product_type": str(product_type),
                "embedding_model": EMBEDDING_MODEL,
            }
            example_count = learned_collection.count_documents(query)
            learning_ready = example_count >= MIN_VECTOR_EXAMPLES
            if learning_ready:
                query_fragments = split_speaker_fragments(raw_text)
                query_embeddings = get_embeddings(query_fragments)
                learned_documents = learned_collection.find(query).sort(
                    "created_at", -1
                ).limit(MAX_LEARNED_EXAMPLES)
                learned_by_reply: dict[str, dict[str, Any]] = {}
                for document in learned_documents:
                    learned_considered += 1
                    embedding = document.get("speaker_embedding")
                    reply_text = str(document.get("reply_text") or "").strip()
                    if (
                        not isinstance(embedding, list)
                        or not embedding
                        or not reply_text
                    ):
                        continue
                    fragment_scores = [
                        cosine_similarity(query_embedding, embedding)
                        for query_embedding in query_embeddings
                    ]
                    similarity = max(fragment_scores, default=0.0)
                    if similarity < threshold:
                        continue
                    best_index = fragment_scores.index(similarity)
                    candidate = {
                        "key": f"qwen_example:{document['_id']}",
                        "source": "qwen_example",
                        "id": str(document["_id"]),
                        "text": reply_text,
                        "speaker_text": str(document.get("speaker_text") or ""),
                        "speaker_fragment": str(
                            document.get("speaker_fragment") or ""
                        ),
                        "matched_current_fragment": query_fragments[best_index],
                        "product_type": str(document.get("product_type") or ""),
                        "similarity": similarity,
                        "weight": LEARNED_EXAMPLE_WEIGHT,
                        "score": similarity * LEARNED_EXAMPLE_WEIGHT,
                        "multi_output": False,
                    }
                    previous = learned_by_reply.get(reply_text)
                    if previous is None or candidate["score"] > previous["score"]:
                        learned_by_reply[reply_text] = candidate
                ranked.extend(learned_by_reply.values())

        ranked.sort(key=lambda item: item["score"], reverse=True)
        selected = ranked[:limit]
        return {
            "candidates": selected,
            "candidate_count": len(selected),
            "best_similarity": (
                max(item["similarity"] for item in selected) if selected else 0.0
            ),
            "threshold": threshold,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimensions": (
                len(query_embeddings[0])
                if learning_ready and query_embeddings
                else 0
            ),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "live_database": database_name,
            "example_count": example_count,
            "minimum_vector_examples": MIN_VECTOR_EXAMPLES,
            "learning_ready": learning_ready,
            "query_fragment_count": len(query_fragments),
            "static_candidates_compared": 0,
            "learned_examples_considered": learned_considered,
            "learned_candidates_selected": sum(
                1 for item in selected if item["source"] == "qwen_example"
            ),
            **cache_status,
        }

    def health(self) -> dict[str, Any]:
        self.mongo_client.admin.command("ping")
        status = self.refresh()
        return {
            "database": DATABASE_NAME,
            "default_collection": DEFAULT_COLLECTION_NAME,
            "user_collection": USER_COLLECTION_NAME,
            "crowd_database": CROWD_DATABASE_NAME,
            "crowd_collection": CROWD_COLLECTION_NAME,
            "config_collection": CONFIG_COLLECTION_NAME,
            "stream_collection": STREAM_COLLECTION_NAME,
            "embedding_model": EMBEDDING_MODEL,
            "similarity_threshold": SIMILARITY_THRESHOLD,
            "max_candidates": MAX_CANDIDATES,
            "max_learned_examples": MAX_LEARNED_EXAMPLES,
            "learned_example_weight": LEARNED_EXAMPLE_WEIGHT,
            "live_database_prefix": LIVE_DATABASE_PREFIX,
            "live_example_collection": LIVE_EXAMPLE_COLLECTION,
            "minimum_vector_examples": MIN_VECTOR_EXAMPLES,
            "max_query_fragments": MAX_QUERY_FRAGMENTS,
            "revision_check_seconds": REVISION_CHECK_SECONDS,
            **status,
        }


candidate_search = CandidateVectorSearch()
