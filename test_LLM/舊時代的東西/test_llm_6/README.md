# Live Stream Auto Reply API

這是一套本機直播自動留言系統。系統讀取直播語音辨識 JSONL 或接收 HTTP API 文字，先以可解釋的政策規則判斷是否適合留言，再使用 MongoDB 中的固定回覆做向量相似度比對；需要時才呼叫 Qwen3 產生自然短留言。

目前預設為混合模式（`hybrid`）：

```text
直播文字／JSONL
    ↓
優先使用 resolved_text，否則使用 raw_text
    ↓
回覆政策：reply / no_reply / uncertain
    ↓
nomic-embed-text 產生向量
    ↓
固定回覆 cosine similarity × weight 排序
    ├─ 高相似度：直接使用固定句
    ├─ 中低相似度：冷卻允許時用 Qwen3 生成
    └─ 不適合回覆或無可用結果：ignore
    ↓
依 account_ids 分配留言並寫入 replies/*.jsonl
```

## 1. 專案檔案與功能

### `README.md`

本文件，記錄整體架構、所有檔案功能、環境變數、啟動方式、API 範例、回覆模式、測試方法與常見問題。

### `live_stream_llm.py`

主要直播回覆服務，預設執行於 `http://127.0.0.1:5002`。

- 從 `inputs/*.jsonl` 增量讀取直播資料，不重複處理已讀區段。
- 支援 API 直接傳入 `raw_text`。
- JSONL 有 `resolved_text` 時優先使用修正後文字。
- 接收 `intent`、`secondary_intents`、`entities` 作為政策與生成上下文。
- 使用 `nomic-embed-text` 產生輸入向量。
- 在 Python 中計算固定回覆的 cosine similarity。
- 以 `similarity × weight` 排序候選句。
- 支援 `fixed`、`hybrid`、`generate` 三種回覆模式。
- Qwen3 成功生成後，每個直播場次冷卻 60 秒。
- 支援多個 `account_ids`，避免多帳號全部輸出相同留言。
- 將結果寫入 `replies/<stream_id>_reply.jsonl`。
- 啟動時快取 MongoDB 中 `enabled=true` 的固定回覆。

API：

| 方法 | 路徑 | 功能 |
|---|---|---|
| POST | `/match` | 直接處理一段直播文字 |
| POST | `/process` | 增量處理 JSONL 檔案 |
| GET/POST | `/latest_reply` | 處理並取得最新回覆 |
| POST | `/reload_replies` | 重新載入 MongoDB 回覆快取 |
| GET | `/streams` | 列出可用直播檔案與讀取位置 |
| GET | `/health` | 檢查 MongoDB、目錄與回覆設定 |

### `reply_policy.py`

不使用 Qwen3 的規則式回覆政策。

輸出欄位：

```json
{
  "action": "reply",
  "reason": "product_question",
  "category": "product",
  "confidence": 0.92
}
```

- `reply`：有明確直播互動訊號，可以進入固定句或生成流程。
- `no_reply`：空白、噪音、純問候感謝、敏感資料、不完整片段或一般聊天。
- `uncertain`：沒有強烈回覆訊號，也沒有明確禁止原因。

主要判斷順序：

1. 空白與 `NO_REPLY`、`ignore` 等明確忽略標記。
2. 純問候、純感謝與敏感內容。
3. 過短的 ASR 噪音。
4. `+1`、下單、想買、留言等購買行動邀請。
5. 顏色、款式等偏好問題。
6. 商品問題與一般直接提問。
7. `PRODUCT_*` 意圖和 entities 輔助判斷。
8. 未說完的片段與 `CHAT` 一般聊天。
9. 其餘標記為 `uncertain`。

### `reply_generator.py`

Qwen3 自然留言生成器。

- 預設模型為 `qwen3:8b`。
- 生成台灣直播觀眾語氣的繁體中文留言。
- 整則生成留言最多 7 字，標點也計入。
- 禁止捏造價格、庫存、尺寸與優惠。
- 移除 `觀眾：`、`留言：` 等前綴。
- 過濾 `<think>`、多行、錯誤角色與超長內容。
- 驗證失敗時由主程式降級使用固定句或 `ignore`。

