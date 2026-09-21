# 逐句 CLS Cache：第一版

## 這版完成什麼

每個已確認、非空的輸入句子，獨立通過中文 Encoder，擷取最後一層 `[CLS]` 向量，保存於本場直播的記憶體 Cache。下一句輸入時，比較它與先前所有 CLS，取出相關歷史，加上最近幾句，將對應原文交給主題分類與回覆模型。

**這是 CLS 記憶檢索，不是新訓練的 Attention 記憶網路。Qwen 收到的是歷史文字，不是 CLS 數字。** 保存每句向量不代表每輪將全部原文送入模型，也不保證模型能精確記住整場所有細節。

```text
當前句 → BGE Encoder → 最後一層 CLS
                         ├─ 寫入本場 Cache（原文、順序、時間、向量）
                         └─ 與寫入前的歷史 CLS 比較
                                      ↓
                        語意前 K 筆 ＋ 最近幾句
                                      ↓
                           去重、限制字數、按時間排序
                                      ↓
                     歷史原文 → 主題分類／Ollama 回覆
```

原本的 `nomic-embed-text` 範例搜尋繼續使用原本的向量空間，和新的 CLS Cache 完全分開。

## 檔案分工

| 檔案 | 責任 |
|---|---|
| `live_reply_bot/cls_encoder.py` | 真正擷取 CLS，固定模型版本，拒絕空句與超長輸入，不默默截斷 |
| `live_reply_bot/cls_cache.py` | 每場所有句子的 CPU float32 向量與原文；重試去重、版本與維度檢查 |
| `live_reply_bot/memory_reader.py` | cosine 全歷史掃描、近期內容保留、提示詞內容字數限制；沒有新訓練參數 |
| `live_reply_bot/engine.py` | 每輪先保存，再做其他模型呼叫；不回覆也保存；開始新場次 |
| `live_reply_bot/topic_inference.py` | 分類提示詞加入本場歷史，協助省略句與指涉判斷 |
| `live_reply_bot/reply_prompt.py` | 明確區分本場歷史和回覆風格範例，不把歷史當作系統指令 |
| `live_reply_bot/demo.py` | 啟動真實 CLS 模型、環境設定、`/new` 與 `/memory` 指令 |
| `tests/test_cls_memory.py` | 不需模型的行為測試，以及可選的真實 CLS 模型測試 |

## 安裝與執行

建議使用 Python 3.12 或 3.13 的獨立環境。在專案根目錄執行：

```bash
python3.12 -m venv .venv-cls
.venv-cls/bin/python -m pip install -r requirements-cls.txt -r requirements-mongo.txt
```

