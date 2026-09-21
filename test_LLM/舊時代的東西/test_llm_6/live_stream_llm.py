import io
import json
import math
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient

import ollama
from reply_generator import CHAT_MODEL, MAX_REPLY_CHARS, generate_live_reply
from reply_policy import evaluate_reply_policy


if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


app = Flask(__name__)
CORS(app)


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.environ.get("MONGODB_DB", "live_stream_db")
DEFAULT_COLLECTION_NAME = os.environ.get("MONGODB_DEFAULT_COLLECTION", "default_replies")
USER_COLLECTION_NAME = os.environ.get("MONGODB_USER_COLLECTION", "user_replies")

INPUT_DIR_NAME = os.environ.get("LLM_INPUT_DIR", "inputs")
REPLY_DIR_NAME = os.environ.get("LLM_REPLY_DIR", "replies")
INPUT_PATTERN = os.environ.get("LLM_INPUT_PATTERN", "*.jsonl")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.75"))
DIRECT_REPLY_THRESHOLD = float(os.environ.get("DIRECT_REPLY_THRESHOLD", "0.88"))
REPLY_MODE = os.environ.get("REPLY_MODE", "hybrid").strip().lower()
GENERATION_COOLDOWN_SECONDS = float(os.environ.get("GENERATION_COOLDOWN_SECONDS", "60"))
MULTI_OUTPUT_REPEAT_PROBABILITY = float(os.environ.get("MULTI_OUTPUT_REPEAT_PROBABILITY", "0.35"))

FILE_POSITIONS: dict[str, int] = {}
LAST_REPLY_RESULTS: dict[str, dict[str, Any]] = {}
CACHED_REPLIES: list[dict[str, Any]] = []
LAST_GENERATION_AT: dict[str, float] = {}

mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = mongo_client[DATABASE_NAME]
default_collection = db[DEFAULT_COLLECTION_NAME]
user_collection = db[USER_COLLECTION_NAME]


def get_current_time() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def resolve_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def get_input_dir() -> Path:
    return resolve_path(INPUT_DIR_NAME)


def get_reply_dir() -> Path:
    return resolve_path(REPLY_DIR_NAME)


def get_stream_id_from_file(path: Path) -> str:
    return path.stem


def normalize_stream_name(value: Any) -> str | None:
    if value is None:
        return None
    stream_name = str(value).strip()
    if not stream_name:
        return None
    return Path(stream_name).stem


def get_reply_file_path_for_stream(stream_id: str) -> Path:
    return get_reply_dir() / f"{stream_id}_reply.jsonl"


def get_reply_file_path(input_file: Path) -> Path:
    return get_reply_file_path_for_stream(get_stream_id_from_file(input_file))


def discover_input_files(stream_id: str | None = None) -> list[Path]:
    input_dir = get_input_dir()
    if not input_dir.exists():
        return []
    files = sorted(path for path in input_dir.glob(INPUT_PATTERN) if path.is_file())
    if stream_id:
        files = [path for path in files if get_stream_id_from_file(path) == stream_id]
    return files


def get_requested_stream_name() -> str | None:
    data = get_request_json_silent()
    return normalize_stream_name(
        data.get("file_name")
        or data.get("stream_file")
        or data.get("stream_id")
        or request.args.get("file_name")
        or request.args.get("stream_file")
        or request.args.get("stream_id")
    )


def get_requested_file_path() -> Path | None:
    data = get_request_json_silent()
    raw_path = (
        data.get("file_path")
        or data.get("jsonl_path")
        or request.args.get("file_path")
        or request.args.get("jsonl_path")
    )
    if not raw_path:
        return None

    path = Path(str(raw_path).strip())
    if not path.suffix:
        path = path.with_suffix(".jsonl")
    if path.is_absolute():
        return path
    return get_input_dir() / path


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


def serialize_reply(source: str, document: dict[str, Any]) -> dict[str, Any] | None:
    embedding = document.get("embedding")
    text = document.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(embedding, list) or not embedding:
        return None
    if not document.get("enabled", True):
        return None
    return {
        "key": f"{source}:{document['_id']}",
        "source": source,
        "id": str(document["_id"]),
        "text": text,
        "embedding": [float(value) for value in embedding],
        "weight": float(document.get("weight", 1.0)),
        "multi_output": bool(document.get("multi_output", False)),
    }


