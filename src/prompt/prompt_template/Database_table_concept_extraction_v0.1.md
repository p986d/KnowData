Task: database table concept extraction

Given a database table group, infer question-independent conceptual semantics
implemented by the physical schema. Do not use or assume any user question,
sub-question, NL2ER model, or task-specific intent.

The table group contains physical tables with the same column structure and a
similar naming pattern. The `representative_table` column structure applies to
all `member_tables`. Analyze the table group as one candidate physical support
unit.

# Input

## Database ID

```text
{{ db_id }}
```

## Optional Database Hint

```text
{{ db_hint }}
```

## Table Group Context

The following table groups are context only. Use them to infer naming patterns,
neighboring concepts, and likely foreign-key targets. Do not cite context table
columns as evidence for the target table group.

```json
{{ table_group_context | tojson_pretty }}
```

## Target Table Group

```json
{{ target_table_group | tojson_pretty }}
```

# Extraction Instructions

Think step by step before producing the final JSON.

1. Infer the physical meaning of the target table group from table names,
   namespaces, column names, data types, descriptions, sample rows, sample
   values, and member table names. Sample values are examples only, not complete
   value domains.

2. Extract stable business concepts represented by the table group. A concept
   may be an entity, relationship, event, lookup/code table, metric fact,
   attribute group, bridge table, snapshot, ledger, status history, or audit
   record.

3. For each concept, identify its grain: what one row, one key, or one logical
   record represents. If the table is a relationship/event/fact table, include
   participant entities and roles.

4. Return all possible identifier columns, natural-key columns, technical-key
   columns, foreign-key-like columns, measure columns, dimensional attributes,
   timestamps, status columns, flags, and descriptive attributes that support
   the concept. Do not invent columns.

5. Identify foreign-key candidates and relationship semantics implied by column
   names, table names, data types, and context table names. Mark them as
   candidates when the schema does not explicitly prove the reference.

6. Put table-management and implementation columns such as delete flags,
   validity flags, update timestamps, audit users, load batch ids, partition
   columns, and surrogate technical keys that are not business attributes in
   `metadata_columns`.

# Final Output

Return one JSON object with this shape:

```json
{
  "group_id": "...",
  "representative_table": "...",
  "member_table_scope": "all_members|selected_members",
  "selected_member_tables": [
    "..."
  ],
  "concepts": [
    {
      "concept_name": "...",
      "concept_type": "entity|relationship|event|lookup|metric_fact|attribute_group|bridge|snapshot|ledger|status_history|audit_record",
      "description": "...",
      "grain": "...",
      "identifier_columns": [
        "column_name_or_representative_full_column_name"
      ],
      "attribute_columns": [
        {
          "column": "column_name_or_representative_full_column_name",
          "semantic_name": "...",
          "description": "...",
          "data_role": "identifier|foreign_key|measure|dimension|timestamp|status|flag|description|metadata|partition|unknown"
        }
      ],
      "participant_entities": [
        {
          "role": "...",
          "entity_name": "...",
          "evidence_columns": [
            "column_name_or_representative_full_column_name"
          ]
        }
      ],
      "evidence_columns": [
        "column_name_or_representative_full_column_name"
      ]
    }
  ],
  "foreign_key_candidates": [
    {
      "columns": [
        "column_name_or_representative_full_column_name"
      ],
      "referenced_concept_or_table": "...",
      "relationship_semantics": "...",
      "confidence": "high|medium|low"
    }
  ],
  "metadata_columns": [
    "column_name_or_representative_full_column_name"
  ],
  "quality_warnings": [
    "..."
  ]
}
```

Rules:

- This is question-independent extraction. Do not mention a user question or
  select columns because they answer a question.
- `member_table_scope=all_members` means the same concept interpretation applies
  to every member table in this table family.
- `member_table_scope=selected_members` requires concrete full table names in
  `selected_member_tables`.
- Every concept should cite concrete evidence columns when possible.
- Return all plausible implementation columns rather than choosing only one
  preferred column.
