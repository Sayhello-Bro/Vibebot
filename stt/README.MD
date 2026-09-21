# 🎧 即時直播語音辨識系統

這個專案是一個基於 Google Cloud Speech-to-Text 的即時直播語音辨識系統，主要用於商品直播語音分析與意圖抽取。

目前主要流程如下：

Facebook Live URL
→ yt-dlp 取得直播音訊來源
→ ffmpeg 轉成 PCM
→ Google Streaming STT 辨識
→ 結果寫入 JSONL
→ 由 context_loader 從 MongoDB 載入 speech_context

---

## 目前系統狀態

目前程式的實際狀態如下：

- Google STT Pipeline 保持原本的 streaming 調用方式不變
- `speech_context` 已從本地 JSON 改為 MongoDB collection 載入
- `context_loader.py` 是目前 speech context 的統一入口
- `import_contexts.py` 是用來把本地 `speech_contexts/clothing/*.json` 匯入 MongoDB 的 seed script
- `test_context_loader.py` 是用來驗證 MongoDB context 是否可讀取的測試腳本

---

## 系統需求

- Windows 10 / 11
- Python 3.13.2（此專案目前使用這個版本）
- 有網路（Google STT）
- 有可用的 MongoDB 實例
- 直播音訊來源由 `yt-dlp` 與 `ffmpeg` 取得

---

## 音訊來源與 STT 流程

目前程式使用以下流程：

1. `Facebook_stream_input.py`
   - 使用 `yt_dlp` 取得 Facebook Live 的 stream URL
2. `ffmpeg`
   - 將直播音訊轉換成 `LINEAR16 / 16kHz PCM`
3. `WASAPI_test.py`
   - 建立 Google Streaming Recognize request
   - 把音訊串流送進 Google STT
4. `context_loader.py`
   - 從 MongoDB 的 `speech_contexts` collection 讀取 context
5. `JSONL output`
   - 目前將辨識結果寫入 `.jsonl` 記錄檔

---

## Speech Context 目前的資料來源

目前 `speech_context` 已不再直接讀取本地 `speech_contexts/*.json`。

現在是透過 `context_loader.py` 由 MongoDB collection 讀取。

預設連線參數：

- `MONGO_URI = mongodb://localhost:27017`
- `MONGO_DB = stt`
- `MONGO_COLLECTION = speech_contexts`

MongoDB 文件格式目前為：

```json
{
  "product_mode": "clothing",
  "intent": "PRODUCT_COLOR_DESC",
  "boost": 20,
  "phrases": ["紅色", "藍色", "黑色"],
  "enable": true
}
```

`load_speech_contexts()` 只會讀取：

- `product_mode` 與目前設定一致的文件
- `enable = true` 的文件

---

## 專案檔案說明

### 主要程式

- `WASAPI_test.py`
  - 主流程入口
  - 會呼叫 `context_loader.load_speech_contexts()`

- `Facebook_stream_input.py`
  - 負責抓取直播音訊 URL 與 ffmpeg pipeline

- `context_loader.py`
  - 目前 speech context 的統一資料來源模組

### 資料匯入與測試

- `import_contexts.py`
  - 將本地 `speech_contexts/clothing/*.json` 匯入 MongoDB
  - 使用 `upsert`，因此可以重複執行
  - 會建立 `product_mode + enable` 的 index

- `test_context_loader.py`
  - 只測試 context loader
  - 不啟動 Google STT
  - 用來確認 MongoDB context 可否正常讀取

### 本地資料夾

- `speech_contexts/clothing/`
  - 仍保留作為匯入來源與備份
  - 現在並不是主運行時讀取來源

---

## MongoDB 設定

若你本機或遠端 MongoDB 沒有啟動，請先確認：

1. MongoDB service 已啟動
2. `MONGO_URI` 可連線
3. `stt.speech_contexts` collection 已建立資料

若需要重跑資料匯入，可使用：

```powershell
python import_contexts.py
```

若要驗證 context loader 是否可以從 MongoDB 讀到資料：

```powershell
python test_context_loader.py
```

---

## Google Cloud 憑證設定

本程式使用 Google Cloud Speech-to-Text API，因此需要正確的 Google Cloud 憑證。

推薦作法：設定環境變數：

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\完整路徑\service_account.json"
```

或將 API Key 放入專案內的 `stt_api_key.txt`。

---

## 輸出檔案

目前主要輸出如下：

1. `stt_annotated_output.jsonl`
   - 每條辨識結果一行
   - 包含原始文字、意圖、實體與 confidence 等資訊

2. `stt_error.log`
   - 發生例外時的錯誤紀錄

---

## 常見問題

### Q1：MongoDB 沒資料

請先跑：

```powershell
python import_contexts.py
```

### Q2：`test_context_loader.py` 出現連線錯誤

先確認：

- MongoDB 已啟動
- `mongodb://localhost:27017` 可連線
- `stt.speech_contexts` collection 有資料

### Q3：沒有任何辨識結果

請先確認：

- Facebook Live URL 是否可抓取
- `ffmpeg` 是否存在
- `Stereo Mix` 是否可用
- Google STT 憑證是否正確

---

## 結論

這個版本的專案目前已經完成了以下重點：

- STT pipeline 保持不變
- `speech_context` 轉為 MongoDB 讀取
- 可透過 `import_contexts.py` 做可重複執行匯入
- 可透過 `test_context_loader.py` 單獨驗證 context loading

如果要深入維護，後續可以再把 `speech_contexts` 進一步擴大為多產品模式資料庫設計。