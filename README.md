# FB 直播自動留言系統

Windows 桌面程式，整合 Chrome 擴充功能、直播語音轉文字（STT）、本機 LLM 與語句資料庫，讓多個 Facebook 帳號處理一場或多場直播。使用者操作請見 [使用說明書](FB直播自動留言系統_使用說明書.md)。

## 快速開始

1. 保留 `final_project` 的資料夾結構，執行 `fb-live-comment-extension/dist/FB_Live_Environment_Setup.exe`，安裝或檢查 Chrome、MongoDB、Ollama 與 `nomic-embed-text`。
2. 在 PowerShell 執行 `ollama pull qwen3:8b`。環境安裝程式目前只下載 `nomic-embed-text`；自動模式另需 `qwen3:8b`。
3. 在每個機器人帳號使用的 Chrome 設定檔，從 `chrome://extensions` 的「載入未封裝項目」安裝 `fb-live-comment-extension/chrome_extension`，並確認 **FB Live Auto Clicker** 已啟用。
4. 確認 MongoDB、Ollama 正在執行，開啟 `fb-live-comment-extension/dist/FB_Live_Auto_Comment.exe`。
5. 新增並登入 Facebook 帳號，勾選要使用的帳號，輸入直播網址，選擇回覆模式並按「套用」，最後按各直播列的「開始」。

## 目前功能

- 最多 8 個機器人字卡，分別對應 Chrome 設定檔；字卡顯示名稱可編輯，登入資料留在設定檔內。
- 最多 5 列直播網址，各列獨立開始、暫停、繼續與停止；停止後網址恢復可編輯。
- 一個帳號可同時處理多列直播，多個帳號也可參與同一列。每列共用 STT 輸入，每個「帳號 × 直播列」使用獨立 LLM 服務與可見終端機；停止該列會關閉其對應 LLM。
- 語句分成「預設／自訂／衝人氣」三類，衝人氣語句保存在獨立資料庫。回覆模式只有「手動／自動」，切換後需按「套用」。手動模式把三類已啟用語句做向量比對，回覆一定逐字取自資料庫；最近 5 次已送出的普通留言不會重複，衝人氣語句不受此限制。自動模式把三類語句提供給 Qwen 參考，也允許生成資料庫以外的新留言。
- 自動模式的 prompt 要求以自然、友善、喜歡主播的觀眾口吻互動，禁止諷刺、嗆聲、貶低、質疑或命令主播；三次未產生回覆時可使用一次安全的固定備援語句。
- 輸入直播網址後嘗試辨識並儲存最近 3 位直播主的個人主頁；「前往」以選取帳號的 Chrome 設定檔開啟主頁。

## 專案結構

```text
final_project/
├─ fb-live-comment-extension/
│  ├─ launcher.py              # Windows 主介面與流程管理
│  ├─ environment_installer.py # 環境檢查／安裝程式
│  ├─ chrome_extension/        # Chrome 擴充功能；載入此資料夾
│  ├─ dist/                    # 打包執行檔、工作資料與執行紀錄
│  │  ├─ FB_Live_Auto_Comment.exe
│  │  ├─ FB_Live_Environment_Setup.exe
│  │  ├─ llm_server.exe
│  │  ├─ stt_worker.exe
│  │  ├─ reply_db_api.exe
│  │  ├─ sessions/
│  │  └─ replies/
│  └─ test_*.py
├─ test_LLM/test_llm_8/     # LLM 回覆服務與測試
├─ stt/                     # 直播語音轉文字程式
└─ live_reply_bot_py/       # 獨立的 Python 示範程式
```

`live_reply_bot_py` 的 `run_demo.py` 是獨立示範，不是桌面直播系統的啟動入口。

## 執行流程與資料

主程式會依選取的帳號與直播列啟動工作。相同直播列的多個帳號共用 STT 檔案，各帳號連到自己的 LLM 服務；不同直播列則使用不同的語音檔案、回覆資料與 LLM 服務。Chrome 擴充功能在對應的直播分頁取得該帳號、直播檔案與 LLM 連接埠資訊，再處理留言。

執行資料位於 `fb-live-comment-extension/dist/sessions/` 與 `fb-live-comment-extension/dist/replies/`。機器人字卡與常用直播主清單保存在 `%LOCALAPPDATA%/FB_Live_Auto_Comment/`，其中包括 `robot_cards.json` 與 `favorite_streamers.json`。關閉字卡不會刪除 Chrome 設定檔或 Facebook 登入資料。

LLM 預設使用 Ollama 的 `qwen3:8b`；語意處理使用 `nomic-embed-text`。語句資料 API 使用 MongoDB。更詳細的 LLM 服務說明見 [test_LLM/test_llm_8/README.md](test_LLM/test_llm_8/README.md)。

## 開發與檢查

原始碼在 `fb-live-comment-extension/launcher.py`、`chrome_extension/`、`stt/` 與 `test_LLM/test_llm_8/`。修改擴充功能後，需在每個使用中的 Chrome 設定檔按「重新載入」，再重新開啟直播分頁。打包程式使用 `fb-live-comment-extension/*.spec`；修改 Python 原始碼後，已存在的 `dist/*.exe` 不會自動更新。

針對帳號字卡、常用直播主與直播分流邏輯，可在專案目錄執行：

```powershell
cd fb-live-comment-extension
python -m unittest test_profile_cards test_favorite_streamers test_llm_sessions
node --test test_content_config.js
```

上述測試檢查局部邏輯；完整的 Facebook 登入、直播擷取與留言流程仍須在已安裝擴充功能的 Chrome 設定檔中驗證。
