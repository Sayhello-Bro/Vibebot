"""Replay resolved_text records through one live session, without posting replies."""

import argparse
import csv
import json
import math
import os
import statistics
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from live_reply_bot.demo import create_engine_from_env
from live_reply_bot.style_presets import get_style_choices


DEFAULT_INPUT_DIR = Path(__file__).resolve().parent / "test_inputs"
TIMING_FIELDS = ("clsMs", "memoryReadMs", "embedMs", "searchMs", "topicMs", "chatMs", "totalMs")
CSV_FIELDS = (
    "source_index", "source_time", "resolved_text", "status", "reply",
    "elapsed_seconds", "reason", "topic_label", "topic_source", "cache_count",
    "history_scanned", *TIMING_FIELDS,
)


def select_input_path(path):
    """Choose one recording, so separate files never share a live-session cache."""
    if not path.is_dir():
        return path
    files = sorted(
        (item for item in path.iterdir()
         if item.is_file() and not item.name.startswith(".")
         and item.suffix.lower() in {".json", ".jsonl", ".txt"}),
        key=lambda item: (item.name.casefold(), item.name),
    )
    if not files:
        raise ValueError(f"資料夾內沒有可測試的檔案，請先放入 .json、.jsonl 或 .txt：{path}")
    if len(files) == 1:
        print(f"使用檔案：{files[0].name}", flush=True)
        return files[0]
    print(f"測試資料夾：{path}", flush=True)
    for index, item in enumerate(files, 1):
        print(f"  {index}. {item.name}", flush=True)
    while True:
        try:
            choice = input("請輸入要測試的檔案編號（Ctrl+C 取消）：").strip()
        except EOFError as exc:
            raise ValueError("未選擇檔案；請在終端機輸入編號，或直接指定檔案路徑") from exc
        try:
            index = int(choice)
        except ValueError:
            index = 0
        if 1 <= index <= len(files):
            return files[index - 1]
        print(f"請輸入 1～{len(files)} 的編號。", flush=True)


def load_records(path):
    """Validate the whole file before running models; preserve file order."""
    content = Path(path).read_text(encoding="utf-8-sig")
    decoder = json.JSONDecoder()
    values = []
    position = 0
    while position < len(content):
        if content[position].isspace():
            position += 1
            continue
        try:
            value, position = decoder.raw_decode(content, position)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"JSON 格式錯誤：第 {exc.lineno} 行、第 {exc.colno} 欄：{exc.msg}"
            ) from exc
        values.append(value)
    if len(values) == 1 and isinstance(values[0], list):
        values = values[0]
    if not values:
        raise ValueError("檔案沒有 JSON 紀錄")
    for index, value in enumerate(values, 1):
        if not isinstance(value, dict):
            raise ValueError(f"第 {index} 筆必須是 JSON 物件")
    return values


def latency_stats(seconds):
    if not seconds:
        return {"count": 0, "mean": None, "median": None, "p95": None, "max": None}
    ordered = sorted(seconds)
    return {
        "count": len(seconds),
        "mean": statistics.mean(seconds),
        "median": statistics.median(seconds),
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max": ordered[-1],
    }


def summarize(rows, selected_count, interrupted=False):
    groups = ("replied", "no_reply", "error", "skipped_invalid", "interrupted")
    counts = {status: sum(row["status"] == status for row in rows) for status in groups}
    return {
        "selected_records": selected_count,
        "written_records": len(rows),
        "remaining_records": selected_count - len(rows),
        "interrupted": interrupted,
        "counts": counts,
        "latency_seconds": {
            name: latency_stats([
                row["elapsed_seconds"] for row in rows if row["status"] in statuses
            ])
            for name, statuses in {
                "completed": {"replied", "no_reply"},
                "replied": {"replied"},
                "no_reply": {"no_reply"},
                "errors": {"error"},
            }.items()
        },
    }


def cache_count(engine):
    return engine.cls_cache.count(engine.session_id) if engine.cls_cache is not None else 0


def csv_row(row):
    flat = {key: row.get(key, "") for key in CSV_FIELDS}
    flat["topic_label"] = row["topic"].get("label", "")
    flat["topic_source"] = row["topic"].get("source", "")
    flat.update({key: row["timings"].get(key, "") for key in TIMING_FIELDS})
    # Transcript/model text is untrusted spreadsheet data. JSONL stays exact.
    return {
        key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
        for key, value in flat.items()
    }


