# Task

Find directional references from one source data concept to other concepts in the same database.

This is not a relationship-concept discovery task:

- Do not create new relationship concepts.
- Do not rewrite the source concept's shape or grain.
- A connection means source table column(s) semantically reference another table-level concept as part of the business domain model.
- A relationship concept may connect to its participant entity concepts.
- An entity or attribute table may connect to lookup, parent, owner, organization, location, or other referenced business concepts.
- Use declared primary keys, unique keys, and foreign keys when present.
- If no declared key exists, infer carefully from column meaning, descriptions, samples, and target concept identifiers.
- Do not output self-connections unless the source column references another row of the same concept, such as a parent/hierarchy reference.
- Prefer fewer high-value connections over exhaustive weak guesses.

# Exclusion Rules

Do not output a connection for operational metadata or lifecycle bookkeeping unless an explicit foreign key or clear target concept exists in the input:

- creator/updater fields, such as `create_user`, `update_user`, `create_user_name`, `update_user_name`
- timestamps, such as create/update/load time
- soft-delete fields, such as delete flags or deleted timestamps
- batch, partition, import, load, sync, or source-tracking fields

If these fields look like they reference a missing user/account/job/source concept, mention that briefly in `analysis_notes` instead of forcing a connection to Person, Department, or Dictionary.

For coded values, output a `lookup_reference` only when the source column clearly belongs to a reusable business dictionary, label/category table, or explicit lookup concept. Do not connect generic flags, lifecycle status, delete flags, data-source flags, or internal enum-like columns to `sys_dict_data` just because they are coded values.

# Source Snapshot

```json
{{ source_snapshot | tojson_pretty }}
```

# Source Concept

```json
{{ source_concept | tojson_pretty }}
```

# Database Concept Index

```json
{{ concept_index | tojson_pretty }}
```

# Connection Types

- `participant_reference`: source concept is a relationship/event/fact and the source column identifies a participating entity concept.
- `lookup_reference`: source column references a reusable business code, dictionary, label, type, category, or other lookup concept.
- `hierarchy_reference`: source column references another row/concept in a parent-child hierarchy.
- `attribute_reference`: source column references an owner, organization, location, or other contextual business entity.
- `possible_reference`: plausible but weak reference when evidence is incomplete. Use this sparingly.

# Column Rules

- `source_columns` must be columns from the source snapshot.
- `target_identifier_columns` must be identifier columns from the target concept when known.
- Prefer full column names exactly as shown in the input.
- If you are uncertain and the evidence is weak, omit the connection and add a short `analysis_notes` item instead.

# Output

Return only valid JSON. Do not wrap it in markdown.

```json
{
  "source_table": "database.schema.source_table",
  "source_concept_ref": "unit:database.schema.source_table",
  "connections": [
    {
      "source_columns": [
        "database.schema.source_table.source_column"
      ],
      "target_concept_ref": "unit:database.schema.target_table",
      "target_table": "database.schema.target_table",
      "target_concept_name": "Target concept name",
      "target_identifier_columns": [
        "database.schema.target_table.target_identifier_column"
      ],
      "connection_type": "participant_reference|lookup_reference|hierarchy_reference|attribute_reference|possible_reference",
      "confidence": "high|medium|low",
      "definition": "Brief explanation of why the source column references the target concept."
    }
  ],
  "analysis_notes": [
    "Optional short note about uncertainty. Maximum 3 notes."
  ]
}
```
