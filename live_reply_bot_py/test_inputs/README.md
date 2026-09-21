# 把測試 JSON 放在這裡

將自己的 `.json`、`.jsonl` 或 `.txt` 檔案直接放進這個資料夾，不需要改檔名。每筆 JSON 應有非空的 `resolved_text` 字串。

原作者本機可能有 `直播紀錄_33筆.json` 等真實測試資料，但不會隨 GitHub 分享。新組員可直接測試專案的人工示範檔：`./run_json_test.command examples/demo_resolved_text.json`，或自行把有授權的資料放進這個資料夾。

先啟動 Docker Desktop，確認 `mongodb-rag` 容器與 Ollama 正在執行，再在專案根目錄的終端機執行：

```bash
./run_json_test.command
```

MongoDB 帳號與認證資料庫已預設為 `admin`，不必每次輸入設定指令。若未提供 URI／密碼，腳本會從 `mongodb-rag` 容器自動讀取已設定的初始化密碼；讀不到時才提示隱藏輸入。密碼不會印出或另存檔案，每次啟動重新讀取。已設定的環境變數會保留，`MONGO_URI` 優先於個別帳密設定。容器改名可設定 `MONGO_DOCKER_CONTAINER`；自動取得僅用於本機預設連接埠與相符帳號。不要用 `bash -x` 或 `source` 執行。

- 只有一個資料檔：直接執行。
- 有多個資料檔：先輸入編號選一份；不同檔案不會自動合併成同場直播。
- 空資料夾：顯示提示，不啟動模型。
- 終端機逐句顯示輸入、回覆與耗時；Ctrl+C 停止。
- 每次測試使用新的 CLS Cache；同一份檔案內按原始順序累積記憶。
- 結果仍會自動存到專案 `test_results/`，方便稍後對照。
- 正式模式必須使用 MongoDB；啟動時連線或授權失敗就停止，不會改用記憶體範例庫。成功時會顯示資料庫、集合與範例筆數，摘要也會保存這些資訊。

只測前五筆：`./run_json_test.command --limit 5`。不掃描子資料夾、隱藏檔或這份 README。輸入檔不會被修改或自動刪除。
