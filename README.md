# Vibebot

**Facebook Live 即時語音辨識與自動留言互動助手**

Vibebot 是一套針對 Facebook Live 場景設計的即時互動系統。系統會擷取直播音訊，透過 Google Cloud Speech-to-Text 轉成文字，再交由本機 LLM / RAG 服務產生回覆，最後由 Chrome Extension 將回覆送到 Facebook Live 留言區。

目前專案名稱以 **Vibebot** 為主，系統主要在 Windows 環境開發，並以 Python、Flask、Google Cloud Speech-to-Text、Ollama / Qwen、Supabase RAG 與 Chrome Extension 組成完整流程。

## Overview

Facebook Live 賣場、直播主互動或即時客服場景中，留言回覆需要快速理解直播語音內容並產生適當回應。Vibebot 嘗試將「直播音訊擷取」、「語音辨識」、「語意處理」、「LLM 回覆生成」與「自動留言」串成一條自動化流程。

系統流程如下：

```text
Facebook Live 音訊
-> 串流音訊擷取
-> Google Streaming Speech-to-Text
-> JSONL 結構化文字
-> LLM / RAG 回覆服務
-> Flask API
-> Chrome Extension
-> Facebook Live 自動留言
```

## Features

- **即時語音辨識**：透過 Google Cloud Speech-to-Text 將直播音訊轉為繁體中文文字。
- **直播音訊擷取**：使用 `yt-dlp` 取得直播串流音訊，並透過 `ffmpeg` 轉成 STT 可處理的 PCM 音訊格式。
- **語意結構化輸出**：將辨識結果輸出為 JSONL，包含原始文字、意圖、次要意圖、信心分數與實體資訊。
- **Speech Context 強化辨識**：支援服飾、飾品等商品情境詞庫，提高特定商品詞彙的辨識穩定度。
- **LLM / RAG 回覆生成**：使用 Ollama 與 Qwen 模型產生回覆，並可透過 Supabase 查詢相關知識內容。
- **本機 API 服務**：Flask server 提供 `/process`、`/latest_reply`、`/health` 等 API 供前端或 Extension 呼叫。
- **Chrome Extension 自動留言**：Extension 會定期輪詢本機 API，取得 AI 回覆後自動填入 Facebook Live 留言框並送出。
- **一鍵啟動器**：`fb-live-comment-extension/launcher.py` 會啟動 LLM server、STT worker，並開啟指定 Facebook Live 頁面。
- **多 Chrome Profile 支援**：啟動器中可選擇不同 Chrome profile，便於多帳號或不同直播情境操作。

## Installation

### Prerequisites

此專案目前依程式碼內容推定需要下列環境：

- Windows 10 / 11
- Python 3.10 或以上版本
- Google Chrome
- Google Cloud Speech-to-Text API 憑證
- `ffmpeg`
- `yt-dlp`
- Ollama
- Qwen 模型，例如 `qwen3:8b`
- Embedding 模型，例如 `nomic-embed-text`
- Supabase 專案與向量查詢函式

> 注意：目前 repository 沒有統一的根目錄 `requirements.txt`，只有 `stt/requirments.txt` 列出部分 STT 依賴。因此下方安裝指令是依現有程式碼整理，實際套件版本仍建議由開發者確認。

### Clone Project

```bash
git clone https://github.com/Sayhello-Bro/Vibebot.git
cd Vibebot
```

### Install Python Packages

STT 模組依賴：

```bash
pip install -r stt/requirments.txt
```

LLM / API / RAG 模組依程式碼可能還需要：

```bash
pip install flask flask-cors supabase ollama yt-dlp imageio-ffmpeg
```

## Configuration

### Google Cloud Speech-to-Text

STT 模組會尋找 `stt/service_account.json`，如果該檔案存在，程式會自動將其設為 `GOOGLE_APPLICATION_CREDENTIALS`。

也可以手動設定環境變數：

```powershell
setx GOOGLE_APPLICATION_CREDENTIALS "C:\path\to\service_account.json"
```

### ffmpeg

`stt/Facebook_stream_input.py` 會依序尋找：