只有自然生成需要 Qwen3；政策判斷與固定句選擇不使用 Qwen3。固定句向量比對仍需要 `nomic-embed-text`。

### `user_input.py`

MongoDB 回覆資料管理 API，預設執行於 `http://127.0.0.1:5001`。

- 初始化六個預設回覆。
- 新增、查詢、修改與刪除自訂回覆。
- 管理 `weight`、`enabled`、`multi_output`。
- 新增或修改文字時產生 embedding。
- 支援 Ollama 或 Sentence Transformers embedding provider。
- 預設回覆文字鎖定，但可修改權重與控制欄位。

API：

| 方法 | 路徑 | 功能 |
|---|---|---|
| GET | `/health` | 資料庫健康檢查 |
| GET | `/default_replies` | 列出預設回覆 |
| GET/POST | `/default_replies/search` | 搜尋預設回覆 |
| PATCH/PUT | `/default_replies/by_text` | 依文字更新預設回覆設定 |
| POST | `/user_input` | 新增自訂回覆 |
| GET | `/user_input` | 列出自訂回覆 |
| GET/POST | `/user_input/search` | 搜尋自訂回覆 |
| GET | `/user_input/<id>` | 依 MongoDB ID 取得回覆 |
| PATCH/PUT | `/user_input/by_text` | 依文字更新自訂回覆 |
| PATCH/PUT | `/user_input/<id>` | 依 ID 更新自訂回覆 |
| DELETE | `/user_input/by_text` | 依文字刪除自訂回覆 |
| DELETE | `/user_input/<id>` | 依 ID 刪除自訂回覆 |
| GET | `/all_replies` | 同時列出預設與自訂回覆 |

### `seed_replies_from_text.py`

- 從舊版 `Text.jsonl` 讀取 `ai_reply` 或 `reply`。
- 忽略空白、`ignore`、`NO_REPLY`、`null` 等內容。
- 回覆去重後產生 embedding。
- 寫入 MongoDB `user_replies` 集合。
- 已存在的相同文字不重複新增。

### `test_reply_policy.py`

不需要啟動 MongoDB 或 Qwen3 的 Python 單元測試，目前涵蓋：

- 購買邀請應回覆。
- 顏色偏好問題應回覆。
- `CHAT` 一般聊天不回覆。
- 敏感資料不回覆。
- Qwen 輸出清理與 7 字限制。

### `test_live_stream.ps1`

PowerShell STT JSONL 整合測試腳本。預設直接將同目錄的 `live_001.jsonl` 完整路徑傳給 `/process`，不使用 `/match` 模擬主播單句。它會檢查：

- `user_input.py` 和 `live_stream_llm.py` 健康狀態。
- 回覆快取重新載入。
- STT JSONL 是否存在且第一筆具有 `raw_text` 或 `resolved_text`。
- API 是否確實讀取指定的 JSONL 完整路徑。
- 每筆新增 STT 紀錄是否套用回覆政策。
- Qwen 生成結果是否最多 7 字。
- 同一 STT 直播檔案的 60 秒生成冷卻。
- 每次執行只處理上次檔案位置之後新增的內容。

腳本不會新增或刪除 MongoDB 資料。

### `live_001.jsonl`、`14.jsonl`

直播語音辨識與上游分析資料範例，可能包含：

- `raw_text`：原始辨識文字。
- `resolved_text`：同音字或辨識錯誤修正後文字。
- `intent`、`secondary_intents`：主要與次要意圖。
- `entities`：交易、顏色、材質、尺寸和款式等實體。
- `misrecognitions`、`homophone_resolution`：辨識錯誤紀錄。

若要由 `/process` 讀取，請放入 `inputs`，或透過 `file_path` 指定完整路徑。

### `llm_server.spec`

PyInstaller 建置設定，用於將 Python 服務打包成 Windows 執行檔。它不是日常啟動入口。

### 自動建立的目錄

- `inputs/`：直播 JSONL 輸入目錄。
- `replies/`：各直播場次的回覆 JSONL。
- `__pycache__/`：Python 快取，可忽略。

