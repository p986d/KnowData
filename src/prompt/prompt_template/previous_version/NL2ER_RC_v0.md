# 用户需求建模任务

在未知数据库具体模式的前提下，根据用户的自然语言需求的表述口径、数据库物理模式的Hint与初步提取的实体-属性集，进一步补全用户需求中的关系语义与约束条件语义，并在必要时对已提取的实体-属性结果做最小必要修正。

本阶段的目标不是生成数据库 schema，而是形成将用户需求与数据库模式提示构建为完整 ER 表示，包括：

* `entity` 与 `attr`
* `relationship`
* `condition`

进一步的，基于上述结果的语义单位，重新构建需求表述 `normalized_intent`，并判断是否完全覆盖用户的需求。

本阶段进行 **关系定义与条件定义**，可以对已有实体-属性进行部分调整，并基于ER重新构建需求表述。

## 建模目标

你需要基于用户需求原文与已提取的实体-关系模型，完成以下任务：

1. 识别实体之间成立的稳定语义关系，形成 `relationship`
2. 识别对实体实例集或关系实例集成立范围起筛选或限制作用的条件，形成 `condition`
3. 可选的，仅在必要时对已有的实体、属性等做最小程度的修正
4. 基于最终的实体-属性-关系-条件，对用户需求进行结构化语义重述，形成完全基于ER语义模型中语义单元的文本描述 `normalized_intent`

## 输入：用户需求描述
{{user_intent}}

{% if db_hint %}
## 输入：用户提及的数据库模式

{{db_hint}}
{% endif %}

## 输入：已提取的实体-属性集
{{entity_attribute}}

## 核心建模原则

### 1. 主要基于现有实体-属性集

本阶段应优先复用已经定义的实体与属性，并在此基础上补全关系与条件约束。 仅在以下情况出现时，才允许对结果做修正：

* 实体边界明显不合理
* 某属性应更适合作为关系的属性
* 某属性遗漏且对当前关系或条件表达必需
* 标识符属性集明显不合理

修正应遵循 **最小必要原则** ，严格贴合用户需求语义及数据库物理模式hint,不可任意新增语义单元。

### 2. 本阶段构建语义结构化表示

本阶段识别：

* 哪些实体之间存在稳定语义关系
* 哪些条件在筛选实体实例集、限制哪些关系成立范围

本阶段不直接输出：

* 需求的SQL查询语句
* 字段映射路径
* 数据对齐流程
* 查询执行过程说明

### 3. Relationship 与 Condition 的区分

* `relationship` 表示多个实体之间成立的、具有稳定业务含义的语义关系谓词，回答的是“哪些对象之间存在什么语义关系”
* `condition` 表示对某个实体实例集或关系实例集施加的成立范围限定，回答的是“哪些对象或关系被保留 / 被限制”

### 4. Normalized Intent 的作用

`normalized_intent` 用于基于当前建模结果，对用户原始需求进行结构更清晰的语义重述，以检查是否完成了对原始需求的完整转化。`normalized_intent`必须严格使用现有实体-关系模型里的语义单元，不能引入新的语义单元。

## 关系建模：Relationship 的定义

`relationship` 表示多个实体通过不同语义角色参与的、具有稳定业务含义的语义关系。关系的定义需要覆盖用户需求说明和数据库模式Hint定义，将实体完整的组织起来；

它回答的是：

* 哪些实体共同参与了该关系
* 每个实体在该关系中扮演什么语义角色
* 每个参与角色的基数与参与约束如何
* 该关系实例自身是否带有属性

每个 `relationship` 包含：

* `name`
* `desc`
* `participants`
* `attr`

其中：

* `name`：该关系的规范化语义名称，应直接表达关系成立的业务语义：
* `desc`：对该关系业务含义的简要说明
* `participants`：参与该关系的实体列表
* `attr`：该关系实例本身的属性

### Relationship 约束：不得定义传递依赖关系

  关系定义必须是原子性的：不得定义具有传递依赖的关系

### Relationship 命名要求

关系名应：

* 表达关系核心语义的谓词 predicate + 参与实体 participants 的角色名 role
* 严格贴合用户表述口径与数据库物理模式的hints 不额外添加未显式提及的关系

优先使用这类表达：

* `customer_places_order`
* `employee_works_for_department`
* `enrollment_enrolled_by_student`
* `enrollment_enroll_in_course`

禁止使用明显偏数据操作口径的关系命名关系，例如：

* `join`
* `lookup`
* `match`
* `map`
* `derive`
* `attach_by`
* `link_via`

除非这些词本身就是用户业务语义的一部分，否则不要用于关系名。

### participants 的定义

`participants` 表示参与该关系的实体及其在关系中的语义角色。

每个 participant 包含：

* `entity`
* `role`
* `cardinality`

其中：

* `entity`：必须填写当前建模中已定义的实体名
* `role`：该实体在该关系中的语义角色
* `cardinality`：participant 在当前关系中的最大基数约束。其含义为：在固定其他所有 participants 实例的前提下，语义上该角色应可对应多少个实体实例；
  只能取：

  * `one`
  * `many`
  * `unknown`

要求：

* 若用户需求语义足以判断，则给出保守但明确的 `cardinality`， 若无法从需求中稳定判断，则使用 `unknown`，不要为了形式完整而强行猜测

### Relationship Attribute 的定义

`relationship.attr` 是一个可选域，表示该关系类型本身的属性语义。仅当某个属性描述的是参与实体之间形成的关联，而不是任何一个关系参与实体的属性时，才应将其建模为 `relationship.attr`。

例如：

* 学生-选课-课程关系上的成绩
* 教师-任职-部门关系上的开始时间 / 结束时间

