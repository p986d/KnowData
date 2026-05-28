# 任务定义

你需要基于问题序列分解中每个子问题的 question 和 ambiguity，构造查询逻辑层面的 question patch。

本阶段延续 `NL2ER_ERA_question_resolve_st1_v0.6.md` 的 ambiguity 处理逻辑，但目标不是重新生成完整问题序列，也不是补充新的 ambiguity，而是根据输入中已有的 ambiguity，构造不同查询语义解释下的子问题 question patch。

查询逻辑差分只考虑查询语义，不考虑物理实现形式。不要分析该查询语义在实际数据库中如何存储、如何连接、如何字段落地，也不要输出 ER、schema、表、列、join key 或 SQL 表达式。

---

# 输入

问题与 ambiguity 列表:

```json
{{ question_ambiguity_units }}
```

---

# 差分原则

1. 输入只包含每个子问题的 `question` 和 `ambiguity`，不得依赖其它上下文。
2. 输出只允许是 question patch，不输出 ambiguity、语义操作、语义限定、阶段性结果、ER 单元或物理实现。
3. 不要创造新的 ambiguity；只使用输入中已有的 ambiguity 作为差分来源。
4. 每个 question patch 表达一种 ambiguity 解释下，原子问题应该被替换成什么 question。
5. 如果某个 ambiguity 不改变查询语义，只是文字表述差异，则不要输出 patch。
6. 查询逻辑差分只考虑查询语义；不要考虑该语义在实际数据中如何实现。
7. patch question 必须是完整、可独立替换原子问题的自然语言查询问题；不要输出解释句、原因、说明或原 ambiguity。

---

# 输出格式

只输出合法 JSON，不要输出 Markdown 代码块。

```json
{
  "ok": true,
  "question_patches": [
    {
      "id": "QP1",
      "target_id": "SQ1",
      "question": ""
    }
  ]
}
```
