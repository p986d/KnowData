任务：数据库表组语义单元分析

给定用户问题和一个数据库表组，判断这个表组是否能为问题提供实体、关系、属性、约束或计算语义的物理实现支持。

表组由结构相同、名称模式相近的一组物理表组成。输入中的 representative_table 是代表表，它的列结构适用于 member_tables 中的所有成员表。你需要把这个表组当作一个语义候选单元来分析，而不是只分析单个物理表。

# Input

## Original User Question

```text
show orders by customer
```

## Decomposed Question Semantics

```text
Find orders.
```

## Database ID

```text
db
```

## Optional Database Hint

```text

```

## External Knowledge

```text

```

## Target Table Group

```json
{
  "table_fullname": "db.public.orders_2021",
  "table_name": "orders_2021",
  "description": "",
  "snapshot_path": "",
  "columns": [
    {
      "column_fullname": "db.public.orders_2021.order_id",
      "column_name": "order_id",
      "data_type": "NUMBER",
      "description": "",
      "sample_values": []
    },
    {
      "column_fullname": "db.public.orders_2021.customer_id",
      "column_name": "customer_id",
      "data_type": "NUMBER",
      "description": "",
      "sample_values": []
    }
  ],
  "sample_rows": [],
  "table_group": {
    "group_id": "group_0001_db.public.orders_2021",
    "grouping_method": "reforce_table_family",
    "group_key": {
      "namespace": "DB.PUBLIC",
      "normalized_table_name": "ORDERS_",
      "column_signature_hash": "0d617317c3ec"
    },
    "representative_table": "db.public.orders_2021",
    "family_size": 2,
    "member_tables": [
      {
        "table_fullname": "db.public.orders_2021",
        "namespace": "db.public",
        "table_name": "orders_2021",
        "snapshot_path": ""
      },
      {
        "table_fullname": "db.public.orders_2022",
        "namespace": "db.public",
        "table_name": "orders_2022",
        "snapshot_path": ""
      }
    ]
  },
  "question_context": {
    "sub_questions": "Find orders."
  }
}
```

# Analysis Requirements

1. 先理解 representative_table 的物理语义，包括表粒度、列含义、样例值和可能的业务对象。
2. 再把整个 table group 作为候选数据来源，判断它是否直接支持问题中的某些逻辑语义单元。
3. 一个表组只需要支持问题的一部分语义即可判为相关，但不能把仅由其他表提供的语义单元归给当前表组。
4. 如果问题包含时间、版本、地区、分区等限制，并且这些限制可以从 member table 名称中判断，请只选择符合限制的 member tables。
5. 如果问题没有限制具体成员表，并且该组语义相关，默认认为所有 member tables 都属于候选范围。
6. `linked_columns` 和每个 `evidence_columns` 只写 representative_table 中真实存在的列名或完整列名。程序会把这些列展开到 selected_member_tables。
7. 不要发明物理列。不要把不存在于 representative_table.columns 的字段写入 evidence columns。

# Output

最终只输出一个 JSON 代码块，格式如下：

```json
{
  "group_id": "...",
  "representative_table": "...",
  "member_table_scope": "all_members|selected_members|none",
  "selected_member_tables": [
    "..."
  ],
  "supported_question_semantics": [
    "..."
  ],
  "is_relevant": true,
  "linked_columns": [
    "column_name_or_representative_full_column_name"
  ],
  "semantic_units": [
    {
      "unit_type": "entity|relationship",
      "unit_name": "...",
      "desc": "...",
      "grain": "...",
      "attributes": [
        {
          "name": "...",
          "semantics": "...",
          "evidence_columns": [
            "column_name_or_representative_full_column_name"
          ]
        }
      ],
      "participants": [
        {
          "role": "...",
          "entity": "...",
          "anchor_attribute": [
            "..."
          ],
          "evidence_columns": [
            "column_name_or_representative_full_column_name"
          ]
        }
      ]
    }
  ]
}
```

Rules:

- `is_relevant=false` 时，`member_table_scope` 必须是 `none`，`selected_member_tables`、`linked_columns`、`semantic_units` 都必须为空数组。
- `member_table_scope=all_members` 时，`selected_member_tables` 可以为空，也可以列出所有成员表。
- `member_table_scope=selected_members` 时，必须在 `selected_member_tables` 中列出具体成员表全名。
- `participants` 只在 relationship 单元中使用；entity 单元使用空数组。
- 每个 attribute 和 participant 都应尽量引用具体物理列作为 `evidence_columns`。
- 逻辑名称使用稳定业务概念，例如 `Patent`、`CPC Classification`、`Backward Citation`，不要直接照抄物理表名，除非表名本身就是清晰业务概念。