# MongoDB Schema

Each document stores one example sentence and its embedding.

```json
{
  "_id": "ObjectId",
  "liveSessionId": "session_001",
  "productHint": "保健食品",
  "topicHint": "膠原蛋白 / 美容",
  "speakerText": "這個每天吃會不會太多？",
  "replyText": "每天吃可以，但建議照建議量，重點是穩定補充。",
  "embedding": [0.12, -0.03, 0.88],
  "embeddingModel": "nomic-embed-text:latest",
  "styleHint": "warm",
  "metadata": {
    "source": "manual",
    "lang": "zh-TW"
  },
  "createdAt": "2026-08-19T00:00:00.000Z"
}
```

Recommended indexes:

- vector search index on `embedding`
- filter indexes on `liveSessionId`, `productHint`, `topicHint`, `styleHint`, `metadata.lang`

Retrieval should use MongoDB `$vectorSearch`.

