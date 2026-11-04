# Vibebot：Facebook 直播自動留言系統

Vibebot 是一套 Windows 桌面直播互動系統，將 Facebook Chrome 擴充功能、直播語音轉文字（STT）、MongoDB 語句資料庫、向量搜尋與本機 LLM 串接在一起。系統可以讓多個 Facebook 帳號同時參與直播，也可以讓同一個帳號分別處理多場直播。

完整的使用者操作說明請參考 [VibeBot_使用說明書.md](VibeBot_使用說明書.md)。本 README 著重於系統架構、檔案用途、資料流與開發維護方式。

## 系統功能

- 最多管理 8 個機器人帳號；每個帳號使用獨立的 Chrome 設定檔保存 Facebook 登入狀態。
- 最多管理 5 列直播網址。每列可以獨立開始、暫停、繼續與停止。
- 支援「一對多」：一個帳號同時處理多場直播；也支援「多對一」：多個帳號共同參與同一場直播。
- 每個直播列使用獨立的直播識別碼、STT 輸出、LLM 工作流程與 Chrome 直播分頁，避免不同直播的內容互相混用。
- 回覆語句分成「預設」、「自訂」與「衝人氣」三類；自訂語句及衝人氣口號可以新增、編輯、啟用、停用與刪除。
- 提供「手動」與「自動」兩種回覆模式：手動模式只使用資料庫已有語句，自動模式由 Qwen 參考語句後產生自然回覆。
- 支援最近直播主清單，輸入直播網址後會嘗試辨識直播主並保存其 Facebook 個人主頁。
- 保存直播工作階段、回覆紀錄、STT 錯誤與 LLM 處理結果，方便除錯與測試。

## 整體運作方式

```text
使用者
  │
  ▼
launcher.py（Windows 桌面介面）
  ├─ 啟動／停止各直播列的 STT 工作程序
  ├─ 為每個「帳號 × 直播列」啟動 LLM 服務
  ├─ 開啟帶有帳號、文字檔與 LLM 連接埠資訊的 Chrome 直播頁
  └─ 透過 HTTP 管理語句與回覆模式
       │
       ├─ WASAPI_test.py / STT worker
       │    Facebook 直播音訊 → yt-dlp／ffmpeg → Google STT → JSONL
       │
       ├─ user_input.py :5001
       │    預設／自訂語句與衝人氣口號的 CRUD API
       │
       └─ live_stream_llm.py :5002 起
            手動：向量搜尋已啟用語句
            自動：累積主播文字後交給 Ollama 的 qwen3:8b
                         │
                         ▼
                 Chrome extension 將回覆送回 Facebook
```

### 直播開始後的資料流

1. 使用者在桌面介面選擇機器人帳號，輸入 Facebook 直播網址並按「開始」。
2. `launcher.py` 建立該直播列的工作目錄與 `stream_id`，啟動 STT 工作程序。
3. STT 從直播取得音訊，轉成 Google Streaming Speech-to-Text 可以讀取的格式，將辨識到的主播文字逐筆寫入 JSONL。
4. LLM 服務讀取同一直播列的文字，依目前回覆模式選擇向量搜尋或 Qwen 生成回覆。
5. Chrome 擴充功能從直播頁取得主播與留言狀態，使用指定的 LLM 連接埠取得回覆並執行留言操作。
6. 回覆與工作階段資料寫入 `dist/replies/`、`dist/sessions/`；停止直播列時，該列的 STT、LLM 與 Chrome 工作程序會被關閉。

同一直播列的多個帳號共用一次 STT 輸入，但每個帳號都有自己的 LLM 服務與 Chrome 設定檔。不同直播列則完全分開處理。

## 回覆模式

| 模式 | 處理方式 | 回覆內容 |
| --- | --- | --- |
| 手動 | 將主播文字轉成向量，與已啟用的預設／自訂／衝人氣語句比對 | 只會逐字使用資料庫中已有的語句；普通留言會避免重複最近 5 次 |
| 自動 | 累積足夠主播文字後，將主播內容與三類語句交給 Ollama 的 `qwen3:8b` | 可參考資料庫語句，也可產生資料庫以外的新留言 |

衝人氣口號使用獨立資料庫。只有主播原文確實出現相符的口號時，系統才會將它視為全體帳號事件；不會只因為出現相近字詞就強制所有帳號留言。自動模式要求回覆自然、友善、像喜歡主播的觀眾，避免諷刺、貶低、命令或質疑主播。

在桌面介面切換模式後，必須按「套用」才會套用到目前直播列。自動模式需要先累積主播文字，因此啟動後不一定立即產生留言。

## 安裝與第一次使用

### 必要環境

