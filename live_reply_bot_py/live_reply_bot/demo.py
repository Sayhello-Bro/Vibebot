import json
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_reply_bot.engine import LiveReplyEngine
from live_reply_bot.example_store import InMemoryExampleStore, create_mongo_vector_store
from live_reply_bot.ollama_client import OllamaClient
from live_reply_bot.style_presets import get_style_choices, get_style_preset
from live_reply_bot.cls_encoder import CLSEncoder, DEFAULT_CLS_MODEL
from live_reply_bot.memory_reader import MemoryReader


class MockEmbedder:
    def embed(self, model: str, input_text: str):
        text = str(input_text or "")
        vec = [0.0] * 16
        for idx, ch in enumerate(text):
            vec[idx % len(vec)] += ord(ch) / 255.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [[v / norm for v in vec]]


class OllamaEmbedder:
    def __init__(self, client: OllamaClient):
        self.client = client

    def embed(self, model: str, input_text: str):
        return self.client.embed(model=model, input_text=input_text)


class MockChatClient:
    def chat(self, model, system, messages, format="json", options=None, keep_alive="5m"):
        user_message = messages[-1]["content"] if messages else ""
        if "直播內容主題分類器" in (system or "") or "候選主題" in user_message:
            speaker_line = ""
            for line in user_message.splitlines():
                if line.startswith("主播句子："):
                    speaker_line = line.replace("主播句子：", "", 1).strip()
                    break

            topic_map = [
                ("翡翠珠寶", ["翡翠", "玉", "珠寶", "緬甸"]),
                ("服飾配件", ["衣服", "胸罩", "內衣", "褲子", "裙子", "鞋子", "包包"]),
                ("家電3C", ["家電", "吸塵器", "手機", "耳機", "電視"]),
                ("保健食品", ["保健", "膠原蛋白", "益生菌", "維他命"]),
                ("美妝保養", ["面膜", "乳液", "洗面乳", "保養", "精華"]),
                ("食品飲料", ["零食", "飲料", "咖啡", "茶", "食品"]),
                ("居家生活", ["鍋具", "收納", "床墊", "枕頭", "清潔"]),
                ("玩具公仔", ["玩具", "公仔", "模型", "盲盒"]),
                ("運動戶外", ["運動", "戶外", "健身", "登山", "露營"]),
            ]

            label = "尚未確定"
            keywords = []
            for topic_label, hints in topic_map:
                if any(hint in speaker_line for hint in hints):
                    label = topic_label
                    keywords = hints[:3]
                    break

            confidence = 0.86 if label != "尚未確定" else 0.18
            return {
                "content": json.dumps(
                    {
                        "label": label,
                        "summary": label,
                        "confidence": confidence,
                        "keywords": keywords,
                        "reason": "mock_topic_classifier",
                        "source": "mock_llm_confirmed",
                    },
                    ensure_ascii=False,
                ),
                "raw": {"mocked": True, "type": "topic"},
            }

        speaker_line = ""
        for line in user_message.splitlines():
            if line.startswith("主播剛說："):
                speaker_line = line.replace("主播剛說：", "", 1).strip()
                break

        should_reply = any(
            word in speaker_line
            for word in [
                "價格",
                "優惠",
                "多少",
                "幾顆",
                "怎麼",
                "可以",
                "有沒有",
                "推薦",
                "效果",
                "尺寸",
                "運費",
                "來自",
                "高級",
                "緬甸",
                "翡翠",
                "天然",
                "正品",
                "產地",
                "材質",
            ]
        )
        cleaned = speaker_line.rstrip("。！？?!")
        reply = f"我先抓重點：{cleaned}。" if should_reply else ""

        return {
            "content": json.dumps(
                {
                    "shouldReply": should_reply,
                    "reply": reply,
                    "reason": "mock",
                    "focus": ["重點回應"] if should_reply else [],
                },
                ensure_ascii=False,
            ),
            "raw": {"mocked": True},
        }


