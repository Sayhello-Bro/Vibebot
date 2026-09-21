import json
import os
from pathlib import Path

import ollama
from pymongo import MongoClient


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_DB", "live_stream_db")
COLLECTION_NAME = os.environ.get("MONGODB_COLLECTION", "user_input")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
SOURCE_FILE = Path(os.environ.get("SEED_TEXT_JSONL", Path(__file__).with_name("Text.jsonl")))

SKIP_REPLIES = {"", "ignore", "[NO_REPLY]", "NO_REPLY", "null", "None"}


def get_embedding(text: str) -> list[float]:
    try:
        response = ollama.embeddings(model=EMBEDDING_MODEL, prompt=text)
        return response["embedding"]
    except Exception:
        response = ollama.embed(model=EMBEDDING_MODEL, input=text)
        return response["embeddings"][0]


def main() -> None:
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(f"Seed source not found: {SOURCE_FILE}")

    replies: list[str] = []
    seen: set[str] = set()

    with SOURCE_FILE.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            reply = str(data.get("ai_reply") or data.get("reply") or "").strip()
            if reply in SKIP_REPLIES or reply.lower() in SKIP_REPLIES:
                continue
            if reply in seen:
                continue

            seen.add(reply)
            replies.append(reply)

    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    collection = client[DATABASE_NAME][COLLECTION_NAME]
    inserted = 0
    skipped = 0

    for reply in replies:
        if collection.find_one({"text": reply}):
            skipped += 1
            continue

        collection.insert_one(
            {
                "text": reply,
                "embedding": get_embedding(reply),
                "weight": 1.0,
                "enabled": True,
            }
        )
        inserted += 1

    print(f"Seed source: {SOURCE_FILE}")
    print(f"Inserted: {inserted}")
    print(f"Skipped existing: {skipped}")
    print(f"Total enabled: {collection.count_documents({'enabled': True})}")


if __name__ == "__main__":
    main()
