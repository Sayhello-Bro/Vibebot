# Live Stream Reply API（重構版）

這一版把候選向量搜尋與直播流程拆開，移除規則式 `reply_policy`，並支援每個
帳號獨立選擇 `qwen_only`、`hybrid` 或 `vector_only`。需要模型的多個帳號會合併
成一次 Qwen3:8b 呼叫。

## 架構

```text
user_input.py :5001
  └─ 候選句新增／修改／停用／刪除
       ├─ 重新產生 embedding（文字改變時）
       ├─ 寫入 MongoDB
       └─ candidate_revision + 1

直播文字或 JSONL
  ↓
live_stream_llm.py :5002
  ↓
依每個 account_id 的 mode 分流
  ├─ qwen_only：讀取啟用候選作參考，不做向量搜尋
  ├─ hybrid：先累積滿 100 字，再用合併文字做向量搜尋與 Qwen 判斷
  └─ vector_only：只執行向量搜尋
       ↓
reply_generator.py
  ├─ 主播文字不足 100 字時先按 stream_id 累積
  ├─ entities、本場最近回覆、總機器人數
  ├─ 每個帳號重新抽取的三種一般風格
  ├─ Qwen 全域哄台判定
  ├─ 啟用候選句
  └─ 一次 Qwen 呼叫輸出所有需要模型的帳號結果
       ↓
依 account_id 與 mode 組合最終結果
       ↓
replies/<stream_id>_reply.txt
```

每個帳號的輸出固定占兩行。第一行只放主播發言；第二行依序放觀眾編號、當下風格、
留言與本批總處理秒數。同一段主播發言有多個帳號時，會各自形成一組兩行紀錄：

```text
這件黑色跟粉色都可以選
觀眾編號=account_001｜觀眾當下風格=reaction｜留言=黑色好看｜花費時間=12.345 秒
這件黑色跟粉色都可以選
觀眾編號=account_002｜觀眾當下風格=question｜留言=還有白色嗎｜花費時間=12.345 秒
```

多個帳號共用同一次批次推論，因此同批帳號會記錄相同的總處理時間。為了維持固定
兩行格式，主播發言或留言原本含有的換行及連續空白會在寫檔時合併成單一空白。

## 檔案

- `user_input.py`：保留原本候選句 CRUD；新增 revision 通知機制。
- `vector_search.py`：新增；負責 MongoDB 候選快取、Ollama embedding、
  cosine similarity、權重排序與自動刷新。
- `live_stream_llm.py`：只保留輸入、流程協調、帳號分配、API 與紀錄。
- `reply_generator.py`：呼叫 Qwen3:8b，並把向量候選句送入 prompt。
- `reply_policy.py`：已移除。
- `test_reply_policy.py`：已移除，以 `test_architecture.py` 取代。
- `test_architecture.py`：離線檢查拆分架構、候選 revision 與回覆驗證。
- `test_live_stream.ps1`：整合測試直播中新增候選、自動刷新與 Qwen 回覆。
- `seed_replies_from_text.py`：匯入後會增加 candidate revision。

## 安裝與模型

```powershell
python -m pip install -r requirements.txt
ollama pull nomic-embed-text
ollama pull qwen3:8b
```

### 啟用同場直播 CLS 記憶（選用）

CLS 記憶與 MongoDB 的留言範例向量是兩套獨立資料。它會逐筆保存主播原始片段的
`[CLS]`，呼叫 Qwen 時只取語意相關 2 段、最近 2 段，歷史文字最多 400 字；
不會把 CLS 數字直接交給 Qwen。相同 `stream_id` 共用一份 RAM 記憶，不同直播互不
混用，刪除直播資料庫時會一起清除。

第一次使用先安裝：

```powershell
python -m pip install -r requirements.txt -r requirements-cls.txt
$env:USE_CLS_MEMORY = "true"
$env:CLS_DEVICE = "cpu"
python live_stream_llm.py
```

第一次啟動會下載固定版本的 `BAAI/bge-small-zh-v1.5`。之後若要完全離線啟動，
可再設定 `$env:CLS_OFFLINE = "true"`。每場最多保留 500 筆，可用
`CLS_MAX_TURNS` 調整；`MEMORY_TOP_K`、`MEMORY_RECENT_K`、`MEMORY_MAX_CHARS`
分別控制相關筆數、近期筆數與歷史文字預算。

目前 CLS 套件採選用安裝；若使用 `llm_server.spec` 打包，必須另外把 PyTorch 與
Transformers 收進執行檔，建議這一階段先用 Python 啟動來測試。

