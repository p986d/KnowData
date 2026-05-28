# NL2ER Refine 0: Unit Implementation Analysis

You are analyzing whether one conceptual ER unit can be implemented by the database evidence returned from schema linking.

The schema linking evidence may be imperfect. Treat it as candidate database evidence, not as ground truth SQL. The linked schema evidence is already aggregated at the whole-question level and enriched from schema snapshots with table descriptions, column types, column descriptions, and sampled values.

## Task

For the target entity or relationship:

1. List the semantic requirements that must be implemented for this unit.
2. First infer the semantics of the linked tables and linked columns from their names, types, descriptions, and sampled values.
3. Decide whether the unit itself is implemented by the linked schema evidence.
4. Decide whether every identifier, participant anchor, and attribute of this unit is implemented.
5. Recommend local changes that would make this unit more database-supported.

Use only the provided question, ER unit, and schema linking evidence. Do not use gold SQL.

## Support Status

Choose exactly one:

- `supported`: The unit semantics and required attributes are supported by the linked evidence.
- `partially_supported`: Some core semantics are supported, but required attributes, anchors, or conditions are missing.
- `unsupported`: The unit has no useful database evidence.
- `misgrounded`: Evidence exists, but it supports a different semantic object or wrong implementation path.
- `over_broad`: Evidence contains the right support but also substantial unrelated evidence that would pollute the unit.
- `needs_rewrite`: The unit should be structurally rewritten using the available database evidence.

## Allowed Recommendation Actions

Use only these action names:

- `keep_unit`
- `rename_unit`
- `replace_unit`
- `merge_units`
- `split_unit`
- `add_attribute`
- `remove_attribute`
- `replace_attribute`
- `add_relation`
- `replace_relation`
- `remove_relation`
- `move_condition_target`
- `mark_unimplemented`

Every recommendation must cite evidence columns when available.

## Analysis Requirements

- Do not assume a linked column supports an attribute only because the names are vaguely similar.
- Use `available_columns`, `linked_columns`, and sampled values to distinguish semantic roles when names are ambiguous.
- Treat `linked_columns` as direct schema-linking evidence; treat non-linked `available_columns` as table-context evidence that can explain missing support or a possible local refinement.
- If an entity is linked to a table that contains a similarly named column but does not match the entity role in the original question, mark it as `misgrounded`.
- If an attribute is implemented by an expression or conversion over a linked column, mark the attribute as supported and explain the required transformation.
- If a unit can be implemented only by combining multiple linked tables, explain the required join or correspondence in `notes`.

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

## Optional External Knowledge

```text
{{ external_knowledge }}
```

## Target Unit

```json
{{ target_unit | tojson_pretty }}
```

## Whole-Question Linked Schema Evidence

```json
{{ schema_evidence | tojson_pretty }}
```

## Output Format

Return only valid JSON. Do not wrap it in markdown.

```json
{
  "unit_name": "...",
  "unit_type": "entity|relationship|connection",
  "semantic_requirements": [
    "..."
  ],
  "linked_schema_semantics": {
    "tables": [
      {
        "table": "...",
        "inferred_semantics": "...",
        "usefulness_for_this_unit": "direct|indirect|irrelevant|conflicting"
      }
    ],
    "columns": [
      {
        "column": "...",
        "inferred_semantics": "...",
        "sample_value_signal": "...",
        "usefulness_for_this_unit": "direct|indirect|irrelevant|conflicting"
      }
    ]
  },
  "unit_semantic_support": {
    "support_status": "supported|partially_supported|unsupported|misgrounded|over_broad|needs_rewrite",
    "implemented_semantics": [
      "..."
    ],
    "missing_semantics": [
      "..."
    ],
    "misgrounded_semantics": [
      "..."
    ],
    "evidence_tables": [
      "..."
    ],
    "evidence_columns": [
      "..."
    ]
  },
  "attribute_support": [
    {
      "attribute_name": "...",
      "attribute_role": "identifier|participant_anchor|regular_attribute|relationship_attribute|condition_target",
      "required_semantics": "...",
      "support_status": "supported|partially_supported|unsupported|misgrounded|needs_rewrite",
      "evidence_columns": [
        "..."
      ],
      "required_transformation": "",
      "reason": "..."
    }
  ],
  "support_status": "supported|partially_supported|unsupported|misgrounded|over_broad|needs_rewrite",
  "recommended_changes": [
    {
      "action": "keep_unit",
      "target": "...",
      "description": "...",
      "evidence_columns": [
        "..."
      ]
    }
  ],
  "notes": "..."
}
```
