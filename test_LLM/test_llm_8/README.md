# Live Stream Reply API（重構版）

這一版提供「預設語句／自訂語句／衝人氣語句」三個資料來源，其中衝人氣語句保存於
獨立 MongoDB。回覆模式只有手動與自動：手動模式對三類已啟用語句做向量比對，只能
逐字採用資料庫內容；自動模式把三類內容都傳給 Qwen，並允許產生資料庫以外的新留言。
需要模型的多個帳號會合併成一次 Qwen3:8b 呼叫。

## 新版前台製作規格

前台請依以下規格調整：

1. 新增「本場直播內容／商品類型」輸入欄，例如服飾、食品、美妝。建立直播時將值
   送入 `product_type`；這是分類提示，不是可以直接寫入留言的商品事實。
2. 候選管理頁分成預設、自訂與衝人氣三類：自訂語句可綁定 `account_ids`；
   衝人氣管理 `+1`、`6`、`888`、`上車` 等全體口號。自訂與衝人氣都提供
   新增、修改、啟用／停用與刪除。帳號不得輸出未啟用或未綁定給自己的自訂語句。
3. 前台只顯示「手動／自動」兩種回覆模式，不顯示舊版帳號層級的
   `qwen_only`、`hybrid`、`vector_only`。手動固定走資料庫向量搜尋，自動固定走 Qwen。
4. 前台持續把主播轉錄文字送入 `raw_text`。後端未累積滿100字時會回傳
   `model_input_pending=true`，此時前台只顯示等待狀態，不顯示觀眾留言。
5. 尺寸、顏色、價格等附加資訊不得由使用者手動填入，也不要傳 `product_context`。
   後端只採用主播本次口令附近文字及 CLS 找到的同場歷史。服飾尺寸先內建
   `S、M、L` 作為辨識參考，但主播沒有說出來時仍不可加入留言。
6. 一次模型批次會回傳全部帳號結果。`has_reply=false` 或留言為 `ignore` 時不要送出；
   `crowd_response=true` 時使用每個帳號自己的 `account_results[].reply`，不可只拿
   最外層第一筆 `crowd_token` 複製給所有帳號。
7. 每個帳號分別計算已送進模型的主播批次。若連續兩個批次沒有留言，第三次會先要求
   Qwen 必須替該帳號留言；若模型仍無法產生有效內容，才用固定短句保底回覆一次，
   來源標記為 `qwen_frequency_fallback`。之後重新計算連續未留言批次。
8. 同一場直播的所有帳號必須共用相同 `stream_id`。最後一個帳號退出後，前台呼叫
   `DELETE /stream_database`，讓後端刪除 MongoDB、CLS 記憶、文字緩衝及頻率計數。
9. 前台建議顯示：帳號、留言、風格、來源、處理秒數及是否仍在累積100字；模型輸出
   的 `evidence_text` 可放在除錯頁，不必顯示給一般使用者。

最小的模型請求資料為：

```json
{
  "stream_id": "live_001_唯一場次碼",
  "product_type": "服飾",
  "raw_text": "主播本次轉錄內容",
  "account_ids": ["account_001", "account_002"],
  "account_modes": {
    "account_001": "qwen_only",
    "account_002": "qwen_only"
  }
}
```

不可再傳入手動尺寸、顏色、價格或 `product_context`。

## 架構

