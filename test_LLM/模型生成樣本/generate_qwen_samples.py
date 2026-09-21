"""Generate labelled livestream reply samples with Qwen3 through Ollama.

The script scans every ``*.jsonl`` file below ``live/``, reads the
``resolved_text`` field, sends five utterances per Qwen request, and writes one
consolidated JSONL output record for every valid input utterance.

Enabled candidate replies and their revision are reloaded from MongoDB before
each batch, so edits made by user_input.py are picked up while this script is
running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import ollama
from pymongo import MongoClient


MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.environ.get("MONGODB_DB", "live_stream_db")
DEFAULT_COLLECTION = os.environ.get(
    "MONGODB_DEFAULT_COLLECTION", "default_replies"
)
USER_COLLECTION = os.environ.get("MONGODB_USER_COLLECTION", "user_replies")
CONFIG_COLLECTION = os.environ.get("MONGODB_CONFIG_COLLECTION", "reply_config")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen3:8b")


@dataclass(frozen=True)
class SourceItem:
    item_id: str
    source_file: str
    source_line: int
    resolved_text: str


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _iter_json_values(
    file_path: Path, relative_path: str
) -> Iterable[tuple[Any, int]]:
    """Decode JSONL, pretty JSON, arrays, or adjacent JSON values.

    Some files use a ``.jsonl`` extension but pretty-print each object across
    many lines.  Parsing the complete text with ``raw_decode`` supports both
    that format and ordinary one-object-per-line JSONL.
    """

    content = file_path.read_text(encoding="utf-8-sig")
    decoder = json.JSONDecoder()
    position = 0
    length = len(content)

    while position < length:
        while position < length and (content[position].isspace() or content[position] == ","):
            position += 1
        if position >= length:
            break

        line_number = content.count("\n", 0, position) + 1
        try:
            value, end = decoder.raw_decode(content, position)
        except json.JSONDecodeError as exc:
            # Recover at the next physical line so one malformed record does
            # not prevent later valid records in the same file from loading.
            print(
                f"[略過] {relative_path}:{line_number} JSON 格式錯誤：{exc.msg}",
                file=sys.stderr,
            )
            next_line = content.find("\n", position)
            if next_line < 0:
                break
            position = next_line + 1
            continue

        yield value, line_number
        position = end


def _iter_documents(value: Any, line_number: int) -> Iterable[tuple[dict[str, Any], int]]:
    """Flatten a top-level JSON array while rejecting scalar JSON values."""

    if isinstance(value, dict):
        yield value, line_number
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item, line_number


def iter_source_items(live_dir: Path) -> Iterable[SourceItem]:
    """Yield non-blank resolved_text values in a deterministic order."""

    for file_path in sorted(live_dir.rglob("*.jsonl")):
        relative_path = file_path.relative_to(live_dir).as_posix()
        document_number = 0
        for value, value_line in _iter_json_values(file_path, relative_path):
            for document, line_number in _iter_documents(value, value_line):
                document_number += 1
                text = document.get("resolved_text")
                if not isinstance(text, str) or not text.strip():
                    print(
                        f"[略過] {relative_path}:{line_number} 沒有有效 resolved_text",
                        file=sys.stderr,
                    )
                    continue
                # Add the document number because array elements can share the
                # same starting line and item_id must always be unique.
                item_id = f"{relative_path}:{line_number}:{document_number}"
                yield SourceItem(
                    item_id=item_id,
                    source_file=relative_path,
                    source_line=line_number,
                    resolved_text=text.strip(),
                )


def batched(items: Iterable[SourceItem], size: int) -> Iterable[list[SourceItem]]:
    batch: list[SourceItem] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


class CandidateLoader:
    def __init__(self, uri: str, database: str) -> None:
        self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.db = self.client[database]
        self.client.admin.command("ping")

    def close(self) -> None:
        self.client.close()

    def load(self) -> tuple[int, list[str]]:
        revision_document = self.db[CONFIG_COLLECTION].find_one(
            {"_id": "candidate_revision"}
        )
        revision = int((revision_document or {}).get("revision", 0))

        values: list[str] = []
        seen: set[str] = set()
        for collection_name in (DEFAULT_COLLECTION, USER_COLLECTION):
            cursor = self.db[collection_name].find(
                {"enabled": True}, {"text": 1}
            )
            for document in cursor:
                text = document.get("text")
                if not isinstance(text, str):
                    continue
                text = text.strip()
                if text and text not in seen:
                    seen.add(text)
                    values.append(text)
        return revision, values


def make_prompt(items: list[SourceItem], candidates: list[str]) -> str:
    candidate_rows = "\n".join(
        f"{index}. {text}" for index, text in enumerate(candidates, start=1)
    ) or "（目前沒有候選語句）"
    input_rows = "\n".join(
        f"{index}. item_id={item.item_id}\n主播發言：{item.resolved_text}"
        for index, item in enumerate(items, start=1)
    )
    return f"""你是台灣直播聊天室的觀眾留言資料標註器。

