import os
import json
import pymongo
from datetime import datetime

# --- 修改後的路徑處理 ---
# 取得目前執行腳本所在的資料夾絕對路徑
current_dir = os.path.dirname(os.path.abspath(__file__))
# 組合出檔案的絕對路徑
INPUT_FILENAME = os.path.join(current_dir, "jewelry_data.jsonl")

# --- 設定區 ---
MONGODB_URI = "mongodb://localhost:27017/"
DB_NAME = "jewelry_live_db"
COLLECTION_NAME = "comment"

def sync_jsonl_to_mongodb():
    try:
        client = pymongo.MongoClient(MONGODB_URI)
        db = client[DB_NAME]
        collection = db[COLLECTION_NAME]
        collection.create_index("raw_text")

        # 這裡會自動帶入上面組合好的絕對路徑
        if not os.path.exists(INPUT_FILENAME):
            print(f"❌ 錯誤：仍然找不到檔案。")
            print(f"📂 程式正在搜尋這個位置：{INPUT_FILENAME}")
            print(f"💡 請確認 jewelry_data.jsonl 是否與此程式碼放在同一個資料夾內。")
            return

        print(f"🚀 檔案路徑確認：{INPUT_FILENAME}")

        count = 0
        with open(INPUT_FILENAME, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                # 解析 JSONL 每一行
                data = json.loads(line)
                raw_text = data.get("raw_text")
                comment = data.get("comment")

                # 2. 執行更新邏輯
                # 如果 raw_text (主播原話) 相同，我們視為同一個記憶主體
                result = collection.update_one(
                    {"raw_text": raw_text},
                    {
                        "$set": {
                            "comment": comment,
                            "last_updated": datetime.now()
                        },
                        "$inc": {"frequency": 1},  # 頻率欄位增加 1
                        "$setOnInsert": {"created_at": datetime.now()} # 僅在第一次新增時紀錄
                    },
                    upsert=True  # 如果不存在則新增，存在則更新
                )
                count += 1

        print(f"✅ 同步完成！共處理 {count} 筆資料。")
        print(f"📍 資料庫：{DB_NAME} | 集合：{COLLECTION_NAME}")

    except Exception as e:
        print(f"❌ 發生異常：{e}")

if __name__ == "__main__":
    sync_jsonl_to_mongodb()