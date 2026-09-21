# GitHub 分享前注意事項

## 組員應拿哪一份？

首次分享建議使用「核心分享包」建立新的私人儲存庫，邀請組員。核心只需要 `live_reply_bot/`、兩個 Python 入口、JSON 啟動腳本、依賴清單、文件、人工示範輸入與測試。依 README 的「快速開始」安裝；每人建立自己的 MongoDB，不共用管理員密碼。

核心分享包不含來源儲存庫的 `.git` 歷史、STT 憑證、API key、真實直播紀錄、模型權重、虛擬環境或打包執行檔。`examples/demo_resolved_text.json` 是人工示範句，不是真實直播逐字稿，也不是模型品質標準答案。

### 本次核心分享包驗證

- 原專案：72 項測試通過，包含 4 項真實 CLS 模型測試。
- 核心分享資料夾：關閉 site-packages 後執行 72 項測試，68 項通過、4 項真實模型測試按設定略過，確認純流程不依賴原始儲存庫的額外模組。
- 六筆人工資料的 Mock 重播完成，0 錯誤；此結果不代表真實模型速度或語意品質。
- Compose 設定驗證：空密碼拒絕、提供測試密碼時通過；沒有啟動新容器或使用真實憑證。
- 分享檔案採明確清單複製，並比對本次發現的憑證及常見私鑰／token 格式，未發現匹配；這不是所有未知秘密的完整安全稽核。
- 沒有在新電腦實際重裝依賴，也沒有在 Windows／Linux 跑完整真實 MongoDB＋Ollama 流程；各平台仍需照 README 自行驗證。

## 來源儲存庫：目前不要直接 push

2026-08-26 本機檢查發現：

| 路徑／項目 | 發現與處理方向 |
|---|---|
| `stt/service_account.json` | 工作目錄和 HEAD 都有 Google 服務帳號私鑰，且已進入歷史；須由擁有者處理金鑰撤銷／更換 |
| `stt/stt_api_key.txt` | 偵測到 API 金鑰格式，檔案已進入歷史；須確認來源、限制與有效性並處理 |
| `test_LLM/舊時代的東西/test_llm_4/rag_chat.py` | 存在硬編碼的 SUPABASE_KEY／JWT；需確認是何種角色與權限，不能未確認就宣稱是安全的公開金鑰 |
| 真實 JSONL、session／reply 紀錄 | 可能包含直播文字、帳號或其他非公開資料，不納入核心分享包 |
| 132 個已追蹤 Python 產物、18 個至少 10 MiB 的現存檔案 | 包含快取與打包產物，不適合一併上傳作為原始碼 |

本次只做本機檢查，**未驗證金鑰是否仍有效、未檢查遠端是否已公開，也未全面掃描所有歷史與打包檔內容**。所有敏感值都未列印或放進本文件。沒有刪除本機憑證、撤銷帳號、修改 Git index、改寫歷史、提交或推送。

`.gitignore` 已補上常見敏感檔案與產物規則，但 **ignore 不會把已追蹤的檔案或舊 commit 移除**。僅刪除最新版本的檔案再 commit，也不能清除舊歷史。

## 如果金鑰可能已經傳出去

先由擁有者到對應平台撤銷／輪替憑證，再評估如何清理儲存庫歷史；不能把刪除本機檔案當作撤銷金鑰。若要改寫共同歷史，需先備份並和組員協調，避免覆蓋別人的工作。[GitHub 官方處理流程](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)、[Google 服務帳號金鑰管理](https://cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys)

這次沒有進行遠端設定或任何金鑰管理操作。來源儲存庫仍需要處理上述風險；改用新的核心分享包不會讓曾暴露的舊金鑰自動失效。

## 使用核心分享包建立新儲存庫

1. 解壓分享包到新的資料夾；不要把舊 `.git`、憑證、`.env`、真實測試資料或打包目錄複製進去。
2. 在 GitHub 建立新的私人儲存庫，邀請組員；或用 GitHub Desktop 將這個新資料夾建立成新儲存庫。
3. 若用終端機，下列命令只能在**新解壓的核心資料夾**執行：

```bash
git init
git branch -M main
git add .gitignore .env.example compose.example.yaml README.md README_CLS_MEMORY.md GITHUB_SHARING.md
git add requirements-cls.txt requirements-mongo.txt run_demo.py replay_json.py run_json_test.command
git add live_reply_bot tests examples test_inputs/README.md
git diff --cached --stat
```

逐一檢查 staged 檔案，確認沒有憑證或私人資料後，才執行：

```bash
git commit -m "Share CLS live reply core and setup guide"
```

接著依 GitHub 新儲存庫頁面的指示設定 remote 與 push；不要把原本來源儲存庫的 remote／歷史直接搬進來。本文件不自動執行上述命令。

## 每次提交前

- 使用明確檔案清單，避免在原始大儲存庫直接 `git add .`。
- 確認 `.env`、金鑰、模型快取、虛擬環境、真實輸入與結果未列入 staged files。
- 查看 `git diff --cached`；這個指令可能顯示尚未移除的敏感值，不要把結果直接貼到聊天或公開 issue。
- 執行 README 的測試；功能測試不是憑證掃描，也不是內容準確率驗證。
- 啟用適用的 GitHub secret scanning／push protection，不能只依賴 `.gitignore`。
- 分享第三方程式、樣本或資料前確認授權；本次沒有替整個歷史儲存庫宣告開源授權。
