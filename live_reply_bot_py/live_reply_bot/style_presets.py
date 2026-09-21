STYLE_PRESETS = {
    "warm": {
        "id": "warm",
        "name": "親切觀眾型",
        "instruction": "語氣自然、親切，像直播間裡會接話的觀眾。可以稱讚、附和、好奇，但不要像客服或銷售員。",
    },
    "curious": {
        "id": "curious",
        "name": "好奇提問型",
        "instruction": "語氣像會追問細節的觀眾，常用簡短提問或好奇接話。自然、輕鬆，不要像銷售話術。",
    },
    "chill": {
        "id": "chill",
        "name": "輕鬆吐槽型",
        "instruction": "語氣輕鬆、口語化，可以稍微吐槽或感嘆，但不要冒犯，也不要像銷售或客服。",
    },
    "short": {
        "id": "short",
        "name": "簡短附和型",
        "instruction": "語氣很短，像聊天室裡隨手回一句的觀眾。以 1 句為主，最多 2 句。",
    },
    "sales": {
        "id": "sales",
        "name": "活潑帶貨型",
        "instruction": "保留作為舊選項，語氣有節奏，但仍要維持觀眾口氣，不要真的變成叫賣。",
    },
    "concise": {
        "id": "concise",
        "name": "簡短俐落型",
        "instruction": "語氣簡短直接，回答精煉，只保留最重要的資訊。避免寒暄太多，盡量一句到兩句完成。",
    },
}


def get_style_preset(style_id: str):
    return STYLE_PRESETS.get(style_id, STYLE_PRESETS["warm"])


def get_style_choices():
    return list(STYLE_PRESETS.values())
