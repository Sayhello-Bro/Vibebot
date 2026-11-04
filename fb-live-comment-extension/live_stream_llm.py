"""Live-stream orchestration API.

Vector work lives in vector_search.py. Qwen work lives in reply_generator.py.
This module only reads input, connects both services, assigns account replies,
and writes compact two-line-per-account stream logs.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS

from cls_encoder import CLSEncoder, DEFAULT_CLS_MODEL, DEFAULT_CLS_REVISION
from cls_memory import CLSCache, MemoryReader, format_live_memory
from reply_generator import (
    CANDIDATE_MATCH_RETRIES,
    CHAT_MODEL,
    GENERATION_TEMPERATURE,
    MAX_PROMPT_CANDIDATES,
    MAX_REPLY_CHARS,
    generate_account_replies,
    get_inference_runtime,
    initialize_inference_runtime,
    normalize_reply_for_compare,
)
from vector_search import MAX_CANDIDATES, MIN_VECTOR_EXAMPLES, candidate_search


if sys.platform == "win32":
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


app = Flask(__name__)
CORS(app)

INPUT_DIR_NAME = os.environ.get("LLM_INPUT_DIR", "inputs")
REPLY_DIR_NAME = os.environ.get("LLM_REPLY_DIR", "replies")
INPUT_PATTERN = os.environ.get("LLM_INPUT_PATTERN", "*.jsonl")
ENABLE_QWEN = os.environ.get("ENABLE_QWEN", "true").lower() in {
    "true",
    "1",
    "yes",
    "y",
}
GENERATION_COOLDOWN_SECONDS = float(
    os.environ.get("GENERATION_COOLDOWN_SECONDS", "0")
)
MULTI_OUTPUT_REPEAT_PROBABILITY = float(
    os.environ.get("MULTI_OUTPUT_REPEAT_PROBABILITY", "0.35")
)
MODEL_MIN_INPUT_CHARS = int(os.environ.get("MODEL_MIN_INPUT_CHARS", "100"))
MODEL_MAX_BUFFER_CHARS = int(os.environ.get("MODEL_MAX_BUFFER_CHARS", "800"))
MODEL_REPLY_GUARANTEE_INTERVAL = 3
RECENT_REPLY_LIMIT = int(os.environ.get("RECENT_REPLY_LIMIT", "5"))
FREQUENCY_FALLBACK_REPLIES = ("收到", "了解", "好喔", "有看到", "原來如此")
USE_CLS_MEMORY = os.environ.get("USE_CLS_MEMORY", "false").lower() in {
    "true",
    "1",
    "yes",
    "y",
}
CLS_MODEL = os.environ.get("CLS_MODEL", DEFAULT_CLS_MODEL)
CLS_REVISION = os.environ.get("CLS_REVISION", DEFAULT_CLS_REVISION)
CLS_MODEL_CACHE = os.environ.get(
    "CLS_MODEL_CACHE", str(Path(__file__).resolve().parent / ".cache-cls" / "models")
)
CLS_DEVICE = os.environ.get("CLS_DEVICE", "cpu")
CLS_OFFLINE = os.environ.get("CLS_OFFLINE", "false").lower() in {
    "true",
    "1",
    "yes",
    "y",
}
CLS_MAX_TURNS = int(os.environ.get("CLS_MAX_TURNS", "500"))
MEMORY_TOP_K = int(os.environ.get("MEMORY_TOP_K", "2"))
MEMORY_RECENT_K = int(os.environ.get("MEMORY_RECENT_K", "2"))
MEMORY_MAX_CHARS = int(os.environ.get("MEMORY_MAX_CHARS", "400"))
STYLE_WEIGHTS = {
    "reaction": int(os.environ.get("STYLE_WEIGHT_REACTION", "4")),
    "question": int(os.environ.get("STYLE_WEIGHT_QUESTION", "1")),
}
ACCOUNT_MODES = {"qwen_only", "hybrid", "vector_only"}
DEFAULT_ACCOUNT_MODE = os.environ.get("DEFAULT_ACCOUNT_MODE", "qwen_only").strip().lower()
if DEFAULT_ACCOUNT_MODE not in ACCOUNT_MODES:
    raise ValueError(
        "DEFAULT_ACCOUNT_MODE must be qwen_only, hybrid, or vector_only"
    )
if (
    MODEL_MIN_INPUT_CHARS < 1
    or MODEL_MAX_BUFFER_CHARS < MODEL_MIN_INPUT_CHARS
):
    raise ValueError("model input character limits are invalid")
if RECENT_REPLY_LIMIT < 1:
    raise ValueError("RECENT_REPLY_LIMIT must be greater than 0")
if any(weight < 0 for weight in STYLE_WEIGHTS.values()) or not sum(
    STYLE_WEIGHTS.values()
):
    raise ValueError("style weights must be non-negative and not all zero")

FILE_POSITIONS: dict[str, int] = {}
LAST_REPLY_RESULTS: dict[str, dict[str, Any]] = {}
LAST_GENERATION_AT: dict[str, float] = {}
STREAM_HISTORY: dict[str, list[dict[str, Any]]] = {}
STYLE_BAGS: dict[str, list[str]] = {}
MODEL_TEXT_BUFFERS: dict[str, str] = {}
MODEL_IGNORE_STREAKS: dict[tuple[str, str], int] = {}
STREAM_PROCESS_LOCKS: dict[str, threading.RLock] = {}
CLS_ENCODER: CLSEncoder | None = None
CLS_CACHE: CLSCache | None = None
CLS_READER: MemoryReader | None = None
CLS_INITIALIZATION_ERROR: str | None = None
CLS_INITIALIZATION_ATTEMPTED = False
STATE_LOCK = threading.RLock()
stream_settings_collection = candidate_search.db["stream_reply_settings"]


def get_stream_reply_setting(stream_id: str) -> dict[str, Any] | None:
    return stream_settings_collection.find_one({"stream_id": stream_id})


def get_stream_reply_mode(stream_id: str) -> str:
    setting = get_stream_reply_setting(stream_id) or {}
    mode = str(setting.get("reply_mode") or "manual").strip().lower()
    # Older builds stored crowd as a reply mode.  Preserve its automatic
    # behaviour while migrating to the two-mode design.
    if mode == "crowd":
        return "auto"
    return mode if mode in {"manual", "auto"} else "manual"


def get_stream_enabled_keys(stream_id: str) -> set[str]:
    setting = get_stream_reply_setting(stream_id)
    if setting is None:
        return {str(item["key"]) for item in candidate_search.list_cached() if item.get("source") in {"default", "user"}}
    return {str(key) for key in setting.get("enabled_keys", [])}


def get_stream_reference_candidates(stream_id: str) -> list[dict[str, Any]]:
    """Return checked default/user phrases supplied to both reply modes."""

    enabled_keys = get_stream_enabled_keys(stream_id)
    return [
        item
        for item in candidate_search.list_cached()
        if item.get("source") in {"default", "user"}
        and str(item.get("key")) in enabled_keys
    ]


def get_current_time() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def get_stream_process_lock(stream_id: str) -> threading.RLock:
    with STATE_LOCK:
        return STREAM_PROCESS_LOCKS.setdefault(stream_id, threading.RLock())


def initialize_cls_memory() -> dict[str, Any]:
    global CLS_ENCODER, CLS_CACHE, CLS_READER
    global CLS_INITIALIZATION_ERROR, CLS_INITIALIZATION_ATTEMPTED
    if not USE_CLS_MEMORY:
        return {"enabled": False, "ready": False, "error": None}
    with STATE_LOCK:
        if CLS_ENCODER is not None and CLS_CACHE is not None and CLS_READER is not None:
            return {"enabled": True, "ready": True, "error": None}
        if CLS_INITIALIZATION_ATTEMPTED:
            return {
                "enabled": True,
                "ready": False,
                "error": CLS_INITIALIZATION_ERROR,
            }
        CLS_INITIALIZATION_ATTEMPTED = True
        try:
            CLS_ENCODER = CLSEncoder(
                model_id=CLS_MODEL,
                revision=CLS_REVISION,
                cache_dir=CLS_MODEL_CACHE,
                device=CLS_DEVICE,
                local_files_only=CLS_OFFLINE,
            )
            CLS_CACHE = CLSCache(CLS_ENCODER.spec, max_turns=CLS_MAX_TURNS)
            CLS_READER = MemoryReader(
                top_k=MEMORY_TOP_K,
                recent_k=MEMORY_RECENT_K,
                max_chars=MEMORY_MAX_CHARS,
            )
            CLS_INITIALIZATION_ERROR = None
        except Exception as exc:
            CLS_INITIALIZATION_ERROR = str(exc)
            CLS_ENCODER = None
            CLS_CACHE = None
            CLS_READER = None
    return {
        "enabled": True,
        "ready": CLS_ENCODER is not None,
        "error": CLS_INITIALIZATION_ERROR,
    }


def observe_cls_memory(
    stream_id: str, raw_text: str, turn_id: str | None
) -> tuple[str, dict[str, Any]]:
    metadata: dict[str, Any] = {
        "enabled": USE_CLS_MEMORY,
        "ready": False,
        "turn_id": turn_id,
        "stored": False,
        "cache_count": 0,
        "history_count": 0,
        "retrieved_count": 0,
        "cls_elapsed_ms": 0.0,
        "memory_read_elapsed_ms": 0.0,
        "records": [],
        "error": None,
    }
    if not USE_CLS_MEMORY:
        return "", metadata
    status = initialize_cls_memory()
    if not status["ready"]:
        metadata["error"] = status["error"]
        return "", metadata
    assert CLS_ENCODER is not None and CLS_CACHE is not None and CLS_READER is not None
    try:
        cached = CLS_CACHE.lookup(stream_id, turn_id, raw_text) if turn_id else None
        if cached is None:
            cls_started = time.perf_counter()
            vector = CLS_ENCODER.encode(raw_text)
            metadata["cls_elapsed_ms"] = round(
                (time.perf_counter() - cls_started) * 1000, 2
            )
            current, history, stored = CLS_CACHE.observe(
                stream_id,
                raw_text,
                vector,
                CLS_ENCODER.spec,
                turn_id=turn_id,
            )
        else:
            current, history = cached
            stored = False
        read_started = time.perf_counter()
        records = CLS_READER.read(current, history)
        metadata["memory_read_elapsed_ms"] = round(
            (time.perf_counter() - read_started) * 1000, 2
        )
        metadata.update(
            {
                "ready": True,
                "turn_id": current.turn_id,
                "stored": stored,
                "cache_count": CLS_CACHE.count(stream_id),
                "history_count": len(history),
                "retrieved_count": len(records),
                "records": records,
            }
        )
        return format_live_memory(records), metadata
    except Exception as exc:
        metadata["error"] = str(exc)
        return "", metadata


def resolve_path(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else Path(__file__).resolve().parent / path


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
    return Path(stream_name).stem if stream_name else None


def get_reply_file_path_for_stream(stream_id: str) -> Path:
    return get_reply_dir() / f"{stream_id}_reply.txt"


def get_reply_file_path(input_file: Path) -> Path:
    return get_reply_file_path_for_stream(get_stream_id_from_file(input_file))


def discover_input_files(stream_id: str | None = None) -> list[Path]:
    input_dir = get_input_dir()
    if not input_dir.exists():
        return []
    files = sorted(path for path in input_dir.glob(INPUT_PATTERN) if path.is_file())
    if stream_id:
        files = [path for path in files if path.stem == stream_id]
    return files


def get_request_json_silent() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


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
    return path if path.is_absolute() else get_input_dir() / path


def parse_account_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        raise ValueError("account_ids must be a list or comma-separated string")
    return list(dict.fromkeys(item for item in values if item))


def get_request_account_ids() -> list[str]:
    data = get_request_json_silent()
    value = data.get("account_ids") if "account_ids" in data else None
    return parse_account_ids(value or request.args.get("account_ids"))


def normalize_account_mode(value: Any) -> str:
    mode = str(value or DEFAULT_ACCOUNT_MODE).strip().lower()
    if mode not in ACCOUNT_MODES:
        raise ValueError(
            f"invalid account mode {value!r}; expected qwen_only, hybrid, or vector_only"
        )
    return mode


def parse_account_modes(value: Any, account_ids: list[str]) -> dict[str, str]:
    """Return one explicit reply mode for every account, preserving compatibility."""

    if value is None:
        supplied: dict[str, Any] = {}
    elif isinstance(value, dict):
        supplied = {str(key).strip(): mode for key, mode in value.items()}
    else:
        raise ValueError("account_modes must be an object keyed by account_id")

    unknown = sorted(set(supplied).difference(account_ids))
    if unknown:
        raise ValueError("account_modes contains unknown accounts: " + ", ".join(unknown))
    return {
        account_id: normalize_account_mode(supplied.get(account_id))
        for account_id in account_ids
    }


def stream_context(stream_id: str) -> str:
    with STATE_LOCK:
        history = list(STREAM_HISTORY.get(stream_id, [])[-2:])
    if not history:
        return ""
    return "；".join(
        f"主播：{item['raw_text']}／留言：{'、'.join(item.get('replies', []))}"
        for item in history
    )


def recent_stream_replies(
    stream_id: str, limit: int = RECENT_REPLY_LIMIT
) -> list[str]:
    with STATE_LOCK:
        flattened = [
            reply
            for item in STREAM_HISTORY.get(stream_id, [])
            for reply in item.get("replies", [])
            if reply and reply.casefold() != "ignore"
        ]
        return flattened[-limit:]


def exclude_recent_fixed_candidates(
    stream_id: str,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Exclude recently sent normal phrases while allowing crowd repeats."""

    recent_keys = {
        normalize_reply_for_compare(reply)
        for reply in recent_stream_replies(stream_id)
        if normalize_reply_for_compare(reply)
    }
    kept: list[dict[str, Any]] = []
    excluded: list[str] = []
    for candidate in candidates:
        text = str(candidate.get("text") or "").strip()
        if candidate.get("source") == "crowd":
            kept.append(candidate)
        elif normalize_reply_for_compare(text) in recent_keys:
            excluded.append(text)
        else:
            kept.append(candidate)
    return kept, excluded


