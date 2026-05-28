Task: ER-aware database table semantic review

Given a user question, a pre-built logical ER model, and one database table group,
review whether the table group can physically support any part of the logical
model, question constraints, intermediate representations, or derived semantics
needed to answer the question.

The table group contains physical tables with the same column structure and a
similar naming pattern. The `representative_table` column structure applies to
all `member_tables`. Analyze the table group as one candidate physical support
unit.

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

## Logical ER Model From NL2ER

```json
{{ logical_model | tojson_pretty }}
```

## Resolve Process / Sub Questions

```text
{{ sub_questions }}
```

## Table Group Context

The following table groups are context only. Use them to understand the target
table group's role in the schema/database. Do not cite columns from context
tables as evidence for the target table group.

```json
{{ table_group_context | tojson_pretty }}
```

## Target Table Group

```json
{{ target_table_group | tojson_pretty }}
```

# Review Instructions

Think step by step before producing the final JSON.

1. Understand the physical meaning of the table group from table name, columns,
   descriptions, samples, member table names, and the table group context. Do
   not assume sample values are complete value domains.

3. Identify physical columns that implement identifier attributes for entities
   and relationships. For every supported logical entity or relationship,
   find all possible identifier columns or identifier column sets, including
   technical keys, natural keys, participant keys, foreign keys, composite keys,
   or columns that can distinguish relationship instances.

4. Identify physical columns that implement semantic representation,
   calculation, and constraint attributes for entities and relationships. This
   includes columns used to represent entity or relationship properties, role
   restrictions, time restrictions, direction restrictions, local scope,
   reference objects, comparison baselines, numerator/denominator definitions,
   deduplication scope, aggregation prerequisites, filtering conditions, and
   other semantic qualifiers required by the question.

5. Identify table columns that physically support logical relationships. This
   includes columns that connect participants, encode participation, matching,
   ownership, containment, event occurrence, relationship status, relationship
   attributes, or other evidence that the relationship can be implemented by
   this table group.

6. Identify columns related to question-specific calculations and derived
   semantics over the above entities, relationships, and attributes. This
   includes columns needed for aggregation, grouping, conditional filtering,
   comparison, ordering, top-k, ratios, percentages, deltas, unit conversion,
   boolean flags, business classifications, and intermediate results such as
   cleaned values, standardized values, filtered subsets, grouped records,
   ranked records, windowed records, joined records, or aggregation bases.

7. Return all possible evidence columns from the representative table that
   satisfy any of the above categories. If a column is relevant by column name
   only, return the column name; if possible, return the representative full
   column name. Do not invent columns. When multiple physical columns may
   implement the same logical semantic, return all possible implementation
   columns rather than selecting only one preferred column.


For each relevant table, return all  All primary key/ identifier of table and all foreign key to other table / entity relation;
  Also, return all metadata control column "DELETE_FLAG"；
  These are solely For table management. return them in "other_columns".



# Final Output

Return one JSON object with this shape:

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
  "supported_logical_entities": [
    {
      "entity_name": "...",
      "supported_attributes": [
        "..."
      ],
      "evidence_columns": [
        "..."
      ],
      "reason": "..."
    }
  ],
  "supported_logical_relations": [
    {
      "relation_name": "...",
      "supported_participants": [
        {
          "role": "...",
          "entity": "...",
          "evidence_columns": [
            "..."
          ]
        }
      ],
      "supported_attributes": [
        "..."
      ],
      "evidence_columns": [
        "..."
      ],
      "reason": "..."
    }
  ],
  "derived_representations": [
    {
      "name": "...",
      "semantics": "...",
      "depends_on_logical_units": [
        "..."
      ],
      "evidence_columns": [
        "..."
      ]
    }
  ],
  "other_columns":List[str]

}
```
