# Live Stream Auto Reply Scripts

這個專案分成兩支 Python 腳本：

- `user_input.py`：管理使用者自定義回覆語句，提供 CRUD API，資料存進 MongoDB。
- `live_stream_llm.py`：讀取 `Text.jsonl` 的 `raw_text`，用向量相似度和權重挑選回覆，結果寫入 `Reply.jsonl`。

MongoDB 位置：

```text
Database: live_stream_db
Collection: user_input
```

## 前置條件

請先確認已安裝並啟動：

1. Python
2. MongoDB
3. Ollama
4. `nomic-embed-text`

安裝 Python 套件：

```powershell
pip install -r requirements.txt
```

下載 Ollama embedding 模型：

```powershell
ollama pull nomic-embed-text
```

預設 MongoDB 位置：

```text
mongodb://localhost:27017
```

如果 MongoDB 位置不同：

```powershell
$env:MONGODB_URI = "mongodb://localhost:27017"
```

## 1. 測試 user_input.py

開啟第一個 PowerShell，在專案資料夾啟動：

```powershell
python user_input.py
```

預設 API：

```text
http://127.0.0.1:5001
```

健康檢查：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:5001/health"
```

### 新增 10 筆語句

請開啟第二個 PowerShell 執行。這種寫法可以避免中文 `text` 變成問號。

```powershell
$sentences = @(
    @{ text = "主播加油"; weight = 1.0; enabled = $true },
    @{ text = "這個看起來很划算"; weight = 1.1; enabled = $true },
    @{ text = "請問尺寸怎麼選"; weight = 1.2; enabled = $true },
    @{ text = "有黑色可以選嗎"; weight = 1.0; enabled = $true },
    @{ text = "我要加一"; weight = 1.4; enabled = $true },
    @{ text = "材質摸起來舒服嗎"; weight = 1.0; enabled = $true },
    @{ text = "現在下單有優惠嗎"; weight = 1.3; enabled = $true },
    @{ text = "可以再介紹一次嗎"; weight = 0.9; enabled = $true },
    @{ text = "這件很適合夏天"; weight = 1.0; enabled = $true },
    @{ text = "主播可以試穿看看嗎"; weight = 1.2; enabled = $true }
)

foreach ($body in $sentences) {
    Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:5001/user_input" `
        -ContentType "application/json; charset=utf-8" `
        -Body (ConvertTo-Json $body -Compress)
}
```

### 查詢

查詢全部：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:5001/user_input"
```

只查啟用中：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:5001/user_input?enabled=true"
```

透過 `text` 查詢：

```powershell
$body = @{
    text = "主播加油"
}

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:5001/user_input/search" `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Json $body -Compress)
```

模糊查詢：

```powershell
$body = @{
    text = "尺寸"
    exact = $false
}

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:5001/user_input/search" `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Json $body -Compress)
```

### 修改

透過 `text` 修改文字與權重：

```powershell
$body = @{
    text = "主播加油"
    new_text = "主播真的很會介紹"
    weight = 1.5
    enabled = $true
}

Invoke-RestMethod `
    -Method Patch `
    -Uri "http://127.0.0.1:5001/user_input/by_text" `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Json $body -Compress)
```

只調整權重：

```powershell
$body = @{
    text = "我要加一"
    weight = 2.0
}

Invoke-RestMethod `
    -Method Patch `
    -Uri "http://127.0.0.1:5001/user_input/by_text" `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Json $body -Compress)
```

停用語句：

```powershell
$body = @{
    text = "可以再介紹一次嗎"
    enabled = $false
}

Invoke-RestMethod `
    -Method Patch `
    -Uri "http://127.0.0.1:5001/user_input/by_text" `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Json $body -Compress)
```

### 刪除

```powershell
$body = @{
    text = "這件很適合夏天"
}

Invoke-RestMethod `
    -Method Delete `
    -Uri "http://127.0.0.1:5001/user_input/by_text" `
    -ContentType "application/json; charset=utf-8" `
    -Body (ConvertTo-Json $body -Compress)
```