五帳號 Hybrid 測試：

```powershell
.\test_five_accounts_hybrid.ps1 -MaxRecords 100
```

畫面會多顯示 CLS 保存數、歷史掃描數、實際帶入數與兩段耗時。每完成一筆就同步
寫入 `test_results/<時間_場次>/results.jsonl` 與 `results.csv`；測試結束時再建立
`summary.json`，其中包含平均、P50、P95 與最大延遲。除非使用
`-KeepLiveDatabase`，腳本結束或按 `Ctrl+C` 時仍會刪除該場 MongoDB 與 CLS 記憶。

Qwen 呼叫前也會軟性預掃描 `加`、`上車`、`扣` 與明確的數字口令，將出現次數和
附近片段標成 `attention=high/watch/none` 提醒模型。預掃描不會直接決定哄台，
因此「加上外套」「衣服扣子」不會只靠關鍵字強制全體回覆；最終哄台 token 還必須
能在本次主播原文中找到。

模型判定為哄台後，系統會再把主播命令正規化成觀眾真正要輸入的 token。例如
「幫我刷888留言」輸出 `888`、「想了解扣6」輸出 `6`、「要的加1」輸出
`+1`，避免所有帳號一起複誦「幫我」等主播用語。

MongoDB 與 Ollama 都必須先啟動。

## 啟動

第一個 PowerShell：

```powershell
cd "專案路徑"
python user_input.py
```

第二個 PowerShell：

```powershell
cd "專案路徑"
python live_stream_llm.py
```

健康檢查：

```powershell
Invoke-RestMethod "http://127.0.0.1:5001/health" |
    ConvertTo-Json -Depth 10
Invoke-RestMethod "http://127.0.0.1:5002/health" |
    ConvertTo-Json -Depth 10
```

## 直播中更新候選句

### 新增

