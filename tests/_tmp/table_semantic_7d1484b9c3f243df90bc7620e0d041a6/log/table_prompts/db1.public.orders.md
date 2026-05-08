任务：表格实体-关系分析

给定问题与表格，分析表格是否支持问题中的实体-关系和相应的属性语义。

输入包含用户的原问题，问题相关语义提示与数据库中的单个表格，分析表格是否支持问题中的实体-关系和相应的属性语义。
输入同时包含一组子问题说明问题求解逻辑，你需要根据问题求解的逻辑，判断该表是否能够提供对问题中的逻辑语义单元（实体、关系、属性）的具体实现支持。

分析过程如下：
首先理解表格的语义：分析单个表格在数据库中物理语义。
其次分析表格与问题的相关性：判断表格能够提供对问题中的逻辑语义单元（实体、关系、属性）的具体实现支持。

列举提示中的物理表列与问题中的查询逻辑，分析物理模式语义与查询逻辑语义；
  
  首先综合分析物理表列与问题中的计算逻辑，理解相关物理模式与查询逻辑的语义。

  注意，物理模式的实体-关系语义建模与逻辑模型不一定一致；以题面的逻辑模型为主，物理模式的实体-关系仅提供语义补充和实现支持。
  实现支持：说明问题中某个逻辑实体或关系可以被数据支持构建，其定义和粒度可实现；对于实现支持的物理数据，不需要以任何形式出现在逻辑模型中。

  语义解释：说明问题的中需要将某个概念定义为逻辑实体或关系，或某个逻辑实体或关系需要某些属性，或某些语义通过某个具体计算方式得出;分析目标表能够给出对这个逻辑实体、关系与属性的物理实现支持。
  
  对于语义解释的物理数据，需要将其重抽象为问题的逻辑定义补充，并将其整合到问题整体的查询逻辑中。



  对数据提示判断相关、无关性：只有直接为题面补充说明逻辑语义的提示才是问题相关的提示。

分析数据实现提示：如果一个问题的逻辑语义单元明确了其实现所需要的表列，与该表列不一致的其它表则不得作为该语义的提供者。


逻辑语义单元应理解为问题所需要的逻辑实体、关系和相关属性。
逻辑实体是问题中具有确定语义身份、明确基础粒度、可独立指称的对象类。实体应理解为类型概念，是一个包含多个实例的集合。
逻辑关系是**两个或多个逻辑实体的角色**之间具有明确独立语义的关联。


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

## Optional External Knowledge

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