## 2. 準備 Text.jsonl

`live_stream_llm.py` 只讀每一行 JSON 裡的 `raw_text`。

範例：

```json
{"time": "2026-05-13T23:11:47.190310", "stream_id": "live_1", "raw_text": "然後他不管很大，就是不會貼在你的，你的大腿上面1坪29的白白小白，大家11029的黑小黑大加一公斤以下哪小60到8實拿大有問題可以打上來。 一定要打大小喔。 ", "intent": "PRODUCT_TRADE_ACTION", "entities": {"trade_action": ["加一"], "color": [], "material": [], "size": [], "style": []}}
```

預設檔名：

```text
Text.jsonl
```

如果檔案位置不同：

```powershell
$env:LLM_TEXT_JSONL = "C:\path\to\Text.jsonl"
```

回覆結果預設會寫到同資料夾：

```text
Reply.jsonl
```

也可以指定位置：

```powershell
$env:LLM_REPLY_JSONL = "C:\path\to\Reply.jsonl"
```

## 3. 測試 live_stream_llm.py

重要：`live_stream_llm.py` 只會在啟動時載入 MongoDB 中 `enabled=true` 的語句。若你啟動後又新增或修改語句，請重啟 `live_stream_llm.py`，或呼叫 `/reload_replies`。

啟動前可設定門檻，預設是 `0.75`：

```powershell
$env:SIMILARITY_THRESHOLD = "0.75"
```

啟動：

```powershell
python live_stream_llm.py
```

預設 API：

```text
http://127.0.0.1:5002
```

健康檢查：

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:5002/health"
```

重新載入 MongoDB 語句快取：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5002/reload_replies"
```

處理 `Text.jsonl` 裡新增的 `raw_text`：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5002/process"
```

從檔案開頭重新測一次：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5002/process?from_start=true"
```

取得最新結果：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5002/latest_reply"
```

## 4. live_stream_llm.py 的流程

```text
啟動時從 MongoDB 載入 enabled=true 的語句與 embedding
-> 讀取 Text.jsonl 新增行
-> 只取 raw_text
-> 用 nomic-embed-text 將 raw_text 轉成 embedding
-> 與快取中的每一句語句 embedding 計算 cosine similarity
-> similarity 低於 0.75 輸出 ignore
-> similarity 超過 0.75 後，使用 similarity * weight 排序
-> 輸出最高分語句
-> 將 raw_text、reply、elapsed_ms 寫入 Reply.jsonl
```

`Reply.jsonl` 每一行會包含：

```json
{
  "timestamp": "[2026-06-14 16:30:00]",
  "raw_text": "主播發言",
  "reply": "選擇的回覆或 ignore",
  "elapsed_ms": 123.45,
  "threshold": 0.75,
  "best_similarity": 0.82,
  "selected": {
    "id": "...",
    "text": "我要加一",
    "similarity": 0.82,
    "weight": 2.0,
    "score": 1.64
  }
}
```

如果輸出是 `ignore`，代表沒有任何語句達到門檻。

## 5. 常見問題

如果 `processed_count` 是 `0`，代表這次沒有讀到新的 `raw_text`。請用 `/health` 看 `text_file` 是否是正確檔案，以及 `last_file_position` 是否已經等於檔案大小。

如果 `cached_reply_count` 是 `0`，代表 `live_stream_llm.py` 啟動時沒有從 MongoDB 載入任何啟用語句。請先確認 `user_input.py` 新增資料成功，且 `enabled=true`。

如果回覆一直是 `ignore`，可先降低門檻測試：

```powershell
$env:SIMILARITY_THRESHOLD = "0.5"
python live_stream_llm.py
```

如果中文變成問號，請使用本 README 的 `$body = @{ ... }` 加上 `ConvertTo-Json` 的 PowerShell 寫法，不要直接把 JSON 字串塞進 `-Body`。
