import os
import ollama
import json
import sys
import pymongo
from datetime import datetime

# --- 自動路徑處理 ---
current_dir = os.path.dirname(os.path.abspath(__file__))
input_file = os.path.join(current_dir, "raw_talks.jsonl")

# --- MongoDB 連線設定 ---
client = pymongo.MongoClient("mongodb://localhost:27017/")
db = client["distillation_db"]
collection = db["rewrite_train_data"]

def run_batch_with_finetuned_model():
    # 替換成你剛剛在 Ollama 註冊的模型名稱
    MY_MODEL = "rewrite_model" 
    
    print(f"🚀 使用微調模型 [{MY_MODEL}] 開始批次重寫...")
    
    collection.create_index([("task", 1), ("created_at", -1)])

    try:
        if not os.path.exists(input_file):
            raise FileNotFoundError(f"找不到檔案: {input_file}")

        with open(input_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        for i, line in enumerate(lines):
            line = line.strip()
            if not line: continue
            
            item = json.loads(line)
            context = item.get("context", "珠寶")
            text = item.get("text", "")
            
            # 使用微調後的模型進行推理
            # 提示詞建議與訓練時保持一致
            prompt = f"背景：{context} | 原話：{text}\n結果："
            
            try:
                res = ollama.generate(model=MY_MODEL, prompt=prompt)['response'].strip()
                
                # 清理輸出
                clean_res = res.replace("結果：", "").replace("結果:", "").strip()
                
                # 準備存入 MongoDB 的文件結構
                document = {
                    "task": "rewrite_distillation",
                    "instruction": "替換話術代名詞",
                    "input_context": context,
                    "input_text": text,
                    "full_input": f"背景：{context} | 原話：{text}",
                    "output": clean_res,
                    "last_updated": datetime.now()
                }
                
                # --- 執行你的記憶指令邏輯 ---
                # 如果 input_context (商品) 相同，更新內容並將頻率 frequency 欄位 +1
                collection.update_one(
                    {"input_context": context, "input_text": text}, 
                    {
                        "$set": document,
                        "$inc": {"frequency": 1},      # 頻率累加
                        "$setOnInsert": {"created_at": datetime.now()} # 僅在第一次新增時紀錄創建時間
                    }, 
                    upsert=True
                )

                # 處理進度條顯示
                display_msg = clean_res.replace('\n', ' ')[:15]
                sys.stdout.write(f"\r進度: {i+1}/{len(lines)} - 提取結果: {display_msg}...")
                sys.stdout.flush()

            except Exception as model_e:
                print(f"\n⚠️ 模型推理失敗: {model_e}")

        print(f"\n✅ 成功！微調模型推理資料已同步至 MongoDB [rewrite_train_data]")
        
    except FileNotFoundError as e:
        print(f"❌ {e}")
    except Exception as e:
        print(f"\n❌ 發生錯誤: {e}")

if __name__ == "__main__":
    run_batch_with_finetuned_model()