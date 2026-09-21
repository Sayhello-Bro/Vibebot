import re


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def count_signals(text: str) -> int:
    normalized = normalize_text(text)
    lower = normalized.lower()
    tokens = [tok for tok in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if tok]
    sell_intent = re.search(r"(有人要|要的|加1|想要|留言|拍|帶走|現場|直播間)", normalized)
    product_description_words = [
        "來自",
        "產地",
        "高級",
        "特價",
        "拍賣",
        "大拍賣",
        "大特價",
        "跳樓",
        "清倉",
        "現貨",
        "優惠",
        "折扣",
        "超值",
        "限量",
        "正品",
        "原裝",
        "天然",
        "純",
        "精品",
        "等級",
        "材質",
        "質感",
        "設計",
        "衣服",
        "裙子",
        "褲子",
        "胸罩",
        "內衣",
        "款",
        "貨",
        "玉",
        "翡翠",
        "緬甸",
        "進口",
        "真品",
    ]

    score = 0
    if len(normalized) >= 12:
        score += 1
    if "?" in normalized or "？" in normalized:
        score += 2
    if re.search(r"(多少|怎麼|可以|有沒有|推薦|比較|優惠|下單|尺寸|成分|效果|價格|運費)", normalized):
        score += 3
    if sell_intent:
        score += 2
    if any(word in normalized for word in product_description_words):
        score += 2
    if re.search(r"(是|屬於|來自|主打|適合|很|超|非常|大特價|特價|拍賣)", normalized) and len(normalized) >= 10:
        score += 1
    if re.search(r"(太棒了|好便宜|哈哈|讚|ok|好的|收到|了解|辛苦)", lower):
        score -= 2
    if len(tokens) <= 2 and not any(word in normalized for word in product_description_words) and not sell_intent:
        score -= 2

    return score


def should_generate_reply(
    text: str,
    min_score: int = 2,
    min_length: int = 8,
):
    normalized = normalize_text(text)
    if not normalized:
        return {"should_reply": False, "reason": "empty_text", "score": -99}

    high_intent_short_form = re.search(
        r"(有人要|要的|加1|想要|留言|拍|帶走|現場|直播間|特價|拍賣|衣服|胸罩|翡翠|緬甸|高級|適合|特價|大特價|跳樓)",
        normalized,
    )

    if len(normalized) < min_length and not high_intent_short_form:
        return {"should_reply": False, "reason": "too_short", "score": -5}

    score = count_signals(normalized)
    if score < min_score:
        return {"should_reply": False, "reason": "signal_too_weak", "score": score}

    return {"should_reply": True, "reason": "ok", "score": score}


def now_ms() -> float:
    import time

    return time.time()
