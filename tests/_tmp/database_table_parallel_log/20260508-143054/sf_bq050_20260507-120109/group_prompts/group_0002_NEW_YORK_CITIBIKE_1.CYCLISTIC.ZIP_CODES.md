任务：数据库表组语义单元分析

给定用户问题和一个数据库表组，判断这个表组是否能为问题提供实体、关系、属性、约束或计算语义的物理实现支持。

表组由结构相同、名称模式相近的一组物理表组成。输入中的 representative_table 是代表表，它的列结构适用于 member_tables 中的所有成员表。你需要把这个表组当作一个语义候选单元来分析，而不是只分析单个物理表。

# Input

## Original User Question

```text
I want to analyze bike trips in New York City for 2014 by linking trip data with weather information to understand how weather conditions (temperature, wind speed, and precipitation) affect bike trips between neighborhoods.

For each combination of starting and ending neighborhoods, I need the following:
1. Total number of bike trips between the neighborhoods.
2. Average trip duration in minutes (rounded to 1 decimal).
3. Average temperature at the start of the trip (rounded to 1 decimal).
4. Average wind speed at the start (in meters per second, rounded to 1 decimal).
5. Average precipitation at the start (in centimeters, rounded to 1 decimal).
6. The month with the most trips (e.g., `4` for April).

The data should be grouped by the starting and ending neighborhoods, with:`zip_codes` in `geo_us_boundaries` used to map the bike trip locations based on latitude and longitude. `zip_codes` in `cyclistic` used to obtain the borough and neighborhood names. Using weather data from the Central Park station for the trip date, covering all trips in 2014.
```

## Database ID

```text
NEW_YORK_CITIBIKE_1
```

## Optional Database Hint

```text
用户提及的Snowflake数据库模式如下：
"NEW_YORK_CITIBIKE_1.GEO_US_BOUNDARIES.ZIP_CODES":{
  "column_names": [
      "area_land_meters",
      "internal_point_lat",
      "zip_code",
      "area_water_meters",
      "state_code",
      "fips_class_code",
      "zip_code_geom",
      "state_fips_code",
      "county",
      "functional_status",
      "state_name",
      "internal_point_geom",
      "mtfcc_feature_class_code",
      "internal_point_lon",
      "city"
  ]
}

"NEW_YORK_CITIBIKE_1.CYCLISTIC.ZIP_CODES":{
  "column_names": [
      "zip",
      "borough",
      "neighborhood"
  ]
}

注意这里包含的仅为用户提及的模式、表和每个表中所有列；不是所有列都一定与用户需求相关。
```

## External Knowledge

```text
[functions_st_within.md]
Categories: Geospatial functions


## ST_WITHIN

Returns true if the first geospatial object is fully contained by the second geospatial object. In other words:

The first GEOGRAPHY object g1 is fully contained by the second GEOGRAPHY object g2.
The first GEOMETRY object g1 is fully contained by the second GEOMETRY object g2.

Calling ST_WITHIN(g1, g2) is equivalent to calling ST_CONTAINS(g2, g1).
Although ST_COVEREDBY and ST_WITHIN might seem similar, the two functions have subtle differences. For details on the differences between “covered by” and “within”, see the Dimensionally Extended 9-Intersection Model (DE-9IM).

Note This function does not support using a GeometryCollection or FeatureCollection as input values.

Tip You can use the search optimization service to improve the performance of queries that call this function.
For details, see Search Optimization Service.

See also:ST_CONTAINS , ST_COVEREDBY


## Syntax

ST_WITHIN( <geography_expression_1> , <geography_expression_2> )

ST_WITHIN( <geometry_expression_1> , <geometry_expression_2> )


## Arguments


geography_expression_1A GEOGRAPHY object that is not a GeometryCollection or FeatureCollection.

geography_expression_2A GEOGRAPHY object that is not a GeometryCollection or FeatureCollection.

geometry_expression_1A GEOMETRY object that is not a GeometryCollection or FeatureCollection.

geometry_expression_2A GEOMETRY object that is not a GeometryCollection or FeatureCollection.


## Returns

BOOLEAN.

## Examples


## GEOGRAPHY examples

This shows a simple use of the ST_WITHIN function:

create table geospatial_table_01 (g1 GEOGRAPHY, g2 GEOGRAPHY);
insert into geospatial_table_01 (g1, g2) values 
    ('POLYGON((0 0, 3 0, 3 3, 0 3, 0 0))', 'POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))');

Copy SELECT ST_WITHIN(g1, g2) 
    FROM geospatial_table_01;
+-------------------+
| ST_WITHIN(G1, G2) |
|-------------------|
| False             |
+-------------------+
```

## Target Table Group

