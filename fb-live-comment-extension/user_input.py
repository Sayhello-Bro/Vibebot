import io
import os
import sys
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import ASCENDING, MongoClient


if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


app = Flask(__name__)
CORS(app)


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_DB", "live_stream_db")
DEFAULT_COLLECTION_NAME = os.environ.get("MONGODB_DEFAULT_COLLECTION", "default_replies")
USER_COLLECTION_NAME = os.environ.get("MONGODB_USER_COLLECTION", "user_replies")

EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "ollama")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

DEFAULT_WEIGHT = 1.0
MIN_WEIGHT = 0.0
MAX_WEIGHT = 10.0

DEFAULT_REPLIES = [
    {"text": "+1", "multi_output": True},
    {"text": "有沒有優惠", "multi_output": False},
    {"text": "我來了", "multi_output": False},
    {"text": "哈哈", "multi_output": False},
    {"text": "這個好", "multi_output": False},
    {"text": "我喜歡", "multi_output": False},
]


mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = mongo_client[DATABASE_NAME]
default_collection = db[DEFAULT_COLLECTION_NAME]
user_collection = db[USER_COLLECTION_NAME]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def success_response(data: Any = None, status_code: int = 200):
    response = {"status": "success"}
    if data is not None:
        response.update(data)
    return jsonify(response), status_code


def error_response(message: str, status_code: int = 400):
    return jsonify({"status": "error", "error": message}), status_code


def get_json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("request body must be JSON object")
    return data


def validate_text(text: Any) -> str:
    if not isinstance(text, str):
        raise ValueError("text is required")
    text = text.strip()
    if not text:
        raise ValueError("text must not be blank")
    return text


def validate_weight(weight: Any) -> float:
    if weight is None:
        return DEFAULT_WEIGHT
    try:
        value = float(weight)
    except (TypeError, ValueError) as exc:
        raise ValueError("weight must be a number") from exc
    if value < MIN_WEIGHT or value > MAX_WEIGHT:
        raise ValueError(f"weight must be between {MIN_WEIGHT} and {MAX_WEIGHT}")
    return value


def parse_bool(value: Any, default: bool = False, field_name: str = "value") -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    raise ValueError(f"{field_name} must be true or false")


def parse_object_id(item_id: str) -> ObjectId:
    if not ObjectId.is_valid(item_id):
        raise ValueError("id is not a valid MongoDB ObjectId")
    return ObjectId(item_id)


def get_ollama_embedding(text: str) -> list[float]:
    import ollama

    try:
        response = ollama.embeddings(model=EMBEDDING_MODEL, prompt=text)
        return response["embedding"]
    except Exception:
        response = ollama.embed(model=EMBEDDING_MODEL, input=text)
        return response["embeddings"][0]


def get_sentence_transformer_embedding(text: str) -> list[float]:
    from sentence_transformers import SentenceTransformer

    if not hasattr(get_sentence_transformer_embedding, "model"):
        get_sentence_transformer_embedding.model = SentenceTransformer(EMBEDDING_MODEL)

    model = get_sentence_transformer_embedding.model
    embedding = model.encode(text, normalize_embeddings=True)
    return [float(value) for value in embedding.tolist()]


def generate_embedding(text: str) -> list[float]:
    if EMBEDDING_PROVIDER == "ollama":
        return get_ollama_embedding(text)
    if EMBEDDING_PROVIDER == "sentence_transformers":
        return get_sentence_transformer_embedding(text)
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {EMBEDDING_PROVIDER}")


def generate_embedding_for_storage(text: str) -> tuple[list[float], str | None]:
    """Allow text to be saved while Ollama is temporarily unavailable."""
    try:
        return generate_embedding(text), None
    except Exception as exc:
        return [], str(exc)


def serialize_document(document: dict[str, Any], show_embedding: bool = False) -> dict[str, Any]:
    item = dict(document)
    item["id"] = str(item.pop("_id"))
    if not show_embedding:
        embedding = item.pop("embedding", None)
        item["embedding_dimensions"] = len(embedding) if embedding else 0
    return item


