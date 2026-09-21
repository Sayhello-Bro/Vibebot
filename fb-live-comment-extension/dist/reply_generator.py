import os
import re
from typing import Any

CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen3:8b")
MAX_REPLY_CHARS = int(os.environ.get("MAX_REPLY_CHARS", "7"))


def clean_generated_reply(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    text = text.strip().splitlines()[0] if text.strip() else ""
    text = re.sub(r"^(?:觀眾|回覆|留言|AI)\s*[：:]\s*", "", text).strip()
    return text.strip('「」『』"\'')


def validate_generated_reply(text: str) -> tuple[bool, str]:
    if not text:
        return False, "empty"
    if len(text) > MAX_REPLY_CHARS:
        return False, "too_long"
    if "<think>" in text.lower() or "\n" in text or "\r" in text:
        return False, "invalid_format"
    if re.search(r"(我是AI|人工智慧|身為AI|客服|主播：|觀眾：)", text, flags=re.IGNORECASE):
        return False, "wrong_role"
    return True, "ok"


def generate_live_reply(source_text: str, candidates: list[str] | None = None,
                        category: str = "unknown", entities: dict[str, Any] | None = None) -> dict[str, Any]:
    import ollama
    candidate_text = "、".join((candidates or [])[:5]) or "無"
    prompt = f"""你是台灣直播間的真實觀眾。請針對主播內容產生一則自然留言。
硬性規則：只輸出留言；使用繁體中文；整則最多 {MAX_REPLY_CHARS} 個字，標點也算；語氣口語；不可捏造價格、庫存、尺寸、優惠或商品事實；資訊不足只表達感受或互動。
分類：{category}
已知實體：{entities or {}}
主播內容：{source_text}
候選留言：{candidate_text}
留言："""
    response = ollama.chat(model=CHAT_MODEL, messages=[{"role": "user", "content": prompt}], think=False,
                           options={"temperature": 0.8, "top_p": 0.9, "num_predict": 24})
    raw = str(response.get("message", {}).get("content", ""))
    reply = clean_generated_reply(raw)
    valid, reason = validate_generated_reply(reply)
    return {"reply": reply if valid else None, "raw_output": raw, "valid": valid,
            "validation_reason": reason, "model": CHAT_MODEL, "max_reply_chars": MAX_REPLY_CHARS}