## 2. 前置需求

需要安裝並啟動：

- Python 3.10 以上版本。
- MongoDB，本機預設位址 `mongodb://localhost:27017`。
- Ollama。
- Ollama 模型 `nomic-embed-text`。
- Ollama 模型 `qwen3:8b`。

安裝 Python 套件：

```powershell
pip install flask flask-cors pymongo ollama sentence-transformers
```

下載模型：

```powershell
ollama pull nomic-embed-text
ollama pull qwen3:8b
ollama list
```

## 3. 環境變數

| 變數 | 預設值 | 用途 |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB 連線 |
| `MONGODB_DB` | `live_stream_db` | 資料庫名稱 |
| `MONGODB_DEFAULT_COLLECTION` | `default_replies` | 預設回覆集合 |
| `MONGODB_USER_COLLECTION` | `user_replies` | 自訂回覆集合 |
| `EMBEDDING_PROVIDER` | `ollama` | 回覆管理 API 的 embedding provider |
| `EMBEDDING_MODEL` | `nomic-embed-text` | 向量模型 |
| `CHAT_MODEL` | `qwen3:8b` | 自然生成模型 |
| `REPLY_MODE` | `hybrid` | `fixed`、`hybrid` 或 `generate` |
| `SIMILARITY_THRESHOLD` | `0.75` | 固定句最低候選相似度 |
| `DIRECT_REPLY_THRESHOLD` | `0.88` | hybrid 直接使用固定句的相似度 |
| `MAX_REPLY_CHARS` | `7` | Qwen 生成留言最大字數 |
| `GENERATION_COOLDOWN_SECONDS` | `60` | 每直播場次的成功生成冷卻 |
| `MULTI_OUTPUT_REPEAT_PROBABILITY` | `0.35` | 可重複句被多帳號再次使用的機率 |
| `LLM_INPUT_DIR` | `inputs` | JSONL 輸入目錄 |
| `LLM_REPLY_DIR` | `replies` | 回覆紀錄目錄 |
| `LLM_INPUT_PATTERN` | `*.jsonl` | 輸入檔案模式 |

## 4. 啟動服務

第一個 PowerShell 視窗：

```powershell
cd "C:\Users\User\Desktop\畢業專題\final_project\test_LLM\test_llm_6"
$env:REPLY_MODE = "hybrid"
$env:CHAT_MODEL = "qwen3:8b"
$env:MAX_REPLY_CHARS = "7"
$env:GENERATION_COOLDOWN_SECONDS = "60"
python user_input.py
```

第二個 PowerShell 視窗使用相同環境變數後執行：

```powershell
cd "C:\Users\User\Desktop\畢業專題\final_project\test_LLM\test_llm_6"
$env:REPLY_MODE = "hybrid"
$env:CHAT_MODEL = "qwen3:8b"
$env:MAX_REPLY_CHARS = "7"
$env:GENERATION_COOLDOWN_SECONDS = "60"
python live_stream_llm.py
```

## 5. API 測試範例

健康檢查：

```powershell
Invoke-RestMethod "http://127.0.0.1:5001/health" | ConvertTo-Json -Depth 10
Invoke-RestMethod "http://127.0.0.1:5002/health" | ConvertTo-Json -Depth 10
```

直接測試購買邀請：

```powershell
$body = @{
    raw_text = "喜歡的幫我留言+1"
    stream_id = "manual_test"
    account_ids = @("viewer_001", "viewer_002")
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:5002/match" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body)) |
    ConvertTo-Json -Depth 20
```

帶上游意圖和 entities：

```powershell
$body = @{
    raw_text = "紅色跟灰色喜歡哪一色？"
    intent = "PRODUCT_COLOR_DESC"
    entities = @{ color = @("紅色", "灰色") }
    stream_id = "manual_test"
    account_ids = @("viewer_001")
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:5002/match" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body)) |
    ConvertTo-Json -Depth 20
```

指定 JSONL 完整路徑：

```powershell
$body = @{
    file_path = "C:\Users\User\Desktop\畢業專題\final_project\test_LLM\test_llm_6\live_001.jsonl"
    account_ids = @("viewer_001")
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:5002/process?from_start=true" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body)) |
    ConvertTo-Json -Depth 20
```