def build_text_query(text: str, exact: bool = True) -> dict[str, Any]:
    text = validate_text(text)
    if exact:
        return {"text": text}
    return {"text": {"$regex": text, "$options": "i"}}


def init_db() -> None:
    for collection in (default_collection, user_collection):
        collection.create_index([("text", ASCENDING)])
        collection.create_index([("enabled", ASCENDING)])
        collection.create_index([("created_at", ASCENDING)])

    now = utc_now()
    default_texts = [reply["text"] for reply in DEFAULT_REPLIES]
    default_collection.update_many(
        {"source": "default", "text": {"$nin": default_texts}},
        {"$set": {"enabled": False, "updated_at": now}},
    )

    for text in default_texts:
        documents = list(default_collection.find({"text": text}).sort("created_at", ASCENDING))
        if len(documents) <= 1:
            continue
        duplicate_ids = [document["_id"] for document in documents[1:]]
        default_collection.delete_many({"_id": {"$in": duplicate_ids}})

    for reply in DEFAULT_REPLIES:
        existing = default_collection.find_one({"text": reply["text"]})
        if existing:
            default_collection.update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        "locked_text": True,
                        "source": "default",
                        "updated_at": now,
                    },
                    "$setOnInsert": {
                        "weight": DEFAULT_WEIGHT,
                        "multi_output": reply["multi_output"],
                        "enabled": True,
                    },
                },
            )
            continue

        default_collection.insert_one(
            {
                "text": reply["text"],
                "embedding": generate_embedding(reply["text"]),
                "weight": DEFAULT_WEIGHT,
                "multi_output": reply["multi_output"],
                "enabled": True,
                "locked_text": True,
                "source": "default",
                "created_at": now,
                "updated_at": now,
            }
        )


def list_collection(collection, source: str):
    enabled_filter = request.args.get("enabled")
    show_embedding = parse_bool(request.args.get("show_embedding"), default=False, field_name="show_embedding")
    limit = int(request.args.get("limit", 100))
    skip = int(request.args.get("skip", 0))

    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if skip < 0:
        raise ValueError("skip must be 0 or greater")

    query: dict[str, Any] = {}
    if enabled_filter is not None:
        query["enabled"] = parse_bool(enabled_filter, field_name="enabled")

    total = collection.count_documents(query)
    cursor = collection.find(query).sort("created_at", ASCENDING).skip(skip).limit(limit)
    return {
        "source": source,
        "total": total,
        "items": [serialize_document(document, show_embedding=show_embedding) for document in cursor],
    }


@app.route("/health", methods=["GET"])
def health():
    try:
        mongo_client.admin.command("ping")
        return success_response(
            {
                "database": DATABASE_NAME,
                "default_collection": DEFAULT_COLLECTION_NAME,
                "user_collection": USER_COLLECTION_NAME,
                "default_count": default_collection.count_documents({}),
                "user_count": user_collection.count_documents({}),
                "embedding_provider": EMBEDDING_PROVIDER,
                "embedding_model": EMBEDDING_MODEL,
            }
        )
    except Exception as exc:
        return error_response(str(exc), 500)


