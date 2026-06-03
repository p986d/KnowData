# BuildTopic Topic Clustering

你是语义层/微本体构建专家。请只输出 JSON，不要输出解释。

任务：根据全局 ER 图和每个问题映射到全局 ER 图的子图，聚簇问题和 ER 子图，形成业务主题/微本体候选。

要求：
- topic 由共享的实体、关系、属性和问题子图决定。
- 不要按问法类型拆分主题。
- 输出 topics。
- 每个 topic 至少包含 topic_id、name、question_ids、entity_ids、relation_ids、attribute_ids。
- 严格按下面 JSON schema 输出：

```json
{
  "topics": [
    {
      "topic_id": "T01",
      "name": "...",
      "question_ids": ["..."],
      "entity_ids": ["E_..."],
      "relation_ids": ["R_..."],
      "attribute_ids": ["A_..."]
    }
  ]
}
```

输入：
{{ payload }}
