import os
import sys
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import ASCENDING, MongoClient, ReturnDocument


if sys.platform == "win32":
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


app = Flask(__name__)
CORS(app)


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_DB", "live_stream_db")
DEFAULT_COLLECTION_NAME = os.environ.get("MONGODB_DEFAULT_COLLECTION", "default_replies")
USER_COLLECTION_NAME = os.environ.get("MONGODB_USER_COLLECTION", "user_replies")
CONFIG_COLLECTION_NAME = os.environ.get("MONGODB_CONFIG_COLLECTION", "reply_config")
CROWD_DATABASE_NAME = os.environ.get("MONGODB_CROWD_DB", "live_stream_crowd_db")
CROWD_COLLECTION_NAME = os.environ.get("MONGODB_CROWD_COLLECTION", "crowd_slogans")
CROWD_CONFIG_COLLECTION_NAME = os.environ.get(
    "MONGODB_CROWD_CONFIG_COLLECTION", "crowd_config"
)

EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "ollama")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

DEFAULT_WEIGHT = 1.0
MIN_WEIGHT = 0.0
MAX_WEIGHT = 10.0

DEFAULT_REPLIES = [
    {"text": "有沒有優惠", "multi_output": False},
    {"text": "我來了", "multi_output": False},
    {"text": "這個好", "multi_output": False},
    {"text": "我喜歡", "multi_output": False},
    {"text": "有別色嗎", "multi_output": False},
    {"text": "尺寸怎麼選", "multi_output": False},
]
DEFAULT_CROWD_SLOGANS = [
    {"text": "+1", "meaning": "下單或表示想要", "response_mode": "same"},
    {"text": "6", "meaning": "主播指定的互動數字", "response_mode": "same"},
    {"text": "888", "meaning": "刷留言或凝聚人氣", "response_mode": "same"},
    {"text": "上車", "meaning": "確認購買或上車", "response_mode": "same"},
]


mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = mongo_client[DATABASE_NAME]
default_collection = db[DEFAULT_COLLECTION_NAME]
user_collection = db[USER_COLLECTION_NAME]
config_collection = db[CONFIG_COLLECTION_NAME]
crowd_db = mongo_client[CROWD_DATABASE_NAME]
crowd_collection = crowd_db[CROWD_COLLECTION_NAME]
crowd_config_collection = crowd_db[CROWD_CONFIG_COLLECTION_NAME]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def bump_candidate_revision(action: str, item_id: Any = None, text: str = "") -> int:
    """Notify live_stream_llm that its candidate cache must be refreshed."""

    document = config_collection.find_one_and_update(
        {"_id": "candidate_revision"},
        {
            "$inc": {"revision": 1},
            "$set": {
                "updated_at": utc_now(),
                "last_action": action,
                "last_item_id": str(item_id) if item_id is not None else None,
                "last_text": text,
            },
            "$setOnInsert": {"created_at": utc_now()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(document.get("revision", 0))


def get_candidate_revision() -> int:
    document = config_collection.find_one({"_id": "candidate_revision"})
    return int(document.get("revision", 0)) if document else 0


def bump_crowd_revision(action: str, item_id: Any = None, text: str = "") -> int:
    document = crowd_config_collection.find_one_and_update(
        {"_id": "crowd_revision"},
        {
            "$inc": {"revision": 1},
            "$set": {
                "updated_at": utc_now(),
                "last_action": action,
                "last_item_id": str(item_id) if item_id is not None else None,
                "last_text": text,
            },
            "$setOnInsert": {"created_at": utc_now()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(document.get("revision", 0))


def get_crowd_revision() -> int:
    document = crowd_config_collection.find_one({"_id": "crowd_revision"})
    return int(document.get("revision", 0)) if document else 0


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


def validate_meaning(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("meaning must be a string")
    return value.strip()[:100]


def validate_response_mode(value: Any) -> str:
    mode = str(value or "same").strip().lower()
    if mode not in {"same", "choice"}:
        raise ValueError("response_mode must be same or choice")
    return mode


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


def validate_account_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("account_ids must be an array")
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


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

    crowd_collection.create_index([("text", ASCENDING)], unique=True)
    crowd_collection.create_index([("enabled", ASCENDING)])
    crowd_collection.create_index([("created_at", ASCENDING)])

    now = utc_now()
    for document in list(default_collection.find({"candidate_type": "crowd_token"})):
        crowd_collection.update_one(
            {"text": document.get("text")},
            {
                "$set": {
                    "text": document.get("text"),
                    "enabled": document.get("enabled", True),
                    "meaning": document.get("meaning", ""),
                    "response_mode": document.get("response_mode", "same"),
                    "candidate_type": "crowd_slogan",
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        default_collection.delete_one({"_id": document["_id"]})
    default_slogan_texts = {item["text"] for item in DEFAULT_CROWD_SLOGANS}
    for document in list(user_collection.find({"candidate_type": "crowd_token"})):
        if document.get("text") in default_slogan_texts:
            crowd_collection.update_one(
                {"text": document.get("text")},
                {
                    "$set": {
                        "text": document.get("text"),
                        "enabled": document.get("enabled", True),
                        "meaning": document.get("meaning", ""),
                        "response_mode": document.get("response_mode", "same"),
                        "candidate_type": "crowd_slogan",
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            user_collection.delete_one({"_id": document["_id"]})
        else:
            user_collection.update_one(
                {"_id": document["_id"]},
                {
                    "$set": {
                        "candidate_type": "custom_phrase",
                        "multi_output": False,
                        "enabled": True,
                        "updated_at": now,
                    },
                    "$unset": {"meaning": "", "response_mode": ""},
                },
            )
    user_collection.update_many(
        {},
        {
            "$set": {
                "candidate_type": "custom_phrase",
                "multi_output": False,
                "updated_at": now,
            }
        },
    )
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
                        "candidate_type": "custom_phrase",
                        "multi_output": reply["multi_output"],
                        "weight": existing.get("weight", DEFAULT_WEIGHT),
                        "enabled": (
                            existing.get("enabled", True)
                            if existing.get("candidate_type") == "custom_phrase"
                            else True
                        ),
                        "updated_at": now,
                    },
                },
            )
            continue

        default_collection.insert_one(
            {
                "text": reply["text"],
                "embedding": [],
                "weight": DEFAULT_WEIGHT,
                "multi_output": reply["multi_output"],
                "enabled": True,
                "locked_text": True,
                "source": "default",
                "candidate_type": "custom_phrase",
                "created_at": now,
                "updated_at": now,
            }
        )

    for slogan in DEFAULT_CROWD_SLOGANS:
        crowd_collection.update_one(
            {"text": slogan["text"]},
            {
                "$set": {
                    **slogan,
                    "candidate_type": "crowd_slogan",
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now, "enabled": True},
            },
            upsert=True,
        )

    bump_candidate_revision("service_initialized")
    bump_crowd_revision("service_initialized")


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
                "config_collection": CONFIG_COLLECTION_NAME,
                "crowd_database": CROWD_DATABASE_NAME,
                "crowd_collection": CROWD_COLLECTION_NAME,
                "default_count": default_collection.count_documents({}),
                "user_count": user_collection.count_documents({}),
                "crowd_count": crowd_collection.count_documents({}),
                "candidate_revision": get_candidate_revision(),
                "crowd_revision": get_crowd_revision(),
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
            parse_bool(data.get("multi_output"), field_name="multi_output")
            update_fields["multi_output"] = True
        if "enabled" in data:
            update_fields["enabled"] = parse_bool(data.get("enabled"), field_name="enabled")
        if "meaning" in data:
            update_fields["meaning"] = validate_meaning(data.get("meaning"))
        if "response_mode" in data:
            update_fields["response_mode"] = validate_response_mode(data.get("response_mode"))
        if not update_fields:
            raise ValueError("no update fields provided")

        update_fields["updated_at"] = utc_now()
        result = default_collection.update_one({"text": text}, {"$set": update_fields})
        if result.matched_count == 0:
            return error_response("item not found", 404)
        revision = bump_candidate_revision("default_updated", text=text)
        return success_response(
            {
                "item": serialize_document(default_collection.find_one({"text": text})),
                "candidate_revision": revision,
            }
        )
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
        document = {
            "text": text,
            "embedding": [],
            "weight": validate_weight(data.get("weight", DEFAULT_WEIGHT)),
            "multi_output": parse_bool(
                data.get("multi_output"), default=False, field_name="multi_output"
            ),
            "enabled": parse_bool(data.get("enabled"), default=True, field_name="enabled"),
            "locked_text": False,
            "source": "user",
            "candidate_type": "custom_phrase",
            "account_ids": validate_account_ids(data.get("account_ids")),
            "created_at": now,
            "updated_at": now,
        }
        result = user_collection.insert_one(document)
        created = user_collection.find_one({"_id": result.inserted_id})
        revision = bump_candidate_revision(
            "user_created", item_id=result.inserted_id, text=text
        )
        return success_response(
            {
                "item": serialize_document(
                    created, show_embedding=data.get("show_embedding", False)
                ),
                "candidate_revision": revision,
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
    update_fields: dict[str, Any] = {
        "candidate_type": "custom_phrase",
    }
    if "new_text" in data or "text_update" in data:
        new_text = validate_text(data.get("new_text") or data.get("text_update"))
        duplicate_query: dict[str, Any] = {"text": new_text}
        if current_id is not None:
            duplicate_query["_id"] = {"$ne": current_id}
        if user_collection.find_one(duplicate_query):
            raise ValueError("cannot have multiple identical user-defined replies")
        update_fields["text"] = new_text
        update_fields["embedding"] = []
    if "weight" in data:
        update_fields["weight"] = validate_weight(data.get("weight"))
    if "multi_output" in data:
        update_fields["multi_output"] = parse_bool(
            data.get("multi_output"), field_name="multi_output"
        )
    if "enabled" in data:
        update_fields["enabled"] = parse_bool(data.get("enabled"), field_name="enabled")
    if "account_ids" in data:
        update_fields["account_ids"] = validate_account_ids(data.get("account_ids"))
    if len(update_fields) == 1:
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
        revision = bump_candidate_revision(
            "user_updated", item_id=existing["_id"], text=updated["text"]
        )
        return success_response(
            {
                "item": serialize_document(
                    updated, show_embedding=data.get("show_embedding", False)
                ),
                "candidate_revision": revision,
            }
        )
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
        revision = bump_candidate_revision(
            "user_updated", item_id=object_id, text=updated["text"]
        )
        return success_response(
            {
                "item": serialize_document(
                    updated, show_embedding=data.get("show_embedding", False)
                ),
                "candidate_revision": revision,
            }
        )
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
        revision = bump_candidate_revision("user_deleted", text=text)
        return success_response(
            {
                "deleted": True,
                "deleted_count": result.deleted_count,
                "candidate_revision": revision,
            }
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/user_input/<item_id>", methods=["DELETE"])
def delete_user_input(item_id: str):
    try:
        object_id = parse_object_id(item_id)
        existing = user_collection.find_one({"_id": object_id})
        result = user_collection.delete_one({"_id": object_id})
        if result.deleted_count == 0:
            return error_response("item not found", 404)
        revision = bump_candidate_revision(
            "user_deleted", item_id=item_id, text=(existing or {}).get("text", "")
        )
        return success_response(
            {"deleted": True, "id": item_id, "candidate_revision": revision}
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/crowd_slogans", methods=["GET", "POST"])
def crowd_slogans():
    try:
        if request.method == "GET":
            return success_response(list_collection(crowd_collection, "crowd_slogan"))
        data = get_json_body()
        text = validate_text(data.get("text"))
        if crowd_collection.find_one({"text": text}):
            return error_response("cannot have multiple identical crowd slogans", 409)
        now = utc_now()
        document = {
            "text": text,
            "enabled": parse_bool(data.get("enabled"), default=True, field_name="enabled"),
            "meaning": validate_meaning(data.get("meaning")),
            "response_mode": validate_response_mode(data.get("response_mode")),
            "candidate_type": "crowd_slogan",
            "created_at": now,
            "updated_at": now,
        }
        result = crowd_collection.insert_one(document)
        revision = bump_crowd_revision("crowd_created", result.inserted_id, text)
        return success_response(
            {
                "item": serialize_document(crowd_collection.find_one({"_id": result.inserted_id})),
                "crowd_revision": revision,
            },
            201,
        )
    except Exception as exc:
        return error_response(str(exc), 400)


def build_crowd_update_fields(data: dict[str, Any], current_id: ObjectId) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "new_text" in data or "text_update" in data:
        new_text = validate_text(data.get("new_text") or data.get("text_update"))
        if crowd_collection.find_one({"text": new_text, "_id": {"$ne": current_id}}):
            raise ValueError("cannot have multiple identical crowd slogans")
        fields["text"] = new_text
    if "enabled" in data:
        fields["enabled"] = parse_bool(data.get("enabled"), field_name="enabled")
    if "meaning" in data:
        fields["meaning"] = validate_meaning(data.get("meaning"))
    if "response_mode" in data:
        fields["response_mode"] = validate_response_mode(data.get("response_mode"))
    if not fields:
        raise ValueError("no update fields provided")
    fields["candidate_type"] = "crowd_slogan"
    fields["updated_at"] = utc_now()
    return fields


@app.route("/crowd_slogans/<item_id>", methods=["GET", "PATCH", "PUT", "DELETE"])
def crowd_slogan_item(item_id: str):
    try:
        object_id = parse_object_id(item_id)
        existing = crowd_collection.find_one({"_id": object_id})
        if existing is None:
            return error_response("item not found", 404)
        if request.method == "GET":
            return success_response({"item": serialize_document(existing)})
        if request.method == "DELETE":
            crowd_collection.delete_one({"_id": object_id})
            revision = bump_crowd_revision("crowd_deleted", object_id, existing.get("text", ""))
            return success_response({"deleted": True, "id": item_id, "crowd_revision": revision})
        fields = build_crowd_update_fields(get_json_body(), object_id)
        crowd_collection.update_one({"_id": object_id}, {"$set": fields})
        updated = crowd_collection.find_one({"_id": object_id})
        revision = bump_crowd_revision("crowd_updated", object_id, updated.get("text", ""))
        return success_response({"item": serialize_document(updated), "crowd_revision": revision})
    except Exception as exc:
        status_code = 409 if "identical" in str(exc) else 400
        return error_response(str(exc), status_code)


@app.route("/crowd_slogans/by_text", methods=["PATCH", "PUT", "DELETE"])
def crowd_slogan_by_text():
    try:
        data = get_json_body()
        text = validate_text(data.get("text"))
        existing = crowd_collection.find_one({"text": text})
        if existing is None:
            return error_response("item not found", 404)
        if request.method == "DELETE":
            crowd_collection.delete_one({"_id": existing["_id"]})
            return success_response({"deleted": True, "crowd_revision": bump_crowd_revision("crowd_deleted", text=text)})
        fields = build_crowd_update_fields(data, existing["_id"])
        crowd_collection.update_one({"_id": existing["_id"]}, {"$set": fields})
        updated = crowd_collection.find_one({"_id": existing["_id"]})
        revision = bump_crowd_revision("crowd_updated", existing["_id"], updated.get("text", ""))
        return success_response({"item": serialize_document(updated), "crowd_revision": revision})
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
        crowd_items = [
            serialize_document(document, show_embedding=False)
            for document in crowd_collection.find({"enabled": True}).sort(
                "created_at", ASCENDING
            )
        ]
        return success_response(
            {
                "total": len(default_items) + len(user_items) + len(crowd_items),
                "default_items": default_items,
                "user_items": user_items,
                "crowd_slogans": crowd_items,
            }
        )
    except Exception as exc:
        return error_response(str(exc), 400)


@app.route("/candidate_revision", methods=["GET"])
def candidate_revision():
    """Expose the revision currently observed by live_stream_llm."""

    try:
        document = config_collection.find_one({"_id": "candidate_revision"}) or {}
        return success_response(
            {
                "candidate_revision": int(document.get("revision", 0)),
                "crowd_revision": get_crowd_revision(),
                "updated_at": document.get("updated_at"),
                "last_action": document.get("last_action"),
                "last_item_id": document.get("last_item_id"),
                "last_text": document.get("last_text"),
            }
        )
    except Exception as exc:
        return error_response(str(exc), 500)


if __name__ == "__main__":
    init_db()
    print("Flask reply database API started", flush=True)
    print(f"MongoDB: {DATABASE_NAME}", flush=True)
    print(f"Default collection: {DEFAULT_COLLECTION_NAME}", flush=True)
    print(f"User collection: {USER_COLLECTION_NAME}", flush=True)
    print(f"Crowd MongoDB: {CROWD_DATABASE_NAME}/{CROWD_COLLECTION_NAME}", flush=True)
    print(f"Candidate revision: {get_candidate_revision()}", flush=True)
    print(f"Embedding: {EMBEDDING_PROVIDER}/{EMBEDDING_MODEL}", flush=True)
    app.run(host="0.0.0.0", port=5001, debug=False)
