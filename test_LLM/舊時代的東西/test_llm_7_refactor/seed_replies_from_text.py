import json
import os
from datetime import datetime, timezone
from pathlib import Path

import ollama
from pymongo import ASCENDING, MongoClient


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_DB", "live_stream_db")
COLLECTION_NAME = os.environ.get("MONGODB_USER_COLLECTION", "user_replies")
CONFIG_COLLECTION_NAME = os.environ.get("MONGODB_CONFIG_COLLECTION", "reply_config")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
SOURCE_FILE = Path(os.environ.get("SEED_TEXT_JSONL", Path(__file__).with_name("Text.jsonl")))

DEFAULT_WEIGHT = float(os.environ.get("SEED_DEFAULT_WEIGHT", "1.0"))
DEFAULT_ENABLED = os.environ.get("SEED_DEFAULT_ENABLED", "true").lower() in {"true", "1", "yes", "y"}
DEFAULT_MULTI_OUTPUT = os.environ.get("SEED_DEFAULT_MULTI_OUTPUT", "false").lower() in {"true", "1", "yes", "y"}
SKIP_REPLIES = {"", "ignore", "[no_reply]", "no_reply", "null", "none"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_reply(reply: str) -> str:
    return reply.strip()


def should_skip_reply(reply: str) -> bool:
    return normalize_reply(reply).lower() in SKIP_REPLIES


def get_embedding(text: str) -> list[float]:
    try:
        response = ollama.embeddings(model=EMBEDDING_MODEL, prompt=text)
        return response["embedding"]
    except Exception:
        response = ollama.embed(model=EMBEDDING_MODEL, input=text)
        return response["embeddings"][0]


def read_replies_from_jsonl(source_file: Path) -> list[str]:
    replies: list[str] = []
    seen: set[str] = set()

    with source_file.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            reply = normalize_reply(str(data.get("ai_reply") or data.get("reply") or ""))
            if should_skip_reply(reply):
                continue

            dedupe_key = reply.casefold()
            if dedupe_key in seen:
                continue

            seen.add(dedupe_key)
            replies.append(reply)

    return replies


def main() -> None:
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(f"Seed source not found: {SOURCE_FILE}")

    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    collection = client[DATABASE_NAME][COLLECTION_NAME]
    config_collection = client[DATABASE_NAME][CONFIG_COLLECTION_NAME]
    collection.create_index([("text", ASCENDING)])
    collection.create_index([("enabled", ASCENDING)])

    replies = read_replies_from_jsonl(SOURCE_FILE)
    inserted = 0
    skipped = 0
    now = utc_now()

    for reply in replies:
        if collection.find_one({"text": reply}):
            skipped += 1
            continue

        collection.insert_one(
            {
                "text": reply,
                "embedding": get_embedding(reply),
                "weight": DEFAULT_WEIGHT,
                "multi_output": DEFAULT_MULTI_OUTPUT,
                "enabled": DEFAULT_ENABLED,
                "locked_text": False,
                "source": "user",
                "created_at": now,
                "updated_at": now,
            }
        )
        inserted += 1

    if inserted:
        config_collection.update_one(
            {"_id": "candidate_revision"},
            {
                "$inc": {"revision": 1},
                "$set": {
                    "updated_at": utc_now(),
                    "last_action": "seed_imported",
                    "last_item_id": None,
                    "last_text": "",
                },
                "$setOnInsert": {"created_at": utc_now()},
            },
            upsert=True,
        )

    print(f"Seed source: {SOURCE_FILE}")
    print(f"Found replies: {len(replies)}")
    print(f"Inserted: {inserted}")
    print(f"Skipped existing: {skipped}")
    print(f"Total enabled: {collection.count_documents({'enabled': True})}")


if __name__ == "__main__":
    main()