def add_stream_history(
    stream_id: str, raw_text: str, account_results: list[dict[str, Any]]
) -> None:
    with STATE_LOCK:
        history = STREAM_HISTORY.setdefault(stream_id, [])
        replies: list[str] = []
        for item in account_results:
            reply = str(item.get("reply") or "")
            if not item.get("has_reply") or not reply or reply.casefold() == "ignore":
                continue
            if reply not in replies:
                replies.append(reply)
        history.append({"raw_text": raw_text, "replies": replies})
        del history[:-20]


def enforce_model_reply_frequency(
    stream_id: str,
    source_text: str,
    account_ids: list[str],
    generated_by_account: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Guarantee one safe reply after three consecutive model ignores."""

    forced_accounts: list[str] = []
    counters: dict[str, int] = {}
    with STATE_LOCK:
        recent = {
            str(reply).casefold()
            for turn in STREAM_HISTORY.get(stream_id, [])
            for reply in turn.get("replies", [])
        }
        used_fallbacks: set[str] = set()
        for index, account_id in enumerate(account_ids):
            key = (stream_id, account_id)
            item = generated_by_account.get(account_id)
            has_model_reply = bool(
                item
                and item.get("valid")
                and item.get("action") == "reply"
                and str(item.get("reply") or "").casefold() != "ignore"
            )
            if has_model_reply:
                MODEL_IGNORE_STREAKS[key] = 0
                counters[account_id] = 0
                continue

            streak = MODEL_IGNORE_STREAKS.get(key, 0) + 1
            if streak < MODEL_REPLY_GUARANTEE_INTERVAL:
                MODEL_IGNORE_STREAKS[key] = streak
                counters[account_id] = streak
                continue

            fallback = next(
                (
                    FREQUENCY_FALLBACK_REPLIES[
                        (index + offset) % len(FREQUENCY_FALLBACK_REPLIES)
                    ]
                    for offset in range(len(FREQUENCY_FALLBACK_REPLIES))
                    if FREQUENCY_FALLBACK_REPLIES[
                        (index + offset) % len(FREQUENCY_FALLBACK_REPLIES)
                    ].casefold()
                    not in recent
                    and FREQUENCY_FALLBACK_REPLIES[
                        (index + offset) % len(FREQUENCY_FALLBACK_REPLIES)
                    ].casefold()
                    not in used_fallbacks
                ),
                FREQUENCY_FALLBACK_REPLIES[
                    index % len(FREQUENCY_FALLBACK_REPLIES)
                ],
            )
            evidence = "".join(source_text.split())[:20]
            if item is None:
                item = {"account_id": account_id}
                generated_by_account[account_id] = item
            item.update(
                {
                    "style": "reaction",
                    "reply": fallback,
                    "action": "reply",
                    "valid": True,
                    "validation_reason": "minimum_reply_frequency",
                    "evidence_text": evidence,
                    "frequency_fallback": True,
                    "model_ignore_streak_before_fallback": streak,
                }
            )
            used_fallbacks.add(fallback.casefold())
            forced_accounts.append(account_id)
            MODEL_IGNORE_STREAKS[key] = 0
            counters[account_id] = 0
    return {
        "interval": MODEL_REPLY_GUARANTEE_INTERVAL,
        "ignore_streaks": counters,
        "forced_accounts": forced_accounts,
    }


def accounts_due_for_model_reply(
    stream_id: str, account_ids: list[str]
) -> list[str]:
    """Tell Qwen which accounts must reply on this (third) model batch."""

    with STATE_LOCK:
        return [
            account_id
            for account_id in account_ids
            if MODEL_IGNORE_STREAKS.get((stream_id, account_id), 0)
            >= MODEL_REPLY_GUARANTEE_INTERVAL - 1
        ]


def draw_account_styles(stream_id: str, account_ids: list[str]) -> dict[str, str]:
    """Draw from a shuffled weighted bag so long-run proportions cannot drift."""

    with STATE_LOCK:
        bag = STYLE_BAGS.setdefault(stream_id, [])
        assigned: dict[str, str] = {}
        for account_id in account_ids:
            if not bag:
                bag.extend(
                    style
                    for style, weight in STYLE_WEIGHTS.items()
                    for _ in range(weight)
                )
                random.shuffle(bag)
            assigned[account_id] = bag.pop()
        return assigned


def text_character_count(text: str) -> int:
    return len("".join(character for character in text if not character.isspace()))


def append_model_text(stream_id: str, raw_text: str) -> tuple[str, int, bool]:
    with STATE_LOCK:
        previous = MODEL_TEXT_BUFFERS.get(stream_id, "")
        combined = " ".join(part for part in (previous, raw_text.strip()) if part)
        if len(combined) > MODEL_MAX_BUFFER_CHARS:
            combined = combined[-MODEL_MAX_BUFFER_CHARS:]
        MODEL_TEXT_BUFFERS[stream_id] = combined
        count = text_character_count(combined)
        return combined, count, count >= MODEL_MIN_INPUT_CHARS


def consume_model_text(stream_id: str) -> str:
    with STATE_LOCK:
        return MODEL_TEXT_BUFFERS.pop(stream_id, "")


def restore_model_text(stream_id: str, consumed_text: str) -> None:
    """Restore a consumed batch when Qwen fails so speech is not lost."""

    if not consumed_text:
        return
    with STATE_LOCK:
        current = MODEL_TEXT_BUFFERS.get(stream_id, "")
        combined = " ".join(part for part in (consumed_text, current) if part)
        if len(combined) > MODEL_MAX_BUFFER_CHARS:
            combined = combined[-MODEL_MAX_BUFFER_CHARS:]
        MODEL_TEXT_BUFFERS[stream_id] = combined


def should_call_qwen(stream_id: str) -> tuple[bool, float]:
    if not ENABLE_QWEN:
        return False, 0.0
    last_generated = LAST_GENERATION_AT.get(stream_id)
    if last_generated is None or GENERATION_COOLDOWN_SECONDS <= 0:
        return True, 0.0
    remaining = max(
        0.0,
        GENERATION_COOLDOWN_SECONDS - (time.monotonic() - last_generated),
    )
    return remaining <= 0, remaining


def assign_replies(
    account_ids: list[str],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    used_keys: set[str] = set()
    results: list[dict[str, Any]] = []
    for account_id in account_ids:
        selected = None
        for candidate in candidates:
            if candidate["key"] not in used_keys:
                selected = candidate
                break
            if candidate.get("multi_output") and (
                random.random() <= MULTI_OUTPUT_REPEAT_PROBABILITY
            ):
                selected = candidate
                break
        if selected:
            used_keys.add(selected["key"])
        results.append(
            {
                "account_id": account_id,
                "reply": selected["text"] if selected else "ignore",
                "has_reply": selected is not None,
                "selected": selected,
                "candidate_count": len(candidates),
            }
        )
    return results


def _choose_for_accounts_unlocked(
    raw_text: str,
    account_ids: list[str],
    stream_id: str = "direct",
    context: dict[str, Any] | None = None,
    account_modes: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not account_ids:
        raise ValueError("account_ids is required")
    context = context or {}
    account_modes = parse_account_modes(account_modes, account_ids)
    account_styles = draw_account_styles(stream_id, account_ids)
    product_type = candidate_search.resolve_stream_product_type(
        stream_id, context.get("product_type")
    )
    initial_learning_status = candidate_search.get_learning_status(
        stream_id, product_type
    )
    started = time.perf_counter()
    cls_prompt_context, cls_memory = observe_cls_memory(
        stream_id,
        raw_text,
        str(context.get("turn_id") or "").strip() or None,
    )

    stream_reply_mode = get_stream_reply_mode(stream_id)
    reference_candidates = get_stream_reference_candidates(stream_id)
    crowd_slogans = candidate_search.list_crowd_slogans()
    fixed_library = reference_candidates + crowd_slogans
    qwen_account_ids = list(account_ids) if stream_reply_mode == "auto" else []
    model_input_text = ""
    model_input_char_count = 0
    model_input_ready = False
    model_input_pending = False
    if qwen_account_ids and ENABLE_QWEN:
        model_input_text, model_input_char_count, model_input_ready = append_model_text(
            stream_id, raw_text
        )
        model_input_pending = not model_input_ready

    vector_account_ids = list(account_ids) if stream_reply_mode == "manual" else []
    recent_candidates_excluded: list[str] = []
    if stream_reply_mode == "manual":
        search_result = {
            **initial_learning_status,
            **candidate_search.search_fixed_candidates(
                raw_text,
                allowed_keys={str(item["key"]) for item in fixed_library},
                limit=MAX_CANDIDATES,
            ),
        }
        fixed_candidates, recent_candidates_excluded = exclude_recent_fixed_candidates(
            stream_id, search_result["candidates"]
        )
        vector_search_performed = True
    else:
        cache_status = candidate_search.refresh()
        fixed_candidates = []
        search_result = {
            "best_similarity": 0.0,
            "threshold": None,
            "candidate_revision": cache_status["candidate_revision"],
            "refreshed": cache_status["refreshed"],
            "cached_reply_count": cache_status["cached_reply_count"],
            "elapsed_ms": 0.0,
            "learned_examples_considered": 0,
            "learned_candidates_selected": 0,
            **initial_learning_status,
            "query_fragment_count": 0,
            "static_candidates_compared": 0,
        }
        vector_search_performed = False

    # Manual mode ranks only the three configured phrase sources. Automatic
    # mode gives those same sources to Qwen and permits newly generated text.
    vector_by_account = {
        item["account_id"]: item
        for item in assign_replies(vector_account_ids, fixed_candidates)
    }
    qwen_allowed, cooldown_remaining = (
        should_call_qwen(stream_id) if qwen_account_ids else (False, 0.0)
    )
    generation: dict[str, Any] | None = None
    generated_by_account: dict[str, dict[str, Any]] = {}
    reply_frequency = {
        "interval": MODEL_REPLY_GUARANTEE_INTERVAL,
        "ignore_streaks": {},
        "forced_accounts": [],
    }
    qwen_called = False

    if qwen_account_ids and qwen_allowed and model_input_ready:
        model_input_text = consume_model_text(stream_id)
        model_input_char_count = text_character_count(model_input_text)
        required_reply_account_ids = accounts_due_for_model_reply(
            stream_id, qwen_account_ids
        )
        try:
            qwen_called = True
            generation_entities = dict(context.get("entities") or {})
            if product_type:
                generation_entities["product_type"] = product_type
            generation = generate_account_replies(
                source_text=model_input_text,
                account_ids=qwen_account_ids,
                candidates=reference_candidates,
                crowd_slogans=crowd_slogans,
                entities=generation_entities,
                stream_context=cls_prompt_context,
                account_styles={
                    account_id: account_styles[account_id]
                    for account_id in qwen_account_ids
                },
                participant_count=len(account_ids),
                forbidden_replies=recent_stream_replies(stream_id),
                required_reply_account_ids=required_reply_account_ids,
                reply_mode="auto",
            )
            generated_by_account = {
                item["account_id"]: item
                for item in generation.get("results", [])
                if isinstance(item, dict) and item.get("account_id")
            }
            reply_frequency = enforce_model_reply_frequency(
                stream_id,
                model_input_text,
                qwen_account_ids,
                generated_by_account,
            )
            generation["results"] = [
                generated_by_account[account_id]
                for account_id in qwen_account_ids
                if account_id in generated_by_account
            ]
            generation["result_count"] = len(generation["results"])
            generation["reply_frequency"] = reply_frequency
            if any(item.get("valid") for item in generated_by_account.values()):
                LAST_GENERATION_AT[stream_id] = time.monotonic()
        except Exception as exc:
            restore_model_text(stream_id, model_input_text)
            generation = {
                "valid": False,
                "validation_reason": "generation_error",
                "error": str(exc),
                "model": CHAT_MODEL,
            }
    crowd_response = bool(generation and generation.get("crowd_response"))
    account_results: list[dict[str, Any]] = []
    for account_id in account_ids:
        account_mode = account_modes[account_id]
        style = account_styles[account_id]
        model_item = generated_by_account.get(account_id)
        vector_item = vector_by_account.get(account_id)
        if model_item and model_item.get("style") in {"reaction", "question"}:
            style = str(model_item["style"])

        if stream_reply_mode == "manual":
            if vector_item and vector_item["has_reply"]:
                account_results.append(
                    {
                        **vector_item,
                        "mode": "manual",
                        "account_mode": account_mode,
                        "style": style,
                        "reply_source": "fixed_vector",
                        "model_result": None,
                    }
                )
            else:
                account_results.append(
                    {
                        "account_id": account_id,
                        "mode": "manual",
                        "account_mode": account_mode,
                        "style": style,
                        "reply": "ignore",
                        "has_reply": False,
                        "reply_source": "fixed_vector_miss",
                        "selected": None,
                        "model_result": None,
                        "candidate_count": len(fixed_candidates),
                    }
                )
            continue

        if crowd_response and model_item and model_item.get("reply"):
            crowd_token = str(model_item["reply"])
            selected = {
                "key": f"crowd:{stream_id}:{time.time_ns()}",
                "source": "qwen_crowd_response",
                "id": None,
                "text": crowd_token,
                "similarity": None,
                "weight": 1.0,
                "score": None,
                "multi_output": True,
            }
            account_results.append(
                {
                    "account_id": account_id,
                    "mode": "auto",
                    "account_mode": account_mode,
                    "style": "crowd_response",
                    "reply": crowd_token,
                    "has_reply": True,
                    "reply_source": "qwen_crowd",
                    "selected": selected,
                    "model_result": model_item,
                    "candidate_count": len(fixed_candidates),
                }
            )
            continue

        if (
            model_item
            and model_item.get("valid")
            and model_item.get("action") == "reply"
            and model_item.get("reply")
        ):
            reply = str(model_item["reply"])
            selected = {
                "key": f"generated:{stream_id}:{account_id}:{time.time_ns()}",
                "source": "qwen_generated",
                "id": None,
                "text": reply,
                "similarity": None,
                "weight": 1.0,
                "score": None,
                "multi_output": False,
            }
            account_results.append(
                {
                    "account_id": account_id,
                    "mode": "auto",
                    "account_mode": account_mode,
                    "style": style,
                    "reply": reply,
                    "has_reply": True,
                    "reply_source": (
                        "qwen_frequency_fallback"
                        if model_item.get("frequency_fallback")
                        else "qwen"
                    ),
                    "evidence_text": str(model_item.get("evidence_text") or ""),
                    "selected": selected,
                    "model_result": model_item,
                    "candidate_count": len(fixed_candidates),
                }
            )
            continue

        if model_item and model_item.get("valid") and model_item.get("action") == "ignore":
            reply_source = "qwen_ignore"
        elif model_input_pending:
            reply_source = "model_input_pending"
        elif generation is not None:
            reply_source = "qwen_error"
        elif not ENABLE_QWEN:
            reply_source = "qwen_disabled"
        elif not qwen_allowed:
            reply_source = "qwen_cooldown"
        else:
            reply_source = "qwen_not_called"
        account_results.append(
            {
                "account_id": account_id,
                "mode": "auto",
                "account_mode": account_mode,
                "style": style,
                "reply": "ignore",
                "has_reply": False,
                "reply_source": reply_source,
                "selected": None,
                "model_result": model_item,
                "candidate_count": len(fixed_candidates),
            }
        )

    first_reply = account_results[0]["reply"] if account_results else "ignore"
    add_stream_history(stream_id, raw_text, account_results)
    learned_example_storage: dict[str, Any] = {"stored_count": 0}
    if qwen_called and generation and generation.get("valid") and product_type and stream_reply_mode == "auto":
        try:
            generated_examples = [
                {
                    **item,
                    "reply_source": "qwen",
                    "has_reply": item.get("action") == "reply",
                }
                for item in generation.get("results", [])
                if isinstance(item, dict) and not item.get("frequency_fallback")
            ]
            learned_example_storage = candidate_search.store_generated_examples(
                stream_id=stream_id,
                product_type=product_type,
                speaker_text=model_input_text,
                replies=generated_examples,
            )
        except Exception as exc:
            learned_example_storage = {
                "stored_count": 0,
                "error": str(exc),
            }
    final_example_count = int(
        learned_example_storage.get(
            "example_count", search_result["example_count"]
        )
    )
    final_learning_ready = final_example_count >= MIN_VECTOR_EXAMPLES

    return {
        "has_reply": any(item["has_reply"] for item in account_results),
        "reply": first_reply,
        "account_results": account_results,
        "account_count": len(account_ids),
        "reply_mode": stream_reply_mode,
        "account_modes": account_modes,
        "account_styles": account_styles,
        "product_type": product_type,
        "style_weights": STYLE_WEIGHTS,
        "candidate_count": len(fixed_candidates),
        "candidates": fixed_candidates,
        "reference_candidate_count": len(reference_candidates),
        "reference_candidates": reference_candidates,
        "crowd_slogan_count": len(crowd_slogans),
        "crowd_slogans": crowd_slogans,
        "best_similarity": search_result["best_similarity"],
        "threshold": search_result["threshold"],
        "candidate_revision": search_result["candidate_revision"],
        "cache_refreshed": search_result["refreshed"],
        "cached_reply_count": search_result["cached_reply_count"],
        "vector_search_elapsed_ms": search_result["elapsed_ms"],
        "learned_examples_considered": search_result[
            "learned_examples_considered"
        ],
        "learned_candidates_selected": search_result[
            "learned_candidates_selected"
        ],
        "live_database": search_result["live_database"],
        "learned_example_count": final_example_count,
        "minimum_vector_examples": search_result["minimum_vector_examples"],
        "learning_ready": final_learning_ready,
        "query_fragment_count": search_result["query_fragment_count"],
        "static_candidates_compared": search_result[
            "static_candidates_compared"
        ],
        "learned_example_storage": learned_example_storage,
        "vector_search_performed": vector_search_performed,
        "recent_reply_limit": RECENT_REPLY_LIMIT,
        "recent_candidates_excluded": recent_candidates_excluded,
        "generation": generation,
        "qwen_enabled": ENABLE_QWEN,
        "qwen_called": qwen_called,
        "qwen_allowed": qwen_allowed,
        "qwen_requested_accounts": qwen_account_ids,
        "reply_frequency": reply_frequency,
        "crowd_response": crowd_response,
        "crowd_token": generation.get("crowd_token") if generation else None,
        "crowd_tokens": generation.get("crowd_tokens", []) if generation else [],
        "crowd_base_token": generation.get("crowd_base_token")
        if generation
        else None,
        "crowd_attributes_by_account": generation.get(
            "crowd_attributes_by_account", {}
        )
        if generation
        else {},
        "model_input_text": model_input_text if qwen_called else None,
        "model_input_char_count": model_input_char_count,
        "model_min_input_chars": MODEL_MIN_INPUT_CHARS,
        "model_input_pending": model_input_pending,
        "generation_cooldown_seconds": GENERATION_COOLDOWN_SECONDS,
        "generation_cooldown_remaining": round(cooldown_remaining, 2),
        "cls_memory": cls_memory,
        "cls_elapsed_ms": cls_memory["cls_elapsed_ms"],
        "memory_read_elapsed_ms": cls_memory["memory_read_elapsed_ms"],
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def choose_for_accounts(
    raw_text: str,
    account_ids: list[str],
    stream_id: str = "direct",
    context: dict[str, Any] | None = None,
    account_modes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Serialize one stream while allowing different streams to run concurrently."""

    with get_stream_process_lock(stream_id):
        return _choose_for_accounts_unlocked(
            raw_text,
            account_ids,
            stream_id=stream_id,
            context=context,
            account_modes=account_modes,
        )


def read_new_entries(
    input_file: Path,
    request_account_ids: list[str],
    request_account_modes: Any = None,
    from_start: bool = False,
) -> list[dict[str, Any]]:
    file_key = str(input_file.resolve())
    with input_file.open("r", encoding="utf-8") as file:
        if from_start:
            FILE_POSITIONS[file_key] = 0
        file.seek(FILE_POSITIONS.get(file_key, 0))
        new_lines = file.readlines()
        FILE_POSITIONS[file_key] = file.tell()

    entries = []
    file_stream_id = input_file.stem
    for line_number, line in enumerate(new_lines, start=1):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw_text = data.get("raw_text") or data.get("resolved_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        account_ids = request_account_ids or parse_account_ids(
            data.get("account_ids") or data.get("account_id")
        )
        account_modes = parse_account_modes(
            request_account_modes
            if request_account_modes is not None
            else data.get("account_modes"),
            account_ids,
        )
        entries.append(
            {
                "raw_text": raw_text.strip(),
                "stream_id": data.get("stream_id") or file_stream_id,
                "viewer_id": data.get("viewer_id"),
                "account_ids": account_ids,
                "account_modes": account_modes,
                "source_file": input_file.name,
                "source_path": str(input_file),
                "source_stream_id": file_stream_id,
                "line_number": line_number,
                "source_data": data,
                "product_type": data.get("product_type"),
            }
        )
    return entries


def single_line_log_value(value: Any) -> str:
    """Collapse embedded whitespace so one field cannot break the log format."""

    return " ".join(str(value or "").split())


def format_reply_log_records(result: dict[str, Any]) -> str:
    """Return exactly two lines for every account result."""

    raw_text = single_line_log_value(result.get("raw_text"))
    elapsed_seconds = float(result.get("elapsed_ms") or 0.0) / 1000.0
    lines: list[str] = []
    for account_result in result.get("account_results", []):
        lines.append(raw_text)
        lines.append(
            "觀眾編號={account_id}｜觀眾當下風格={style}｜留言={reply}｜"
            "花費時間={elapsed_seconds:.3f} 秒".format(
                account_id=single_line_log_value(account_result.get("account_id")),
                style=single_line_log_value(account_result.get("style")),
                reply=single_line_log_value(account_result.get("reply")),
                elapsed_seconds=elapsed_seconds,
            )
        )
    return "\n".join(lines) + ("\n" if lines else "")


def append_reply_log_for_stream(stream_id: str, result: dict[str, Any]) -> None:
    get_reply_dir().mkdir(parents=True, exist_ok=True)
    formatted = format_reply_log_records(result)
    if not formatted:
        return
    with STATE_LOCK:
        with get_reply_file_path_for_stream(stream_id).open(
            "a", encoding="utf-8"
        ) as file:
            file.write(formatted)


def print_processing_result(result: dict[str, Any]) -> None:
    """Keep the live LLM console useful for both fixed and automatic replies."""
    print("-" * 72, flush=True)
    print(
        f"{result.get('timestamp', get_current_time())} "
        f"[{result.get('stream_id', 'unknown')}] 收到句子："
        f"{single_line_log_value(result.get('raw_text'))}",
        flush=True,
    )
    minimum_chars = int(result.get("model_min_input_chars") or 0)
    if minimum_chars:
        print(
            f"模型累積：{int(result.get('model_input_char_count') or 0)}/{minimum_chars} 字｜"
            f"Qwen 呼叫：{'是' if result.get('qwen_called') else '否'}",
            flush=True,
        )
    for item in result.get("account_results", []):
        print(
            f"帳號 {item.get('account_id', 'unknown')}｜"
            f"結果來源：{single_line_log_value(item.get('reply_source')) or 'unknown'}｜"
            f"回覆：{single_line_log_value(item.get('reply')) or 'ignore'}",
            flush=True,
        )
    generation = result.get("generation")
    if isinstance(generation, dict) and generation.get("error"):
        print(f"Qwen 錯誤：{generation['error']}", flush=True)


def process_raw_text(
    raw_text: str,
    stream_id: str,
    account_ids: list[str],
    extra: dict[str, Any] | None = None,
    account_modes: dict[str, str] | None = None,
) -> dict[str, Any]:
    chosen = choose_for_accounts(
        raw_text, account_ids, stream_id, extra, account_modes=account_modes
    )
    result = {
        "timestamp": get_current_time(),
        "stream_id": stream_id,
        "raw_text": raw_text,
        "account_ids": account_ids,
        "reply_file": str(get_reply_file_path_for_stream(stream_id)),
        **chosen,
    }
    result.update(
        {key: value for key, value in (extra or {}).items() if value is not None}
    )
    append_reply_log_for_stream(stream_id, result)
    LAST_REPLY_RESULTS[stream_id] = result
    print_processing_result(result)
    return result


@app.route("/stream_replies", methods=["GET", "PATCH"])
def stream_replies():
    """Per-stream manual/automatic mode and checked replies used by the desktop UI."""
    try:
        data = get_request_json_silent()
        stream_id = normalize_stream_name(data.get("stream_id") or request.args.get("stream_id"))
        if not stream_id:
            return jsonify({"status": "error", "error": "stream_id is required"}), 400
        setting = get_stream_reply_setting(stream_id)
        enabled_keys = get_stream_enabled_keys(stream_id)
        reply_mode = get_stream_reply_mode(stream_id)
        if request.method == "PATCH":
            reply_key = str(data.get("reply_key") or "").strip()
            requested_mode = str(data.get("reply_mode") or "").strip().lower()
            if requested_mode and requested_mode not in {"manual", "auto"}:
                return jsonify({"status": "error", "error": "invalid reply_mode"}), 400
            if not reply_key and not requested_mode:
                return jsonify({"status": "error", "error": "reply_key or reply_mode is required"}), 400
            if requested_mode:
                reply_mode = requested_mode
            if reply_key:
                if bool(data.get("enabled")):
                    enabled_keys.add(reply_key)
                else:
                    enabled_keys.discard(reply_key)
            stream_settings_collection.update_one(
                {"stream_id": stream_id},
                {"$set": {"enabled_keys": sorted(enabled_keys), "reply_mode": reply_mode, "updated_at": get_current_time()}},
                upsert=True,
            )
        return jsonify({"status": "success", "stream_id": stream_id, "enabled_keys": sorted(enabled_keys), "reply_mode": reply_mode, "configured": setting is not None or request.method == "PATCH"})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/match", methods=["POST"])
def match():
    try:
        data = get_request_json_silent()
        raw_text = str(data.get("raw_text") or "").strip()
        if not raw_text:
            return jsonify({"status": "error", "error": "raw_text is required"}), 400
        result = process_raw_text(
            raw_text,
            get_requested_stream_name() or "direct",
            parse_account_ids(data.get("account_ids") or request.args.get("account_ids")),
            {
                "source": "api",
                "intent": data.get("intent"),
                "secondary_intents": data.get("secondary_intents", []),
                "entities": data.get("entities", {}),
                "product_type": data.get("product_type"),
                "turn_id": data.get("turn_id"),
            },
            account_modes=parse_account_modes(
                data.get("account_modes"),
                parse_account_ids(data.get("account_ids") or request.args.get("account_ids")),
            ),
        )
        return jsonify({"status": "success", "result": result})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400


@app.route("/process", methods=["POST"])
def process():
    request_data = get_request_json_silent()
    requested_path = get_requested_file_path()
    stream_id = get_requested_stream_name()
    from_start = request.args.get("from_start", "").lower() in {
        "true",
        "1",
        "yes",
        "y",
    }
    request_account_ids = get_request_account_ids()
    request_account_modes = request_data.get("account_modes")
    if requested_path is not None:
        input_files = [requested_path] if requested_path.exists() else []
        stream_id = requested_path.stem
    else:
        input_files = discover_input_files(stream_id)

    if stream_id and not input_files:
        expected = str(requested_path or (get_input_dir() / f"{stream_id}.jsonl"))
        return jsonify(
            {
                "status": "error",
                "error": "input jsonl file not found",
                "stream_id": stream_id,
                "expected_file": expected,
            }
        ), 404

    processed, errors, scanned = [], [], []
    try:
        for input_file in input_files:
            key = str(input_file.resolve())
            entries = read_new_entries(
                input_file,
                request_account_ids,
                request_account_modes=request_account_modes,
                from_start=from_start,
            )
            scanned.append(
                {
                    "stream_id": input_file.stem,
                    "file": str(input_file),
                    "reply_file": str(get_reply_file_path(input_file)),
                    "last_file_position": FILE_POSITIONS.get(key, 0),
                    "new_entry_count": len(entries),
                }
            )
            for entry in entries:
                try:
                    data = entry["source_data"]
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
                            "line_number": entry["line_number"],
                            "turn_id": (
                                f"{entry['source_file']}:{entry['line_number']}"
                            ),
                            "intent": data.get("intent"),
                            "secondary_intents": data.get("secondary_intents", []),
                            "entities": data.get("entities", {}),
                            "product_type": (
                                request_data.get("product_type")
                                or entry.get("product_type")
                            ),
                            "original_raw_text": data.get("raw_text"),
                        },
                        account_modes=entry["account_modes"],
                    )
                    processed.append(result)
                    print(
                        f"{result['timestamp']} stream={result['stream_id']} "
                        f"qwen={result['qwen_called']} elapsed_ms={result['elapsed_ms']}",
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
                "processed_count": len(processed),
                "error_count": len(errors),
                "scanned_files": scanned,
                "cached_reply_count": candidate_search.cached_count,
                "candidate_revision": candidate_search.revision,
                "results": processed,
                "errors": errors,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/latest_reply", methods=["GET", "POST"])
def latest_reply():
    stream_id = get_requested_stream_name()
    response = process()
    status_code = 200
    if isinstance(response, tuple):
        response, status_code = response
    data = response.get_json(silent=True) or {}
    results = data.get("results") or []
    if stream_id:
        latest = next(
            (item for item in reversed(results) if item.get("stream_id") == stream_id),
            LAST_REPLY_RESULTS.get(stream_id, {}),
        )
    else:
        latest = results[-1] if results else next(
            reversed(LAST_REPLY_RESULTS.values()), {}
        )
    return jsonify(
        {
            "status": data.get("status", "success"),
            "stream_id": latest.get("stream_id", stream_id),
            "has_reply": bool(latest.get("has_reply")),
            "reply": latest.get("reply", ""),
            "raw_text": latest.get("raw_text", ""),
            "account_results": latest.get("account_results", []),
            "elapsed_ms": latest.get("elapsed_ms"),
            "best_similarity": latest.get("best_similarity"),
            "candidate_revision": candidate_search.revision,
            "processed_count": data.get("processed_count", 0),
            "error_count": data.get("error_count", 0),
            "results": results,
            "errors": data.get("errors", []),
        }
    ), status_code


@app.route("/reload_replies", methods=["POST"])
def reload_replies():
    try:
        return jsonify({"status": "success", **candidate_search.refresh(force=True)})
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/stream_product", methods=["GET", "POST", "PUT"])
def stream_product():
    try:
        data = get_request_json_silent()
        stream_id = normalize_stream_name(
            data.get("stream_id") or request.args.get("stream_id")
        )
        if not stream_id:
            return jsonify(
                {"status": "error", "error": "stream_id is required"}
            ), 400
        if request.method in {"POST", "PUT"}:
            product_type = str(data.get("product_type") or "").strip()
            if not product_type:
                return jsonify(
                    {
                        "status": "error",
                        "error": "product_type is required",
                    }
                ), 400
            product_type = candidate_search.set_stream_product_type(
                stream_id, product_type
            )
        else:
            product_type = candidate_search.get_stream_product_type(stream_id)
        learning_status = candidate_search.get_learning_status(
            stream_id, product_type
        )
        return jsonify(
            {
                "status": "success",
                "stream_id": stream_id,
                "product_type": product_type,
                **learning_status,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/stream_database", methods=["DELETE"])
def delete_stream_database():
    try:
        data = get_request_json_silent()
        stream_id = normalize_stream_name(
            data.get("stream_id") or request.args.get("stream_id")
        )
        if not stream_id:
            return jsonify(
                {"status": "error", "error": "stream_id is required"}
            ), 400
        process_lock = get_stream_process_lock(stream_id)
        with process_lock:
            deletion = candidate_search.delete_live_database(stream_id)
            removed_cls_turns = (
                CLS_CACHE.clear_session(stream_id) if CLS_CACHE is not None else 0
            )
            with STATE_LOCK:
                LAST_REPLY_RESULTS.pop(stream_id, None)
                LAST_GENERATION_AT.pop(stream_id, None)
                STREAM_HISTORY.pop(stream_id, None)
                STYLE_BAGS.pop(stream_id, None)
                MODEL_TEXT_BUFFERS.pop(stream_id, None)
                for key in [
                    key for key in MODEL_IGNORE_STREAKS if key[0] == stream_id
                ]:
                    MODEL_IGNORE_STREAKS.pop(key, None)
                STREAM_PROCESS_LOCKS.pop(stream_id, None)
        return jsonify(
            {
                "status": "success",
                **deletion,
                "removed_cls_turns": removed_cls_turns,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/candidate_cache", methods=["GET"])
def candidate_cache():
    try:
        include_embedding = request.args.get("show_embedding", "").lower() in {
            "true",
            "1",
            "yes",
        }
        items = candidate_search.list_cached(include_embedding)
        return jsonify(
            {
                "status": "success",
                "candidate_revision": candidate_search.revision,
                "total": len(items),
                "items": items,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.route("/streams", methods=["GET"])
def list_streams():
    streams = []
    for input_file in discover_input_files():
        key = str(input_file.resolve())
        reply_file = get_reply_file_path(input_file)
        streams.append(
            {
                "stream_id": input_file.stem,
                "input_file": str(input_file),
                "input_size": input_file.stat().st_size,
                "last_file_position": FILE_POSITIONS.get(key, 0),
                "reply_file": str(reply_file),
                "reply_exists": reply_file.exists(),
            }
        )
    return jsonify({"status": "success", "streams": streams})


@app.route("/health", methods=["GET"])
def health():
    try:
        return jsonify(
            {
                "status": "ok",
                **candidate_search.health(),
                "input_dir": str(get_input_dir()),
                "reply_dir": str(get_reply_dir()),
                "stream_count": len(discover_input_files()),
                "qwen_enabled": ENABLE_QWEN,
                "chat_model": CHAT_MODEL,
                "max_reply_chars": MAX_REPLY_CHARS,
                "max_prompt_candidates": MAX_PROMPT_CANDIDATES,
                "prompt_candidates_unlimited": MAX_PROMPT_CANDIDATES == 0,
                "generation_temperature": GENERATION_TEMPERATURE,
                "candidate_match_retries": CANDIDATE_MATCH_RETRIES,
                "model_min_input_chars": MODEL_MIN_INPUT_CHARS,
                "model_max_buffer_chars": MODEL_MAX_BUFFER_CHARS,
                "model_reply_guarantee_interval": MODEL_REPLY_GUARANTEE_INTERVAL,
                "cls_memory": {
                    **initialize_cls_memory(),
                    "model": CLS_MODEL,
                    "revision": CLS_REVISION,
                    "device": CLS_DEVICE,
                    "offline": CLS_OFFLINE,
                    "max_turns_per_stream": CLS_MAX_TURNS,
                    "active_streams": CLS_CACHE.session_count()
                    if CLS_CACHE is not None
                    else 0,
                    "top_k": MEMORY_TOP_K,
                    "recent_k": MEMORY_RECENT_K,
                    "max_chars": MEMORY_MAX_CHARS,
                },
                "style_weights": STYLE_WEIGHTS,
                "style_bag_size": sum(STYLE_WEIGHTS.values()),
                "generation_cooldown_seconds": GENERATION_COOLDOWN_SECONDS,
                "multi_output_repeat_probability": MULTI_OUTPUT_REPEAT_PROBABILITY,
                "account_modes": sorted(ACCOUNT_MODES),
                "default_account_mode": DEFAULT_ACCOUNT_MODE,
                "inference_runtime": get_inference_runtime(),
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


if __name__ == "__main__":
    get_input_dir().mkdir(parents=True, exist_ok=True)
    get_reply_dir().mkdir(parents=True, exist_ok=True)
    status = candidate_search.refresh(force=True)
    print("Flask live_stream_llm API started", flush=True)
    print(f"Cached replies: {status['cached_reply_count']}", flush=True)
    print(f"Candidate revision: {status['candidate_revision']}", flush=True)
    print(f"Embedding model: {candidate_search.health()['embedding_model']}", flush=True)
    print(f"Qwen enabled/model: {ENABLE_QWEN}/{CHAT_MODEL}", flush=True)
    cls_status = initialize_cls_memory()
    print(
        f"CLS memory enabled/ready: {USE_CLS_MEMORY}/{cls_status['ready']}",
        flush=True,
    )
    if cls_status.get("error"):
        print(f"CLS memory warning: {cls_status['error']}", flush=True)
    runtime = initialize_inference_runtime() if ENABLE_QWEN else get_inference_runtime()
    print(
        "Qwen runtime: profile={profile}, num_ctx={num_ctx}, preloaded={preloaded}, "
        "preload_ms={preload_elapsed_ms}".format(**runtime),
        flush=True,
    )
    if runtime.get("error"):
        print(f"Qwen preload warning: {runtime['error']}", flush=True)
    print(f"Generation cooldown: {GENERATION_COOLDOWN_SECONDS}s", flush=True)
    port = int(os.environ.get("LLM_PORT", "5002"))
    if not 1 <= port <= 65535:
        raise ValueError("LLM_PORT must be between 1 and 65535")
    app.run(host="127.0.0.1", port=port, debug=False)