不要把明显属于某个实体本身的属性错误挂到关系上。

## 条件建模：Condition 的定义

`condition` 表示对某类实体或某类关系集，施加的语义限定条件，其核心作用是：

* 缩小某个实体的候选实例范围
* 或限制某类关系在什么条件下成立

### 判定标准

一条语义说明可判为 `condition`，当且仅当：

1. 它有明确的作用对象，表达一个成立 / 不成立的限定条件
2. 加上该条件后，该对象的实例范围会被缩小，或该关系的成立范围会被限制

### 常见形式

`condition` 可以包括但不限于：

* 时间限定
* 空间限定
* 身份 / 状态 / 类型限定
* 来源限定
* 关联对象属性限定
* 关系存在性限定
* 聚合阈值限定
* 相对条件或集合成员条件

### 不属于 condition 的内容

以下内容通常不应写入 `condition`：

* 对齐规则
* 映射路径
* 派生步骤
* 输出字段来源说明
* 分组、排序、排名
* 数据操作层面的连接、匹配或计算过程

其中：

* 聚合阈值本身若用于筛选对象，可写入 `condition`
* 但排序、展示、编号、保留小数位等结果组织要求，不写入 `condition`

### 每个 condition 包含

* `name`
* `target`
* `basis`
* `condition_desc`

其中：

* `name`：该条件的规范化名称
* `target`：该条件主要作用于谁，即被筛选的实体实例集或被限制成立范围的关系实例集，只能填写已定义的实体名或关系名
* `basis`：该条件定义所依赖的必要最少语义单位
* `condition_desc`：基于 `basis`的语义单位对条件的描述

### basis 的定义

`basis` 表示该条件在后续数据验证时所依赖的最小语义单位集合，可引用以下三类已建模单元：

* 实体属性：`entity_name.attr_name`
* 关系名：`relationship_name`
* 关系属性：`relationship_name.attr_name`

要求：

* `basis` 应尽量最小化，只保留判断该条件成立所必需的语义依据
* `basis` 不是自然语言解释，而是对已建模 ER 单元的引用
* 若条件依赖某实体通过某关系连接到另一实体属性，则应同时写出相关关系名与所依赖属性

例如：

* “2024年的订单”

  * `target`: `order`
  * `basis`: [`order.order_date`]
  * `condition_desc`: `order_date 为 2024 年的订单`
* “有下单记录的客户”

  * `target`: `customer`
  * `basis`: [`customer_places_order`]
  * `condition_desc`: `在 customer_places_order 关系中存在的customer`

## 实体与属性修正要求

本阶段输出的 `entities` 应表示经过本阶段检查与必要修正后的最终实体结果。

允许的修正包括：

* 添加、删除实体
* 添加删除现有实体的属性
* 调整 `identifier_attrs` 与 `identifier_source`
* 调整实体 `role`

输出时保留的全部实体，并为每个实体标记当前状态 `status`字段：

* `unchanged`：该实体未被修正
* `modified`：该实体在本阶段发生了局部修正
* `added`：该实体为本阶段新增

要求：

* 修正应提高整体语义一致性，对原实体-属性集最小化修改
* 不要因为关系建模方便而任意改写 原实体-属性集
* 对于不再保留的实体，直接从最终 `entities` 中移除
* 对于 `unchanged` 实体，只输出 `name` 与 `status`即可
* 对于 `modified` 或 `added` 实体，输出必要的实体字段；若涉及属性调整，可在 `attr` 中标记属性状态

## normalized_intent 的写法

`normalized_intent` 基于最终的实体、属性、关系与条件，对用户原始需求做结构更清晰的语义重述。

`normalized_intent` 必须仅使用现有建模中的实体、关系、属性与条件作为语义单位来表述需求，所有的ER模型中的语义单位需要用``包裹起来。

此外，若用户明确指定了统计口径、结果细节等要求，也应尽量保留在 `normalized_intent` 中，例如：

* 按照哪种统计口径返回结果
* 返回哪些结果项
* 结果项单位
* 是否需要四舍五入或保留小数位
* 返回格式或编号形式
* 若存在多条候选时的选择规则

这些内容属于结果语义的一部分，应保留在 `normalized_intent` 中；
同时，对比normalized_intent 与 用户原本的表述，判断是否覆盖用户需求。

---

## 输出格式

首先根据任务指示，逐步思考综合分析，列举与需求语义和数据库Schema Hint 匹配的现有实体的关系和条件；

**禁止直接输出json， 必须分析清楚实体、关系、条件如何对应到需求、数据库schema hint并完整的表示该语义**；

然后，根据上述的分析结果，你需要总结为一个 JSON 格式的输出对象：

```json
{
  "entities": [
    {
      "name": "...",
      "status": "unchanged | modified | added", 
      (对于unchanged entity 以下字段可省略)
      "desc": "...",
      "role": "fact | dimension | other",
      "identifier_attrs": [],
      "identifier_source": "explicit | natural | introduced",
      "attr": [
        {
          "name": "...",
          "status": "unchanged | modified | added",
          "desc": "..."
        }
      ]
    },
    ...
  ],
  "relationships": [
    {
      "name": "...",
      "participants": [
        {
          "entity": "...",
          "role": "...",
          "cardinality": "one | many | unknown"
        }
      ],
      "attr": [
        {
          "name": "...",
          "desc": "..."
        }
        (可选属性)
      ]，
      "desc": "..."
    }
  ],
  "conditions": [
    {
      "name": "...",
      "target": "...",
      "basis": [],
      "condition_desc": ""
    }
  ],
  "normalized_intent": "...",
  "user_original_requirement_coverage": "..."
}
```
