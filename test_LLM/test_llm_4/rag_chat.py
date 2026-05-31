import os
import sys
import io
import json
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
import ollama

# 強制 UTF-8 設定，徹底解決 Windows 環境問號亂碼問題
if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

app = Flask(__name__)
CORS(app)

# =========================
# Supabase & Embedding 設定
# =========================
SUPABASE_URL = "https://hupgbmvajprqwdtftske.supabase.co/"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imh1cGdibXZhanBycXdkdGZ0c2tlIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODk4ODIzMiwiZXhwIjoyMDk0NTY0MjMyfQ.oB8fJwmbuz1SqfoM17xNpgBun_be970Cs4Pk_O8swso"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
EMBED_MODEL = "nomic-embed-text"

# 修正：全面回歸你指定的 Text.jsonl 檔案
FILE_NAME = os.environ.get("LLM_TEXT_JSONL", "Text.jsonl")

# 🧠 全域變數：紀錄目前 Text.jsonl 讀取到了哪一個位元組 (Byte Position)
LAST_FILE_POSITION = 0

def get_current_time():
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")


def get_embedding(text: str):
    try:
        response = ollama.embeddings(model=EMBED_MODEL, prompt=text)
        return response["embedding"]
    except Exception:
        response = ollama.embed(model=EMBED_MODEL, input=text)
        return response["embeddings"][0]

# =========================
# 核心 RAG 處理邏輯
# =========================
def process_rag_reply(question: str) -> str:
    """傳入問題，執行 Supabase 檢索與 Qwen Rerank，回傳最終答案"""
    try:
        query_embedding = get_embedding(question)
        response = supabase.rpc(
            "match_documents",
            {"query_embedding": query_embedding, "match_count": 5, "filter": {}}
        ).execute()
        results = response.data

        choices = []
        for r in results:
            text = r["content"]
            if "觀眾：" in text:
                reply = text.split("觀眾：")[-1].strip()
                choices.append(reply)
        choices = list(set(choices))

        if len(results) == 0 or len(choices) == 0:
            return "[NO_REPLY]"

        choices_text = "\n".join(choices)
        prompt = f"""
        你是一位珠寶直播間觀眾。
        請根據使用者訊息：
        選出最適合的聊天室回覆。
        規則：
        - 只能從候選句子中選一句
        - 禁止自己創造句子
        - 如果都不適合：
        輸出 [NO_REPLY]

        使用者訊息：
        {question}

        候選句子：
        {choices_text}

        請直接輸出一句：
        """
        
        qwen_response = ollama.chat(
            model="qwen3:8b",
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0}
        )
        return qwen_response["message"]["content"].strip()
    except Exception as e:
        return f"[ERROR: {str(e)}]"

# =========================
# Flask API 端點
# =========================
@app.route('/process', methods=['POST'])
def process():
    global LAST_FILE_POSITION
    current_time = get_current_time()
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = FILE_NAME if os.path.isabs(FILE_NAME) else os.path.join(current_dir, FILE_NAME)
    
    if not os.path.exists(full_path):
        print(f"{current_time} ❌ 找不到檔案: {full_path}", flush=True)
        return jsonify({"error": f"File {FILE_NAME} not found"}), 404

    processed_logs = []

    try:
        # 使用 'r+' 讀寫模式打開，不破壞原檔案內容，並允許在尾端追加日誌
        with open(full_path, 'r+', encoding='utf-8') as f:
            # 1. 將讀取指針移到上一次處理結束的位置（若是第一次呼叫，LAST_FILE_POSITION 就會是 0，從頭批量讀取）
            f.seek(LAST_FILE_POSITION)
            
            # 2. 批量讀取從目前位置開始的所有新行
            new_lines = f.readlines()
            
            # 3. 更新全域檔案位置，記住目前讀到了檔案的哪個 Byte
            LAST_FILE_POSITION = f.tell()
            
            # 4. 開始循環篩選並處理新訊息
            valid_inputs = []
            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    # 排除含有 ai_reply 的日誌行，避免重複讀取與無窮迴圈
                    if "ai_reply" in data:
                        continue
                    # 修正：讀取欄位名稱改回 raw_text
                    if "raw_text" in data and data["raw_text"].strip():
                        valid_inputs.append(data["raw_text"].strip())
                except json.JSONDecodeError:
                    continue

            if valid_inputs:
                print(f"\n{current_time} 📥 [發現新更新] 偵測到 {len(valid_inputs)} 筆未處理主播話術，開始批量回覆...", flush=True)
                
                # 5. 依序處理 RAG 並即時將結果寫回同一個 Text.jsonl 當作日誌紀錄
                for idx, text_to_process in enumerate(valid_inputs, 1):
                    reply = process_rag_reply(text_to_process)
                    print(f"   ({idx}/{len(valid_inputs)}) 輸入: {text_to_process} -> 🤖 AI: {reply}", flush=True)
                    
                    log_data = {
                        "timestamp": get_current_time(),
                        "raw_text": text_to_process,
                        "ai_reply": reply
                    }
                    
                    # 將指針移到檔案最末尾並寫入
                    f.seek(0, 2)
                    f.write(json.dumps(log_data, ensure_ascii=False) + "\n")
                    
                    processed_logs.append({
                        "input": text_to_process,
                        "reply": reply
                    })
                
                # 寫完日誌後，再次將指針移到最末尾，防止下一次呼叫時回頭去讀自己剛剛寫入的日誌
                f.seek(0, 2)
                LAST_FILE_POSITION = f.tell()
                print("-" * 50, flush=True)
            else:
                print(f"{current_time} 💤 呼交成功，但 Text.jsonl 中沒有任何新的 raw_text 訊息。", flush=True)

    except Exception as e:
        print(f"{current_time} 🔥 處理 JSONL 發生錯誤: {e}", flush=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "status": "success",
        "timestamp": current_time,
        "processed_count": len(processed_logs),
        "results": processed_logs
    })


@app.route('/latest_reply', methods=['GET', 'POST'])
def latest_reply():
    result = process()
    status_code = 200

    if isinstance(result, tuple):
        result, status_code = result

    data = result.get_json(silent=True) or {}
    results = data.get("results") or []
    latest = results[-1] if results else {}
    reply = latest.get("reply", "")

    return jsonify({
        "status": data.get("status", "success" if status_code == 200 else "error"),
        "has_reply": bool(reply),
        "reply": reply,
        "input": latest.get("input", ""),
        "processed_count": data.get("processed_count", 0),
        "results": results,
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    print(f"🚀 Flask RAG 服務正在啟動...", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=False)