def build_seed_examples(embedder, embedding_model: str):
    return [
        {
            "productHint": "保健食品",
            "topicHint": "膠原蛋白",
            "speakerText": "這個一天吃幾顆比較好？",
            "replyText": "通常照包裝建議量就可以，先從規定劑量開始最穩。",
            "embedding": embedder.embed(embedding_model, "這個一天吃幾顆比較好？")[0],
            "embeddingModel": embedding_model,
            "styleHint": "warm",
            "metadata": {"source": "seed"},
        },
        {
            "productHint": "家電",
            "topicHint": "吸塵器",
            "speakerText": "這台吸力夠嗎？",
            "replyText": "如果是一般居家清潔，這個吸力基本夠用，重點是機身輕不輕。",
            "embedding": embedder.embed(embedding_model, "這台吸力夠嗎？")[0],
            "embeddingModel": embedding_model,
            "styleHint": "sales",
            "metadata": {"source": "seed"},
        },
    ]


def choose_style_interactively(default_style_id="warm"):
    choices = get_style_choices()
    choice_map = {item["id"]: item for item in choices}

    print("Choose a reply style for the viewer persona:")
    for index, item in enumerate(choices, start=1):
        print(f"  {index}. {item['name']} ({item['id']})")
    print(f"Press Enter for default: {get_style_preset(default_style_id)['name']} ({default_style_id})")

    while True:
        raw = input("Style> ").strip().lower()
        if not raw:
            return default_style_id
        if raw in choice_map:
            return raw
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return choices[idx - 1]["id"]
        print("Invalid style. Please choose by number or style id.")


def build_mongo_uri_from_env():
    mongo_uri = os.environ.get("MONGO_URI")
    if mongo_uri:
        return mongo_uri

    host = os.environ.get("MONGO_HOST", "127.0.0.1")
    port = os.environ.get("MONGO_PORT", "27017")
    username = os.environ.get("MONGO_USERNAME")
    password = os.environ.get("MONGO_PASSWORD")
    auth_source = os.environ.get("MONGO_AUTH_SOURCE", "admin")

    if username and password:
        return (
            f"mongodb://{quote_plus(username)}:{quote_plus(password)}"
            f"@{host}:{port}/?authSource={quote_plus(auth_source)}"
        )

    return f"mongodb://{host}:{port}"


def display_result(result, show_meta=False):
    """Keep normal replies clean; never dump vectors or raw model thinking."""
    if show_meta:
        details = {
            key: result[key]
            for key in ("shouldReply", "reply", "reason", "gateScore", "memory", "timings")
            if key in result
        }
        details["topic"] = {
            key: value for key, value in result.get("topic", {}).items()
            if key in {"label", "summary", "confidence", "keywords", "source"}
        }
        print(json.dumps(details, ensure_ascii=False, indent=2, default=str))
    else:
        reply = result.get("reply", "")
        if reply:
            print(reply)
        # Total processing time includes CLS, retrieval, topic inference and
        # reply generation; it is not just the final model's chat duration.
        elapsed_ms = result.get("responseTimeMs")
        if elapsed_ms is None:
            elapsed_ms = result.get("timings", {}).get("totalMs")
        if elapsed_ms is not None:
            if reply:
                print(f"[回覆時間] {elapsed_ms / 1000.0:.2f} 秒")
            else:
                print(f"[處理時間] {elapsed_ms / 1000.0:.2f} 秒（本次不回覆）")


