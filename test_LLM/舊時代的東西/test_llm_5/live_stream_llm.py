import io
import json
import math
import os
import sys
import time
from datetime import datetime
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient

import ollama


if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


app = Flask(__name__)
CORS(app)


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_DB", "live_stream_db")
COLLECTION_NAME = os.environ.get("MONGODB_COLLECTION", "user_input")

TEXT_FILE_NAME = os.environ.get("LLM_TEXT_JSONL", "Text.jsonl")
REPLY_FILE_NAME = os.environ.get("LLM_REPLY_JSONL", "Reply.jsonl")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.65"))

LAST_FILE_POSITION = 0
LAST_REPLY_RESULT: dict[str, Any] = {}
CACHED_REPLIES: list[dict[str, Any]] = []

mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
collection = mongo_client[DATABASE_NAME][COLLECTION_NAME]


def get_current_time() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def resolve_path(file_name: str) -> str:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return file_name if os.path.isabs(file_name) else os.path.join(current_dir, file_name)


def get_text_file_path() -> str:
    return resolve_path(TEXT_FILE_NAME)


def get_reply_file_path() -> str:
    return resolve_path(REPLY_FILE_NAME)


def load_replies_from_mongodb() -> list[dict[str, Any]]:
    replies: list[dict[str, Any]] = []

    for document in collection.find({"enabled": True}):
        embedding = document.get("embedding")
        text = document.get("text")

        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(embedding, list) or not embedding:
            continue

        replies.append(
            {
                "id": str(document["_id"]),
                "text": text,
                "embedding": [float(value) for value in embedding],
                "weight": float(document.get("weight", 1.0)),
            }
        )

    return replies


def refresh_reply_cache() -> int:
    global CACHED_REPLIES
    CACHED_REPLIES = load_replies_from_mongodb()
    return len(CACHED_REPLIES)


def get_embedding(text: str) -> list[float]:
    try:
        response = ollama.embeddings(model=EMBEDDING_MODEL, prompt=text)
        return response["embedding"]
    except Exception:
        response = ollama.embed(model=EMBEDDING_MODEL, input=text)
        return response["embeddings"][0]


def cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    if not vector_a or not vector_b or len(vector_a) != len(vector_b):
        return 0.0

    dot = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)


def choose_reply(streamer_text: str) -> dict[str, Any]:
    start_time = time.perf_counter()
    streamer_embedding = get_embedding(streamer_text)
    candidates: list[dict[str, Any]] = []
    best_similarity = 0.0

    for reply in CACHED_REPLIES:
        similarity = cosine_similarity(streamer_embedding, reply["embedding"])
        best_similarity = max(best_similarity, similarity)

        if similarity < SIMILARITY_THRESHOLD:
            continue

        score = similarity * reply["weight"]
        candidates.append(
            {
                "id": reply["id"],
                "text": reply["text"],
                "similarity": similarity,
                "weight": reply["weight"],
                "score": score,
            }
        )

    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = candidates[0] if candidates else None
    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    return {
        "timestamp": get_current_time(),
        "input": streamer_text,
        "reply": selected["text"] if selected else "ignore",
        "has_reply": selected is not None,
        "elapsed_ms": elapsed_ms,
        "threshold": SIMILARITY_THRESHOLD,
        "best_similarity": selected["similarity"] if selected else best_similarity,
        "selected": selected,
        "candidate_count": len(candidates),
    }


def read_new_raw_texts(full_path: str, from_start: bool = False) -> list[str]:
    global LAST_FILE_POSITION

    with open(full_path, "r", encoding="utf-8") as file:
        if from_start:
            LAST_FILE_POSITION = 0

        file.seek(LAST_FILE_POSITION)
        new_lines = file.readlines()
        LAST_FILE_POSITION = file.tell()

    valid_inputs: list[str] = []
    for line in new_lines:
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        raw_text = data.get("raw_text")
        if isinstance(raw_text, str) and raw_text.strip():
            valid_inputs.append(raw_text.strip())

    return valid_inputs


def append_reply_log(result: dict[str, Any]) -> None:
    log_data = {
        "timestamp": result["timestamp"],
        "raw_text": result["input"],
        "reply": result["reply"],
        "elapsed_ms": result["elapsed_ms"],
        "threshold": result["threshold"],
        "best_similarity": result["best_similarity"],
        "selected": result["selected"],
    }

    with open(get_reply_file_path(), "a", encoding="utf-8") as file:
        file.write(json.dumps(log_data, ensure_ascii=False) + "\n")