- Windows 10／11
- Google Chrome
- MongoDB
- Ollama
- `nomic-embed-text`：手動模式的語意向量模型
- `qwen3:8b`：自動模式的回覆模型
- Google Cloud Speech-to-Text 憑證

### 安裝步驟

1. 保留整個 `Vibebot` 資料夾結構，不要單獨搬動 `dist`、`stt` 或 `test_LLM`。
2. 執行 `fb-live-comment-extension/dist/FB_Live_Environment_Setup.exe`，檢查或安裝 Chrome、MongoDB、Ollama 與 `nomic-embed-text`。
3. 在 PowerShell 補下載自動模式需要的模型：

   ```powershell
   ollama pull qwen3:8b
   ollama pull nomic-embed-text
   ```

4. 開啟 Chrome 的 `chrome://extensions`，開啟「開發人員模式」，按「載入未封裝項目」，選擇 `fb-live-comment-extension/chrome_extension`。
5. 確認每個要使用的 Chrome 設定檔都已載入並啟用 **FB Live Auto Clicker**。
6. 啟動 MongoDB 與 Ollama，再執行 `fb-live-comment-extension/dist/FB_Live_Auto_Comment.exe`。

若要以 Python 執行桌面介面，可在專案根目錄執行：

```powershell
python fb-live-comment-extension/launcher.py
```

## 使用方式

### 1. 建立機器人帳號

1. 在左上方「機器人管理」按「新增帳號」。
2. 程式會開啟對應的 Chrome 設定檔；在該視窗登入 Facebook。
3. 回到桌面介面後，勾選要參與直播的帳號。
4. 字卡顯示名稱可以修改，但只會改變介面名稱，不會改變 Facebook 帳號。

最多可建立 8 個帳號。關閉字卡不會刪除 Chrome 設定檔或 Facebook 登入資料。

### 2. 加入直播並啟動

1. 在「直播來源管理」新增直播列。
2. 輸入完整的 Facebook 直播網址。
3. 選擇要參與的機器人帳號。
4. 在右側選擇「手動」或「自動」，設定語句後按「套用」。
5. 按該直播列的「開始」。
6. 執行期間可按「暫停」；要繼續時按「繼續」。
7. 按「停止」會關閉該列的 STT、LLM 與 Chrome 工作流程，網址會恢復成可編輯狀態。

同一個帳號可以在不同列處理不同直播；同一列也可以同時勾選多個帳號。系統會以「帳號 × 直播列」分配 LLM 服務，避免帳號間共享錯誤的回覆狀態。

### 3. 管理留言語句

- **預設**：系統內建的基本語句，只能啟用或停用。
- **自訂**：使用者自行建立的語句，可以編輯文字、啟用狀態與帳號綁定。
- **衝人氣**：如 `+1`、`6`、`888`、`上車` 等口號，保存在獨立資料庫，可單獨管理。

手動模式會從已啟用語句中選擇最符合主播內容的語句；自動模式會把已啟用語句當成寫作參考。若要暫時停止某句話，建議停用而不是刪除，之後仍可恢復。

## 檔案結構與用途

```text
Vibebot/
├─ fb-live-comment-extension/       # 桌面程式、Chrome 擴充功能與 LLM 服務
│  ├─ launcher.py                    # Windows 主介面與整體流程控制
│  ├─ login.py                       # 帳號登入與啟動主介面
│  ├─ profile_cards.py               # 機器人帳號卡片與 Chrome profile 對應
│  ├─ favorite_streamers.py          # 常用直播主資料管理
│  ├─ environment_installer.py       # 執行環境檢查／安裝
│  ├─ chrome_extension/              # 應載入 Chrome 的擴充功能來源
│  │  ├─ manifest.json
│  │  ├─ background.js
│  │  └─ content.js
│  ├─ user_input.py                  # 語句與衝人氣口號 API，預設連接埠 5001
│  ├─ live_stream_llm.py             # 直播回覆 API，預設連接埠 5002
│  ├─ reply_generator.py             # Qwen prompt、回覆解析與安全規則
│  ├─ reply_policy.py                # 回覆風格與固定安全備援規則
│  ├─ vector_search.py               # Ollama embedding、候選快取與相似度搜尋
│  ├─ seed_replies_from_text.py      # 將 JSONL 口號資料匯入資料庫
│  ├─ dist/                          # 可執行檔、工作資料與執行紀錄
│  ├─ build/、build_pyinstaller/      # PyInstaller 建置中間檔
│  └─ test_*.py、test_*.js           # 桌面流程與局部邏輯測試
├─ stt/                              # 直播語音轉文字元件（本次未更動）
│  ├─ WASAPI_test.py                 # Google Streaming STT 主要入口
│  ├─ Facebook_stream_input.py       # 取得直播音訊來源並交給 ffmpeg
│  ├─ context_loader.py              # 從 MongoDB 載入 speech context
│  ├─ import_contexts.py             # 匯入服飾等 speech context
│  ├─ speech_contexts/               # context 匯入來源與備份
│  └─ 轉錄檔案/                       # STT 轉錄 JSONL 資料
├─ test_LLM/                         # LLM 回覆服務、測試與歷史實驗
│  ├─ test_llm_8/                    # 目前桌面啟動器使用的 LLM 版本
│  ├─ 模型生成樣本/                   # 模型測試輸入與輸出
│  ├─ 舊時代的東西/                   # 歷史版本與已封存實驗
│  │  ├─ test_llm_4/
│  │  ├─ test_llm_5/
│  │  ├─ test_llm_6/
│  │  ├─ test_llm_7/
│  │  └─ test_llm_7_refactor/
│  ├─ test_llm_4/、test_llm_6/        # 原位置保留的舊資料；不會被目前啟動器使用
│  └─ comment_gen/、test_distillation/ # 回覆生成與蒸餾實驗
├─ live_reply_bot_py/                # 獨立的 CLS／直播回覆示範套件
├─ shiwei/                           # RAG 與 MongoDB 早期實驗
├─ outputs/                          # 報告、簡報或介面使用的 SVG／圖片素材
├─ VibeBot_使用說明書.md              # Windows 使用者操作手冊
└─ README.md                         # 專案架構與開發說明
```

