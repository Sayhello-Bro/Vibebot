import json
import threading
import time
import uuid
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .reply_policy import normalize_text, should_generate_reply
from .reply_prompt import build_system_prompt, build_user_prompt
from .topic_inference import TopicInference
from .zh_tw import to_traditional_chinese
from .cls_cache import CLSCache
from .memory_reader import MemoryReader


def now_ns() -> int:
    return time.perf_counter_ns()


def ns_to_ms(value: int) -> float:
    return value / 1_000_000.0


def safe_parse_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None


class LiveReplyEngine:
    def __init__(
        self,
        embedder,
        llm_client,
        example_store,
        embedding_model: str = "nomic-embed-text:latest",
        reply_model: str = "qwen3:8b",
        topic_model: Optional[str] = None,
        style_id: str = "warm",
        reply_cooldown_ms: int = 6000,
        max_context_turns: int = 6,
        top_k: int = 5,
        cls_encoder=None,
        cls_cache=None,
        memory_reader=None,
        session_id: Optional[str] = None,
        reply_max_tokens: int = 256,
        topic_max_tokens: int = 192,
    ):
        if max_context_turns < 1:
            raise ValueError("max_context_turns must be positive")
        if reply_max_tokens < 1 or topic_max_tokens < 1:
            raise ValueError("Output token limits must be positive")
        self.reply_max_tokens = reply_max_tokens
        if cls_encoder is None and (cls_cache is not None or memory_reader is not None):
            raise ValueError("CLS memory requires a CLS encoder")
        self._turn_lock = threading.RLock()
        self.session_id = session_id or uuid.uuid4().hex
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("A nonempty session_id is required")
        self.cls_encoder = cls_encoder
        self.cls_cache = None
        if cls_encoder is not None:
            self.cls_cache = cls_cache if cls_cache is not None else CLSCache(cls_encoder.spec)
        if self.cls_cache is not None and self.cls_cache.spec != cls_encoder.spec:
            raise ValueError("CLS cache and encoder do not match")
        if self.cls_cache is not None and self.cls_cache.count(self.session_id):
            raise ValueError("Use a new session ID; resuming sessions is not implemented")
        self.memory_reader = memory_reader or MemoryReader()
        self.embedder = embedder
        self.llm_client = llm_client
        self.example_store = example_store
        self.embedding_model = embedding_model
        self.reply_model = reply_model
        self.topic_model = topic_model or reply_model
        self.style_id = style_id
        self.max_context_turns = max_context_turns
        self.top_k = top_k
        self.recent_turns: List[dict] = []
        self.topic_inferer = TopicInference(
            embedder=self.embedder,
            llm_client=self.llm_client,
            embedding_model=self.embedding_model,
            topic_model=self.topic_model,
            max_tokens=topic_max_tokens,
        )
        self.topic_state = {
            "label": "尚未確定",
            "summary": "尚未確定",
            "confidence": 0.0,
            "keywords": [],
            "source": "init",
            "candidates": [],
        }

    def process_turn(self, speaker_text: str, options: Optional[dict] = None) -> Dict[str, Any]:
        # One engine represents one active stream; serialize its state changes.
        with self._turn_lock:
            return self._process_turn(speaker_text, options)

    def start_session(self, session_id: Optional[str] = None):
        """Begin a fresh stream without deleting vectors of earlier streams."""
        with self._turn_lock:
            next_id = session_id or uuid.uuid4().hex
            if not isinstance(next_id, str) or not next_id.strip():
                raise ValueError("A nonempty session_id is required")
            if next_id == self.session_id or (
                self.cls_cache is not None and self.cls_cache.count(next_id)
            ):
                raise ValueError("Use a new session ID; resuming sessions is not implemented")
            self.session_id = next_id
            self.recent_turns.clear()
            self.topic_state = {
                "label": "尚未確定", "summary": "尚未確定", "confidence": 0.0,
                "keywords": [], "source": "init", "candidates": [],
            }
            return next_id

    def _process_turn(self, speaker_text: str, options: Optional[dict] = None) -> Dict[str, Any]:
        options = options or {}
        start_ns = now_ns()
        normalized_text = normalize_text(speaker_text)
        min_retrieval_similarity = options.get("minRetrievalSimilarity", 0.72)
        live_memory = []
        cls_ms = 0.0
        memory_read_ms = 0.0
        memory_meta = {
            "enabled": self.cls_encoder is not None,
            "mode": "cls_retrieval" if self.cls_encoder is not None else "disabled",
            "sessionId": self.session_id,
            "cacheCount": self.cls_cache.count(self.session_id) if self.cls_cache is not None else 0,
            "historyScanned": 0,
            "retrieved": [],
        }
        if not normalized_text:
            total_ms = ns_to_ms(now_ns() - start_ns)
            return {
                "shouldReply": False, "reason": "empty_text", "gateScore": -99,
                "reply": "", "retrievedExamples": [], "topic": self.topic_state,
                "memory": memory_meta, "responseTimeMs": total_ms,
                "timings": {"clsMs": 0.0, "memoryReadMs": 0.0, "totalMs": total_ms},
            }

        if self.cls_encoder is not None:
            cls_start = now_ns()
            current_cls = self.cls_encoder.encode(normalized_text)
            cls_ms = ns_to_ms(now_ns() - cls_start)
            memory_start = now_ns()
            # Persist before any downstream embedding, classification or chat
            # can fail. observe() returns a snapshot excluding the current turn.
            current, history = self.cls_cache.observe(
                self.session_id, normalized_text, current_cls,
                self.cls_encoder.spec, turn_id=options.get("turnId"),
            )
            live_memory = self.memory_reader.read(current, history)
            memory_read_ms = ns_to_ms(now_ns() - memory_start)
            memory_meta.update({
                "turnId": current.turn_id,
                "sequence": current.sequence,
                "encoder": asdict(self.cls_encoder.spec),
                "cacheCount": self.cls_cache.count(self.session_id),
                "historyScanned": len(history),
                "retrieved": live_memory,
            })

        gate = should_generate_reply(
            normalized_text,
            min_score=options.get("minScore", 2),
        )
        if not gate["should_reply"]:
            # CLS storage/read has already completed. Preserve the sentence,
            # but do not spend two more model calls on a rejected utterance.
            self._remember_turn(normalized_text, None)
            total_ms = ns_to_ms(now_ns() - start_ns)
            return {
                "shouldReply": False,
                "reason": gate["reason"],
                "gateScore": gate["score"],
                "reply": "",
                "retrievedExamples": [],
                "topic": {**self.topic_state, "source": "unchanged_rule_skip"},
                "memory": memory_meta,
                "timings": {
                    "clsMs": cls_ms, "memoryReadMs": memory_read_ms,
                    "embedMs": 0.0, "searchMs": 0.0, "topicMs": 0.0,
                    "chatMs": 0.0, "totalMs": total_ms,
                },
                "responseTimeMs": total_ms,
            }

        embed_start_ns = now_ns()
        embeddings = self.embedder.embed(self.embedding_model, normalized_text)
        query_embedding = embeddings[0] if embeddings and isinstance(embeddings[0], list) else embeddings
        embed_ms = ns_to_ms(now_ns() - embed_start_ns)

        search_start_ns = now_ns()
        retrieved_examples = self.example_store.search_similar(
            query_embedding,
            top_k=options.get("topK", self.top_k),
            filter=options.get("filter", {}),
        )
        search_ms = ns_to_ms(now_ns() - search_start_ns)

        topic_start_ns = now_ns()
        self.topic_state = self.topic_inferer.infer(
            normalized_text,
            query_embedding,
            top_k=4,
            live_memory=live_memory,
        )
        topic_ms = ns_to_ms(now_ns() - topic_start_ns)
        # The reader already reserves bounded space for recent sentences. Do
        # not duplicate them or accidentally include later turns on a retry.
        context = [] if self.cls_encoder is not None else [
            turn["text"] for turn in self.recent_turns[-self.max_context_turns :]
        ]
        usable_examples = [
            example for example in retrieved_examples if example.get("similarity", 0.0) >= min_retrieval_similarity
        ]

        system_prompt = build_system_prompt(
            style_id=options.get("styleId", self.style_id),
            topic_summary=self.topic_state["summary"],
        )
        user_prompt = build_user_prompt(
            speaker_text=normalized_text,
            recent_context=context,
            retrieved_examples=usable_examples,
            style_id=options.get("styleId", self.style_id),
            topic_summary=self.topic_state["summary"],
            live_memory=live_memory,
        )

        chat_start_ns = now_ns()
        response = self.llm_client.chat(
            model=options.get("replyModel", self.reply_model),
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            format="json",
            options={
                "temperature": options.get("temperature", 0.5),
                "top_p": options.get("topP", 0.9),
                "num_predict": options.get("maxReplyTokens", self.reply_max_tokens),
            },
        )
        chat_ms = ns_to_ms(now_ns() - chat_start_ns)

        parsed = safe_parse_json(response.get("content", ""))
        if (
            not isinstance(parsed, dict)
            or not isinstance(parsed.get("shouldReply"), bool)
            or not isinstance(parsed.get("reply"), str)
        ):
            # A token cap can truncate JSON. Never publish raw JSON/analysis as
            # a reply, and never auto-retry an expensive generation in this path.
            parsed = {
                "shouldReply": False,
                "reply": "",
                "reason": "invalid_model_response",
                "focus": [],
            }

        should_reply = bool(parsed.get("shouldReply"))
        reply = parsed.get("reply", "").strip() if should_reply else ""
        reply = to_traditional_chinese(reply)
        reason = to_traditional_chinese(str(parsed.get("reason") or gate["reason"]))
        focus = parsed.get("focus", [])
        if isinstance(focus, str):
            focus = to_traditional_chinese(focus)
        elif isinstance(focus, list):
            focus = [to_traditional_chinese(item) if isinstance(item, str) else item for item in focus]

        self._remember_turn(normalized_text, reply or None)

        total_ms = ns_to_ms(now_ns() - start_ns)

        return {
            "shouldReply": should_reply,
            "reason": reason,
            "gateScore": gate["score"],
            "reply": reply,
            "focus": focus,
            "retrievedExamples": usable_examples,
            "topic": self.topic_state,
            "memory": memory_meta,
            "timings": {
                "clsMs": cls_ms,
                "memoryReadMs": memory_read_ms,
                "topicMs": topic_ms,
                "embedMs": embed_ms,
                "searchMs": search_ms,
                "chatMs": chat_ms,
                "totalMs": total_ms,
            },
            "responseTimeMs": total_ms,
            "raw": response.get("raw"),
        }

    def _remember_turn(self, text: str, reply: Optional[str]):
        self.recent_turns.append({"text": text, "reply": reply, "at": time.time()})
        if len(self.recent_turns) > 30:
            self.recent_turns.pop(0)
