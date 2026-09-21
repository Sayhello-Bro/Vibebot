import os
import json
import sys
from pathlib import Path

from google.cloud import speech
from pymongo.errors import PyMongoError


INTENT_KEY_MAP = {
    "PRODUCT_TRADE_ACTION": "base_context",
    "PRODUCT_COLOR_DESC": "color_context",
    "PRODUCT_MATERIAL": "fabric_context",
    "PRODUCT_SIZE_SPEC": "size_context",
    "PRODUCT_STYLE_DESC": "style_context",
}


def _get_mongo_collection():
    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise RuntimeError(
            "pymongo is required to load speech contexts from MongoDB."
        ) from exc

    mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    mongo_db = os.environ.get("MONGO_DB", "stt")
    mongo_collection = os.environ.get("MONGO_COLLECTION", "speech_contexts")

    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        db = client[mongo_db]
        return db[mongo_collection]
    except PyMongoError as exc:
        raise RuntimeError(
            f"Failed to connect to MongoDB using MONGO_URI={mongo_uri}, MONGO_DB={mongo_db}, "
            f"MONGO_COLLECTION={mongo_collection}. Details: {exc}"
        ) from exc


def _resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _load_local_contexts(product_mode: str):
    """Use packaged JSON contexts when MongoDB has no enabled documents."""
    context_dir = _resource_dir() / "speech_contexts" / product_mode
    documents = []
    for path in sorted(context_dir.glob("*_context.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        document["_context_key"] = path.stem
        documents.append(document)
    return documents

def load_speech_contexts(product_mode: str = "clothing"):
    """Load speech context definitions from the MongoDB collection.

    Rules:
    - Only documents with enable=True are loaded.
    - Only documents matching the given product_mode are loaded.
    - Returned structure remains compatible with the old WASAPI_test.py code.

    Returns
    -------
    tuple[dict[str, speech.SpeechContext], list[speech.SpeechContext], dict[str, list[str]]]
        (CONTEXTS, SPEECH_CONTEXT_LIST, INTENT_RULES)
    """
    collection = _get_mongo_collection()

    try:
        documents = list(collection.find(
            {
                "product_mode": product_mode,
                "enable": True,
            }
        ))
    except PyMongoError as exc:
        documents = []

    if not documents:
        documents = _load_local_contexts(product_mode)

    contexts = {}
    loaded_docs = []

    for doc in documents:
        intent = doc.get("intent")
        if not intent:
            continue

        context_key = doc.get("_context_key") or INTENT_KEY_MAP.get(intent)
        if not context_key:
            continue

        contexts[context_key] = speech.SpeechContext(
            phrases=doc.get("phrases", []),
            boost=doc.get("boost", 10.0),
        )
        loaded_docs.append(doc)

    if not loaded_docs:
        raise RuntimeError(
            f"MongoDB collection '{collection.name}' has no enabled speech contexts for product_mode='{product_mode}'."
        )

    speech_context_list = list(contexts.values())
    intent_rules = {
        doc["intent"]: contexts[doc.get("_context_key") or INTENT_KEY_MAP[doc["intent"]]].phrases
        for doc in loaded_docs
    }

    return contexts, speech_context_list, intent_rules