def replay_records(engine, records, jsonl_file, csv_file, *, temperature=0.5):
    """No sleeps, parallel requests, retries, or resets between records."""
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    writer.writeheader()
    csv_file.flush()
    rows = []
    interrupted = False
    try:
        for index, record in enumerate(records, 1):
            text = record.get("resolved_text")
            valid = isinstance(text, str) and bool(text.strip())
            row = {
                "source_index": index,
                "source_time": record.get("time"),
                "resolved_text": text,
                "turn_id": f"replay-{engine.session_id}-{index}",
                "status": "skipped_invalid",
                "reply": "",
                "elapsed_seconds": None,
                "reason": "resolved_text 必須是非空字串",
                "topic": {}, "timings": {}, "cache_count": cache_count(engine),
                "history_scanned": None, "retrieved_memory": [],
            }
            if valid:
                print(f"\n[{index}/{len(records)}] 輸入：{text}", flush=True)
                started = time.perf_counter()
                try:
                    result = engine.process_turn(text, {"turnId": row["turn_id"], "temperature": temperature})
                    row["elapsed_seconds"] = time.perf_counter() - started
                    row["reply"] = result.get("reply", "")
                    row["reason"] = result.get("reason", "")
                    row["status"] = "replied" if row["reply"] else "no_reply"
                    if row["reason"] == "invalid_model_response":
                        row["status"] = "error"
                    row["topic"] = {
                        key: value for key, value in result.get("topic", {}).items()
                        if key in {"label", "summary", "confidence", "source"}
                    }
                    row["timings"] = {
                        key: value for key, value in result.get("timings", {}).items() if key in TIMING_FIELDS
                    }
                    memory = result.get("memory", {})
                    row["history_scanned"] = memory.get("historyScanned")
                    row["retrieved_memory"] = [
                        {key: value for key, value in item.items()
                         if key in {"turnId", "sequence", "text", "selection", "similarity", "truncated"}}
                        for item in memory.get("retrieved", [])
                    ]
                except KeyboardInterrupt:
                    interrupted = True
                    row.update(status="interrupted", reason="使用者中斷", elapsed_seconds=time.perf_counter() - started)
                except Exception as exc:
                    # Preserve both the failure and any CLS saved before it.
                    row.update(status="error", reason=f"{type(exc).__name__}: {exc}", elapsed_seconds=time.perf_counter() - started)
                row["cache_count"] = cache_count(engine)

            # Flush after each record so completed results survive later failure.
            jsonl_file.write(json.dumps(row, ensure_ascii=False) + "\n")
            jsonl_file.flush()
            writer.writerow(csv_row(row))
            csv_file.flush()
            rows.append(row)
            if row["status"] == "replied":
                print(f"回覆：{row['reply']}\n[回覆時間] {row['elapsed_seconds']:.2f} 秒", flush=True)
            elif row["status"] == "no_reply":
                print(f"[處理時間] {row['elapsed_seconds']:.2f} 秒（本次不回覆）", flush=True)
            elif row["status"] == "skipped_invalid":
                print(f"\n[{index}/{len(records)}] 略過：{row['reason']}", flush=True)
            else:
                print(f"[{row['status']}] {row['reason']}（{row['elapsed_seconds']:.2f} 秒）", flush=True)
            if interrupted:
                break
    except KeyboardInterrupt:
        interrupted = True
    return summarize(rows, len(records), interrupted)


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必須大於 0")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="依序測試 JSON／JSONL／連續 JSON 物件的 resolved_text；正式模式必須使用 MongoDB，預設使用本機 Ollama 與 CLS。")
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT_DIR,
                        help="檔案或資料夾；省略時從專案 test_inputs 資料夾選檔")
    parser.add_argument("--output-dir", type=Path, help="新建的結果資料夾；不可已存在")
    parser.add_argument("--limit", type=positive_int, help="只處理檔案最前面的 N 筆，包含無效紀錄")
    parser.add_argument("--style", choices=[item["id"] for item in get_style_choices()], default=os.environ.get("STYLE_ID", "warm"))
    parser.add_argument("--temperature", type=float, default=0.5)
    parser.add_argument("--mock", action="store_true", help="僅驗證讀檔與流程，不使用真實 CLS／Ollama，不能代表效能或品質")
    args = parser.parse_args(argv)
    if args.style not in {item["id"] for item in get_style_choices()}:
        parser.error("STYLE_ID 不是有效的風格名稱")
    if not math.isfinite(args.temperature) or args.temperature < 0:
        parser.error("temperature 必須是非負有限數值")
    try:
        input_path = select_input_path(args.input.expanduser().resolve())
        records = load_records(input_path)
    except KeyboardInterrupt:
        print("\n已取消測試。")
        return 130
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    total_records = len(records)
    records = records[:args.limit] if args.limit else records
    if not any(isinstance(row.get("resolved_text"), str) and row["resolved_text"].strip() for row in records):
        parser.error("選取範圍沒有可測試的 resolved_text")
    run_name = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    output_dir = (args.output_dir or Path(__file__).resolve().parent / "test_results" / run_name).expanduser().resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        parser.error(f"無法建立結果資料夾（不覆蓋既有資料）：{exc}")

    metadata = {
        "input_file": str(input_path), "total_records": total_records,
        "started_at": datetime.now().astimezone().isoformat(),
        "mode": "mock" if args.mock else "ollama",
        "mongo_required": not args.mock,
        "style": args.style, "temperature": args.temperature,
        "timing_note": "每筆從呼叫引擎到取得結果；不含初始化、終端輸出、檔案寫入或原始時間戳間隔。p95 使用 nearest-rank。",
    }
    print(f"共 {total_records} 筆，本次測試前 {len(records)} 筆。結果：{output_dir}", flush=True)
    if args.mock:
        print("MOCK 模式：不是真實模型，耗時與回覆不能當作效能／品質結果。", flush=True)
    started = time.perf_counter()
    try:
        engine = create_engine_from_env(
            args.style, use_ollama=not args.mock,
            use_cls_memory=False if args.mock else None,
            use_mongo=False if args.mock else None,
        )
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        metadata.update(status="initialization_failed", error=f"{type(exc).__name__}: {exc}")
        (output_dir / "summary.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"初始化失敗：{exc}\n請確認 MongoDB、Ollama 已啟動，驅動與 CLS 模型已備妥。", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    metadata.update(
        initialization_seconds=time.perf_counter() - started,
        session_id=engine.session_id,
        cls_enabled=engine.cls_encoder is not None,
        cls_encoder=asdict(engine.cls_encoder.spec) if engine.cls_encoder is not None else None,
        reply_model=engine.reply_model, topic_model=engine.topic_model,
        embedding_model=engine.embedding_model, think=getattr(engine.llm_client, "think", None),
        reply_max_tokens=engine.reply_max_tokens, topic_max_tokens=engine.topic_inferer.max_tokens,
        example_backend=engine.example_backend,
    )
    if engine.cls_encoder is None:
        print("注意：本次 CLS 記憶未啟用。", flush=True)
    with (output_dir / "results.jsonl").open("x", encoding="utf-8") as jsonl_file, (output_dir / "results.csv").open("x", encoding="utf-8-sig", newline="") as csv_file:
        summary = replay_records(engine, records, jsonl_file, csv_file, temperature=args.temperature)
    summary.update(metadata)
    summary.update(finished_at=datetime.now().astimezone().isoformat(), final_cache_count=cache_count(engine))
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = summary["counts"]
    print(f"\n完成：回覆 {counts['replied']}，不回覆 {counts['no_reply']}，錯誤 {counts['error']}，略過 {counts['skipped_invalid']}，中斷 {counts['interrupted']}。", flush=True)
    for label, key in [("有回覆", "replied"), ("不回覆", "no_reply")]:
        stats = summary["latency_seconds"][key]
        if stats["count"]:
            print(f"{label}：平均 {stats['mean']:.2f} 秒／中位數 {stats['median']:.2f} 秒／P95 {stats['p95']:.2f} 秒", flush=True)
    print(f"CLS 保存 {summary['final_cache_count']} 筆。結果：{output_dir}", flush=True)
    return 130 if summary["interrupted"] else (1 if counts["error"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