def create_engine_from_env(style_id, *, use_ollama=None, use_cls_memory=None, use_mongo=None):
    """Real runs require MongoDB; explicit mock runs never touch real data."""
    if use_ollama is None:
        mode = os.environ.get("USE_OLLAMA", "1")
        if mode not in {"0", "1"}:
            raise SystemExit("USE_OLLAMA 必須是 1（正式模式）或 0（Mock 測試）。")
        use_ollama = mode == "1"
    if use_cls_memory is None:
        use_cls_memory = os.environ.get("USE_CLS_MEMORY", "1" if use_ollama else "0") == "1"
    if use_ollama and (use_mongo is False or os.environ.get("USE_MONGO") == "0"):
        raise SystemExit("正式模式必須使用 MongoDB；請移除 USE_MONGO=0，不能改用記憶體範例庫。")
    use_mongo = bool(use_ollama)
    mongo_uri = build_mongo_uri_from_env()
    mongo_db = os.environ.get("MONGO_DB", "live_reply_bot")
    mongo_collection = os.environ.get("MONGO_COLLECTION", "reply_examples_v2")
    mongo_direct = os.environ.get("MONGO_DIRECT_CONNECTION", "1") != "0"
    embedding_model = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text:latest" if use_ollama else "mock-embed")
    reply_model = os.environ.get("REPLY_MODEL", "qwen3:8b" if use_ollama else "mock-reply")
    topic_model = os.environ.get("TOPIC_MODEL", reply_model)

    # Check the required backend before loading CLS or calling Ollama.
    if use_mongo:
        try:
            example_store = create_mongo_vector_store(
                mongo_uri=mongo_uri,
                db_name=mongo_db,
                collection_name=mongo_collection,
                direct_connection=mongo_direct,
            )
        except Exception as exc:
            # Never print a credential-bearing URI or raw driver exception.
            code = getattr(exc, "code", None)
            hint = (
                "MongoDB 拒絕授權；請在本機設定 MONGO_URI，或 MONGO_USERNAME、MONGO_PASSWORD、"
                "MONGO_AUTH_SOURCE，並確認帳號有權限存取目標資料庫。"
                if code in {13, 18} else
                "請確認 MongoDB 已啟動、連線與帳號設定正確，並已安裝 requirements-mongo.txt。"
            )
            raise SystemExit(
                f"MongoDB 初始化失敗（{type(exc).__name__}）；正式模式已停止，不會改用記憶體範例庫。"
                + hint
            ) from None
    else:
        example_store = InMemoryExampleStore()

    if use_ollama:
        thinking = os.environ.get("OLLAMA_THINK", "0").lower()
        if thinking not in {"0", "1", "auto"}:
            raise ValueError("OLLAMA_THINK must be 0, 1 or auto")
        llm_client = OllamaClient(think=None if thinking == "auto" else thinking == "1")
        embedder = OllamaEmbedder(llm_client)
    else:
        llm_client = MockChatClient()
        embedder = MockEmbedder()

    cls_encoder = None
    memory_reader = None
    if use_cls_memory:
        print("Loading actual CLS encoder (first run may download the model)...")
        cls_encoder = CLSEncoder(
            model_id=os.environ.get("CLS_MODEL", DEFAULT_CLS_MODEL),
            revision=os.environ.get("CLS_REVISION"),
            cache_dir=os.environ.get("CLS_MODEL_CACHE"),
            device=os.environ.get("CLS_DEVICE", "cpu"),
            local_files_only=os.environ.get("CLS_OFFLINE") == "1",
        )
        memory_reader = MemoryReader(
            top_k=int(os.environ.get("MEMORY_TOP_K", "4")),
            recent_k=int(os.environ.get("MEMORY_RECENT_K", "4")),
            max_chars=int(os.environ.get("MEMORY_MAX_CHARS", "4000")),
        )

    seed_examples = build_seed_examples(embedder, embedding_model)
    if use_mongo:
        inserted = example_store.seed_if_empty(seed_examples)
        example_count = example_store.count()
        print(f"Mongo backend: {mongo_db}.{mongo_collection} ({example_count} examples; seeded {inserted})", flush=True)
    else:
        for example in seed_examples:
            example_store.insert_example(example)
        example_count = len(example_store.items)
        print("MOCK 模式：使用記憶體範例庫，不會連線 MongoDB。", flush=True)

    engine = LiveReplyEngine(
        embedder=embedder,
        llm_client=llm_client,
        example_store=example_store,
        embedding_model=embedding_model,
        reply_model=reply_model,
        topic_model=topic_model,
        style_id=style_id,
        top_k=3,
        cls_encoder=cls_encoder,
        memory_reader=memory_reader,
        session_id=os.environ.get("LIVE_SESSION_ID"),
        reply_max_tokens=int(os.environ.get("REPLY_MAX_TOKENS", "256")),
        topic_max_tokens=int(os.environ.get("TOPIC_MAX_TOKENS", "192")),
    )
    engine.example_backend = {
        "type": "mongodb" if use_mongo else "memory_mock",
        "database": mongo_db if use_mongo else None,
        "collection": mongo_collection if use_mongo else None,
        "count_at_startup": example_count,
    }
    return engine