`from_start=true` 會從頭處理整份檔案，可能呼叫大量 embedding，正式使用前請留意資料筆數。

## 6. 執行自動測試

先啟動 MongoDB、Ollama、`user_input.py` 和 `live_stream_llm.py`，再開第三個 PowerShell。以下指令會直接讀取專案同目錄的 `live_001.jsonl`；第一次啟動服務時，檔案位置從 0 開始，因此會處理整份檔案：

```powershell
cd "C:\Users\User\Desktop\畢業專題\final_project\test_LLM\test_llm_6"
Set-ExecutionPolicy -Scope Process Bypass
.\test_live_stream.ps1
```

之後 STT 繼續把新資料追加到同一個檔案時，再執行相同指令只會處理新增加的行。`live_stream_llm.py` 會在記憶體內保存各檔案的讀取位置。

強制重新從檔案第一行測試：

```powershell
.\test_live_stream.ps1 -FromStart
```

`live_001.jsonl` 筆數較多，`-FromStart` 會對整份檔案重新執行 embedding 和政策判斷，請勿在正式直播進行中頻繁使用。

指定另一個 STT JSONL：

```powershell
.\test_live_stream.ps1 `
    -JsonlPath "D:\stt_output\live_002.jsonl" `
    -AccountIds @("viewer_001", "viewer_002")
```

指定不同 API 位址與 JSONL：

```powershell
.\test_live_stream.ps1 `
    -JsonlPath "C:\path\to\stt_live.jsonl" `
    -ReplyApiBase "http://127.0.0.1:5002" `
    -DataApiBase "http://127.0.0.1:5001"
```

離線單元測試：

```powershell
python -m unittest test_reply_policy.py -v
```

## 7. 回覆模式

### `fixed`

只使用政策與固定回覆，不使用 Qwen3。需要 `nomic-embed-text`。

### `hybrid`（推薦）

- 政策允許回覆。
- 最高相似度達 `DIRECT_REPLY_THRESHOLD` 時直接用固定句。
- 未達直接門檻時，在冷卻允許的情況呼叫 Qwen3。
- Qwen 失敗則降級到固定句或 `ignore`。

### `generate`

政策為 `reply` 或 `uncertain` 時優先生成；仍保留固定句作為失敗備援。不建議未經測試直接用於正式直播。

## 8. 多帳號、權重與重複輸出

- 每個候選分數為 `similarity × weight`。
- `enabled=false` 的句子不載入直播回覆快取。
- `multi_output=false`：同一批帳號不可重複使用同一句。
- `multi_output=true`：依 `MULTI_OUTPUT_REPEAT_PROBABILITY` 決定是否重複。
- Qwen 每次只生成一個候選，不會為每個帳號個別呼叫模型。
- 修改 MongoDB 回覆後要呼叫 `POST /reload_replies`。

## 9. 匯入舊回覆

```powershell
$env:SEED_TEXT_JSONL = "C:\path\to\Text.jsonl"
python seed_replies_from_text.py
Invoke-RestMethod -Method Post "http://127.0.0.1:5002/reload_replies"
```

## 10. 常見問題

### 固定句判斷需要 Qwen3 嗎？

不需要。政策使用 Python 規則，固定句使用 `nomic-embed-text` 與 cosine similarity；只有自然生成需要 Qwen3。

### Qwen 回覆超過 7 字會怎樣？

結果會被判定無效，不直接截斷，然後降級使用固定句或 `ignore`。

### 冷卻期間是否完全不回覆？

不會。冷卻只禁止同直播場次再次自然生成，固定句仍可正常使用。

### 修改回覆後為什麼直播服務沒有更新？

直播服務使用記憶體快取，請執行：

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:5002/reload_replies"
```

### MongoDB 或 Qwen 失敗會怎樣？

- MongoDB／embedding 無法使用時，固定句比對可能無法執行並由 API 回報錯誤。
- Qwen 失敗不會中止整個服務，會降級使用固定候選或 `ignore`。
