import ollama
import json
import os
import sys
import pymongo
from datetime import datetime

def run_fact_extraction_to_mongodb():
    # --- 設定 ---
    current_dir = os.path.dirname(os.path.abspath(__file__))
    source_jsonl = os.path.join(current_dir, "raw_data.jsonl")
    
    # --- MongoDB 設定 ---
    try:
        client = pymongo.MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=2000)
        db = client["distillation_db"]
        target_collection = db["fact_train_data"]
        client.admin.command('ping')
        print("✅ 成功連接 MongoDB")
    except Exception as e:
        print(f"❌ 無法連接 MongoDB: {e}")
        return

    # --- 讀取來源 JSONL (逐行容錯處理) ---
    all_data = []
    if not os.path.exists(source_jsonl):
        print(f"❌ 找不到來源檔案: {source_jsonl}")
        return

    print(f"📖 正在解析 JSONL 檔案...")
    with open(source_jsonl, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                # 每一行單獨解析成 JSON 物件
                data_item = json.loads(line)
                all_data.append(data_item)
            except json.JSONDecodeError as e:
                # 如果某一行格式不對，打印出來但不中斷程式
                print(f"⚠️ 第 {line_num} 行格式有誤，已跳過。錯誤原因: {e}")

    total = len(all_data)
    if total == 0:
        print("⚠️ 檔案中沒有可用的有效資料。")
        return

    print(f"🚀 開始處理 {total} 筆資料並存入資料庫 [fact_train_data]...")

    for i, item in enumerate(all_data):
        # 這裡根據你 JSONL 的 key 抓取資料，如果是 'input'
        raw_text = item.get("input", "")
        if not raw_text:
            continue

            # --- 重點：Prompt 和 邏輯必須在迴圈內 ---
        prompt = f"""
分析珠寶帶貨主播的話，提取商品事實。 
規則： 
1. 包含價格、規格(如產地、外觀、大小、重量等)、活動(如抽獎、特價、贈送)時，提取10字內事實，請勿補充額外內容。 
2. 回覆格式：商品名稱 事實
3. 若有一個商品有多個事實則加在後面
4. 若有多個商品則換行輸出
5. 不重要則只回覆 Ignore。

範例1(一個商品一個事實)：
原話：這顆水晶的色澤偏淡紫色
結果：水晶 淡紫色

範例2(一個商品多個事實)：
原話：螢石在紫外燈下會發光，而且現在特價2000
結果：螢石 發光 特價2000

範例3(多個商品多個事實):
原話：這個水晶跟玉鐲都是來自巴西，水晶偏藍，玉鐲偏綠
結果：水晶 巴西 偏藍
玉鐲 巴西 偏綠

範例4：
原話：哎呀剛才喝水差點嗆到，今天的直播間冷氣有點強
結果：Ignore

原話：{raw_text}
結果："""

        try:
            # 呼叫教師模型
            res = ollama.generate(model="qwen3:14b", prompt=prompt)['response'].strip()
            clean_fact = res.replace("商品名稱", "").replace("：", "").replace(":", "").strip()
            
            document = {
                "instruction": "提取珠寶事實",
                "input": raw_text,
                "output": clean_fact,
                "metadata": {
                    "task": "fact_distillation",
                    "created_at": datetime.now(),
                    "model": "qwen3:14b"
                }
            }

            # 存入 MongoDB
            target_collection.update_one(
                {"input": raw_text}, 
                {"$set": document}, 
                upsert=True
            )

            # 顯示進度
            safe_display = clean_fact.replace('\n', ' ')
            sys.stdout.write(f"\r進度: {i+1}/{total} | 提取結果: {safe_display[:20]}...")
            sys.stdout.flush()

        except Exception as e:
            print(f"\n⚠️ 處理第 {i+1} 筆失敗: {e}")

    print(f"\n\n✅ 全部處理完成！")

if __name__ == "__main__":
    run_fact_extraction_to_mongodb()