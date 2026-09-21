import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .vector_utils import top_k_by_similarity


class InMemoryExampleStore:
    def __init__(self):
        self.items: List[dict] = []

    def insert_example(self, doc: dict) -> dict:
        item = {
            "_id": doc.get("_id") or str(uuid.uuid4()),
            "createdAt": doc.get("createdAt") or datetime.now(timezone.utc),
            **doc,
        }
        self.items.append(item)
        return item

    def search_similar(self, query_embedding, top_k: int = 5, filter: Optional[dict] = None):
        filter = filter or {}
        filtered = []
        for item in self.items:
            matched = True
            for key, value in filter.items():
                if item.get(key) != value:
                    matched = False
                    break
            if matched:
                filtered.append(item)
        return top_k_by_similarity(query_embedding, filtered, top_k)


class MongoVectorStore:
    def __init__(self, collection):
        self.collection = collection
        self._ensure_collection()

    def _ensure_collection(self):
        collection_names = self.collection.database.list_collection_names()
        if self.collection.name not in collection_names:
            self.collection.database.create_collection(self.collection.name)

        self.collection.create_index("embeddingModel")
        self.collection.create_index("topicHint")
        self.collection.create_index("productHint")
        self.collection.create_index("styleHint")
        self.collection.create_index("createdAt")

    def count(self) -> int:
        return self.collection.count_documents({})

    def insert_example(self, doc: dict) -> dict:
        payload = {
            "productHint": doc.get("productHint"),
            "topicHint": doc.get("topicHint"),
            "speakerText": doc["speakerText"],
            "replyText": doc["replyText"],
            "embedding": doc["embedding"],
            "embeddingModel": doc["embeddingModel"],
            "liveSessionId": doc.get("liveSessionId"),
            "styleHint": doc.get("styleHint"),
            "metadata": doc.get("metadata", {}),
            "createdAt": doc.get("createdAt") or datetime.now(timezone.utc),
        }
        result = self.collection.insert_one(payload)
        return {"_id": result.inserted_id, **payload}

    def seed_if_empty(self, examples: List[dict]) -> int:
        if self.count() > 0:
            return 0
        inserted = 0
        for example in examples:
            self.insert_example(example)
            inserted += 1
        return inserted

    def search_similar(self, query_embedding, top_k: int = 5, filter: Optional[dict] = None):
        filter = filter or {}
        query = {}
        for key, value in filter.items():
            query[key] = value

        candidates = list(
            self.collection.find(
                query,
                {
                    "productHint": 1,
                    "topicHint": 1,
                    "speakerText": 1,
                    "replyText": 1,
                    "embeddingModel": 1,
                    "metadata": 1,
                    "createdAt": 1,
                    "embedding": 1,
                },
            )
        )

        docs = []
        for doc in candidates:
            doc = dict(doc)
            docs.append(doc)

        ranked = top_k_by_similarity(query_embedding, docs, top_k)
        return ranked


def create_mongo_vector_store(
    mongo_uri: str,
    db_name: str,
    collection_name: str = "reply_examples_v2",
    direct_connection: bool = True,
):
    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise RuntimeError("pymongo is required to use MongoVectorStore") from exc

    client = MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=3000,
        directConnection=direct_connection,
    )
    client.admin.command("ping")
    collection = client[db_name][collection_name]
    return MongoVectorStore(collection)