### 主要檔案說明

#### 桌面程式與 Chrome 擴充功能

- `launcher.py`：建立桌面視窗、管理帳號卡、直播列、回覆模式、語句列表及工作程序。它會依帳號與直播列產生對應的 LLM 連接埠，並將 JSONL 與 LLM 位置傳給 Chrome 擴充功能。
- `login.py`：提供登入入口；開發時可執行 `python login.py`，打包後會啟動桌面執行檔。
- `profile_cards.py`：維護帳號卡名稱、Chrome 設定檔與帳號 ID 的對應。
- `favorite_streamers.py`：保存最近使用的直播主個人頁面，最多保留 3 筆。
- `chrome_extension/manifest.json`：Chrome 擴充功能宣告與權限。
- `chrome_extension/background.js`：擴充功能背景工作。
- `chrome_extension/content.js`：在 Facebook 直播頁尋找留言區、讀取直播資訊並執行留言操作。

#### LLM 與資料庫

- `user_input.py`：提供預設／自訂語句與衝人氣口號的查詢、新增、修改、停用與刪除 API。
- `live_stream_llm.py`：接收主播文字、管理 `stream_id`、累積模型輸入、分配帳號回覆、保存回覆紀錄並提供健康檢查。
- `vector_search.py`：使用 `nomic-embed-text` 產生 embedding，執行 cosine similarity、候選排序與快取更新。
- `reply_generator.py`：組合 Qwen prompt、要求 `reaction` 或 `question` 風格、驗證模型輸出，並避免不安全或不適合的留言。
- `reply_policy.py`：集中處理回覆風格、禁用語句與固定備援回覆。
- `seed_replies_from_text.py`：把指定 JSONL 的 `token`／`text` 匯入衝人氣口號資料庫，不應把一般 AI 回覆誤當成口號。

目前桌面啟動器使用 `test_LLM/test_llm_8/` 的 `live_stream_llm.py` 與 `user_input.py` 作為 LLM 服務來源；`test_LLM/舊時代的東西/` 只保留歷史測試與備份用途。

#### STT

STT 資料夾本次沒有移動或修改。其主要流程是：Facebook 直播網址 → `yt-dlp` 取得音訊 → `ffmpeg` 轉換 PCM → Google Streaming STT → JSONL。`context_loader.py` 從 MongoDB 的 `stt.speech_contexts` collection 載入辨識 context；`import_contexts.py` 可將 `speech_contexts/` 的 JSON 匯入 MongoDB。

#### 其他實驗資料夾

- `live_reply_bot_py/`：獨立的 Python CLS 記憶與直播回覆引擎，和 Windows Facebook 桌面流程不是同一個啟動入口。
- `shiwei/`：早期 RAG、MongoDB 與聊天實驗。
- `outputs/`：Vibebot 圖示、流程圖、封面等 SVG／圖片素材。
- `test_LLM/舊時代的東西/`：歷史版本、舊測試與 PyInstaller 產物；除非要重現舊實驗，不要把它當成目前程式來源。

## 執行資料與資料庫

### 本機檔案

