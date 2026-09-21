import io
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import ASCENDING, MongoClient
from bson import ObjectId


if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


app = Flask(__name__)
CORS(app)


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_DB", "live_stream_db")
COLLECTION_NAME = os.environ.get("MONGODB_COLLECTION", "user_input")

EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "ollama")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

DEFAULT_WEIGHT = 1.0
MIN_WEIGHT = 0.0
MAX_WEIGHT = 10.0


mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
collection = mongo_client[DATABASE_NAME][COLLECTION_NAME]


def init_db() -> None:
    collection.create_index([("enabled", ASCENDING)])
    collection.create_index([("created_at", ASCENDING)])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def success_response(data: Any = None, status_code: int = 200):
    response = {"status": "success"}
    if data is not None:
        response.update(data)
    return jsonify(response), status_code


def error_response(message: str, status_code: int = 400):
    return jsonify({"status": "error", "error": message}), status_code


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


def parse_object_id(item_id: str) -> ObjectId:
    if not ObjectId.is_valid(item_id):
        raise ValueError("id is not a valid MongoDB ObjectId")
    return ObjectId(item_id)


def parse_enabled(value: Any, default: bool = True) -> bool:
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
    raise ValueError("enabled must be true or false")


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


def get_json_body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("request body must be JSON object")
    return data


@app.route("/health", methods=["GET"])
def health():
    try:
        mongo_client.admin.command("ping")
        return success_response(
            {
                "database": DATABASE_NAME,
                "collection": COLLECTION_NAME,
                "embedding_provider": EMBEDDING_PROVIDER,
                "embedding_model": EMBEDDING_MODEL,
            }
        )
    except Exception as exc:
        return error_response(str(exc), 500)