```powershell
$body = @{
    text = "這款好好看"
    weight = 1.0
    enabled = $true
    multi_output = $false
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:5001/user_input" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

回應會包含：

```json
{
  "status": "success",
  "candidate_revision": 12,
  "item": {
    "id": "...",
    "text": "這款好好看"
  }
}
```

`live_stream_llm.py` 不必重啟。下一次向量搜尋會發現 revision 已變成 12，
並重新載入候選快取。

### 修改文字或設定

```powershell
$body = @{
    new_text = "這件好好看"
    weight = 1.5
    enabled = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Patch `
    -Uri "http://127.0.0.1:5001/user_input/<MongoDB ID>" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

修改文字會重新產生 embedding；只改 `weight`、`enabled` 或
`multi_output` 不會浪費時間重算 embedding。

### 暫時停用

```json
{"enabled": false}
```

使用 `PATCH /user_input/<id>` 傳送。停用比刪除安全，之後可再次啟用。

### 刪除

```powershell
Invoke-RestMethod -Method Delete `
    "http://127.0.0.1:5001/user_input/<MongoDB ID>"
```

### 查看 revision 與直播快取

```powershell
Invoke-RestMethod "http://127.0.0.1:5001/candidate_revision" |
    ConvertTo-Json -Depth 10

Invoke-RestMethod "http://127.0.0.1:5002/candidate_cache" |
    ConvertTo-Json -Depth 10
```

仍然保留手動強制刷新：

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:5002/reload_replies"
```

## 處理一段直播文字

```powershell
$body = @{
    raw_text = "這件黑色跟粉色你們喜歡哪一個"
    stream_id = "live_001"
    account_ids = @("viewer_001", "viewer_002")
    account_modes = @{
        viewer_001 = "qwen_only"
        viewer_002 = "hybrid"
    }
    entities = @{ color = @("黑色", "粉色") }
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:5002/match" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body)) |
    ConvertTo-Json -Depth 30
```

結果會包含：

- `candidates`：這次達到門檻的向量候選。
- `reference_candidates`：實際提供給 Qwen 作為語氣參考的啟用候選。
- `candidate_revision`：搜尋時使用的候選版本。
- `cache_refreshed`：這次是否自動刷新。
- `generation`：Qwen 原始輸出、驗證結果、選用候選與耗時。
- `account_results`：各帳號最後取得的留言。
- `vector_search_elapsed_ms`、`elapsed_ms`：效能時間。
- `account_modes`：每個帳號實際使用的模式。
- `reply_source`：各帳號最後採用 `qwen`、`qwen_ignore`、`vector` 或其他失敗狀態。
- `vector_search_performed`：這次是否真的執行主播文字向量搜尋。
- `account_styles`：本句為各帳號重新抽取的風格。
- `model_input_char_count`、`model_input_pending`：模型文字累積狀態。

### 三種帳號模式

| 模式 | Qwen | 向量搜尋 | 模型 Ignore／失敗後行為 |
|---|---:|---:|---|
| `qwen_only` | 是 | 否 | 直接 `ignore` |
| `hybrid` | 累積滿100字後呼叫 | 滿100字後執行 | 未滿100字全部等待，不回覆 |
| `vector_only` | 否 | 是 | 使用向量結果或 `ignore` |

未指定模式時使用 `DEFAULT_ACCOUNT_MODE=hybrid`，以相容舊請求。即使全部帳號都是
`qwen_only`，目前啟用的候選語句仍會作為模型的語氣參考，但不會進行 cosine
similarity，也不會直接將候選句分配給帳號。

三帳號全部由 Qwen 回覆的測試：

```powershell
.\test_three_accounts.ps1 -MaxRecords 3
```

腳本會暫時把啟用候選調整為十句測試留言，
完成後恢復測試前狀態。`MaxRecords` 預設為 3；若確定要處理整份
`live_001.jsonl`，使用 `-MaxRecords 0`。

五帳號全部使用 Hybrid：

```powershell
.\test_five_accounts_hybrid.ps1 -MaxRecords 3
```

### 兩種一般風格與全體哄台事件

| 風格 | 任務 |
|---|---|
| `reaction` | 針對主播內容表達感受、評價、理解或意願，不可只重述商品或主播原句 |
| `question` | 詢問尚未說明的資訊，或請主播展示、比較、示範及補充；不可詢問已回答的內容 |

`request` 已合併至 `question`。預設抽籤袋比例為 `reaction:question = 2:1`。
每三次分配一定正好符合比例，只隨機改變順序；
抽完會重新洗牌，因此不會因獨立亂數長期偏向某個風格。每處理一筆主播發言，所有
帳號都會繼續從該直播場次的抽籤袋取得新風格。

`crowd_response` 不參與抽籤，而是 Qwen 對整批主播內容做出的全域事件判定。主播明確
要求觀眾輸入指定字、數字、符號或短代號時，Qwen 回傳 `crowd_response=true` 與
`crowd_token`；程式會用該內容覆蓋本次所有帳號的向量及一般模型結果，讓所有人一起
哄台。沒有加入關鍵字硬過濾。為了不漏掉這種事件，只要場內有 `qwen_only` 或
`hybrid` 帳號，累積滿 100 字後便會呼叫 Qwen；非哄台時仍由 Hybrid 的向量結果優先。

### 模型輸入至少 100 字

`qwen_only` 或 `hybrid` 目前主播文字不足 100 字時，API 不會呼叫模型；Hybrid 也不會
提前執行向量搜尋或輸出候選，而是依 `stream_id` 累積文字並回傳
`reply_source=model_input_pending`。累積達 100 字後，合併文字才會同時送入向量搜尋與
`reply_generator.py`。緩衝預設最多保留最近 800 字，且只存在記憶體；
服務重啟後會消失。

## 處理 JSONL

```powershell
$body = @{
    file_path = "C:\path\to\live_001.jsonl"
    account_ids = @("viewer_001")
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:5002/process?from_start=true" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

注意：只要有 Hybrid 或 Qwen-only 帳號，累積文字達 100 字時就會呼叫 Qwen 進行
全域哄台判定。CPU 電腦仍可能耗時很久，正式直播不建議反覆用 `from_start=true`
處理整份歷史資料。

## 同場 Qwen 回覆範例學習

每場直播可設定產品種類。PowerShell 測試範例：

```powershell
.\test_live_stream.ps1 -ProductType "服飾" -MaxRecords 20
.\test_five_accounts_hybrid.ps1 -ProductType "服飾" -MaxRecords 20
```

PowerShell 每次執行時會為這次直播產生新的 `StreamId`，因此即使用同一個JSONL，
下一次新開測試也會使用新的MongoDB。同一支測試中的所有帳號共用同一個
`StreamId`。其他獨立腳本若要加入同一場直播，請傳入畫面顯示的相同ID：

```powershell
.\test_five_accounts_hybrid.ps1 -StreamId "live_001_20260826_170000_abc123" -MaxRecords 20
```

測試正常結束或按 `Ctrl+C` 進入 `finally` 時，腳本會呼叫
`DELETE /stream_database`，刪除該場測試的資料庫及場次索引。
若另一支腳本只是加入同一場直播，請在次要腳本加上 `-KeepLiveDatabase`，由主測試
結束時統一刪除，避免其中一位觀眾先離開就刪掉共用資料庫。

也可以直接呼叫直播 API：

```powershell
$body = @{ stream_id = "live_001"; product_type = "服飾" } |
    ConvertTo-Json
Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:5002/stream_product" `
    -ContentType "application/json" -Body $body
```

產品種類索引會存入主資料庫的 `stream_profiles`。每個 `stream_id` 另外建立一個
`live_reply_<stream_id>_<hash>` MongoDB資料庫，內含 `stream_meta` 與
`reply_examples`。Qwen 每個非 `Ignore` 留言必須同時逐字指出它回應的主播原文片段；
程式確認片段存在後，才儲存片段、留言、片段embedding、完整主播輸入、帳號、風格和時間。

預設有效配對未滿 `MIN_VECTOR_EXAMPLES=20` 時只使用Qwen，不做向量比對。達到門檻後，
程式把新的100字主播內容切成片段，只和該場資料庫中的舊主播片段比較；相似度達
`SIMILARITY_THRESHOLD=0.8` 才取出對應留言。固定候選語句只供Qwen參考語氣，永遠
不直接和主播發言做向量比較。非哄台時向量優先；Qwen判定有哄台token時則覆蓋全部帳號。

MongoDB 查看方式：

```javascript
use live_stream_db
db.stream_profiles.find().pretty()

// 先從PowerShell輸出的「學習資料庫」取得實際名稱，例如：
use live_reply_live_001_12345678
db.reply_examples.find(
  {},
  {product_type:1, speaker_fragment:1, reply_text:1, style:1, created_at:1}
).pretty()
```

## Qwen 如何使用候選句

`reply_generator.py` 預設把目前所有啟用候選文字放入 prompt，數量會隨MongoDB
實際候選數量變動；不再傳來源與相似度：

```text
我喜歡｜這個好
```

Qwen 一次先回傳全域哄台判定，再於一般情況替帳號回傳留言：

```json
{
  "crowd": false,
  "token": null,
  "replies": [
    {"text":"黑色好看","evidence":"這一個黑色的"},
    {"text":"Ignore","evidence":""}
  ]
}
```

`replies` 依提示中的帳號順序對應。非Ignore的 `evidence` 必須是主播原文片段。Ollama
同時使用 JSON Schema 限制欄位型別。模型上下文中的直播歷史按主播發言合併，
每筆包含該次所有有效留言，且只傳最近兩筆。

### 防止模型直接複製候選句

模型輸出後會先經過 NFKC 正規化，移除空白與標點，再和所有啟用候選句比較。
例如「我來了」、「我 來了！」與「我來了～」都視為相同。同批帳號重複及本場最近
10 個留言也會被拒絕。單筆留言過長、複製候選或重複時直接改為 `Ignore`，不再呼叫
模型重試；只有 JSON 無法解析、缺少必要結構或一般回覆數量不等於帳號數量時，才整批
重試。全體哄台事件是刻意附和，因此允許重複及原樣輸出候選句。

服務啟動時會先預載 Qwen，接著透過 Ollama 的已載入模型資訊判斷 `cpu`、`gpu` 或
`mixed`。Ollama 仍自行決定實際運算裝置；程式只依結果選擇 context 大小，混合模式
採用較保守的 CPU context。

`generation.results` 會提供 `rejection_history`、`rejected_replies`、
`attempt_count` 與 `retry_exhausted`，批次層級則提供 `retry_count`、
`attempt_metrics` 和 `temperature`。

## 環境變數

| 變數 | 預設 | 功能 |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB |
| `MONGODB_DB` | `live_stream_db` | 資料庫 |
| `MONGODB_CONFIG_COLLECTION` | `reply_config` | revision 集合 |
| `MONGODB_STREAM_COLLECTION` | `stream_profiles` | 直播產品種類 |
| `LIVE_DATABASE_PREFIX` | `live_reply` | 每場直播資料庫名稱前綴 |
| `LIVE_EXAMPLE_COLLECTION` | `reply_examples` | 每場片段／留言配對集合 |
| `EMBEDDING_MODEL` | `nomic-embed-text` | 向量模型 |
| `SIMILARITY_THRESHOLD` | `0.8` | 最低 cosine similarity |
| `MAX_CANDIDATES` | `8` | 單次最多回傳學習留言數 |
| `MAX_LEARNED_EXAMPLES` | `500` | 每次最多比較的同場學習範例 |
| `LEARNED_EXAMPLE_WEIGHT` | `1.0` | 學習範例的向量分數權重 |
| `MIN_VECTOR_EXAMPLES` | `20` | 啟用同場向量比對前的最低有效配對數 |
| `MAX_QUERY_FRAGMENTS` | `12` | 每次最多比較的當前主播片段數 |
| `MAX_PROMPT_CANDIDATES` | `0` | Qwen候選上限；0表示全部啟用候選 |
| `CHAT_MODEL` | `qwen3:8b` | 生成模型 |
| `MAX_REPLY_CHARS` | `7` | 回覆長度上限 |
| `MAX_EVIDENCE_CHARS` | `40` | Qwen引用主播原文片段上限 |
| `GENERATION_TEMPERATURE` | `0.8` | Qwen 生成隨機程度 |
| `CANDIDATE_MATCH_RETRIES` | `1` | 結構性 JSON 錯誤的整批重試次數 |
| `MODEL_KEEP_ALIVE` | `30m` | Qwen 在 Ollama 記憶體中的保留時間 |
| `CPU_NUM_CTX` | `2048` | CPU／mixed profile 的 context 大小 |
| `GPU_NUM_CTX` | `4096` | GPU profile 的 context 大小 |
| `MODEL_MIN_INPUT_CHARS` | `100` | Qwen 最低主播文字長度 |
| `MODEL_MAX_BUFFER_CHARS` | `800` | 每場模型文字緩衝上限 |
| `STYLE_WEIGHT_REACTION` | `2` | 反應型抽籤數 |
| `STYLE_WEIGHT_QUESTION` | `1` | 詢問型抽籤數 |
| `ENABLE_QWEN` | `true` | 是否呼叫 Qwen |
| `DEFAULT_ACCOUNT_MODE` | `hybrid` | 未指定帳號模式時的預設值 |
| `GENERATION_COOLDOWN_SECONDS` | `0` | 同直播生成冷卻 |
| `CANDIDATE_REVISION_CHECK_SECONDS` | `0` | revision 檢查間隔；0 表示每次搜尋檢查 |

## 離線產生 Qwen 標註樣本

`generate_qwen_samples.py` 會遞迴掃描 `live` 資料夾內所有 `.jsonl`，讀取每一行的
`resolved_text`，每五句呼叫一次 Qwen3。Qwen 必須對每句產生留言，或在不需回覆時
輸出 `Ignore`。所有結果會集中寫入 `generated_samples.jsonl`。

```powershell
cd C:\Users\KKDan\Documents\Codex\2026-08-04\new-chat\outputs\test_llm_7_refactor
python .\generate_qwen_samples.py
```

先用十筆資料測試：

```powershell
python .\generate_qwen_samples.py --max-items 10
```

腳本會在每批開始前讀取 MongoDB 中目前啟用的候選語句，因此候選內容更新後，下一批
會自動使用新內容並記錄 `candidate_revision`。預設覆寫輸出；若要接續寫入可加
`--append`。可用 `--live-dir`、`--output` 與 `--batch-size` 指定其他位置或批次大小。

若效能不足，可以設定：

```powershell
$env:GENERATION_COOLDOWN_SECONDS = "15"
```

這會在同一直播場次成功生成後暫停 Qwen 15 秒，但固定候選仍能作為備援。

## 測試

離線架構測試：

```powershell
python -m unittest test_architecture.py -v
```

啟動兩個 API 後執行整合測試：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\test_live_stream.ps1
```

整合測試會新增一個臨時候選、確認直播快取自動更新、用 JSONL 呼叫Qwen，最後
刪除臨時候選與本次直播資料庫。

## 設計提醒

1. 每段直播都呼叫 Qwen3:8b，延遲會遠高於 embedding 與 cosine 排序。
2. Flask 開發伺服器不適合正式高併發部署。
3. `FILE_POSITIONS`、冷卻與直播歷史目前只存在記憶體，服務重啟後會歸零。
4. 候選句本身只有短留言，與長主播語句做 embedding 相似度不一定代表「適合
   回答」。後續最好把候選資料改為 `trigger_text + reply_text`，搜尋 trigger，
   輸出 reply。
5. Qwen 回傳的 `selected_candidate_number` 是模型自述，不代表嚴格可驗證的因果
   解釋；可用於觀察，但不應視為模型內部推理證據。