請逐一判斷下列每一句主播發言是否適合由觀眾留言回覆：
- 適合回覆：生成一句自然、簡短的繁體中文觀眾留言。
- 不適合回覆：reply 必須精確填寫 \"Ignore\"。
- 每個輸入都必須輸出一次，不可遺漏、合併或增加項目。
- 候選語句只用來參考語氣與常見說法，不可逐字照抄。
- 不要扮演主播，不要解釋判斷理由，不要輸出 Markdown。
- candidate_reference 若有參考候選語句，填候選編號；否則填 null。

目前啟用的候選語句：
{candidate_rows}

主播發言：
{input_rows}

只輸出合法 JSON，格式必須是：
{{"results":[{{"item_id":"原始 item_id","reply":"留言或 Ignore","candidate_reference":1}}]}}
""".strip()


def response_value(response: Any, key: str, default: Any = None) -> Any:
    if isinstance(response, dict):
        return response.get(key, default)
    return getattr(response, key, default)


def parse_results(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型沒有輸出 JSON 物件")
        try:
            value = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型輸出的 JSON 無法解析：{exc}") from exc
    results = value.get("results") if isinstance(value, dict) else None
    if not isinstance(results, list):
        raise ValueError("模型輸出缺少 results 陣列")
    return [row for row in results if isinstance(row, dict)]


def call_qwen(
    items: list[SourceItem], candidates: list[str], retries: int
) -> tuple[dict[str, dict[str, Any]], str, float, dict[str, Any]]:
    expected_ids = {item.item_id for item in items}
    last_error = ""
    last_raw = ""
    total_elapsed_ms = 0.0
    last_metrics: dict[str, Any] = {}

    for attempt in range(1, retries + 2):
        started = time.perf_counter()
        response = ollama.chat(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": make_prompt(items, candidates)}],
            format="json",
            stream=False,
            think=False,
            keep_alive="10m",
            options={"temperature": 0.3, "top_p": 0.9, "num_predict": 500},
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        total_elapsed_ms += elapsed_ms
        message = response_value(response, "message", {})
        last_raw = str(response_value(message, "content", "")).strip()
        last_metrics = {
            key: response_value(response, key)
            for key in (
                "total_duration",
                "load_duration",
                "prompt_eval_duration",
                "eval_duration",
                "prompt_eval_count",
                "eval_count",
            )
            if response_value(response, key) is not None
        }
        try:
            parsed = parse_results(last_raw)
            by_id: dict[str, dict[str, Any]] = {}
            for row in parsed:
                item_id = str(row.get("item_id", ""))
                reply = row.get("reply")
                if item_id not in expected_ids or item_id in by_id:
                    continue
                if not isinstance(reply, str) or not reply.strip():
                    continue
                reply = reply.strip()
                if reply.casefold() == "ignore":
                    reply = "Ignore"
                reference = row.get("candidate_reference")
                try:
                    reference = int(reference) if reference is not None else None
                except (TypeError, ValueError):
                    reference = None
                if reference is not None and not 1 <= reference <= len(candidates):
                    reference = None
                by_id[item_id] = {
                    "reply": reply,
                    "candidate_reference": reference,
                }
            missing = expected_ids.difference(by_id)
            if not missing:
                return by_id, last_raw, round(total_elapsed_ms, 2), last_metrics
            last_error = "模型遺漏項目：" + ", ".join(sorted(missing))
        except ValueError as exc:
            last_error = str(exc)
        print(
            f"[重試 {attempt}/{retries + 1}] {last_error}", file=sys.stderr
        )

    raise RuntimeError(
        f"Qwen 在 {retries + 1} 次嘗試後仍未回傳完整批次：{last_error}; "
        f"raw_output={last_raw[:500]}"
    )


def build_parser() -> argparse.ArgumentParser:
    project_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="讀取 live/**/*.jsonl 的 resolved_text，批次呼叫 Qwen 並集中輸出 JSONL。"
    )
    parser.add_argument("--live-dir", type=Path, default=project_dir / "live")
    parser.add_argument(
        "--output", type=Path, default=project_dir / "generated_samples.jsonl"
    )
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--append", action="store_true", help="附加至現有輸出；預設會覆寫輸出檔。"
    )
    parser.add_argument(
        "--max-items", type=int, default=0, help="測試用；0 代表處理全部資料。"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size 必須至少為 1")
    if args.retries < 0:
        raise SystemExit("--retries 不可小於 0")
    if not args.live_dir.is_dir():
        raise SystemExit(f"找不到 live 資料夾：{args.live_dir}")

    source_items: Iterable[SourceItem] = iter_source_items(args.live_dir)
    if args.max_items > 0:
        from itertools import islice

        source_items = islice(source_items, args.max_items)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    loader = CandidateLoader(MONGODB_URI, MONGODB_DB)
    written = 0
    failed_batches = 0
    started_all = time.perf_counter()

    print(f"模型：{CHAT_MODEL}")
    print(f"來源：{args.live_dir.resolve()}")
    print(f"輸出：{args.output.resolve()}")
    try:
        with args.output.open(mode, encoding="utf-8", newline="\n") as output:
            for batch_number, items in enumerate(
                batched(source_items, args.batch_size), start=1
            ):
                revision, candidates = loader.load()
                batch_started_at = now_iso()
                print(
                    f"[批次 {batch_number}] {len(items)} 句，"
                    f"候選 {len(candidates)} 句，revision={revision}"
                )
                try:
                    results, raw, elapsed_ms, metrics = call_qwen(
                        items, candidates, args.retries
                    )
                    error = None
                except Exception as exc:
                    results = {}
                    raw = ""
                    elapsed_ms = 0.0
                    metrics = {}
                    error = str(exc)
                    failed_batches += 1
                    print(f"[失敗] 批次 {batch_number}：{error}", file=sys.stderr)

                completed_at = now_iso()
                per_item_ms = round(elapsed_ms / len(items), 2) if items else 0.0
                for item in items:
                    result = results.get(item.item_id)
                    reply = result["reply"] if result else None
                    reference = result.get("candidate_reference") if result else None
                    record = {
                        "item_id": item.item_id,
                        "source_file": item.source_file,
                        "source_line": item.source_line,
                        "resolved_text": item.resolved_text,
                        "action": (
                            "ignore" if reply == "Ignore" else "reply" if reply else "error"
                        ),
                        "generated_reply": reply,
                        "candidate_reference": reference,
                        "referenced_candidate_text": (
                            candidates[reference - 1]
                            if reference is not None
                            else None
                        ),
                        "candidate_revision": revision,
                        "candidate_snapshot": candidates,
                        "model": CHAT_MODEL,
                        "batch_number": batch_number,
                        "batch_size": len(items),
                        "batch_started_at": batch_started_at,
                        "generated_at": completed_at,
                        "batch_elapsed_ms": elapsed_ms,
                        "estimated_item_elapsed_ms": per_item_ms,
                        "ollama_metrics": metrics,
                        "raw_model_output": raw,
                        "error": error if result is None else None,
                    }
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    output.flush()
                    written += 1
    finally:
        loader.close()

    total_seconds = time.perf_counter() - started_all
    print(
        f"完成：寫入 {written} 筆，失敗批次 {failed_batches}，"
        f"總時間 {total_seconds:.2f} 秒"
    )
    return 1 if failed_batches else 0


if __name__ == "__main__":
    raise SystemExit(main())