```text
user_input.py :5001
  ├─ 自訂語句 CRUD → live_stream_db
  │    └─ text、enabled、account_ids、candidate_revision
  └─ 衝人氣口號 CRUD → live_stream_crowd_db
       └─ text、meaning、response_mode、crowd_revision

直播文字或 JSONL
  ↓
live_stream_llm.py :5002
  ↓
依直播的 reply_mode 分流
  ├─ manual：對已啟用的預設／自訂／衝人氣語句做向量搜尋，只採用現有內容
  └─ auto：累積滿 100 字後，把三類語句與主播內容交給 Qwen，可生成新內容
       ↓
reply_generator.py
  ├─ 主播文字不足 100 字時先按 stream_id 累積
  ├─ entities、本場最近回覆、總機器人數
  ├─ Qwen 自行判斷 reaction 或 question
  ├─ Qwen 全域衝人氣判定
  ├─ 自動模式同時接收預設、自訂與衝人氣語句
  ├─ 一般回覆要求自然友善、喜歡主播，禁止嗆聲或貶低主播
  ├─ 原文出現完全相同口號時全體回覆
  ├─ 口號附加資訊取自主播語境
  └─ 一次 Qwen 呼叫輸出所有需要模型的帳號結果
       ↓
依 reply_mode 組合最終結果
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

- `user_input.py`：管理主資料庫的自訂語句，以及獨立口號資料庫的衝人氣口號 CRUD。
- `vector_search.py`：新增；負責 MongoDB 候選快取、Ollama embedding、
  cosine similarity、權重排序與自動刷新。
- `live_stream_llm.py`：只保留輸入、流程協調、帳號分配、API 與紀錄。
- `reply_generator.py`：呼叫 Qwen3:8b，組合基礎口令與有來源的附加資訊。
- `reply_policy.py`：已移除。
- `test_reply_policy.py`：已移除，以 `test_architecture.py` 取代。
- `test_architecture.py`：離線檢查拆分架構、候選 revision 與回覆驗證。
- `test_live_stream.ps1`：整合測試直播中新增候選、自動刷新與 Qwen 回覆。
- `seed_replies_from_text.py`：只從 JSONL 的 `token/text` 匯入口號到獨立的
  `live_stream_crowd_db`，不讀一般AI留言。

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

## 直播中更新自訂語句與衝人氣口號

### 新增自訂語句

```powershell
$body = @{
    text = "這款好好看"
    weight = 1.0
    enabled = $true
    multi_output = $false
    account_ids = @("account_001", "account_002")
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

`account_ids=[]` 代表全部帳號皆可使用。手動模式會把已啟用的預設、自訂與衝人氣
語句納入向量比對；自動模式也會把三類語句放進 Qwen prompt 作為參考。

### 新增衝人氣口號（獨立資料庫）

```powershell
$body = @{
    text = "+1"
    enabled = $true
    meaning = "下單或表示想要"
    response_mode = "same"
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
    -Uri "http://127.0.0.1:5001/crowd_slogans" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

衝人氣口號存放於 `live_stream_crowd_db.crowd_slogans`，不與自訂語句混用。主播
原文出現完全相同文字時，才允許全體帳號回覆；例如口號是 `+1`，原文必須真的包含
`+1`，只有「加一、加一些、這一家」都不算。

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

自訂語句可修改文字、`enabled`、`multi_output` 或 `account_ids`。口號則使用
`PATCH /crowd_slogans/<id>` 修改文字、啟用狀態、用途及回覆方式。

### 暫時停用

```json
{"enabled": false}
```

使用 `PATCH /user_input/<id>` 傳送。停用比刪除安全，之後可再次啟用。
衝人氣口號使用相同 JSON 傳給 `PATCH /crowd_slogans/<id>`。

### 刪除

```powershell
Invoke-RestMethod -Method Delete `
    "http://127.0.0.1:5001/user_input/<MongoDB ID>"
```

衝人氣口號刪除端點為 `DELETE /crowd_slogans/<MongoDB ID>`。

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
- `reference_candidates`：本場已啟用並提供給自動模式的預設與自訂語句。
- `candidate_revision`：搜尋時使用的候選版本。
- `cache_refreshed`：這次是否自動刷新。
- `generation`：Qwen 原始輸出、驗證結果、選用候選與耗時。
- `account_results`：各帳號最後取得的留言。
- `vector_search_elapsed_ms`、`elapsed_ms`：效能時間。
- `reply_mode`：本場實際使用的 `manual` 或 `auto`。
- `account_modes`：舊版相容欄位；桌面流程由 `reply_mode` 決定。
- `reply_source`：各帳號最後採用 `fixed_vector`、`qwen`、`qwen_ignore` 或其他狀態。
- `vector_search_performed`：這次是否真的執行主播文字向量搜尋。
- `account_styles`：本句為各帳號重新抽取的風格。
- `model_input_char_count`、`model_input_pending`：模型文字累積狀態。

### 兩種回覆模式

| 模式 | Qwen | 向量搜尋 | 回覆範圍 |
|---|---:|---:|---|
| `manual` | 否 | 是 | 只使用已啟用的三類資料；排除最近 5 次普通留言，衝人氣除外 |
| `auto` | 累積滿100字後呼叫 | 否 | 參考三類語句，也可生成新的自然留言 |

未設定直播回覆模式時預設為 `manual`。自動模式的主播文字未累積滿 100 字時先等待。
舊版 API 的 `account_modes` 仍接受原有值，但桌面流程的分流由 `reply_mode` 決定。

三帳號全部由 Qwen 回覆的測試：

```powershell
.\test_three_accounts.ps1 -MaxRecords 3
```

腳本會暫時把啟用口令調整為 `+1、6、888、上車`，
完成後恢復測試前狀態。`MaxRecords` 預設為 3；若確定要處理整份
`live_001.jsonl`，使用 `-MaxRecords 0`。

五帳號全部使用 Hybrid：

```powershell
.\test_five_accounts_hybrid.ps1 -MaxRecords 3
```

### 兩種一般風格與全體衝人氣事件

| 風格 | 任務 |
|---|---|
| `reaction` | 針對主播內容表達感受、評價、理解或意願，不可只重述商品或主播原句 |
| `question` | 詢問尚未說明的資訊，或請主播展示、比較、示範及補充；不可詢問已回答的內容 |

`request` 已合併至 `question`。非衝人氣時，Qwen 會按每筆內容自行輸出
`reaction` 或 `question`，並以提問型約 1、反應型約 4 作為軟性比例目標。

`crowd_response` 是 Qwen 對整批主播內容做出的全域事件判定。主播明確要求觀眾輸入
指定口令時，Qwen 必須從啟用口令選擇 `base_token`，再視上下文加入尺寸、顏色、
價格等 `attributes`。程式只從口令附近的主播原文與 CLS 找到的同場歷史抽取可用值，
再逐帳號組合留言；有多個可靠的同類選項時會分散使用，沒有可靠值就只輸出口令。沒有加入
關鍵字硬觸發。為了不漏掉這種事件，只要場內有 `qwen_only` 或
`hybrid` 帳號，累積滿 100 字後便會呼叫 Qwen；非哄台時仍由 Hybrid 的向量結果優先。
即使 Qwen 判斷為衝人氣，`base_token` 仍須在本次主播號召中有直接證據；「加一些」、
「加一點」及 ASR 轉出的「這一家」不會被視為 `+1`。

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

測試不接受手動商品補充資料。尺寸、顏色與價格必須由主播當場說出；模型只決定這次
是否需要附加可驗證的值，不會每次強制加入。

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
$body = @{
    stream_id = "live_001"
    product_type = "服飾"
} |
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
`SIMILARITY_THRESHOLD=0.8` 才取出對應留言。衝人氣口令永遠不直接和主播發言做
向量比較。非衝人氣時向量優先；Qwen判定有衝人氣token時則覆蓋全部帳號。

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

## Qwen 自動生成與衝人氣口號

`reply_generator.py` 會把已啟用的預設／自訂語句與衝人氣語句分開放入自動模式的
prompt。一般留言同時要求自然喜歡主播、友善互動，禁止諷刺、嗆聲、貶低、質疑或
命令主播。

```text
[{"token":"+1","meaning":"下單或表示想要"},{"token":"888","meaning":"凝聚人氣"}]
```

主播原文若出現完全相同的啟用口號，該批次強制走全體衝人氣；Qwen負責判斷尺寸、
顏色、價格等附加資訊。沒有完全相同口號時，Qwen 依主播內容自行生成：

```json
{
  "crowd": false,
  "base_token": null,
  "crowd_replies": [],
  "replies": [
    {"text":"黑色好看","evidence":"這一個黑色的","style":"reaction"},
    {"text":"Ignore","evidence":"","style":"question"}
  ]
}
```

`replies` 依提示中的帳號順序對應。非Ignore的 `evidence` 必須是主播原文片段。Ollama
同時使用 JSON Schema 限制欄位型別。模型上下文中的直播歷史按主播發言合併，
每筆包含該次所有有效留言，且只傳最近兩筆。

### 一般留言與衝人氣口令分離

一般留言由模型自行生成 reaction/question。同批帳號重複及
本場最近留言仍會被拒絕。衝人氣時 `base_token` 必須是啟用口令；每個帳號的
`crowd_replies.attributes` 必須出自口令附近原文或同場 CLS 歷史，伺服器才會組成
最多7字的最終留言。沒有附加值時，全體帳號只輸出相同口令是預期行為。

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
| `MAX_PROMPT_CANDIDATES` | `0` | 傳給 Qwen 的候選上限；0 表示不限制 |
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
| `RECENT_REPLY_LIMIT` | `5` | 禁止重複的最近普通留言筆數；衝人氣語句除外 |
| `STYLE_WEIGHT_REACTION` | `4` | 反應型抽籤數 |
| `STYLE_WEIGHT_QUESTION` | `1` | 詢問型抽籤數 |
| `ENABLE_QWEN` | `true` | 是否呼叫 Qwen |
| `DEFAULT_ACCOUNT_MODE` | `qwen_only` | 舊版帳號模式相容欄位；桌面由直播 `reply_mode` 分流 |
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

腳本會在每批開始前讀取 MongoDB 中目前啟用的衝人氣口令，因此口令更新後，下一批
會自動使用新內容並記錄 `candidate_revision`。預設覆寫輸出；若要接續寫入可加
`--append`。可用 `--live-dir`、`--output` 與 `--batch-size` 指定其他位置或批次大小。

若效能不足，可以設定：

```powershell
$env:GENERATION_COOLDOWN_SECONDS = "15"
```

這會在同一直播場次成功生成後暫停 Qwen 15 秒；衝人氣判斷也會一起延後。

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

整合測試會同步四個衝人氣口令、確認直播快取自動更新、用 JSONL 呼叫Qwen，最後
恢復口令狀態並刪除本次直播資料庫。

## 設計提醒

1. 每段直播都呼叫 Qwen3:8b，延遲會遠高於 embedding 與 cosine 排序。
2. Flask 開發伺服器不適合正式高併發部署。
3. `FILE_POSITIONS`、冷卻與直播歷史目前只存在記憶體，服務重啟後會歸零。
4. 自訂語句與衝人氣口號不建立或參與 embedding；只有同場 Qwen 成功範例會進入一般留言的
   向量搜尋，避免把短口令和長主播發言直接比較。
5. 尺寸、顏色與價格不接受使用者手動補充；只有主播語境中可驗證的內容能成為附加值。
