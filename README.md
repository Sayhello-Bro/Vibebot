# FB Live Auto Comment System
Real-time Speech Recognition and Automatic Live Interaction Assistant

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)
![Chrome Extension](https://img.shields.io/badge/Chrome-Extension-green)
![License: MIT](https://img.shields.io/badge/License-MIT-green)

本專題實作一套即時直播互動系統，整合：

- Streaming Speech-to-Text（語音辨識）
- Local LLM 回覆生成
- Chrome Extension 自動留言
- Launcher.exe 一鍵啟動整合流程

系統可即時監聽直播語音內容，自動產生回覆並發布留言。

---

# 系統概述

整體流程如下：

Speech → STT → JSONL → LLM → API → Chrome Extension → Auto Comment

系統會：

1. 擷取直播聲音
2. 即時轉文字
3. 分析語意
4. 產生回覆
5. 自動發布留言

---

# 系統特色

## 即時語音辨識（Streaming STT）

支援：

- Google Streaming Speech-to-Text
- Stereo Mix / VB-Cable 音訊擷取
- interim / final 即時辨識
- 穩定句子判斷機制

---

## 語意分析模組

包含：

- 同音字修正
- 誤辨識偵測
- Intent Detection
- Entity Extraction

---

## 本地 LLM Server

功能：

- 讀取最新 JSONL 語句
- 生成對應回覆
- 提供 REST API

API：


http://127.0.0.1:5000/latest_reply


---

## Chrome Extension 自動留言

Extension 會：

- 定期呼叫 API
- 取得回覆文字
- 自動填入留言框
- 模擬 Enter 發送留言

---

## 一鍵啟動系統

執行：


FB_Live_Auto_Comment.exe


即可自動：

- 啟動 STT worker
- 啟動 LLM server
- 開啟 Chrome 直播頁面

---

# 系統架構流程


使用者啟動 exe
↓
Launcher 啟動系統模組
↓
Streaming STT
↓
句子穩定判斷
↓
語意資訊輸出 JSONL
↓
LLM Server 讀取最新語句
↓
生成回覆內容
↓
API 提供回覆
↓
Chrome Extension 輪詢 API
↓
自動留言送出


---

# 專案結構


TEST1/
├── g/
│ ├── WASAPI_test.py
│ ├── speech_contexts/
│ └── service_account.json
│
├── release/
│ ├── FB_Live_Auto_Comment.exe
│ ├── stt_worker.exe
│ ├── llm_server.exe
│ ├── test_llm.py
│ ├── stt_annotated_output.jsonl
│ └── live_transcript.txt
│
├── fb-live-comment-extension/
│ ├── manifest.json
│ ├── content.js
│ └── background.js
│
└── launcher.py


---

# 系統需求

Python 3.10+

安裝必要套件：


pip install google-cloud-speech
pip install sounddevice
pip install numpy
pip install flask


---

# 執行方式

## 方法一（推薦）

直接執行：


FB_Live_Auto_Comment.exe


系統會自動：

- 啟動 STT worker
- 啟動 LLM server
- 開啟直播頁面

---

## 方法二（開發模式）

手動執行：

啟動 STT：


python WASAPI_test.py


啟動 LLM：


python test_llm.py


載入 Chrome Extension：


Load unpacked extension


---

# API 說明

取得最新回覆：


GET http://127.0.0.1:5000/latest_reply


回傳格式：


{
"source_text": "...",
"reply": "..."
}


---

# 語音辨識流程


音訊輸入
↓
Chunk segmentation
↓
Streaming STT
↓
Sentence stabilization
↓
Homophone correction
↓
Intent detection
↓
Entity extraction
↓
JSONL output


---

# 輸出檔案說明

## live_transcript.txt

儲存最新辨識語句

範例：


今天幫我下單三件XL


---

## stt_annotated_output.jsonl

儲存語意分析結果

範例：


{
"resolved_text": "今天幫我下單三件XL",
"intent": "PRODUCT_TRADE_ACTION"
}


---

# Chrome Extension 流程


開啟直播頁面
↓
輪詢 API
↓
取得回覆文字
↓
填入留言框
↓
送出留言


---

# 系統完整流程


使用者啟動 exe
↓
系統擷取直播聲音
↓
語音轉文字
↓
語意分析
↓
LLM 生成回覆
↓
Extension 自動留言


---

# 模組分工

本系統包含四個主要模組：

Speech Recognition Module  
負責即時語音轉文字與語意輸出

LLM Server Module  
負責讀取語句並生成回覆內容

Chrome Extension Module  
負責取得回覆並發布留言

Launcher Module  
負責整合並一鍵啟動整個系統

---

# 未來改進方向

可擴充：

- 多人語者辨識
- 上下文記憶模型
- GPT-based 語意理解
- 多平台直播支援
- 智慧回覆策略優化

---

# License

MIT License