```json
{
  "table_fullname": "NEW_YORK_CITIBIKE_1.CYCLISTIC.ZIP_CODES",
  "table_name": "ZIP_CODES",
  "description": "",
  "snapshot_path": "D:\\Workspace\\ReFoRCE\\spider2-snow\\resource\\databases\\NEW_YORK_CITIBIKE_1\\CYCLISTIC\\ZIP_CODES.json",
  "columns": [
    {
      "column_fullname": "NEW_YORK_CITIBIKE_1.CYCLISTIC.ZIP_CODES.zip",
      "column_name": "zip",
      "data_type": "NUMBER",
      "description": "",
      "sample_values": [
        10458,
        10468,
        10453,
        10457,
        10456
      ]
    },
    {
      "column_fullname": "NEW_YORK_CITIBIKE_1.CYCLISTIC.ZIP_CODES.borough",
      "column_name": "borough",
      "data_type": "TEXT",
      "description": "",
      "sample_values": [
        "Bronx"
      ]
    },
    {
      "column_fullname": "NEW_YORK_CITIBIKE_1.CYCLISTIC.ZIP_CODES.neighborhood",
      "column_name": "neighborhood",
      "data_type": "TEXT",
      "description": "",
      "sample_values": [
        "Bronx Park and Fordham",
        "Central Bronx",
        "High Bridge and Morrisania"
      ]
    }
  ],
  "sample_rows": [
    {
      "zip": 10458,
      "borough": "Bronx",
      "neighborhood": "Bronx Park and Fordham"
    },
    {
      "zip": 10468,
      "borough": "Bronx",
      "neighborhood": "Bronx Park and Fordham"
    }
  ],
  "table_group": {
    "group_id": "group_0002_NEW_YORK_CITIBIKE_1.CYCLISTIC.ZIP_CODES",
    "grouping_method": "reforce_table_family",
    "group_key": {
      "namespace": "NEW_YORK_CITIBIKE_1.CYCLISTIC",
      "normalized_table_name": "ZIP_CODES",
      "column_signature_hash": "addf2c2f881a"
    },
    "representative_table": "NEW_YORK_CITIBIKE_1.CYCLISTIC.ZIP_CODES",
    "family_size": 1,
    "member_tables": [
      {
        "table_fullname": "NEW_YORK_CITIBIKE_1.CYCLISTIC.ZIP_CODES",
        "namespace": "NEW_YORK_CITIBIKE_1.CYCLISTIC",
        "table_name": "ZIP_CODES",
        "snapshot_path": "D:\\Workspace\\ReFoRCE\\spider2-snow\\resource\\databases\\NEW_YORK_CITIBIKE_1\\CYCLISTIC\\ZIP_CODES.json"
      }
    ]
  }
}
```


# Analysis
分析过程如下：

1. 首先理解表格的语义：分析单个表格在数据库中物理语义。

2. 其次分析表格与问题的相关性：判断表格能够提供对问题中的逻辑语义单元（实体、关系、属性）的具体实现支持。
  
  问题的部分性支持：一个表格只需要且一般只支持问题中的部分逻辑语义单元。

  注意，物理模式的实体-关系语义建模与逻辑模型不一定一致；逻辑实体-关系应以题面的需求为主，物理模式的实体-关系仅提供语义补充和实现支持。
  实现支持：说明问题中某个逻辑实体或关系可以被数据支持构建，其定义和粒度可实现；对于实现支持的物理数据，不需要以任何形式出现在逻辑模型中。

  语义解释：说明问题的中需要将某个概念定义为逻辑实体或关系，或某个逻辑实体或关系需要某些属性，或某些语义通过某个具体计算方式得出;分析目标表能够给出对这个逻辑实体、关系与属性的物理实现支持。
  
  对于语义解释的物理数据，需要将其重抽象为问题的逻辑定义补充，并将其整合到问题整体的查询逻辑中。

3. 明确服从数据实现提示：如果一个问题的逻辑语义单元明确了其实现所需要的表列，分析表可以提供该语义单元，但不是问题提示指定的提供者，则分析表不应将该语义单元作为semantic unit.

4. 对数据判断相关、无关性：只有直接为问题提供某个逻辑实体或关系语义的表是相关的。相关表格只需要为问题提供至少一个逻辑语义单元即可。如果该表支持的语义单元在问题中明确由其它表提供，则该表不能提供该语义单元。

5. 属性语义分析：该表提供的逻辑语义单元在问题求解逻辑中需要哪些语义表达、操作和语义限定。
  
  逐条分别分析以下内容：

  (1) 语义表达与操作：逻辑语义单元涉及哪些语义任务。例如：

  确定对象或对象子集；
  确定对象之间的联系、参与或匹配；
  表示对象的语义；
  对既有对象或联系施加条件约束；
  在对象或联系基础上进行聚合、比较、排序、Top-k、派生计算；
  形成后续步骤所依赖的阶段性结果。
  多义召回优先：对每个语义要素，分析实现的多义性：如果存在多个可能的实现列，召回所有这些列；

  (2) 语义限定：逻辑语义单元的对象、联系和计算具体受到哪些语义约束；
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