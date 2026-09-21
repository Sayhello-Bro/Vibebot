import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pymongo import ASCENDING, MongoClient


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_CROWD_DB", "live_stream_crowd_db")
COLLECTION_NAME = os.environ.get("MONGODB_CROWD_COLLECTION", "crowd_slogans")
CONFIG_COLLECTION_NAME = os.environ.get("MONGODB_CROWD_CONFIG_COLLECTION", "crowd_config")
SOURCE_FILE = Path(
    os.environ.get(
        "SEED_CROWD_TOKEN_JSONL", Path(__file__).with_name("crowd_tokens.jsonl")
    )
)

DEFAULT_WEIGHT = float(os.environ.get("SEED_DEFAULT_WEIGHT", "1.0"))
DEFAULT_ENABLED = os.environ.get("SEED_DEFAULT_ENABLED", "true").lower() in {"true", "1", "yes", "y"}
SKIP_TOKENS = {"", "ignore", "[no_reply]", "no_reply", "null", "none"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_token(token: str) -> str:
    return token.strip()


def should_skip_token(token: str) -> bool:
    return normalize_token(token).lower() in SKIP_TOKENS


def read_tokens_from_jsonl(source_file: Path) -> list[dict[str, str]]:
    tokens: list[dict[str, str]] = []
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

            token = normalize_token(str(data.get("token") or data.get("text") or ""))
            if should_skip_token(token):
                continue

            dedupe_key = token.casefold()
            if dedupe_key in seen:
                continue

            seen.add(dedupe_key)
            response_mode = str(data.get("response_mode") or "same").lower()
            if response_mode not in {"same", "choice"}:
                response_mode = "same"
            tokens.append(
                {
                    "text": token,
                    "meaning": str(data.get("meaning") or "").strip()[:100],
                    "response_mode": response_mode,
                }
            )

    return tokens


def main() -> None:
    if not SOURCE_FILE.exists():
        raise FileNotFoundError(f"Seed source not found: {SOURCE_FILE}")

    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    collection = client[DATABASE_NAME][COLLECTION_NAME]
    config_collection = client[DATABASE_NAME][CONFIG_COLLECTION_NAME]
    collection.create_index([("text", ASCENDING)])
    collection.create_index([("enabled", ASCENDING)])

    tokens = read_tokens_from_jsonl(SOURCE_FILE)
    inserted = 0
    skipped = 0
    now = utc_now()

    for item in tokens:
        if collection.find_one({"text": item["text"]}):
            skipped += 1
            continue

        collection.insert_one(
            {
                "text": item["text"],
                "enabled": DEFAULT_ENABLED,
                "locked_text": False,
                "source": "seed",
                "candidate_type": "crowd_slogan",
                "meaning": item["meaning"],
                "response_mode": item["response_mode"],
                "created_at": now,
                "updated_at": now,
            }
        )
        inserted += 1

    if inserted:
        config_collection.update_one(
            {"_id": "crowd_revision"},
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
    print(f"Found crowd tokens: {len(tokens)}")
    print(f"Inserted: {inserted}")
    print(f"Skipped existing: {skipped}")
    print(f"Total enabled: {collection.count_documents({'enabled': True})}")


if __name__ == "__main__":
    main()
