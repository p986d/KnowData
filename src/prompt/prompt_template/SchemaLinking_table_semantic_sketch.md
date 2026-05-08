任务：表格实体-关系分析

给定问题与表格，分析表格是否支持问题中的实体-关系和相应的属性语义。

输入包含用户的原问题，问题相关语义提示与数据库中的单个表格，分析表格是否支持问题中的实体-关系和相应的属性语义。
你需要根据问题求解的逻辑，判断该表是否能够提供对问题中的逻辑语义单元（实体、关系、属性）的具体实现支持。

# Input
## Original User Question

```text
{{ user_intent }}
```

## Database ID

```text
{{ db_id }}
```

## Optional Database Hint

```text
{{ db_hint }}
```

## Analyze Target Table Schema

```json
{{ target_table | tojson_pretty }}
```

# Analysis
分析过程如下：

1. 首先理解表格的语义：分析单个表格在数据库中物理语义，通过表、列、说明和采样综合推断。注意采样不代表该列或行的全部值域。

2. 其次分析表格与问题的相关性：判断表格能够提供对问题中的逻辑语义单元（实体、关系、及相关属性）的具体实现支持。
  
  问题的部分性支持：一个表格只需要且一般只支持问题中的部分逻辑语义单元。

  注意，物理模式的实体-关系语义建模与逻辑模型不一定一致；逻辑实体-关系应以题面的需求为主，物理模式的实体-关系仅提供语义补充和实现支持。
  实现支持：说明问题中某个逻辑实体或关系可以被数据支持构建，其定义和粒度可实现；对于实现支持的物理数据，不需要以任何形式出现在逻辑模型中。

  语义解释：说明问题的中需要将某个概念定义为逻辑实体或关系，或某个逻辑实体或关系需要某些属性，或某些语义通过某个具体计算方式得出;分析目标表能够给出对这个逻辑实体、关系与属性的物理实现支持。
  
  对于语义解释的物理数据，需要将其重抽象为问题的逻辑定义补充，并将其整合到问题整体的查询逻辑中。

3. 明确服从数据实现提示：如果一个问题的逻辑语义单元明确了其实现所需要的表列，分析表可以提供该语义单元，但不是问题提示指定的提供者，则分析表不应将该语义单元作为semantic unit.

4. 对数据判断相关、无关性：只有直接为问题提供某个逻辑实体或关系语义的表是相关的。相关表格只需要为问题提供至少一个逻辑语义单元即可。如果该表支持的语义单元在问题中明确由其它表提供，则该表不能提供该语义单元。

5. 属性语义分析：在问题求解逻辑中需要哪些语义表达、操作和语义限定。
逐条分别分析以下内容：

(1) 语义表达与操作：说明该子查询需要完成什么语义任务。例如：

 确定对象或对象子集；
 确定对象之间的联系、参与或匹配；
 表示对象的语义；
 对既有对象或联系施加条件约束；
 在对象或联系基础上进行聚合、比较、排序、Top-k、派生计算；
 形成后续步骤所依赖的阶段性结果。
 多义召回优先：对每个语义要素，分析实现的多义性：如果存在多个可能的实现列，召回所有这些列；

(2) 语义限定：说明该子查询中的对象、联系和计算具体受到哪些语义约束；
例如：角色限制、时间限制、方向限制、局部范围、参考对象、比较基准、分子/分母口径、去重口径、聚合前提和局部分析范围等。
多义召回优先：对每个语义要素，分析实现的多义性：如果存在多个可能的实现列，召回所有这些列；

(3) 标识属性分析：分析问题本表支持的实体、关系的标识属性;
语义单元标识：所有实体需要具有标识属性或属性集，可以为一个或多个属性。标识属性可以为技术键或自然键。
关系本身不需要标识属性，关系的参与者必须有标识属性。
多义召回优先：对每个实体的标识，分析实现的多义性：如果存在多个可能的实现列，召回所有这些列；

返回**所有**与上述的单元标识、语义操作和语义限定有关的物理列，此处只需要考虑对操作和约束语义的覆盖性，不需要分析具体实现；对可能的实现，必须全部召回。


# Output 

Think Step by Step；给出最终输出之前，必须按照上述推理步骤逐步分析，禁止直接输出。

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