@app.route("/process", methods=["POST"])
def process():
    global LAST_REPLY_RESULT

    full_path = get_text_file_path()
    from_start = request.args.get("from_start", "").lower() in {"true", "1", "yes", "y"}

    if not os.path.exists(full_path):
        return jsonify({"status": "error", "error": f"File {TEXT_FILE_NAME} not found"}), 404

    processed_logs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    try:
        valid_inputs = read_new_raw_texts(full_path, from_start=from_start)

        for index, streamer_text in enumerate(valid_inputs, start=1):
            try:
                result = choose_reply(streamer_text)
                append_reply_log(result)
                LAST_REPLY_RESULT = result
                processed_logs.append(result)

                print(
                    f"{result['timestamp']} ({index}/{len(valid_inputs)}) "
                    f"reply={result['reply']} elapsed_ms={result['elapsed_ms']}",
                    flush=True,
                )
            except Exception as exc:
                errors.append({"input": streamer_text, "error": str(exc)})
                print(f"{get_current_time()} ERROR input={streamer_text} error={exc}", flush=True)

        return jsonify(
            {
                "status": "success" if not errors else "partial_success",
                "processed_count": len(processed_logs),
                "error_count": len(errors),
                "text_file": full_path,
                "reply_file": get_reply_file_path(),
                "last_file_position": LAST_FILE_POSITION,
                "cached_reply_count": len(CACHED_REPLIES),
                "results": processed_logs,
                "errors": errors,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/latest_reply", methods=["GET", "POST"])
def latest_reply():
    result = process()
    status_code = 200

    if isinstance(result, tuple):
        result, status_code = result

    data = result.get_json(silent=True) or {}
    results = data.get("results") or []
    latest = results[-1] if results else LAST_REPLY_RESULT

    return jsonify(
        {
            "status": data.get("status", "success" if status_code == 200 else "error"),
            "has_reply": bool(latest.get("has_reply")),
            "reply": latest.get("reply", ""),
            "input": latest.get("input", ""),
            "elapsed_ms": latest.get("elapsed_ms"),
            "best_similarity": latest.get("best_similarity"),
            "processed_count": data.get("processed_count", 0),
            "error_count": data.get("error_count", 0),
            "text_file": data.get("text_file", get_text_file_path()),
            "reply_file": data.get("reply_file", get_reply_file_path()),
            "last_file_position": data.get("last_file_position", LAST_FILE_POSITION),
            "cached_reply_count": len(CACHED_REPLIES),
            "results": results,
            "errors": data.get("errors", []),
        }
    ), status_code


@app.route("/reload_replies", methods=["POST"])
def reload_replies():
    try:
        count = refresh_reply_cache()
        return jsonify({"status": "success", "cached_reply_count": count})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/user_reply", methods=["GET", "POST", "DELETE"])
def user_reply():
    if request.method == "GET":
        try:
            items = []
            for document in collection.find({}, {"embedding": 0}).sort("text", 1):
                items.append(
                    {
                        "id": str(document["_id"]),
                        "text": document.get("text", ""),
                        "weight": float(document.get("weight", 1.0)),
                        "enabled": bool(document.get("enabled", True)),
                    }
                )
            return jsonify({"status": "success", "items": items, "count": len(items)})
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 500

    if request.method == "DELETE":
        try:
            data = request.get_json(silent=True) or {}
            text = str(data.get("text", "")).strip()

            if not text:
                return jsonify({"status": "error", "error": "text is required"}), 400

            result = collection.delete_one({"text": text})
            cached_count = refresh_reply_cache()
            return jsonify(
                {
                    "status": "success",
                    "deleted_count": result.deleted_count,
                    "text": text,
                    "cached_reply_count": cached_count,
                }
            )
        except Exception as exc:
            return jsonify({"status": "error", "error": str(exc)}), 500

    try:
        data = request.get_json(silent=True) or {}
        text = str(data.get("text", "")).strip()
        weight = float(data.get("weight", 1.0))
        enabled = bool(data.get("enabled", True))

        if not text:
            return jsonify({"status": "error", "error": "text is required"}), 400

        embedding = get_embedding(text)
        result = collection.update_one(
            {"text": text},
            {
                "$set": {
                    "text": text,
                    "embedding": embedding,
                    "weight": weight,
                    "enabled": enabled,
                    "updated_at": datetime.now(),
                },
                "$setOnInsert": {"created_at": datetime.now()},
            },
            upsert=True,
        )
        cached_count = refresh_reply_cache()

        return jsonify(
            {
                "status": "success",
                "action": "inserted" if result.upserted_id else "updated",
                "text": text,
                "cached_reply_count": cached_count,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/health", methods=["GET"])
def health():
    try:
        text_path = get_text_file_path()
        reply_path = get_reply_file_path()
        text_exists = os.path.exists(text_path)
        reply_exists = os.path.exists(reply_path)
        mongo_client.admin.command("ping")

        return jsonify(
            {
                "status": "ok",
                "database": DATABASE_NAME,
                "collection": COLLECTION_NAME,
                "text_file": text_path,
                "text_file_exists": text_exists,
                "text_file_size": os.path.getsize(text_path) if text_exists else 0,
                "reply_file": reply_path,
                "reply_file_exists": reply_exists,
                "reply_file_size": os.path.getsize(reply_path) if reply_exists else 0,
                "last_file_position": LAST_FILE_POSITION,
                "cached_reply_count": len(CACHED_REPLIES),
                "embedding_model": EMBEDDING_MODEL,
                "similarity_threshold": SIMILARITY_THRESHOLD,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("LLM_PORT", "5000"))
    cached_count = refresh_reply_cache()
    print("Flask live_stream_llm API started", flush=True)
    print(f"MongoDB: {DATABASE_NAME}.{COLLECTION_NAME}", flush=True)
    print(f"Cached replies: {cached_count}", flush=True)
    print(f"Text file: {get_text_file_path()}", flush=True)
    print(f"Reply file: {get_reply_file_path()}", flush=True)
    print(f"Embedding model: {EMBEDDING_MODEL}", flush=True)
    print(f"Similarity threshold: {SIMILARITY_THRESHOLD}", flush=True)
    print(f"Port: {port}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False)