- `fb-live-comment-extension/dist/sessions/`：每場直播的主播文字、工作階段與 metadata。
- `fb-live-comment-extension/dist/replies/`：各帳號或各直播列的回覆紀錄。
- `fb-live-comment-extension/dist/stt_error.log`：STT 執行錯誤。
- `%LOCALAPPDATA%/FB_Live_Auto_Comment/robot_cards.json`：機器人字卡與帳號設定。
- `%LOCALAPPDATA%/FB_Live_Auto_Comment/favorite_streamers.json`：常用直播主清單。

`dist/` 中的 `.exe`、`.pkg`、`.pyz`、`build/` 與 `build_pyinstaller/` 都是打包或執行產物。修改 Python 原始碼後，既有執行檔不會自動更新，必須重新執行對應的 PyInstaller spec。

### MongoDB 與 Ollama

- MongoDB 儲存預設語句、自訂語句、衝人氣口號、直播設定與部分回覆範例。
- Ollama 提供 `nomic-embed-text` 向量模型與 `qwen3:8b` 生成模型。
- LLM 服務通常以 `127.0.0.1:5001` 提供語句 API，以 `127.0.0.1:5002` 起提供直播回覆 API；桌面程式會為不同帳號／直播列使用不同連接埠。
- CLS 記憶與直播快取屬於測試或服務執行時資料；不同 `stream_id` 不應共用另一場直播的歷史。

不要將 `stt/service_account.json`、`stt/stt_api_key.txt`、Facebook 登入資料、MongoDB 密碼或 Ollama 私人設定提交到公開儲存庫。

## 開發與測試

### 桌面程式局部測試

在專案根目錄執行：

```powershell
cd fb-live-comment-extension
python -m unittest test_profile_cards test_favorite_streamers test_llm_sessions
node --test test_content_config.js
```

這些測試涵蓋帳號卡、常用直播主、直播分流與 Chrome 設定資料。完整的 Facebook 登入、音訊擷取、Google STT 與留言送出流程，仍需在已安裝擴充功能且已登入 Facebook 的 Chrome 設定檔中驗證。

### 直接啟動 LLM 測試服務

目前版本的 LLM 測試服務位於 `test_LLM/test_llm_8/`：

```powershell
cd test_LLM/test_llm_8
python -m pip install -r requirements.txt
ollama pull nomic-embed-text
ollama pull qwen3:8b

# 第一個 PowerShell
python user_input.py

# 第二個 PowerShell
python live_stream_llm.py
```

MongoDB 與 Ollama 必須先啟動。`test_LLM/test_llm_8/README.md` 包含 API、JSONL 測試、候選語句 CRUD、CLS 記憶與測試腳本的完整說明。

### STT 測試

STT 的安裝、Google 憑證、MongoDB context 與測試方式請以 [stt/README.MD](stt/README.MD) 為準。本次整理沒有修改 STT 來源檔或資料檔。

## 常見問題

- **Chrome 沒有留言**：確認目前使用的 Chrome 設定檔已載入 `chrome_extension/`、擴充功能已啟用、直播列已開始，且回覆模式已按「套用」。
- **手動模式沒有回覆**：確認至少有一筆語句已啟用，且主播文字與候選語句達到向量相似度門檻。
- **自動模式沒有立刻回覆**：系統需要先累積主播文字，也可能因模型判斷不適合回覆而保持安靜。
- **LLM 啟動失敗**：確認 MongoDB、Ollama、`qwen3:8b` 與 `nomic-embed-text` 都可用，再查看對應 LLM 終端機。
- **某個帳號沒有回覆**：確認帳號字卡已勾選、Facebook 已登入、Chrome 擴充功能已在該設定檔啟用，以及該帳號沒有被停用。
- **修改程式後執行檔沒有變化**：`dist/*.exe` 是舊的打包結果，需重新建置或直接用 Python 啟動來源碼。
- **不同直播出現互相混用的內容**：檢查是否誤用了相同 `stream_id`、相同 JSONL 或相同 LLM 連接埠；正式流程應由 `launcher.py` 建立分流。

## 維護原則

1. 目前桌面流程的主要來源是 `fb-live-comment-extension/`、`stt/` 與 `test_LLM/test_llm_8/`。
2. `test_LLM/舊時代的東西/` 用於保存歷史版本，不應直接修改來修正目前功能。
3. 修改 Chrome 擴充功能後，要在每個使用中的 Chrome 設定檔重新載入擴充功能並重新開啟直播頁面。
4. 修改 Python 原始碼後，開發測試可直接執行 `.py`；交付給使用者時才重新建置 `dist/` 執行檔。
5. 不要手動混合不同直播的 STT JSONL、`stream_id`、回覆資料庫或 LLM 連接埠。