1. 環境變數 `FFMPEG_PATH`
2. `stt/ffmpeg.exe`
3. 程式中寫死的本機路徑
4. `imageio_ffmpeg` 提供的 ffmpeg

建議使用環境變數指定：

```powershell
setx FFMPEG_PATH "C:\path\to\ffmpeg.exe"
```

### Ollama Models

請先確認本機 Ollama 已安裝需要的模型，例如：

```bash
ollama pull qwen3:8b
ollama pull nomic-embed-text
```

## Usage

### Option 1: 使用啟動器

```bash
python fb-live-comment-extension/launcher.py
```

啟動器會開啟圖形介面，使用者可輸入 Facebook Live URL、選擇 Chrome profile，並啟動完整流程。

啟動後程式會嘗試：

1. 啟動 LLM / RAG Flask server
2. 等待 `/health` API 回應
3. 啟動 STT worker
4. 開啟指定 Facebook Live 頁面

### Option 2: 使用已打包的 EXE 展示

若要用打包後的執行檔展示，可使用下列檔案：

```text
fb-live-comment-extension/dist/FB_Live_Auto_Comment.exe
```

同一個 `dist/` 目錄中也包含系統啟動時會用到的模組：

```text
fb-live-comment-extension/dist/launcher.exe
fb-live-comment-extension/dist/stt_worker.exe
fb-live-comment-extension/dist/llm_server.exe
```

另外，repo 中也保留個別模組的打包輸出：

```text
stt/dist/stt_worker.exe
test_LLM/test_llm_4/dist/llm_server.exe
```

展示時可以使用 `.exe` 版本；若需要說明系統如何運作，則可搭配 Python 腳本展示各模組邏輯。

### Option 3: 分開啟動 Python 模組

啟動 LLM / RAG API：

```bash
python test_LLM/test_llm_4/rag_chat.py
```

目前主要使用的 LLM / RAG 版本是：

```text
test_LLM/test_llm_4/rag_chat.py
```

啟動 STT：

```bash
python stt/WASAPI_test.py --url "https://www.facebook.com/..."
```

STT 可使用的參數包含：

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--url` | 程式內建測試 URL | Facebook Live URL |
| `--output` | `Text.jsonl` | STT 輸出的 JSONL 檔案 |
| `--stream-id` | `live_1` | 串流識別名稱 |
| `--chrome-profile` | `Default` | Chrome profile 名稱 |

### Load Chrome Extension

1. 開啟 Chrome
2. 進入 `chrome://extensions/`
3. 開啟 Developer mode
4. 選擇 Load unpacked
5. 載入 `fb-live-comment-extension/`

Extension 會在 Facebook 頁面中執行 `content.js`，並定期呼叫本機 API：

```text
POST http://127.0.0.1:5000/process
```

## API Reference

### Health Check

```http
GET http://127.0.0.1:5000/health
```

Response:

```json
{
  "status": "ok"
}
```

### Process New STT Records

```http
POST http://127.0.0.1:5000/process
```

功能：

- 讀取 `Text.jsonl` 中尚未處理的新語音文字
- 將 `raw_text` 送入 RAG / LLM
- 將產生的 `ai_reply` 寫回 JSONL
- 回傳本次處理結果

Response example:

```json
{
  "status": "success",
  "processed_count": 1,
  "results": [
    {
      "input": "我要一件黑色 XL",
      "reply": "已幫你登記黑色 XL"
    }
  ]
}
```

### Latest Reply

```http
GET http://127.0.0.1:5000/latest_reply
POST http://127.0.0.1:5000/latest_reply
```

功能：

- 呼叫 `/process`
- 回傳最新一筆 AI 回覆

## Core Modules

### STT Module

位置：

```text
stt/WASAPI_test.py
stt/Facebook_stream_input.py
```

功能：

- 從 Facebook Live URL 擷取音訊串流
- 使用 ffmpeg 轉為 16 kHz、mono、LINEAR16 PCM
- 串接 Google Streaming Speech-to-Text
- 使用 Speech Context 強化商品詞彙辨識
- 產生 JSONL 結構化輸出

### LLM / RAG Module

位置：

```text
test_LLM/test_llm_4/rag_chat.py
```

