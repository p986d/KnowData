任务：数据库表组语义单元分析

给定用户问题和一个数据库表组，判断这个表组是否能为问题提供实体、关系、属性、约束或计算语义的物理实现支持。

表组由结构相同、名称模式相近的一组物理表组成。输入中的 representative_table 是代表表，它的列结构适用于 member_tables 中的所有成员表。你需要把这个表组当作一个语义候选单元来分析，而不是只分析单个物理表。

# Input

## Original User Question

```text
Using the "bitcoin_prices" table, please calculate the daily percentage change in trading volume for each ticker from August 1 to August 10, 2021, ensuring that any volume ending in "K" or "M" is accurately converted to thousands or millions, any "-" volume is treated as zero, only non-zero volumes are used to determine the previous day's volume, and the results are ordered by ticker and date.
```

## Database ID

```text
BANK_SALES_TRADING
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
  "table_fullname": "BANK_SALES_TRADING.BANK_SALES_TRADING.SHOPPING_CART_USERS",
  "table_name": "SHOPPING_CART_USERS",
  "description": "",
  "snapshot_path": "D:\\Workspace\\ReFoRCE\\spider2-snow\\resource\\databases\\BANK_SALES_TRADING\\BANK_SALES_TRADING\\SHOPPING_CART_USERS.json",
  "columns": [
    {
      "column_fullname": "BANK_SALES_TRADING.BANK_SALES_TRADING.SHOPPING_CART_USERS.user_id",
      "column_name": "user_id",
      "data_type": "NUMBER",
      "description": "",
      "sample_values": [
        1,
        2,
        4,
        5,
        7
      ]
    },
    {
      "column_fullname": "BANK_SALES_TRADING.BANK_SALES_TRADING.SHOPPING_CART_USERS.cookie_id",
      "column_name": "cookie_id",
      "data_type": "TEXT",
      "description": "",
      "sample_values": [
        "c4ca42",
        "c81e72",
        "a87ff6",
        "e4da3b",
        "8f14e4"
      ]
    },
    {
      "column_fullname": "BANK_SALES_TRADING.BANK_SALES_TRADING.SHOPPING_CART_USERS.start_date",
      "column_name": "start_date",
      "data_type": "TEXT",
      "description": "",
      "sample_values": [
        "2020-02-04",
        "2020-01-18",
        "2020-02-22",
        "2020-02-01",
        "2020-02-09"
      ]
    }
  ],
  "sample_rows": [
    {
      "user_id": 1,
      "cookie_id": "c4ca42",
      "start_date": "2020-02-04"
    }
  ],
  "table_group": {
    "group_id": "group_0014_BANK_SALES_TRADING.BANK_SALES_TRADING.SHOPPING_CART_USERS",
    "grouping_method": "reforce_table_family",
    "group_key": {
      "namespace": "BANK_SALES_TRADING.BANK_SALES_TRADING",
      "normalized_table_name": "SHOPPING_CART_USERS",
      "column_signature_hash": "258fd98b348d"
    },
    "representative_table": "BANK_SALES_TRADING.BANK_SALES_TRADING.SHOPPING_CART_USERS",
    "family_size": 1,
    "member_tables": [
      {
        "table_fullname": "BANK_SALES_TRADING.BANK_SALES_TRADING.SHOPPING_CART_USERS",
        "namespace": "BANK_SALES_TRADING.BANK_SALES_TRADING",
        "table_name": "SHOPPING_CART_USERS",
        "snapshot_path": "D:\\Workspace\\ReFoRCE\\spider2-snow\\resource\\databases\\BANK_SALES_TRADING\\BANK_SALES_TRADING\\SHOPPING_CART_USERS.json"
      }
    ]
  }
}
```


# Analysis
分析过程如下：

## Step 1
首先理解表格的语义：分析单个表格在数据库中物理语义，通过表、列、说明和采样综合推断。注意采样不代表该列或行的全部值域。


## Step 2
其次分析表格与问题的相关性：判断表格能够提供对问题中的逻辑语义单元（实体、关系、属性）的具体实现支持。
  
  问题的部分性支持：一个表格只需要且一般只支持问题中的部分逻辑语义单元。

  注意，物理模式的实体-关系语义建模与逻辑模型不一定一致；逻辑实体-关系应以题面的需求为主，物理模式的实体-关系仅提供语义补充和实现支持。
  实现支持：说明问题中某个逻辑实体或关系可以被数据支持构建，其定义和粒度可实现；对于实现支持的物理数据，不需要以任何形式出现在逻辑模型中。

  语义解释：说明问题的中需要将某个概念定义为逻辑实体或关系，或某个逻辑实体或关系需要某些属性，或某些语义通过某个具体计算方式得出;分析目标表能够给出对这个逻辑实体、关系与属性的物理实现支持。
  
  对于语义解释的物理数据，需要将其重抽象为问题的逻辑定义补充，并将其整合到问题整体的查询逻辑中。

## Step 3
数据判断相关性：为问题提供**至少一个逻辑语义单元实现**支持的表格即为相关表格。相关表格不需要完整提供所有的逻辑单元：单个表一般也不可能提供完整的逻辑单元。

* 不能因为表格没有覆盖问题全部或主要的逻辑单元，就认为表格无关；
* 不能因为某个逻辑和实体的属性没有被表格完整支持，就认为表格无关；

明确服从数据实现提示：如果本表可以提供该语义单元，但是该语义单元在问题中明确由其它表提供，则该表不应提供该语义单元。

## Step 4
属性语义分析：该表提供的逻辑语义单元在问题求解逻辑中需要哪些语义表达、操作和语义限定。
  对该表支持的逻辑实体或关系，依次分析如下内容：
  
### Step 4.1 

  分析语义表达与操作：逻辑语义单元在问题中涉及哪些语义任务。例如：
  确定对象或对象子集；
  确定对象之间的联系、参与或匹配；
  表示对象的语义；
  对既有对象或联系施加条件约束；
  在对象或联系基础上进行聚合、比较、排序、Top-k、派生计算；
  形成后续步骤所依赖的阶段性结果。


### Step 4.2 
  分析语义限定：逻辑语义单元的对象、联系和计算在问题中具体受到哪些语义约束；
  例如：角色限制、时间限制、方向限制、局部范围、参考对象、比较基准、分子/分母口径、去重口径、聚合前提和局部分析范围等。

### Step 4.3
  标识属性分析：分析问题本表支持的实体、关系的标识属性;
  语义单元标识：所有实体需要具有标识属性或属性集，可以为一个或多个属性。标识属性可以为技术键或自然键。
  关系本身不需要标识属性，关系的参与者必须有标识属性。

## Step 4.4 
  多种不同实现的综合考量：  
  * 考虑语义的不同实现方式：实体标识可能在物理表中有多种实现方式，使用不同的列；需要返回所有可能的实现列，不能只局限于一种表达方式；

返回**所有**与上述的单元标识、语义操作和语义限定有关的物理列，此处只需要考虑对操作和约束语义的覆盖性，不需要分析具体实现；对可能的实现，必须全部召回。


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