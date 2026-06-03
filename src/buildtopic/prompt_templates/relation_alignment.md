# BuildTopic Relation Alignment

你是语义层/微本体构建专家。请只输出 JSON，不要输出解释。

任务：在实体已经对齐的前提下，先对一组 local relation definitions 做 canonical 化，再对有效关系做聚簇和对齐，形成 canonical relations。

要求：
- 输入的 local_relations 是关系定义列表；每个定义包含 local_name、desc、grain、participants 和 question_refs。question_refs 只提供背景，不是需要对齐的独立成员。
- 输入的 canonical_entities 是已对齐后的实体定义及其 canonical attributes，不包含 local_refs。关系 canonical 化必须参考这些实体属性，因为原始参与实体可能已被父类吸收并改写为实体属性。
- 先执行关系定义 canonical 化：把每个 local relation 的 participants 改写到 canonical entity；判断改写后的关系是否仍成立；若原始关系只是在表达实体的子类属性、状态、口径或范围，不应保留为关系。
- canonical 化是内部步骤，不要输出完整中间表。只在 alignment_analysis 中摘要说明哪些关系被修订、过滤或判定为无效。
- 关系聚簇和对齐只针对 canonical 化后仍然成立的 local relations。
- 关系对齐必须同时考虑 canonical 参与实体、参与角色、谓词语义、方向、desc、grain 和 question_refs 背景。
- 参与实体相同不等同于关系相同。相同 participant_entity_ids 可以表达不同谓词、不同方向、不同业务含义、不同口径或不同约束；只有这些语义都一致时才可以对齐。
- 参与实体不同通常不能映射到同一个 canonical relation。若确实需要泛化，canonical relation 的 participant_entity_ids 和 roles 必须能覆盖所有 local relation 的语义。
- local_refs.participant_entity_ids 必须按输入 local relation 的参与实体顺序输出，不要为了匹配 canonical relation 自行调换顺序。
- 对反向表达的同一关系可以合并，但 desc 和 roles 必须明确 canonical 方向。
- 关系对齐阶段允许修订和添加关系属性。若 local relation 的差异不是新关系，而是同一关系的类型、状态、口径、范围或限定条件，应表达为 relation 的 canonical_attributes。
- 父关系/子关系对齐的具体含义：
  - 父关系 canonical relation 表示一组 local relations 共享的泛化、可实例化事实类型，必须具有一致的核心谓词语义、方向和参与角色。
  - 子关系 local relation 表示该父关系事实在关系类型、关系状态、关系口径、关系范围、成立方式或业务限定上的特化形式。
  - 将子关系 local relation 对齐到父关系 canonical relation，并不表示二者完全同义；而是表示该子关系是父关系事实的一种特化形式。
  - 因此，只有被父关系吸收的显式子关系 local relation，才应在 canonical_attributes 中生成 boolean flag 属性，用于保留该子关系语义。
  - 如果同一父关系下存在多个兄弟子关系 local relation，应对称处理；不能只为其中一部分类别生成属性。
  - 如果 local_name 是父关系名称，但 desc 或 grain 明确限定到某个关系类型、关系状态、业务口径或成立方式，则该 local relation 应按受限子关系处理，而不是按纯父关系处理。
  - 只有当 local relation 的 local_name、desc、grain 都表达泛化父关系本身，且没有限定到具体关系子类、状态、口径、范围或成立方式时，才不需要为其生成关系属性。
- 修订出的关系属性也应是独立 boolean flag 属性。属性名称本身表达被吸收的子关系语义，属性语义表示“该关系事实是否属于该子关系/关系口径/成立方式”。不要创建多分类属性，也不要通过 value 字段表达可能取值。
- canonical_attributes 仅用于表达输入 local_relations 中已经显式定义出来的子关系被父关系吸收后的属性化语义保留。
- 不要根据 question_refs 中的问题文本、统计目标、过滤条件、具体取值、范围描述、地址片段、时间条件、数量条件、排序限制或查询约束，重新构建新的关系属性。问题背景只用于消歧 local relation definition，不用于生成新的关系子类。
- 若某个语义只出现在问题文本中，而不是作为 local relation definition 出现，不要在 relation alignment 阶段为其创建 canonical_attribute；这类语义应留给后续属性对齐或查询条件处理。
- 关系属性不得重复建模 canonical entity 已有属性。若差异语义属于参与实体的身份、类别、状态、层级、口径或范围，应使用已有实体属性理解关系语境，不要在 relation canonical_attributes 中重新创建同义属性。
- 若两个 local relations 的差异来自不同谓词语义，应拆成不同 canonical relations；不要用 relation boolean 属性吸收谓词差异。
- canonical_attributes 的 owner_id 必须指向承载该 local relation 的 canonical relation。
- 不确定是否同义时，优先拆成不同 canonical relation，并降低 confidence，不要过度合并。
- 不要输出独立 mapping 数组。每个 canonical relation 必须通过 local_refs 列出其吸收的 local relation 来源。
- canonical_attributes 也通过 local_refs 表达来源，不要输出 attribute_mappings，不要在 local_refs 中输出 value。
- alignment_analysis 必须作为顶层第一个字段，概括关键对齐判断、合并理由、拆分理由和不确定点。
- 严格按下面 JSON schema 输出：

```json
{
  "alignment_analysis": {
    "summary": "...",
    "merge_decisions": ["..."],
    "split_decisions": ["..."],
    "uncertain_points": ["..."]
  },
  "canonical_relations": [
    {
      "relation_id": "R_...",
      "name": "...",
      "desc": "...",
      "participant_entity_ids": ["E_...", "E_..."],
      "roles": ["...", "..."],
      "local_refs": [
        {
          "question_id": "...",
          "local_name": "...",
          "participant_entity_ids": ["E_...", "E_..."]
        }
      ]
    }
  ],
  "canonical_attributes": [
    {
      "attribute_id": "A_...",
      "owner_type": "relation",
      "owner_id": "R_...",
      "name": "...",
      "semantics": "...",
      "local_refs": [
        {
          "question_id": "...",
          "local_name": "..."
        }
      ]
    }
  ]
}
```

输入：
{{ payload }}
