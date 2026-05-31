import pymongo
import datetime
import ollama
import time
import torch
import numpy as np
import sys
import io
import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from sentence_transformers import SentenceTransformer, util

# 強制 UTF-8 設定，徹底解決 Windows 環境問號亂碼問題
if sys.platform == "win32":
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

app = Flask(__name__)
CORS(app)

# ================= 設定區 =================
GEN_MODEL = "qwen3:8b" 
FAST_MODEL = "qwen2.5:3b" 
EMBED_MODEL = "shibing624/text2vec-base-chinese"
SIMILARITY_THRESHOLD = 0.75  # 嚴格門檻
# =========================================

embed_engine = SentenceTransformer(EMBED_MODEL)

class LiveMemoryAPI:
    def __init__(self):
        self.client = pymongo.MongoClient("mongodb://localhost:27017/")
        self.db = self.client["live_stream_db"]
        self.collection = self.db["memories"]
        # self.collection.delete_many({}) # 視需求決定是否每次執行都清空資料庫
        print("🚀 記憶庫已就緒", flush=True)

    def get_embedding(self, text):
        return embed_engine.encode(text).tolist()
    
    def find_best_reference(self, current_text):
        # 1. 抓取資料庫所有記憶，並根據 frequency 由大到小排序
        # 2026-01-14 規則：頻率越高代表越核心的商品資訊
        all_memories = list(self.collection.find().sort("frequency", -1))
    
        if not all_memories:
            return "珠寶"

        # 2. 將當前主播話術向量化
        current_vec = embed_engine.encode(current_text, convert_to_tensor=True).to("cpu")

        print(f"🚀 開始檢索（依頻率排序，總共 {len(all_memories)} 筆資料）...")

        # 3. 逐一比對 (Iterative Search)
        for memory in all_memories:
            # 將該筆記憶的向量轉為 tensor
            doc_vec = torch.tensor(np.array(memory['embedding']), dtype=torch.float32).to("cpu")
        
            # 計算相似度
            score = util.cos_sim(current_vec, doc_vec).item()
        
            # 4. 只要超過門檻 0.75 就結束
            if score >= SIMILARITY_THRESHOLD:
                # 取得主詞 (summary 的第一個詞)
                ref_name = memory['summary'].split(maxsplit=1)[0]
                freq = memory.get('frequency', 1)
                print(f"✅ [命中高頻] 找到 [{ref_name}] (相似度: {score:.2f}, 頻率: {freq})")
                return ref_name

        # 5. 若循環結束都沒超過 0.75，則回傳最新一筆（保底機制）
        last = self.collection.find_one(sort=[("timestamp", -1)])
        ref = last['summary'].split(maxsplit=1)[0] if last else "無"
        print(f"⚠️ 所有記憶比對分數皆未過門檻，使用最新一筆記憶: [{ref}]")
        return ref

    def rewrite(self, text, memorie):
        """代名詞重寫：嚴禁模型解釋，只輸出重寫結果"""
        rewrite_prompt = f"""
        你是一個直播助理。如果「主播原話」包含代名詞(如：這罐、這個、它、這款、剛剛說的)，請替換為「背景記憶」的商品名稱。
        規則：
        1. 沒代名詞則原樣輸出。
        2. 嚴禁輸出任何解釋或理由。

        範例1(有代名詞)： 
        主播原話：這罐多少錢？ 
        背景記憶：洗面乳 
        結果：洗面乳多少錢？
        
        範例2(無代名詞)： 
        主播原話：哎呀剛才喝水差點嗆到，今天的直播間冷氣有點強。 
        背景記憶：洗面乳 
        結果：哎呀剛才喝水差點嗆到，今天的直播間冷氣有點強。 
        
        範例3(有代名詞但是無關)： 
        主播原話：剛剛那個人進來講了兩句話就離開了。 
        背景記憶：洗面乳 
        結果：剛剛那個人進來講了兩句話就離開了。

        背景記憶：{memorie}
        主播原話：{text}
        結果："""
        res = ollama.generate(model=FAST_MODEL, prompt=rewrite_prompt)['response'].strip()
        # 清理可能出現的標籤
        return res.replace("結果：", "").replace("結果:", "").split('\n')[0].strip()

    def filter(self, text):
        """事實提取：10字內"""
        filter_prompt = f"""
        分析珠寶帶貨主播的話，提取商品事實。 
        規則： 
        1. 包含價格、規格(如產地、外觀、大小、重量等)、活動(如抽獎、特價、贈送)時，提取10字內事實，請勿補充額外內容。 
        2. 回覆格式：商品名稱 事實
        3. 若有多個事實則加在後面
        4. 不重要則只回覆 Ignore。

        範例1：
        原話：這顆水晶的色澤偏淡紫色
        結果：水晶 淡紫色

        範例2：
        原話：螢石在紫外燈下會發光，而且現在特價2000
        結果：螢石 發光 特價2000

        範例3：
        原話：哎呀剛才喝水差點嗆到，今天的直播間冷氣有點強
        結果：Ignore

        原話：{text}
        結果："""
        raw_result = ollama.generate(model=FAST_MODEL, prompt=filter_prompt)['response'].strip()
        
        # --- 硬過濾邏輯 ---
        # 1. 排除模型可能噴出的 "商品名稱"、"商品名稱：" 等標籤
        clean_result = raw_result.replace("商品名稱", "").replace("：", "").replace(":", "").strip()
        
        # 2. 針對範例中可能出現的「結果：」也一併清理
        clean_result = clean_result.replace("結果", "").strip()
        
        return clean_result
    
    def final(self, text, context):
        """意圖識別：強制從清單選擇"""
        final_prompt = f"""
        根據背景與主播發言，從以下清單選一個輸出，嚴禁解釋：[有沒有優惠, +1, 我來了, 哈哈, 這個好, 我喜歡]，若覺得沒有適合的則輸出。
        提示：若背景為講述一個商品的相關資訊，則從以下清單選一個輸出，嚴禁解釋：[有沒有優惠, 我來了, 這個好, 我喜歡]，若主播包含加一，則輸出 +1。
        背景：{context}
        主播：{text}
        選擇："""
        res = ollama.generate(model=GEN_MODEL, prompt=final_prompt, stream=False)['response'].strip()
        
        # 程式碼硬過濾
        valid = ["有沒有優惠", "+1", "我來了", "哈哈", "這個好", "我喜歡"]
        for v in valid:
            if v in res: return v
        return ""

    def _save_or_merge(self, res_filter):
        parts = res_filter.split(maxsplit=1)
        new_prod = parts[0]
        new_fact = parts[1] if len(parts) > 1 else ""

        # 全域搜尋相同主詞 (利用正則匹配 summary 開頭)
        existing = self.collection.find_one({"summary": {"$regex": f"^{new_prod}"}})
        
        if existing:
            old_sum = existing['summary']
            # 如果新事實不在舊摘要中，則附加
            updated = old_sum if (not new_fact or new_fact in old_sum) else f"{old_sum} {new_fact}"
            
            self.collection.update_one(
                {"_id": existing["_id"]},
                {
                    "$set": {
                        "summary": updated[:100], 
                        "embedding": self.get_embedding(updated[:100]), 
                        "timestamp": datetime.datetime.now()
                    },
                    "$inc": {"frequency": 1}
                }
            )
            print(f"♻️ 記憶合併: [{new_prod}] 頻率增至 {existing.get('frequency', 1)+1}", flush=True)
        else:
            self.collection.insert_one({
                "summary": res_filter,
                "embedding": self.get_embedding(res_filter),
                "frequency": 1,
                "timestamp": datetime.datetime.now()
            })
            print(f"✨ 新記憶儲存: {res_filter}", flush=True)

    def chat_logic(self, anchor_text):
        """核心邏輯：處理單筆文本並更新記憶庫"""
        safe_text = anchor_text.strip()
        if not safe_text: return ""
        
        start_total = time.time()
        # 1. 檢索參考點 (代名詞用)
        ref = self.find_best_reference(safe_text)

        # 2. 代名詞重寫
        query = self.rewrite(safe_text, ref)
        
        # 3. 事實提取與合併 (1/14 指令)
        res_f = self.filter(query)
        if "Ignore" not in res_f and len(res_f) > 1:
            self._save_or_merge(res_f)

        # 4. 全域檢索背景 (0.75 門檻)
        cursor = list(self.collection.find())
        context = ""
        if cursor:
            doc_vecs = torch.tensor([d['embedding'] for d in cursor])
            scores = util.cos_sim(torch.tensor(self.get_embedding(query)), doc_vecs)[0]
            best = [cursor[i]['summary'] for i in range(len(scores)) if scores[i] > SIMILARITY_THRESHOLD]
            context = " ".join(best[:3])

        # 5. 輸出意圖
        reply = self.final(query, context)
        print(f"""
            原話: {safe_text}
            重寫: {query}
            意圖: {reply} 
            時長: ({time.time()-start_total:.2f}s)""", flush=True)
        return reply
    
    def process_jsonl(self, file_path):
        """新增：讀取 JSONL 檔案並批次處理"""
        if not os.path.exists(file_path):
            print(f"❌ 找不到檔案: {file_path}")
            return

        print(f"📂 開始處理檔案: {file_path}")
        results = []
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    data = json.loads(line)
                    # 關鍵點：唯讀取 resolved_text
                    text_to_process = data.get("resolved_text", "")
                    
                    if text_to_process:
                        print(f"\n📝 處理中: {text_to_process[:30]}...")
                        res = self.chat_logic(text_to_process)
                        results.append(res)
                        
                except json.JSONDecodeError:
                    print(f"⚠️ 跳過損壞的行: {line[:20]}...")
                except Exception as e:
                    print(f"🔥 發生錯誤: {e}")

        print(f"\n✅ 處理完成，共處理 {len(results)} 筆資料。")
        return results

bot = LiveMemoryAPI()

@app.route('/process', methods=['POST'])
def process():
    # 1. 自動獲取目前 .py 檔案所在的資料夾路徑
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 2. 組合出相對路徑 (假設檔案就在 .py 旁邊)
    file_name = "live_data.jsonl"
    full_path = os.path.join(current_dir, file_name)
    
    # 執行讀取
    bot.process_jsonl(full_path)
    
    # 處理 API 請求
    data = request.get_json(force=True)
    text = data.get("text", "")
    reply = bot.chat_logic(text)
    return jsonify({"reply": reply})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)