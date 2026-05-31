from supabase import create_client
from langchain_ollama import OllamaEmbeddings
import ollama

# =========================
# Supabase
# =========================

SUPABASE_URL = ""
SUPABASE_KEY = ""


supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================
# Embedding Model
# =========================

embedding_model = OllamaEmbeddings(
    model="nomic-embed-text"
)

# =========================
# 使用者輸入
# =========================
while True:

    question = input("你：")

# =========================
# 問題 embedding
# =========================

    query_embedding = embedding_model.embed_query(question)

# =========================
# Vector Search
# =========================

    response = supabase.rpc(
        "match_documents",
        {
            "query_embedding": query_embedding,
            "match_count": 5,
            "filter": {}
        }
    ).execute()

    results = response.data

# =========================
# 沒找到
# =========================

    if len(results) == 0:

        print("\nAI：")
        print("[NO_REPLY]")
        exit()

# =========================
# 建立候選句子
# =========================

    choices = []

    for r in results:

        text = r["content"]

        if "觀眾：" in text:

            reply = text.split("觀眾：")[-1].strip()

            choices.append(reply)

    # 去重複
    choices = list(set(choices))


# =========================
# 沒候選
# =========================

    if len(choices) == 0:

        print("\nAI：")
        print("[NO_REPLY]")
        exit()

# =========================
# 給 Qwen rerank
# =========================

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

# =========================
# Qwen Rerank
# =========================

    response = ollama.chat(
        model="qwen3:8b",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        options={
            "temperature": 0
        }
    )

    reply = response["message"]["content"].strip()

# =========================
# 輸出
# =========================

    print("\nAI：")
    print(reply)
    