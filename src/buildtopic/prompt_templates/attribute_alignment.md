# BuildTopic Attribute Alignment

你是语义层/微本体构建专家。请只输出 JSON，不要输出解释。

任务：对同一个 owner 下的属性进行对齐。owner 可能是 canonical entity，也可能是 canonical relation。

要求：
- 只在同一个 owner 内对齐属性，不要跨 owner 合并。
- 输入中可能包含 existing_attributes，它们来自实体/关系对齐阶段的修订建议。
- 对 existing_attributes 和 local_attributes 一起对齐：可以保留、合并、重命名或补充 semantics，但不要丢失已有属性的业务含义。
- 如果 local_attributes 为空但 existing_attributes 不为空，仍需输出 existing_attributes 对应的 canonical_attributes 和已有/必要的 attribute_mappings。
- 输出 canonical_attributes 和 attribute_mappings。
- attribute_mappings 必须能追溯到 question_id、owner_type、owner_id、local_name。
- 如果 existing_attributes 和 local_attributes 都为空，输出两个空数组。
- 严格按下面 JSON schema 输出：

```json
{
  "canonical_attributes": [
    {
      "attribute_id": "A_...",
      "owner_type": "entity",
      "owner_id": "E_...",
      "name": "...",
      "semantics": "...",
      "question_ids": ["..."]
    }
  ],
  "attribute_mappings": [
    {
      "question_id": "...",
      "owner_type": "entity",
      "owner_id": "E_...",
      "local_name": "...",
      "attribute_id": "A_...",
      "confidence": 0.0
    }
  ]
}
```

输入：
{{ payload }}
