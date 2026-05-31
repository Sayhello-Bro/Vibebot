import pymongo
import json
import os
import torch
import numpy as np
import sys
import io
import ollama
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from sentence_transformers import SentenceTransformer, util

# 強制 UTF-8 設定
if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# --- 關鍵修正：必須先定義 app ---
app = Flask(__name__)
CORS(app)

# ================= 設定區 =================
GEN_MODEL = "qwen3:8b" 
EMBED_MODEL = "shibing624/text2vec-base-chinese"
TRACKER_FILE = "file_pointer.json"  # 記錄讀取進度
# =========================================

embed_engine = SentenceTransformer(EMBED_MODEL)

class LiveMemoryAPI:
    def __init__(self):
        # 連接到存放 100 筆標註資料的資料庫
        try:
            self.client = pymongo.MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=2000)
            self.client.server_info() # 測試連線
            self.db = self.client["jewelry_live_db"]
            self.collection = self.db["comment"]
            print("🚀 珠寶知識庫與 MongoDB 已就緒", flush=True)
        except Exception as e:
            print(f"❌ MongoDB 連線失敗: {e}", flush=True)

    def format_all_references(self, all_docs):
        """
        將 MongoDB 取得的資料格式化為條列文字
        格式：主播說：xxx 觀眾說：xxx (換行)
        """
        if not all_docs:
            return "目前無參考資料"

        formatted_list = []
        for doc in all_docs:
            # 取得主播與觀眾的內容，若無資料則給預設值
            anchor_text = doc.get('raw_text', '（無內容）')
            audience_text = doc.get('comment', '（無內容）')
            
            # 依照格式組合字串
            line = f"主播說：{anchor_text} 觀眾說：{audience_text}"
            formatted_list.append(line)

        # 用換行符號連接所有行
        return "\n".join(formatted_list)

    def get_top_3_references(self, current_text):
        """需求 3：找出三個最高分的參考資料"""
        all_docs = list(self.collection.find())
        if not all_docs:
            return "目前無參考資料"

        current_vec = embed_engine.encode(current_text, convert_to_tensor=True)
        doc_texts = [d.get('raw_text', '') for d in all_docs]
        doc_vecs = embed_engine.encode(doc_texts, convert_to_tensor=True)
        
        cos_scores = util.cos_sim(current_vec, doc_vecs)[0]
        top_results = torch.topk(cos_scores, k=min(3, len(all_docs)))
        
        ref_context = ""
        for score, idx in zip(top_results.values, top_results.indices):
            matched_doc = all_docs[idx]
            ref_context += f"參考主播說：{matched_doc.get('raw_text')} | 參考觀眾回：{matched_doc.get('comment')}\n"
        
        return ref_context

    def process_new_logs(self, file_path):
        if not os.path.exists(file_path):
            print(f"❌ 嚴重錯誤：找不到檔案 {os.path.abspath(file_path)}", flush=True)
            return []

        # 讀取進度
        last_line = 0
        if os.path.exists(TRACKER_FILE):
            with open(TRACKER_FILE, 'r') as tf:
                last_line = json.load(tf).get("last_line", 0)
        
        # 增加這行確認讀取狀態
        with open(file_path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
        
        print(f"📊 檔案總行數: {len(all_lines)} | 上次處理到: {last_line}", flush=True)

        results = []
        current_line_count = 0

        for line in all_lines:
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
                    # 檢索與生成邏輯
                    all_docs = list(self.collection.find())
                    top_3_refs = self.get_top_3_references(raw_text)
                    all_refs = self.format_all_references(all_docs)
                    
                    final_prompt = f"""
                        你是一位直播的觀眾，請依照當前主播說的話做出簡短的回覆，規則如下：
                        1. 回覆禁止超過十個字
                        2. 底下有參考資料，請優先參考前三名的參考資料

                        範例
                        主播說：這顆拉長石是馬達加斯加產的，光澤很強
                        簡短回覆：閃光好漂亮喔

                        前三名參考資料：{top_3_refs}
                        全部參考資料：{all_refs}
                        主播說：{raw_text}

                        簡短回覆：
                        """
                    
                    response_data = ollama.generate(model=GEN_MODEL, prompt=final_prompt)
                    response = response_data['response'].strip()
                    duration = time.time() - start_time
                    
                    # --- 修改後的 Print 格式 ---
                    print("-" * 30)
                    print(f"主播原話：{raw_text}")
                    print(f"最終回覆：{response}")
                    print(f"時長：{duration:.2f}s")
                    print("-" * 30, flush=True)
                    
                    results.append({"raw": raw_text, "reply": response})
                    
            except Exception as e:
                print(f"🔥 第 {current_line_count} 行處理出錯: {e}", flush=True)

        # 關鍵：強制更新進度檔
        with open(TRACKER_FILE, 'w') as tf:
            json.dump({"last_line": current_line_count}, tf)
            print(f"💾 進度已更新至第 {current_line_count} 行", flush=True)
            
        return results

# 實例化邏輯類別
bot = LiveMemoryAPI()

# --- 路由設定 ---

@app.route('/sync', methods=['POST'])
def sync():
    """觸發同步 Text.jsonl 並處理新內容"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(current_dir, "Text.jsonl")
    
    processed_data = bot.process_new_logs(file_path)
    
    return jsonify({
        "status": "success",
        "processed_count": len(processed_data),
        "data": processed_data
    })

if __name__ == "__main__":
    # 啟動 Flask
    app.run(host="0.0.0.0", port=5000, debug=True)