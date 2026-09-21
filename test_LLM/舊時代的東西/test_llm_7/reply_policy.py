import re
from typing import Any

INTERACTION_PATTERNS = {
    "purchase": r"(要的|想買|要買|加一|\+1|下單|帶走|留(?:言|個)|喊\+?1|有沒有人要)",
    "question": r"([?？]|請問|想問|問一下|有沒有|是不是|多少|哪(?:個|裡|一)|誰|為什麼|怎麼|如何|要不要|可不可以|能不能|會不會|好不好|行不行|幾|什麼)",
    "product": r"(價格|價錢|優惠|特價|折扣|現貨|尺寸|大小|顏色|材質|厚度|款式|搭配|出貨|發貨|品質|適合|衣服|上衣|褲|裙|外套|鞋|包|飾品|珠寶|翡翠|寶石|鑽石)",
    "preference": r"(喜歡哪|哪個好看|哪色|什麼色|選哪|想看哪|比較喜歡)",
}
NO_REPLY_PATTERNS = {
    "explicit_no_reply": r"^\s*(?:\[?no_reply\]?|ignore|none|null)\s*$",
    "greeting_or_thanks": r"^\s*(?:謝謝|謝啦|感謝|你好|嗨|哈囉)[！!。．~～\s]*$",
    "sensitive": r"(身分證|信用卡|銀行帳號|住址|電話號碼|手機號碼|病歷|診斷|處方|政治立場)",
}
INCOMPLETE_ENDINGS = ("然後", "所以", "就是", "這個", "那個", "因為", "如果", "但是", "可是", "還有", "跟")


def _result(action: str, reason: str, category: str, confidence: float) -> dict[str, Any]:
    return {"action": action, "reason": reason, "category": category, "confidence": confidence}


def evaluate_reply_policy(text: str, intent: str | None = None,
                          secondary_intents: list[str] | None = None,
                          entities: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an explainable reply decision without calling an LLM."""
    normalized = re.sub(r"\s+", "", (text or "").strip())
    if not normalized:
        return _result("no_reply", "empty_text", "noise", 1.0)
    for reason, pattern in NO_REPLY_PATTERNS.items():
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            category = "safety" if reason == "sensitive" else "chat"
            return _result("no_reply", reason, category, 0.98)
    if len(normalized) <= 2 and not re.search(INTERACTION_PATTERNS["purchase"], normalized):
        return _result("no_reply", "too_short", "noise", 0.95)

    intent_names = {str(intent or "").upper()}
    intent_names.update(str(item).upper() for item in (secondary_intents or []))
    entity_values = "".join(str(item) for value in (entities or {}).values()
                            for item in (value if isinstance(value, list) else [value]))
    if re.search(INTERACTION_PATTERNS["purchase"], normalized):
        return _result("reply", "purchase_call_to_action", "purchase", 0.96)
    if re.search(INTERACTION_PATTERNS["preference"], normalized):
        return _result("reply", "preference_question", "preference", 0.94)

    has_question = bool(re.search(INTERACTION_PATTERNS["question"], normalized))
    has_product = bool(re.search(INTERACTION_PATTERNS["product"], normalized + entity_values))
    product_intent = any(name.startswith("PRODUCT_") for name in intent_names)
    if has_question and (has_product or product_intent):
        return _result("reply", "product_question", "product", 0.92)
    if has_question:
        return _result("reply", "direct_question", "conversation", 0.82)
    if product_intent and has_product:
        return _result("reply", "product_description", "product", 0.74)
    if normalized.endswith(INCOMPLETE_ENDINGS):
        return _result("no_reply", "incomplete_fragment", "noise", 0.86)
    if str(intent or "").upper() == "CHAT":
        return _result("no_reply", "non_interactive_chat", "chat", 0.80)
    return _result("uncertain", "no_strong_signal", "unknown", 0.50)


def should_generate_reply(text: str) -> bool:
    return evaluate_reply_policy(text)["action"] == "reply"