@app.route("/default_replies", methods=["GET"])
def list_default_replies():
    try:
        return success_response(list_collection(default_collection, "default"))
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/default_replies/search", methods=["GET", "POST"])
def search_default_replies():
    try:
        if request.method == "POST":
            data = get_json_body()
            text = data.get("text")
            exact = parse_bool(data.get("exact"), default=True, field_name="exact")
            show_embedding = parse_bool(data.get("show_embedding"), default=False, field_name="show_embedding")
        else:
            text = request.args.get("text")
            exact = parse_bool(request.args.get("exact"), default=True, field_name="exact")
            show_embedding = parse_bool(request.args.get("show_embedding"), default=False, field_name="show_embedding")

        documents = list(default_collection.find(build_text_query(text, exact=exact)).sort("created_at", ASCENDING))
        if not documents:
            return error_response("item not found", 404)
        return success_response(
            {
                "source": "default",
                "total": len(documents),
                "items": [serialize_document(document, show_embedding=show_embedding) for document in documents],
            }
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/default_replies/by_text", methods=["PATCH", "PUT"])
def update_default_reply_by_text():
    try:
        data = get_json_body()
        text = validate_text(data.get("text"))
        update_fields: dict[str, Any] = {}

        if "new_text" in data or "text_update" in data:
            raise ValueError("default reply text is locked and cannot be changed")
        if "weight" in data:
            update_fields["weight"] = validate_weight(data.get("weight"))
        if "multi_output" in data:
            update_fields["multi_output"] = parse_bool(data.get("multi_output"), field_name="multi_output")
        if "enabled" in data:
            update_fields["enabled"] = parse_bool(data.get("enabled"), field_name="enabled")
        if not update_fields:
            raise ValueError("no update fields provided")

        update_fields["updated_at"] = utc_now()
        result = default_collection.update_one({"text": text}, {"$set": update_fields})
        if result.matched_count == 0:
            return error_response("item not found", 404)
        return success_response({"item": serialize_document(default_collection.find_one({"text": text}))})
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input", methods=["POST"])
def create_user_input():
    try:
        data = get_json_body()
        text = validate_text(data.get("text"))
        if user_collection.find_one({"text": text}):
            return error_response("cannot have multiple identical user-defined replies", 409)

        now = utc_now()
        embedding, embedding_error = generate_embedding_for_storage(text)
        document = {
            "text": text,
            "embedding": embedding,
            "embedding_pending": not bool(embedding),
            "weight": validate_weight(data.get("weight", DEFAULT_WEIGHT)),
            "multi_output": parse_bool(data.get("multi_output"), default=False, field_name="multi_output"),
            "enabled": parse_bool(data.get("enabled"), default=True, field_name="enabled"),
            "locked_text": False,
            "source": "user",
            "created_at": now,
            "updated_at": now,
        }
        result = user_collection.insert_one(document)
        created = user_collection.find_one({"_id": result.inserted_id})
        return success_response(
            {
                "item": serialize_document(created, show_embedding=data.get("show_embedding", False)),
                "embedding_pending": not bool(embedding),
                "warning": embedding_error,
            },
            201,
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input", methods=["GET"])
def list_user_inputs():
    try:
        return success_response(list_collection(user_collection, "user"))
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/search", methods=["GET", "POST"])
def search_user_input_by_text():
    try:
        if request.method == "POST":
            data = get_json_body()
            text = data.get("text")
            exact = parse_bool(data.get("exact"), default=True, field_name="exact")
            show_embedding = parse_bool(data.get("show_embedding"), default=False, field_name="show_embedding")
        else:
            text = request.args.get("text")
            exact = parse_bool(request.args.get("exact"), default=True, field_name="exact")
            show_embedding = parse_bool(request.args.get("show_embedding"), default=False, field_name="show_embedding")

        documents = list(user_collection.find(build_text_query(text, exact=exact)).sort("created_at", ASCENDING))
        if not documents:
            return error_response("item not found", 404)
        return success_response(
            {
                "source": "user",
                "total": len(documents),
                "items": [serialize_document(document, show_embedding=show_embedding) for document in documents],
            }
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/<item_id>", methods=["GET"])
def get_user_input(item_id: str):
    try:
        show_embedding = parse_bool(request.args.get("show_embedding"), default=False, field_name="show_embedding")
        document = user_collection.find_one({"_id": parse_object_id(item_id)})
        if document is None:
            return error_response("item not found", 404)
        return success_response({"item": serialize_document(document, show_embedding=show_embedding)})
    except Exception as exc:
        return error_response(str(exc), 400)


def build_user_update_fields(data: dict[str, Any], current_id: ObjectId | None = None) -> dict[str, Any]:
    update_fields: dict[str, Any] = {}
    if "new_text" in data or "text_update" in data:
        new_text = validate_text(data.get("new_text") or data.get("text_update"))
        duplicate_query: dict[str, Any] = {"text": new_text}
        if current_id is not None:
            duplicate_query["_id"] = {"$ne": current_id}
        if user_collection.find_one(duplicate_query):
            raise ValueError("cannot have multiple identical user-defined replies")
        update_fields["text"] = new_text
        update_fields["embedding"] = generate_embedding(new_text)
    if "weight" in data:
        update_fields["weight"] = validate_weight(data.get("weight"))
    if "multi_output" in data:
        update_fields["multi_output"] = parse_bool(data.get("multi_output"), field_name="multi_output")
    if "enabled" in data:
        update_fields["enabled"] = parse_bool(data.get("enabled"), field_name="enabled")
    if not update_fields:
        raise ValueError("no update fields provided")
    update_fields["updated_at"] = utc_now()
    return update_fields


@app.route("/user_input/by_text", methods=["PATCH", "PUT"])
def update_user_input_by_text():
    try:
        data = get_json_body()
        original_text = validate_text(data.get("text"))
        existing = user_collection.find_one({"text": original_text})
        if existing is None:
            return error_response("item not found", 404)
        update_fields = build_user_update_fields(data, current_id=existing["_id"])
        user_collection.update_one({"_id": existing["_id"]}, {"$set": update_fields})
        updated = user_collection.find_one({"_id": existing["_id"]})
        return success_response({"item": serialize_document(updated, show_embedding=data.get("show_embedding", False))})
    except Exception as exc:
        status_code = 409 if "identical" in str(exc) else 400
        return error_response(str(exc), status_code)


@app.route("/user_input/<item_id>", methods=["PATCH", "PUT"])
def update_user_input(item_id: str):
    try:
        data = get_json_body()
        object_id = parse_object_id(item_id)
        if user_collection.find_one({"_id": object_id}) is None:
            return error_response("item not found", 404)
        update_fields = build_user_update_fields(data, current_id=object_id)
        user_collection.update_one({"_id": object_id}, {"$set": update_fields})
        updated = user_collection.find_one({"_id": object_id})
        return success_response({"item": serialize_document(updated, show_embedding=data.get("show_embedding", False))})
    except Exception as exc:
        status_code = 409 if "identical" in str(exc) else 400
        return error_response(str(exc), status_code)


@app.route("/user_input/by_text", methods=["DELETE"])
def delete_user_input_by_text():
    try:
        data = get_json_body()
        text = validate_text(data.get("text"))
        result = user_collection.delete_one({"text": text})
        if result.deleted_count == 0:
            return error_response("item not found", 404)
        return success_response({"deleted": True, "deleted_count": result.deleted_count})
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/<item_id>", methods=["DELETE"])
def delete_user_input(item_id: str):
    try:
        result = user_collection.delete_one({"_id": parse_object_id(item_id)})
        if result.deleted_count == 0:
            return error_response("item not found", 404)
        return success_response({"deleted": True, "id": item_id})
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/all_replies", methods=["GET"])
def list_all_replies():
    try:
        show_embedding = parse_bool(request.args.get("show_embedding"), default=False, field_name="show_embedding")
        default_items = [
            serialize_document(document, show_embedding=show_embedding)
            for document in default_collection.find({"enabled": True}).sort("created_at", ASCENDING)
        ]
        user_items = [
            serialize_document(document, show_embedding=show_embedding)
            for document in user_collection.find({"enabled": True}).sort("created_at", ASCENDING)
        ]
        return success_response(
            {
                "total": len(default_items) + len(user_items),
                "default_items": default_items,
                "user_items": user_items,
            }
        )
    except Exception as exc:
        return error_response(str(exc), 400)


if __name__ == "__main__":
    init_db()
    print("Flask reply database API started", flush=True)
    print(f"MongoDB: {DATABASE_NAME}", flush=True)
    print(f"Default collection: {DEFAULT_COLLECTION_NAME}", flush=True)
    print(f"User collection: {USER_COLLECTION_NAME}", flush=True)
    print(f"Embedding: {EMBEDDING_PROVIDER}/{EMBEDDING_MODEL}", flush=True)
    app.run(host="0.0.0.0", port=5001, debug=False)