@app.route("/user_input", methods=["POST"])
def create_user_input():
    try:
        data = get_json_body()
        text = validate_text(data.get("text"))
        weight = validate_weight(data.get("weight", DEFAULT_WEIGHT))
        enabled = parse_enabled(data.get("enabled"), default=True)
        now = utc_now()

        document = {
            "text": text,
            "embedding": generate_embedding(text),
            "weight": weight,
            "enabled": enabled,
            "created_at": now,
            "updated_at": now,
        }

        result = collection.insert_one(document)
        created = collection.find_one({"_id": result.inserted_id})

        return success_response(
            {"item": serialize_document(created, show_embedding=data.get("show_embedding", False))},
            201,
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input", methods=["GET"])
def list_user_inputs():
    try:
        query: dict[str, Any] = {}

        if "enabled" in request.args:
            query["enabled"] = parse_enabled(request.args.get("enabled"))

        limit = int(request.args.get("limit", 100))
        skip = int(request.args.get("skip", 0))
        show_embedding = parse_enabled(request.args.get("show_embedding"), default=False)

        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if skip < 0:
            raise ValueError("skip must be 0 or greater")

        total = collection.count_documents(query)
        cursor = (
            collection.find(query)
            .sort("created_at", ASCENDING)
            .skip(skip)
            .limit(limit)
        )

        return success_response(
            {
                "total": total,
                "items": [
                    serialize_document(document, show_embedding=show_embedding)
                    for document in cursor
                ],
            }
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/search", methods=["GET", "POST"])
def search_user_input_by_text():
    try:
        if request.method == "POST":
            data = get_json_body()
            text = data.get("text")
            exact = parse_enabled(data.get("exact"), default=True)
            show_embedding = parse_enabled(data.get("show_embedding"), default=False)
        else:
            text = request.args.get("text")
            exact = parse_enabled(request.args.get("exact"), default=True)
            show_embedding = parse_enabled(request.args.get("show_embedding"), default=False)

        query = build_text_query(text, exact=exact)
        documents = list(collection.find(query).sort("created_at", ASCENDING))

        if not documents:
            return error_response("item not found", 404)

        return success_response(
            {
                "total": len(documents),
                "items": [
                    serialize_document(document, show_embedding=show_embedding)
                    for document in documents
                ],
            }
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/<item_id>", methods=["GET"])
def get_user_input(item_id: str):
    try:
        show_embedding = parse_enabled(request.args.get("show_embedding"), default=False)
        document = collection.find_one({"_id": parse_object_id(item_id)})

        if document is None:
            return error_response("item not found", 404)

        return success_response({"item": serialize_document(document, show_embedding=show_embedding)})
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/by_text", methods=["PATCH", "PUT"])
def update_user_input_by_text():
    try:
        data = get_json_body()
        original_text = validate_text(data.get("text"))
        exact = parse_enabled(data.get("exact"), default=True)
        update_fields: dict[str, Any] = {}

        if "new_text" in data:
            new_text = validate_text(data.get("new_text"))
            update_fields["text"] = new_text
            update_fields["embedding"] = generate_embedding(new_text)

        if "weight" in data:
            update_fields["weight"] = validate_weight(data.get("weight"))

        if "enabled" in data:
            update_fields["enabled"] = parse_enabled(data.get("enabled"))

        if not update_fields:
            raise ValueError("no update fields provided")

        update_fields["updated_at"] = utc_now()
        result = collection.update_many(build_text_query(original_text, exact=exact), {"$set": update_fields})

        if result.matched_count == 0:
            return error_response("item not found", 404)

        updated = list(collection.find(build_text_query(update_fields.get("text", original_text), exact=True)))
        return success_response(
            {
                "matched_count": result.matched_count,
                "modified_count": result.modified_count,
                "items": [
                    serialize_document(document, show_embedding=data.get("show_embedding", False))
                    for document in updated
                ],
            }
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/<item_id>", methods=["PATCH", "PUT"])
def update_user_input(item_id: str):
    try:
        data = get_json_body()
        update_fields: dict[str, Any] = {}

        if "text" in data:
            text = validate_text(data.get("text"))
            update_fields["text"] = text
            update_fields["embedding"] = generate_embedding(text)

        if "weight" in data:
            update_fields["weight"] = validate_weight(data.get("weight"))

        if "enabled" in data:
            update_fields["enabled"] = parse_enabled(data.get("enabled"))

        if not update_fields:
            raise ValueError("no update fields provided")

        object_id = parse_object_id(item_id)
        update_fields["updated_at"] = utc_now()

        result = collection.update_one({"_id": object_id}, {"$set": update_fields})
        if result.matched_count == 0:
            return error_response("item not found", 404)

        updated = collection.find_one({"_id": object_id})
        return success_response(
            {"item": serialize_document(updated, show_embedding=data.get("show_embedding", False))}
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/by_text", methods=["DELETE"])
def delete_user_input_by_text():
    try:
        data = get_json_body()
        text = validate_text(data.get("text"))
        exact = parse_enabled(data.get("exact"), default=True)
        result = collection.delete_many(build_text_query(text, exact=exact))

        if result.deleted_count == 0:
            return error_response("item not found", 404)

        return success_response({"deleted": True, "deleted_count": result.deleted_count})
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/<item_id>", methods=["DELETE"])
def delete_user_input(item_id: str):
    try:
        result = collection.delete_one({"_id": parse_object_id(item_id)})

        if result.deleted_count == 0:
            return error_response("item not found", 404)

        return success_response({"deleted": True, "id": item_id})
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/<item_id>/disable", methods=["POST", "PATCH"])
def disable_user_input(item_id: str):
    try:
        object_id = parse_object_id(item_id)
        result = collection.update_one(
            {"_id": object_id},
            {"$set": {"enabled": False, "updated_at": utc_now()}},
        )

        if result.matched_count == 0:
            return error_response("item not found", 404)

        document = collection.find_one({"_id": object_id})
        return success_response({"item": serialize_document(document)})
    except Exception as exc:
        return error_response(str(exc), 400)


if __name__ == "__main__":
    init_db()
    print("Flask user_input CRUD API started", flush=True)
    print(f"MongoDB: {DATABASE_NAME}.{COLLECTION_NAME}", flush=True)
    print(f"Embedding: {EMBEDDING_PROVIDER}/{EMBEDDING_MODEL}", flush=True)
    app.run(host="0.0.0.0", port=5001, debug=False)
