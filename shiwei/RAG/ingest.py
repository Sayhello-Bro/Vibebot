from langchain_community.document_loaders import TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings

from supabase import create_client

# =========================
# Supabase 設定
# =========================

SUPABASE_URL = ""
SUPABASE_KEY = ""

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================
# 讀取 txt 文件
# =========================

loader = TextLoader(
    "simple.txt",
    encoding="utf-8"
)

documents = loader.load()

# =========================
# Chunk 設定
# =========================
# 根據我們剛剛在 n8n 討論的：
#
# - 一組對話 = 一個 chunk
# - overlap 不需要
# - chunk 不要太大
#

text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=30,
    chunk_overlap=0,
    separators=["\n\n"]
)

docs = text_splitter.split_documents(documents)

# =========================
# Embedding Model
# =========================

embedding_model = OllamaEmbeddings(
    model="nomic-embed-text"
)

# =========================
# 寫入 Supabase docs table
# =========================

for doc in docs:

    text = doc.page_content.strip()

    # 避免空 chunk
    if not text:
        continue

    embedding = embedding_model.embed_query(text)

    data = {
        "content": text,
        "metadata": {
            "source": "simple.txt"
        },
        "embedding": embedding
    }

    supabase.table("docs").insert(data).execute()

    print(f"寫入成功:\n{text}\n")

print("全部完成")