功能：

- 讀取 STT 產生的 `Text.jsonl`
- 使用 Ollama embedding 模型產生查詢向量
- 透過 Supabase RPC 查詢相近文件
- 使用 Qwen 模型生成直播回覆
- 透過 Flask API 提供結果給 Chrome Extension

### Chrome Extension Module

位置：

```text
fb-live-comment-extension/manifest.json
fb-live-comment-extension/content.js
```

功能：

- 在 Facebook 頁面載入 content script
- 每 4 秒呼叫本機 API
- 偵測 Facebook 留言輸入框
- 將 AI 回覆文字填入並送出

### Launcher Module

位置：

```text
fb-live-comment-extension/launcher.py
```

功能：

- 提供 Windows GUI 啟動介面
- 選擇 Chrome profile
- 輸入 Facebook Live URL
- 啟動 LLM server、STT worker 與 Chrome

## Project Structure

```text
Vibebot/
├── README.md                         # 原始專案說明
├── fb-live-comment-extension/         # Chrome Extension 與 Windows 啟動器
│   ├── manifest.json                  # Chrome Extension manifest
│   ├── content.js                     # 自動留言 content script
│   ├── launcher.py                    # 一鍵啟動 GUI
│   ├── server.py                      # 簡易 Flask config server
│   └── dist/                          # 展示用打包執行檔
│       ├── FB_Live_Auto_Comment.exe
│       ├── launcher.exe
│       ├── stt_worker.exe
│       └── llm_server.exe
├── stt/                               # 語音辨識與直播音訊處理
│   ├── WASAPI_test.py                 # Google Streaming STT 主程式
│   ├── Facebook_stream_input.py       # yt-dlp / ffmpeg 串流擷取
│   ├── requirments.txt                # STT 依賴套件
│   ├── speech_contexts/               # 商品詞庫與 Speech Context
│   ├── dist/stt_worker.exe            # STT worker 打包執行檔
│   └── 轉錄檔案/                      # 測試或轉錄輸出資料
├── test_LLM/                          # LLM 回覆、RAG 與測試程式
│   ├── test_llm.py                    # MongoDB / sentence-transformers 版本測試
│   ├── test_llm_4/
│   │   ├── rag_chat.py                # 目前主要使用的 Supabase + Ollama RAG API
│   │   └── dist/llm_server.exe        # LLM server 打包執行檔
│   ├── comment_gen/                   # 留言生成相關測試
│   └── test_distillation/             # distillation 測試資料與腳本
├── shiwei/                            # 其他 RAG / MongoDB 測試程式
├── unsloth_compiled_cache/            # Unsloth trainer cache
├── 0420 專題簡報.pptx                 # 專題簡報
└── 0420 測試影片.mp4                  # 測試影片
```

## Output Files

### `Text.jsonl`

STT 與 LLM 之間共用的中介檔案。STT 會寫入語音辨識結果，LLM server 會讀取新資料並附加 AI 回覆。

STT record example:

```json
{
  "time": "2026-05-31T12:00:00",
  "stream_id": "live_1",
  "raw_text": "我要一件黑色 XL",
  "intent": "PRODUCT_TRADE_ACTION",
  "secondary_intents": ["PRODUCT_COLOR_DESC", "PRODUCT_SIZE_SPEC"],
  "confidence": 0.93,
  "entities": {
    "trade_action": ["要"],
    "color": ["黑色"],
    "material": [],
    "size": ["XL"],
    "style": []
  }
}
```

LLM reply record example:

```json
{
  "timestamp": "[2026-05-31 12:00:05]",
  "raw_text": "我要一件黑色 XL",
  "ai_reply": "已幫你登記黑色 XL"
}
```

## Notes and Limitations

- 目前專案沒有根目錄 `requirements.txt`，安裝流程需要再整理。
- 程式碼中有部分本機路徑與測試 URL，正式部署前應改為設定檔或環境變數。
- 部分中文註解與文件在目前檢視環境出現亂碼，建議統一使用 UTF-8 編碼重新整理。
- `fb-live-comment-extension/content.js` 會自動送出 Facebook 留言，實際使用前應確認平台規範與使用者授權。
