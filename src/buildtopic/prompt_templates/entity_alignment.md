# BuildTopic Entity Alignment

你是语义层/微本体构建专家。请只输出 JSON，不要输出解释。

任务：对一组 local entity definitions 做实体对齐，形成 canonical entities。

要求：
- 输入的 local_entities 是实体定义列表；每个定义包含 local_name、desc、grain 和 question_refs。question_refs 只提供背景，不是需要对齐的独立成员。
- 只根据 local_name、desc、grain 和 question_refs 背景判断实体语义.

- 泛化父类实体对齐：实体允许且优先按父类对齐。父类是一个泛化型概念，其能包含多个问题中提及的不同形式的子类概念。
  若多个 local entity 只是同一抽象对象的子类、层级、角色、状态、口径或范围差异，或者某一些local_entity 象征父类，一些local_entity象征子类，这些情况可归并到同一个父类 canonical entity，以表达问题间共通的查询概念。父类的定义必须综合local_entity 和所有背景问题综合定义。
  
  父类/子类对齐的具体含义：
- 父类 canonical entity 表示一组 local entities 共享的泛化、可实例化对象类型。
- 子类 local entity 表示该父类对象在某个特定层级、类型、角色、身份、状态、业务口径上的具体实例对象集合。
- 将子类 local entity 对齐到父类 canonical entity，并不表示二者完全同义；而是表示该子类是父类对象的一种特化形式。
- 因此，所有被父类吸收的显式子类 local entity，都必须在 canonical_attributes 中生成一个 boolean flag 属性，用于保留该子类语义。
- 如果同一父类下存在多个兄弟子类 local entity，应对称处理；不能只为其中一部分类别生成属性。
- 如果 local_name 是父类名称，但 desc 或 grain 明确限定到某个子类、层级或业务口径，则该 local entity 应按受限子类处理，而不是按纯父类处理。
- 只有当 local entity 的 local_name、desc、grain 都表达泛化父类本身，且没有限定到具体子类、层级、状态或口径时，才不需要为其生成子类属性。

- 子类属性化表示：按父类对齐后，必须用 canonical_attributes 保留被吸收的子类语义。若一个local_entity原本为父类概念层级，则其在泛化父类对齐后不需要进行子类的属性化表示。 被吸收的子类语义必须表达为独立的 boolean flag 属性。属性名称本身表达子类语义，属性语义表示“是否属于该子类”。不要创建多分类属性，也不要通过 value 字段表达子类的可能取值。

- canonical_attributes 仅用于表达输入 local_entities 中已经显式定义出来的子类实体被父类吸收后的属性化语义保留。

- 不要根据 question_refs 中的问题文本、过滤条件、具体取值、范围描述、地址片段、时间条件、数量条件或查询约束，重新构建新的子类属性。问题背景只用于消歧 local entity definition，不用于生成新的实体子类。

- 若某个语义只出现在问题文本中，而不是作为 local entity definition 出现，不要在 entity alignment 阶段为其创建 canonical_attribute；这类语义应留给后续属性对齐或查询条件处理。

- 不要仅因为 local entity 之间存在上下级、包含、管辖、归属、层级差异或参与关系不同，就拆成多个 canonical entities。此类差异默认属于父类实体的属性或关系角色。
- 只有当两个 local entity 不共享同一父类对象语义，或无法用父类属性表达其差异时，才拆成不同 canonical entity。
- 不要输出独立 mapping 数组。每个 canonical entity 必须通过 local_refs 列出其吸收的 local entity 来源。
- canonical_attributes 也通过 local_refs 表达来源，不要输出 attribute_mappings，不要在 local_refs 中输出 value。
- alignment_analysis 必须作为顶层第一个字段，概括关键对齐判断、父类归并理由、拆分理由和不确定点。
- 严格按下面 JSON schema 输出：

```json
{
  "alignment_analysis": {
    "summary": "...",
    "parent_alignment_decisions": ["..."],
    "split_decisions": ["..."],
    "uncertain_points": ["..."]
  },
  "canonical_entities": [
    {
      "entity_id": "E_...",
      "name": "...",
      "desc": "...",
      "grain": "...",
      "local_refs": [
        {
          "question_id": "...",
          "local_name": "..."
        }
      ]
    }
  ],
  "canonical_attributes": [
    {
      "attribute_id": "A_...",
      "owner_type": "entity",
      "owner_id": "E_...",
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