def main():
    show_meta = os.environ.get("SHOW_META") == "1"
    selected_style_id = choose_style_interactively(os.environ.get("STYLE_ID", "warm"))
    engine = create_engine_from_env(selected_style_id)
    cls_encoder = engine.cls_encoder
    use_cls_memory = cls_encoder is not None
    backend = engine.example_backend

    print("Live Reply Bot interactive mode")
    print(f"Current style: {get_style_preset(selected_style_id)['name']} ({selected_style_id})")
    print("Type a livestream sentence and press Enter.")
    print("Commands: /exit, /quit, /style <id>, /style, /new, /memory, /debug on|off")
    print("Formal mode requires Ollama and MongoDB; USE_OLLAMA=0 is mock-only.")
    print(f"Live session: {engine.session_id}")
    if cls_encoder is not None:
        print(f"CLS memory: {cls_encoder.spec.model_id}, {cls_encoder.spec.dimension} dimensions")
        print("Every confirmed nonempty input is cached; generation receives retrieved text, not vectors.")
    else:
        print("CLS memory: DISABLED. Mock embeddings are not CLS; set USE_CLS_MEMORY=1 to enable.")
    if backend["type"] == "mongodb":
        print(f"Mongo collection: {backend['database']}.{backend['collection']}")
    else:
        print("Mongo collection: disabled (explicit mock test)")
    print("")

    while True:
        try:
            turn = input("> ").strip()
        except EOFError:
            print("")
            break

        if not turn:
            continue

        if turn in {"/exit", "/quit"}:
            break

        if turn in {"/debug on", "/debug off"}:
            show_meta = turn == "/debug on"
            print("除錯資訊已開啟。" if show_meta else "已切換為顯示回覆與總耗時。")
            continue

        if turn == "/new":
            print(f"New live session: {engine.start_session()}")
            continue

        if turn == "/memory":
            count = engine.cls_cache.count(engine.session_id) if engine.cls_cache is not None else 0
            print(f"CLS memory enabled={use_cls_memory}, session={engine.session_id}, cached={count}")
            continue

        if turn.startswith("/style"):
            parts = turn.split(maxsplit=1)
            if len(parts) == 1:
                selected_style_id = choose_style_interactively(selected_style_id)
            else:
                requested = parts[1].strip().lower()
                if requested in {item["id"] for item in get_style_choices()}:
                    selected_style_id = requested
                elif requested.isdigit():
                    choices = get_style_choices()
                    idx = int(requested)
                    if 1 <= idx <= len(choices):
                        selected_style_id = choices[idx - 1]["id"]
                    else:
                        print("Unknown style.")
                        continue
                else:
                    print("Unknown style.")
                    continue

            engine.style_id = selected_style_id
            print(f"Switched to: {get_style_preset(selected_style_id)['name']} ({selected_style_id})")
            print("")
            continue

        try:
            result = engine.process_turn(turn, {"styleId": selected_style_id})
        except (ValueError, RuntimeError) as exc:
            print(f"Turn failed: {exc}")
            print("If CLS storage already succeeded, that input remains in this session's cache.")
            continue
        display_result(result, show_meta=show_meta)


if __name__ == "__main__":
    main()