原作者已建立本機 `.venv-cls`，但組員新 clone 後仍需自行建立環境與下載模型。模型檔預設放在專案 `.cache-cls/models/`，兩者都已加入 `.gitignore`，不隨 GitHub 分享。完整的 Docker／Ollama 安裝與第一次下載步驟請先照 [README 快速開始](README.md#快速開始) 執行。

啟動完整版本（先啟動 MongoDB 與 Ollama、設定資料庫帳密，準備 `qwen3:8b` 和 `nomic-embed-text:latest`）：

```bash
USE_OLLAMA=1 USE_CLS_MEMORY=1 SHOW_META=0 .venv-cls/bin/python -m live_reply_bot
```

正式模式現在必須連線 MongoDB，並先確認權限；失敗就停止，不會退回記憶體範例庫。`requirements-mongo.txt` 提供必要驅動。預設集合為 `live_reply_bot.reply_examples_v2`，可透過 `MONGO_URI` 或 `MONGO_USERNAME`、`MONGO_PASSWORD`、`MONGO_AUTH_SOURCE` 等設定連線。CLS Cache 仍在 RAM，沒有改存 MongoDB。

只測 CLS 保存與流程，不呼叫真實回覆模型：

```bash
USE_OLLAMA=0 USE_CLS_MEMORY=1 SHOW_META=0 .venv-cls/bin/python -m live_reply_bot
```

此時 CLS 是真的，但回覆／分類是 Mock，不能用來評估生成品質。

| 指令 | 用途 |
|---|---|
| `/memory` | 顯示 CLS 功能是否開啟、本場 ID 和已保存句數 |
| `/debug on`、`/debug off` | 開啟精簡除錯資訊／顯示回覆與總耗時 |
| `/new` | 開始新場次，重設近期文字與主題；不讀取舊場次向量 |
| `/style curious` | 切換回覆口吻 |
| `/exit` | 結束；本版 RAM Cache 也隨程序結束消失 |

## 設定

| 環境變數 | 預設／說明 |
|---|---|
| `USE_CLS_MEMORY` | 使用 Ollama 時預設 `1`，Mock 模式預設 `0`；可明確覆寫 |
| `CLS_MODEL` | `BAAI/bge-small-zh-v1.5`，512 維 CLS |
| `CLS_REVISION` | 預設模型固定為 `7999e1d3359715c523056ef9478215996d62a620`；自訂模型須提供版本 |
| `CLS_MODEL_CACHE` | 專案 `.cache-cls/models/` |
| `CLS_DEVICE` | `cpu`；其他裝置需自行驗證可用性與記憶體需求 |
| `CLS_OFFLINE` | `1` 時只讀本機已下載權重 |
| `LIVE_SESSION_ID` | 未設定則產生 UUID |
| `MEMORY_TOP_K` | `4`，從整場歷史選語意相關句 |
| `MEMORY_RECENT_K` | `4`，保留近期句子，補足「這個」「剛才」的上下文 |
| `MEMORY_MAX_CHARS` | `4000`，歷史原文內容的字元預算，不是整份 prompt 的 token 限制 |
| `OLLAMA_THINK` | 預設 `0`，實際關閉 Qwen 思考；`1` 開啟，`auto` 沿用伺服器預設 |
| `REPLY_MAX_TOKENS` | 回覆 JSON 上限，預設 `256` tokens |
| `TOPIC_MAX_TOKENS` | 主題 JSON 上限，預設 `192` tokens |
| `SHOW_META` | 預設 `0`，顯示回覆與總耗時；`1` 顯示精簡除錯資訊，不列出向量及原始模型輸出 |

讀取結果先保留最近句子，剩餘預算給相關歷史；去重後按句序送入模型。字元預算太小時，可能沒有空間提供較早歷史。截斷只發生在提示詞副本，會標記 `truncated`，不刪除 Cache 原文或向量。

第一版沒有套用原本範例庫的 `0.72` 門檻；不同模型的分數不可直接沿用。Top-K 有可能取到不相關內容，需用自己的直播資料評估與調整。

## 程式使用方式

```python
from live_reply_bot import CLSEncoder, LiveReplyEngine

# 保留原本的 embedder、llm_client、example_store 初始化。
engine = LiveReplyEngine(
    embedder=embedder,
    llm_client=llm_client,
    example_store=example_store,
    cls_encoder=CLSEncoder(local_files_only=True),
    session_id="live_001",
)

result = engine.process_turn("今天介紹翡翠手鐲。", {"turnId": "segment-001"})
print(result["memory"])
```

語音辨識整合時，只把 **已確認的片段** 傳進來，使用穩定的 `turnId`。同場同 ID 同原文重送只保存一次；同 ID 不同文字會報錯，第一版不支援轉錄修訂。相同原文若是不同 ID，仍視為不同次發言逐句保存。

`turnId` 只保證 Cache 寫入冪等，不保證重試不重新生成回覆。外部真正發送留言的程式也需要自己的去重機制。

同一 engine 串行處理輸入，處理順序就是 Cache 句序。一個 engine 對應一個活躍直播；不要讓多個 engine 同時處理同一場。跨場資料以 session ID 隔離。

查看實際向量（不建議每輪全部列印）：

```python
turns = engine.cls_cache.turns(engine.session_id)
last = turns[-1]
print(last.sequence, last.text, len(last.cls))
print(last.cls[:5])
```

`/new` 不刪除舊場 Cache。確定不再需要某場時，呼叫 `engine.cls_cache.clear_session(old_session_id)` 回收記憶體。不要在仍有處理中請求時刪除場次。

## 驗證

不需要 PyTorch 或模型的行為測試：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

已下載權重後，啟用真正 CLS 的測試：

```bash
PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 RUN_REAL_CLS=1 .venv-cls/bin/python -m unittest discover -s tests -v
```

涵蓋：每句保存、超過 30 句、零／錯維度／NaN 向量、場次隔離、重試去重、並行重試、當前句不匹配自己、無回覆也保存、模型失敗仍保留、提示詞預算、歷史進入兩個模型提示詞、精確 CLS 擷取與一個中文檢索案例。

這些測試不等於完整的直播理解評估。後續應按直播場次拆分訓練／驗證資料，比較「無 Cache」「只有最近句子」「CLS 檢索＋最近句子」，測量跨句指涉、商品切換、價格修正、跨場污染與回覆延遲。

## 已知限制與風險

- **RAM Cache 尚無持久化或恢復**：程序重啟會清空；不會把直播歷史自動寫入 MongoDB。
- **有保存，不等於每輪讀完整場**：每輪掃描全部 CLS，但只把有界的歷史原文交給模型；尚無持續更新的整場摘要。
- **商品切換未做結構化追蹤**：提示詞要求避免混淆，但未實作商品 ID 與事實版本管理，不能保證價格一定正確。
- **CLS 是有損表示**：數字、否定與代詞不一定能靠 cosine 正確找回；原文保留供核對。
- **逐句不是逐字**：超過 Encoder token 上限會拒絕，不能默默把一大段直播只編前半段。
- **短句仍可能不回覆**：原有回覆門檻保留；短句仍會保存並參與後續理解。規則未通過時直接返回，跳過範例 embedding 與兩次文字生成；本輪主題不更新，標記 `unchanged_rule_skip`。
- **歷史文字可能含惡意指令**：以 JSON 資料引用並在 system 提醒，不保證完全免疫 prompt injection；目前不新增任何執行工具或自動發送功能。
- **線性掃描成本會隨場次長度增加**：原始向量用 float32 bytes 儲存，metadata／原文另計；讀取仍是 Python cosine 掃描，需以長直播實測效能。
- **模型或版本改變要重編**：新 CLS 不可拿去比較舊 Nomic 向量，也不可混用不同 CLS Encoder 的結果。

模型擷取依據：[BGE 官方 CLS 使用方式](https://huggingface.co/BAAI/bge-small-zh-v1.5#using-huggingface-transformers)。

## 延遲改善

目前預設在 Ollama API 關閉思考，限制主題／回覆輸出 token，並對規則拒絕的輸入快速返回。CLS 每句保存與整場檢索不變。詳細設定、品質取捨與截斷處理請見主 README 的「回覆速度設定」。
