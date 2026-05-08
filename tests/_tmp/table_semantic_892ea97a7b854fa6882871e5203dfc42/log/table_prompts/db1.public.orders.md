任务：表格实体-关系分析

给定问题与表格，分析表格是否支持问题中的实体-关系和相应的属性语义。

输入包含用户的原问题，问题相关语义提示与数据库中的单个表格，分析表格是否支持问题中的实体-关系和相应的属性语义。
输入同时包含一组子问题说明问题求解逻辑，你需要根据问题求解的逻辑，判断该表是否能够提供对问题中的逻辑语义单元（实体、关系、属性）的具体实现支持。

分析过程如下：
1. 首先理解表格的语义：分析单个表格在数据库中物理语义。


2. 其次分析表格与问题的相关性：判断表格能够提供对问题中的逻辑语义单元（实体、关系、属性）的具体实现支持。

  注意，物理模式的实体-关系语义建模与逻辑模型不一定一致；逻辑实体-关系应以题面的需求为主，物理模式的实体-关系仅提供语义补充和实现支持。
  实现支持：说明问题中某个逻辑实体或关系可以被数据支持构建，其定义和粒度可实现；对于实现支持的物理数据，不需要以任何形式出现在逻辑模型中。

  语义解释：说明问题的中需要将某个概念定义为逻辑实体或关系，或某个逻辑实体或关系需要某些属性，或某些语义通过某个具体计算方式得出;分析目标表能够给出对这个逻辑实体、关系与属性的物理实现支持。
  
  对于语义解释的物理数据，需要将其重抽象为问题的逻辑定义补充，并将其整合到问题整体的查询逻辑中。

3. 明确服从数据实现提示：如果一个问题的逻辑语义单元明确了其实现所需要的表列，分析表可以提供该语义单元，但不是问题提示指定的提供者，则分析表不应将该语义单元作为semantic unit.

4. 该表可以为问题提供逻辑实体或关系，则该表需要被标注为有关的。进一步分析问题涉及该实体、关系的语义操作和语义限定。
语义操作：说明该子查询需要完成什么语义任务，需要哪些属性支持。例如：

 确定对象或对象子集；
 确定对象之间的联系、参与或匹配；
 表示对象的特定属性；
 对既有对象或联系施加条件约束；
 在对象或联系基础上进行聚合、比较、排序、Top-k、派生计算；
 形成后续步骤所依赖的阶段性结果。

语义限定：说明该子查询中的对象、联系和计算具体受到哪些语义约束；需要全面表述问题语义包含的约束，不得遗漏；
语义约束可以包括：角色限制、时间限制、方向限制、局部范围、参考对象、比较基准、分子/分母口径、去重口径、聚合前提和局部分析范围等。

物理表的列视为语义实现，所有与上述的语义操作和语义限定有关的都需要返回作为语义实现。


明确数据实现提示：如果一个问题的逻辑语义单元明确了其实现所需要的表列，分析表可以提供该语义单元，但不是问题提示指定的提供者，则分析表不应将该语义单元作为semantic unit.
。
  对数据判断相关、无关性：只有直接为题面补充和实现逻辑语义的问题相关的提示。




## Original User Question

```text
show orders
```

## Resolution Logic

```text
Subquery 1: Find orders.
Subquery 2: Group orders by customer.
```

## Database ID

```text
db1
```

## Optional Database Hint

```text

```

## Analyze Target Table

```json
{
  "table_fullname": "db1.public.orders",
  "table_name": "orders",
  "columns": [
    {
      "column_fullname": "db1.public.orders.order_id",
      "column_name": "order_id"
    },
    {
      "column_fullname": "db1.public.orders.customer_id",
      "column_name": "customer_id"
    }
  ],
  "sample_rows": [
    {
      "order_id": 1,
      "customer_id": 10
    }
  ]
}
```

## Columns In This Table

```json
[
  "db1.public.orders.customer_id",
  "db1.public.orders.order_id"
]
```


## Output Format

```json
{
  "table_fullname": "...",
  "supported_question_semantics": [
    "..."
  ],
  "is_relevant": true / false,
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
            "..."
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
            "..."
          ]
        }
      ]
    }
  ]
}
```

Rules:

- `participants` are required only for `relationship` units; use an empty list for entity units.
- `semantic_units` may be empty when the target table provides no useful logical support for the question.
- Every attribute should cite concrete physical columns in `evidence_columns` when possible.
- Use stable logical names like the existing NL2ER style: `Patent`, `CPC Classification`, `Backward Citation`, not raw table names unless the table name is already the logical concept.
- Do not invent physical columns.