def load_replies_from_mongodb() -> list[dict[str, Any]]:
    replies: list[dict[str, Any]] = []
    for source, collection in (("default", default_collection), ("user", user_collection)):
        for document in collection.find({"enabled": True}):
            reply = serialize_reply(source, document)
            if reply:
                replies.append(reply)
    return replies


def refresh_cache() -> dict[str, int]:
    global CACHED_REPLIES
    CACHED_REPLIES = load_replies_from_mongodb()
    return {"cached_reply_count": len(CACHED_REPLIES)}


def parse_account_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        raise ValueError("account_ids must be a list or comma-separated string")
    return [item for item in items if item]


def get_request_json_silent() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def get_request_account_ids() -> list[str]:
    data = get_request_json_silent()
    if "account_ids" in data:
        return parse_account_ids(data.get("account_ids"))
    if request.args.get("account_ids"):
        return parse_account_ids(request.args.get("account_ids"))
    return []


def build_candidates(raw_embedding: list[float]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for reply in CACHED_REPLIES:
        similarity = cosine_similarity(raw_embedding, reply["embedding"])
        if similarity < SIMILARITY_THRESHOLD:
            continue
        candidates.append(
            {
                "key": reply["key"],
                "source": reply["source"],
                "id": reply["id"],
                "text": reply["text"],
                "similarity": similarity,
                "weight": reply["weight"],
                "score": similarity * reply["weight"],
                "multi_output": reply["multi_output"],
            }
        )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def choose_for_accounts(raw_text: str, account_ids: list[str], stream_id: str = "direct",
                        context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not account_ids:
        raise ValueError("account_ids is required")

    start_time = time.perf_counter()
    raw_embedding = get_embedding(raw_text)
    candidates = build_candidates(raw_embedding)
    context = context or {}
    policy = evaluate_reply_policy(raw_text, intent=context.get("intent"),
                                   secondary_intents=context.get("secondary_intents"),
                                   entities=context.get("entities"))
    best_similarity = max((item["similarity"] for item in candidates), default=0.0)
    now = time.monotonic()
    last_generated = LAST_GENERATION_AT.get(stream_id)
    cooldown_remaining = max(0.0, GENERATION_COOLDOWN_SECONDS - (now - last_generated)) \
        if last_generated is not None else 0.0
    generation: dict[str, Any] | None = None
    generated_candidate: dict[str, Any] | None = None
    policy_allows_reply = policy["action"] == "reply" or (
        policy["action"] == "uncertain" and REPLY_MODE == "generate"
    )
    should_try_generation = (
        policy_allows_reply and REPLY_MODE in {"hybrid", "generate"}
        and cooldown_remaining <= 0
        and (REPLY_MODE == "generate" or best_similarity < DIRECT_REPLY_THRESHOLD)
    )
    if should_try_generation:
        try:
            generation_started = time.perf_counter()
            generation = generate_live_reply(raw_text, [item["text"] for item in candidates],
                                             policy["category"], context.get("entities"))
            generation["elapsed_ms"] = round((time.perf_counter() - generation_started) * 1000, 2)
            if generation.get("valid") and generation.get("reply"):
                LAST_GENERATION_AT[stream_id] = time.monotonic()
                generated_candidate = {
                    "key": f"generated:{stream_id}:{time.time_ns()}", "source": "qwen_generated",
                    "id": None, "text": generation["reply"], "similarity": None,
                    "weight": 1.0, "score": None, "multi_output": False,
                }
        except Exception as exc:
            generation = {"valid": False, "validation_reason": "generation_error",
                          "error": str(exc), "model": CHAT_MODEL}

    usable_candidates = candidates if policy_allows_reply else []
    if generated_candidate is not None:
        usable_candidates = [generated_candidate, *usable_candidates]
    used_reply_keys: set[str] = set()
    account_results: list[dict[str, Any]] = []

    for account_id in account_ids:
        selected = None
        skipped_by_repeat_probability = []
        for candidate in usable_candidates:
            already_used = candidate["key"] in used_reply_keys
            if not already_used:
                selected = candidate
                break
            if not candidate["multi_output"]:
                continue
            if random.random() <= MULTI_OUTPUT_REPEAT_PROBABILITY:
                selected = candidate
                break
            skipped_by_repeat_probability.append(candidate["text"])

        if selected:
            used_reply_keys.add(selected["key"])

        account_results.append(
            {
                "account_id": account_id,
                "reply": selected["text"] if selected else "ignore",
                "has_reply": selected is not None,
                "selected": selected,
                "candidate_count": len(usable_candidates),
                "skipped_by_repeat_probability": skipped_by_repeat_probability,
            }
        )

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return {
        "has_reply": any(item["has_reply"] for item in account_results),
        "reply": account_results[0]["reply"] if account_results else "ignore",
        "account_results": account_results,
        "elapsed_ms": elapsed_ms,
        "threshold": SIMILARITY_THRESHOLD,
        "multi_output_repeat_probability": MULTI_OUTPUT_REPEAT_PROBABILITY,
        "best_similarity": best_similarity,
        "candidate_count": len(usable_candidates),
        "account_count": len(account_ids),
        "policy": policy,
        "reply_mode": REPLY_MODE,
        "generation": generation,
        "generation_cooldown_seconds": GENERATION_COOLDOWN_SECONDS,
        "generation_cooldown_remaining": round(cooldown_remaining, 2),
    }


def read_new_entries(input_file: Path, request_account_ids: list[str], from_start: bool = False) -> list[dict[str, Any]]:
    file_key = str(input_file.resolve())
    with input_file.open("r", encoding="utf-8") as file:
        if from_start:
            FILE_POSITIONS[file_key] = 0
        file.seek(FILE_POSITIONS.get(file_key, 0))
        new_lines = file.readlines()
        FILE_POSITIONS[file_key] = file.tell()

    entries: list[dict[str, Any]] = []
    file_stream_id = get_stream_id_from_file(input_file)
    for line_number, line in enumerate(new_lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_text = data.get("resolved_text") or data.get("raw_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        entry_account_ids = request_account_ids or parse_account_ids(data.get("account_ids") or data.get("account_id"))
        entries.append(
            {
                "raw_text": raw_text.strip(),
                "stream_id": data.get("stream_id") or file_stream_id,
                "viewer_id": data.get("viewer_id"),
                "account_ids": entry_account_ids,
                "source_file": input_file.name,
                "source_path": str(input_file),
                "source_stream_id": file_stream_id,
                "line_number": line_number,
                "source_data": data,
            }
        )
    return entries


def append_reply_log_for_stream(stream_id: str, result: dict[str, Any]) -> None:
    reply_dir = get_reply_dir()
    reply_dir.mkdir(parents=True, exist_ok=True)
    reply_file = get_reply_file_path_for_stream(stream_id)
    with reply_file.open("a", encoding="utf-8") as file:
        file.write(json.dumps(result, ensure_ascii=False) + "\n")


def process_raw_text(raw_text: str, stream_id: str, account_ids: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    extra = extra or {}
    chosen = choose_for_accounts(raw_text, account_ids, stream_id=stream_id, context=extra)
    result = {
        "timestamp": get_current_time(),
        "stream_id": stream_id,
        "raw_text": raw_text,
        "account_ids": account_ids,
        "reply": chosen["reply"],
        "has_reply": chosen["has_reply"],
        "account_results": chosen["account_results"],
        "elapsed_ms": chosen["elapsed_ms"],
        "threshold": chosen["threshold"],
        "multi_output_repeat_probability": chosen["multi_output_repeat_probability"],
        "best_similarity": chosen["best_similarity"],
        "candidate_count": chosen["candidate_count"],
        "account_count": chosen["account_count"],
        "policy": chosen["policy"],
        "reply_mode": chosen["reply_mode"],
        "generation": chosen["generation"],
        "generation_cooldown_seconds": chosen["generation_cooldown_seconds"],
        "generation_cooldown_remaining": chosen["generation_cooldown_remaining"],
        "reply_file": str(get_reply_file_path_for_stream(stream_id)),
    }
    result.update(extra)
    append_reply_log_for_stream(stream_id, result)
    LAST_REPLY_RESULTS[stream_id] = result
    return result


@app.route("/match", methods=["POST"])
def match():
    try:
        data = get_request_json_silent()
        raw_text = str(data.get("raw_text") or "").strip()
        if not raw_text:
            return jsonify({"status": "error", "error": "raw_text is required"}), 400
        stream_id = get_requested_stream_name() or "direct"
        account_ids = parse_account_ids(data.get("account_ids") or request.args.get("account_ids"))
        result = process_raw_text(raw_text, stream_id, account_ids, {
            "source": "api",
            "intent": data.get("intent"),
            "secondary_intents": data.get("secondary_intents", []),
            "entities": data.get("entities", {}),
        })
        return jsonify({"status": "success", "result": result})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400


@app.route("/process", methods=["POST"])
def process():
    requested_file_path = get_requested_file_path()
    stream_id = get_requested_stream_name()
    from_start = request.args.get("from_start", "").lower() in {"true", "1", "yes", "y"}
    request_account_ids = get_request_account_ids()
    if requested_file_path is not None:
        input_files = [requested_file_path] if requested_file_path.exists() else []
        stream_id = get_stream_id_from_file(requested_file_path)
    else:
        input_files = discover_input_files(stream_id=stream_id)

    if stream_id and not input_files:
        expected_file = str(requested_file_path or (get_input_dir() / f"{stream_id}.jsonl"))
        return jsonify(
            {
                "status": "error",
                "error": "input jsonl file not found",
                "stream_id": stream_id,
                "expected_file": expected_file,
                "input_dir": str(get_input_dir()),
                "hint": "Put the file under input_dir, set LLM_INPUT_DIR before starting live_stream_llm.py, or pass file_path/jsonl_path.",
            }
        ), 404

    processed_logs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scanned_files: list[dict[str, Any]] = []

    try:
        for input_file in input_files:
            file_key = str(input_file.resolve())
            entries = read_new_entries(input_file, request_account_ids, from_start=from_start)
            scanned_files.append(
                {
                    "stream_id": get_stream_id_from_file(input_file),
                    "file": str(input_file),
                    "reply_file": str(get_reply_file_path(input_file)),
                    "last_file_position": FILE_POSITIONS.get(file_key, 0),
                    "new_entry_count": len(entries),
                }
            )
            for entry in entries:
                try:
                    result = process_raw_text(
                        entry["raw_text"],
                        entry["stream_id"],
                        entry["account_ids"],
                        {
                            "source": "jsonl",
                            "viewer_id": entry["viewer_id"],
                            "source_file": entry["source_file"],
                            "source_path": entry["source_path"],
                            "source_stream_id": entry["source_stream_id"],
                            "intent": entry["source_data"].get("intent"),
                            "secondary_intents": entry["source_data"].get("secondary_intents", []),
                            "entities": entry["source_data"].get("entities", {}),
                            "original_raw_text": entry["source_data"].get("raw_text"),
                        },
                    )
                    processed_logs.append(result)
                    print(
                        f"{result['timestamp']} stream={result['stream_id']} "
                        f"accounts={result['account_count']} elapsed_ms={result['elapsed_ms']}",
                        flush=True,
                    )
                except Exception as exc:
                    errors.append(
                        {
                            "stream_id": entry["stream_id"],
                            "source_file": entry["source_file"],
                            "raw_text": entry["raw_text"],
                            "error": str(exc),
                        }
                    )
        return jsonify(
            {
                "status": "success" if not errors else "partial_success",
                "processed_count": len(processed_logs),
                "error_count": len(errors),
                "input_dir": str(get_input_dir()),
                "reply_dir": str(get_reply_dir()),
                "input_pattern": INPUT_PATTERN,
                "scanned_files": scanned_files,
                "cached_reply_count": len(CACHED_REPLIES),
                "results": processed_logs,
                "errors": errors,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/latest_reply", methods=["GET", "POST"])
def latest_reply():
    stream_id = get_requested_stream_name()
    result = process()
    status_code = 200
    if isinstance(result, tuple):
        result, status_code = result
    data = result.get_json(silent=True) or {}
    results = data.get("results") or []
    if stream_id:
        latest = next((item for item in reversed(results) if item.get("stream_id") == stream_id), {})
        latest = latest or LAST_REPLY_RESULTS.get(stream_id, {})
    else:
        latest = results[-1] if results else next(reversed(LAST_REPLY_RESULTS.values()), {})
    return jsonify(
        {
            "status": data.get("status", "success" if status_code == 200 else "error"),
            "stream_id": latest.get("stream_id", stream_id),
            "has_reply": bool(latest.get("has_reply")),
            "reply": latest.get("reply", ""),
            "raw_text": latest.get("raw_text", ""),
            "account_results": latest.get("account_results", []),
            "elapsed_ms": latest.get("elapsed_ms"),
            "best_similarity": latest.get("best_similarity"),
            "processed_count": data.get("processed_count", 0),
            "error_count": data.get("error_count", 0),
            "cached_reply_count": len(CACHED_REPLIES),
            "results": results,
            "errors": data.get("errors", []),
        }
    ), status_code


@app.route("/reload_replies", methods=["POST"])
def reload_replies():
    try:
        counts = refresh_cache()
        return jsonify({"status": "success", **counts})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/streams", methods=["GET"])
def list_streams():
    files = discover_input_files()
    streams = []
    for input_file in files:
        file_key = str(input_file.resolve())
        reply_file = get_reply_file_path(input_file)
        streams.append(
            {
                "stream_id": get_stream_id_from_file(input_file),
                "input_file": str(input_file),
                "input_size": input_file.stat().st_size,
                "last_file_position": FILE_POSITIONS.get(file_key, 0),
                "reply_file": str(reply_file),
                "reply_exists": reply_file.exists(),
                "reply_size": reply_file.stat().st_size if reply_file.exists() else 0,
            }
        )
    return jsonify(
        {
            "status": "success",
            "input_dir": str(get_input_dir()),
            "reply_dir": str(get_reply_dir()),
            "input_pattern": INPUT_PATTERN,
            "stream_count": len(streams),
            "streams": streams,
        }
    )


@app.route("/health", methods=["GET"])
def health():
    try:
        input_dir = get_input_dir()
        reply_dir = get_reply_dir()
        input_files = discover_input_files()
        mongo_client.admin.command("ping")
        return jsonify(
            {
                "status": "ok",
                "database": DATABASE_NAME,
                "default_collection": DEFAULT_COLLECTION_NAME,
                "user_collection": USER_COLLECTION_NAME,
                "input_dir": str(input_dir),
                "input_dir_exists": input_dir.exists(),
                "reply_dir": str(reply_dir),
                "reply_dir_exists": reply_dir.exists(),
                "input_pattern": INPUT_PATTERN,
                "stream_count": len(input_files),
                "cached_reply_count": len(CACHED_REPLIES),
                "embedding_model": EMBEDDING_MODEL,
                "similarity_threshold": SIMILARITY_THRESHOLD,
                "direct_reply_threshold": DIRECT_REPLY_THRESHOLD,
                "reply_mode": REPLY_MODE,
                "chat_model": CHAT_MODEL,
                "max_reply_chars": MAX_REPLY_CHARS,
                "generation_cooldown_seconds": GENERATION_COOLDOWN_SECONDS,
                "multi_output_repeat_probability": MULTI_OUTPUT_REPEAT_PROBABILITY,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


if __name__ == "__main__":
    get_input_dir().mkdir(parents=True, exist_ok=True)
    get_reply_dir().mkdir(parents=True, exist_ok=True)
    counts = refresh_cache()
    print("Flask multi-stream live_stream_llm API started", flush=True)
    print(f"MongoDB: {DATABASE_NAME}", flush=True)
    print(f"Default collection: {DEFAULT_COLLECTION_NAME}", flush=True)
    print(f"User collection: {USER_COLLECTION_NAME}", flush=True)
    print(f"Cached replies: {counts['cached_reply_count']}", flush=True)
    print(f"Input dir: {get_input_dir()}", flush=True)
    print(f"Reply dir: {get_reply_dir()}", flush=True)
    print(f"Input pattern: {INPUT_PATTERN}", flush=True)
    print(f"Embedding model: {EMBEDDING_MODEL}", flush=True)
    print(f"Similarity threshold: {SIMILARITY_THRESHOLD}", flush=True)
    print(f"Direct reply threshold: {DIRECT_REPLY_THRESHOLD}", flush=True)
    print(f"Reply mode: {REPLY_MODE}", flush=True)
    print(f"Chat model: {CHAT_MODEL}", flush=True)
    print(f"Generated reply max chars: {MAX_REPLY_CHARS}", flush=True)
    print(f"Generation cooldown seconds: {GENERATION_COOLDOWN_SECONDS}", flush=True)
    print(f"Multi-output repeat probability: {MULTI_OUTPUT_REPEAT_PROBABILITY}", flush=True)
    app.run(host="0.0.0.0", port=5002, debug=False)
