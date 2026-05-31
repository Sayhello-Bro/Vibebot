import pymongo
import json
import os
import torch
import sys
import io
import time
import asyncio  # 引入非同步庫
import ollama   # 使用 ollama 的 AsyncClient
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sentence_transformers import SentenceTransformer, util

# 強制 UTF-8 設定
if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# --- 改用 FastAPI ---
app = FastAPI(title="多直播間平行記憶系統")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= 設定區 =================
GEN_MODEL = "qwen3:8b" 
EMBED_MODEL = "shibing624/text2vec-base-chinese"
# =========================================

embed_engine = SentenceTransformer(EMBED_MODEL)
# 初始化 Ollama 非同步客戶端
async_ollama = ollama.AsyncClient()

class LiveMemoryAPI:
    def __init__(self):
        # 連接 MongoDB
        self.client = pymongo.MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=2000)
        self.db = self.client["jewelry_live_db"]
        print("🚀 珠寶多場景知識庫與 MongoDB 已就緒", flush=True)

    # 關鍵：改為 async 函式，並傳入 live_id 進行隔離
    async def process_new_logs(self, live_id: str):
        # 🚀 修正：動態獲取目前專案檔案的絕對路徑目錄
        base_dir = os.path.dirname(os.path.abspath(__file__))
        
        file_path = os.path.join(base_dir, f"Text_{live_id}.jsonl")
        tracker_file = os.path.join(base_dir, f"file_pointer_{live_id}.json")

        if not os.path.exists(file_path):
            print(f"❌ [{live_id}] 找不到路徑: {os.path.abspath(file_path)}", flush=True)
            return []

        last_line = 0
        if os.path.exists(tracker_file):
            with open(tracker_file, 'r') as tf:
                last_line = json.load(tf).get("last_line", 0)

        results = []
        current_line_count = 0

        # 動態撈取該直播間專屬的 MongoDB 集合
        collection = self.db[f"comment_{live_id}"]
        # 使用 asyncio.to_thread 讓同步的 PyMongo 查詢不卡住主線程
        all_docs = await asyncio.to_thread(lambda: list(collection.find()))

        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                current_line_count += 1
                if current_line_count <= last_line:
                    continue 
                
                start_time = time.time()
                try:
                    line_data = line.strip()
                    if not line_data: continue
                    
                    data = json.loads(line_data)
                    raw_text = data.get("raw_text", "")
                    
                    if raw_text:
                        
                        final_prompt = f"""
                        你是一位直播的觀眾，請依照當前主播說的話做出簡短的回覆，規則如下：
                        1. 回覆只能有以下幾種：+1、有沒有優惠、我來了、哈哈、這個好、我喜歡
                        2. 如果是主播進行無關發言，則回覆 IGNORE

                        簡短回覆：
                        """
                        # 🚀 真正實現平行的關鍵：使用 await 呼叫非同步 Ollama
                        response_data = await async_ollama.generate(model=GEN_MODEL, prompt=final_prompt)
                        response = response_data['response'].strip()
                        
                        duration = time.time() - start_time
                        
                        print(f"\n===== [直播間: {live_id}] 處理完成 =====")
                        print(f"主播原話：{raw_text}")
                        print(f"最終回覆：{response}")
                        print(f"時長：{duration:.2f}s")
                        print("=" * 35, flush=True)
                        
                        results.append({"raw": raw_text, "reply": response})
                        
                except Exception as e:
                    print(f"🔥 [{live_id}] 第 {current_line_count} 行出錯: {e}", flush=True)

        with open(tracker_file, 'w') as tf:
            json.dump({"last_line": current_line_count}, tf)
            
        return results

bot = LiveMemoryAPI()

# --- FastAPI 路由設計 ---
# 透過 URL 路徑動態帶入 live_id (例如 /sync/roomA, /sync/roomB)
@app.post("/sync/{live_id}")
async def sync(live_id: str):
    """平行觸發指定直播間的同步"""
    processed_data = await bot.process_new_logs(live_id)
    return {
        "status": "success",
        "live_id": live_id,
        "processed_count": len(processed_data),
        "data": processed_data
    }

if __name__ == "__main__":
    import uvicorn
    # 啟動 FastAPI 伺服器
    uvicorn.run(app, host="0.0.0.0", port=5000)