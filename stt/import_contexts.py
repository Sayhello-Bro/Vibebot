from pathlib import Path
import json
from pymongo import MongoClient


MONGO_URI = "mongodb://localhost:27017"
MONGO_DB = "stt"
MONGO_COLLECTION = "speech_contexts"
PRODUCT_MODE = "clothing"

INTENT_FALLBACK_BY_FILE = {
    "base_context": "PRODUCT_TRADE_ACTION",
    "color_context": "PRODUCT_COLOR_DESC",
    "fabric_context": "PRODUCT_MATERIAL",
    "size_context": "PRODUCT_SIZE_SPEC",
    "style_context": "PRODUCT_STYLE_DESC",
}


def main():
    base_dir = Path(__file__).resolve().parent / "speech_contexts" / PRODUCT_MODE

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[MONGO_DB]
    collection = db[MONGO_COLLECTION]

    collection.create_index(
        [
            ("product_mode", 1),
            ("enable", 1),
        ],
        name="product_mode_enable_idx",
    )

    for file_path in sorted(base_dir.glob("*.json")):
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        intent = data.get("intent") or INTENT_FALLBACK_BY_FILE.get(file_path.stem)
        if not intent:
            print(f"Skipped file without recognized intent: {file_path.name}")
            continue

        collection.update_one(
            {
                "product_mode": PRODUCT_MODE,
                "intent": intent,
            },
            {
                "$set": {
                    "boost": data.get("boost", 10.0),
                    "phrases": data.get("phrases", []),
                    "enable": True,
                }
            },
            upsert=True,
        )

        print(
            f"Upserted: product_mode={PRODUCT_MODE}, intent={intent}"
        )

    print(f"Done. Collection: {db.name}.{collection.name}")


if __name__ == "__main__":
    main()
