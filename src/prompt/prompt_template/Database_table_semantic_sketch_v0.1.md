任务：数据库表组语义单元分析

给定用户问题和一个数据库表组，判断这个表组是否能为问题提供实体、关系、属性、约束或计算语义的物理实现支持。

表组由结构相同、名称模式相近的一组物理表组成。输入中的 representative_table 是代表表，它的列结构适用于 member_tables 中的所有成员表。你需要把这个表组当作一个语义候选单元来分析，而不是只分析单个物理表。

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

## External Knowledge

```text
{{ external_knowledge }}
```

## Target Table Group


```json
{{ target_table_group | tojson_pretty }}
```


# Analysis
分析过程如下：


## Step 0
此处给出的是一个数据库的多个表格集中的一个表格组，重点分析单个表的语义。
首先理解表格的语义：分析单个表格在数据库中物理语义，通过表、列、说明和采样综合推断。注意采样不代表该列或行的全部值域。

## Step 1 
1. 首先遍历提示中所有的物理表列，找到与问题有关的所有提示和与分析目标表有关的所有提示。
2. 相关提示分析：如果表在提示中提及，必须明确该表的物理语义。到其它表的关联。
3. 进一步，明确该提示说明了哪些逻辑实体、关系的属性需要通过该表实现。

## Step 2
该表是否能用于问题查询的具体实现：

判断表格能够提供对与问题相关的什么逻辑语义单元（实体、关系、属性）的具体实现的支持。

注意分析提示与外部知识：外部知识可能与问题有关，也可能无关。有关的外部知识作为问题的补充说明，其语义也需要得到具体实现支持。

此处只需要明确对问题语义的可能支持，不需要判断必要性、唯一性与必须性。不得假设本表支持的语义应通过其它表表示。

但是，这个限定作用也可以通过其他表（如居住关系表本身）来实现，因此该表对于此问题不是必需的。

## Step 3

判断**部分**语义支持性：
能用于问题查询，提供**任何**相关语义实现的表即为部分语义支持的表。这个语义要素可以是实体、关系或属性。

一个查询通常由多个部分语义支持的表构成，**每个表为问题提供部分语义**。除去此处的分析目标表外，其它语义内容也会通过数据库中其它的表实现，这些表在此处未给出，但是认为其它语义也有相应实现。唯一需要考虑的是这个表是否能够为问题提供语义要素支持。


## Step 4
属性语义分析：该表提供的逻辑语义单元在问题求解逻辑中需要哪些语义表达、操作和语义限定，逻辑语义单元应该如何标识。
  对该表支持的逻辑实体或关系，依次分析如下内容：
  
### Step 4.1 

  分析语义表达与操作：逻辑语义单元在问题中涉及哪些语义表示和操作。例如：
  确定对象或对象子集；
  确定对象之间的联系、参与或匹配；
  表示对象的语义；
  对既有对象或联系施加条件约束；
  在对象或联系基础上进行聚合、比较、排序、Top-k、派生计算；
  形成后续步骤所依赖的阶段性结果。
  多种不同实现的综合考量：  
  * 考虑语义表示和操作的多种实现：可能在物理表中有多种实现方式，使用不同的列；需要返回所有可能的实现列，不能只局限于一种表达方式；

### Step 4.2 
  分析语义限定：逻辑语义单元的对象、联系和计算在问题中具体受到哪些语义约束；
  例如：角色限制、时间限制、方向限制、局部范围、参考对象、比较基准、分子/分母口径、去重口径、聚合前提和局部分析范围等。
  * 考虑语义限定的多种实现：可能在物理表中有多种实现方式，使用不同的列；需要返回所有可能的实现列，不能只局限于一种表达方式；

### Step 4.3
  标识属性分析：分析问题本表支持的实体、关系的标识属性;
  语义单元标识：所有实体和关系需要具有标识属性或属性集，可以为一个或多个属性。标识属性可以为技术键或自然键。
  * 考虑语义标识的多种实现：可能在物理表中有多种实现方式，使用不同的列；需要返回所有可能的实现列，不能只局限于一种表达方式；


返回**所有**与上述的单元标识、语义操作和语义限定和元数据控制有关的物理列，此处只需要覆盖性，不需要分析具体实现；对可能的实现，必须全部召回。

## Step 5
  其它要求：
  返回每个数据表的实现/系统管理属性： 分析物理表支持的逻辑实体、关系相关的物理实现/系统管理属性；所有名为Delete_flag/Is_valid列都要返回。
  返回每个表的主外键属性；
  这些属性不用于支持逻辑建模，单纯作为物理表管理，返回在other columns。

# Output 

Think Step by Step；给出最终输出之前，必须严格按照上述推理步骤逐步分析，禁止直接输出。

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
  "is_relevant_supportive": true,
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
  ],
  "other_columns":List[str]
}
```

Rules:

- `is_relevant_supportive=false` 时，`member_table_scope` 必须是 `none`，`selected_member_tables`、`linked_columns`、`semantic_units` 都必须为空数组。
- `member_table_scope=all_members` 时，`selected_member_tables` 可以为空，也可以列出所有成员表。
- `member_table_scope=selected_members` 时，必须在 `selected_member_tables` 中列出具体成员表全名。
- `participants` 只在 relationship 单元中使用；entity 单元使用空数组。
- 每个 attribute 和 participant 都应尽量引用具体物理列作为 `evidence_columns`。
