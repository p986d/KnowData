# NL2ER Refine 0: Global ER Refinement

You are refining a conceptual ER model using schema linking evidence and unit-level implementation analyses.

The goal is not to match gold SQL. The goal is to produce a refined ER model that preserves the user question semantics as much as possible while making every entity, relationship, attribute, and condition explicitly supported by the available database evidence.

## Task

Use the unit analyses as local evidence, then make a global decision:

1. Merge the unit-level findings.
2. Resolve conflicts between units.
3. Rewrite units that are unsupported or misgrounded.
4. Add missing attributes, relations, or conditions only when they are required by the question and supported by evidence.
5. Remove or mark units that cannot be supported.
6. Report any question semantics that remain unsupported.

Do not use gold SQL. Do not invent database columns. Every refinement must be justified by evidence from schema linking.

## Refinement Principles

- Preserve the original question semantics.
- Prefer the smallest structural change that makes the model database-supported.
- A relationship may be implemented as a same-table fact, a join path, or a derived correspondence.
- Do not keep surrogate identifiers if the database evidence points to concrete natural keys or composite keys.
- If a field is necessary for an operation such as grouping, filtering, aggregation, ordering, top-k, or window logic, represent it as an attribute, condition, or operation requirement.
- If a semantic cannot be implemented from the evidence, list it in `unsupported_semantics` instead of hiding it.
- Treat `linked_columns` as direct schema-linking evidence. Non-linked `available_columns` in a linked table may be used as table-context evidence, but note when a refinement depends on a column that schema linking did not directly select.

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

## Initial ER Model

```json
{{ initial_er | tojson_pretty }}
```

## Whole-Question Linked Schema Evidence

```json
{{ schema_evidence | tojson_pretty }}
```

## Unit Implementation Analyses

```json
{{ unit_analyses | tojson_pretty }}
```

## Output Format

Return only valid JSON. Do not wrap it in markdown.

The `refined_er` should use the same general shape as `nl2er_output.json`: top-level `entities`, `relations`, and `conditions`. You may include `operations` as a demo-only extension when the question has important computation semantics that are not naturally represented as ER conditions.

```json
{
  "refined_er": {
    "entities": [
      {
        "entity_name": "...",
        "desc": "...",
        "grain": "...",
        "attributes": [
          {
            "name": "...",
            "semantics": "..."
          }
        ],
        "primary_key": [
          "..."
        ],
        "implementation": {
          "support_status": "supported|partially_supported|unsupported|misgrounded|over_broad|needs_rewrite",
          "evidence_tables": [
            "..."
          ],
          "evidence_columns": [
            "..."
          ],
          "notes": "..."
        }
      }
    ],
    "relations": [
      {
        "relation_name": "...",
        "desc": "...",
        "grain": "...",
        "participants": [
          {
            "role": "...",
            "entity": "...",
            "anchor_attribute": [
              "..."
            ]
          }
        ],
        "link_condition": "...",
        "attributes": [
          {
            "name": "...",
            "semantics": "..."
          }
        ],
        "implementation": {
          "implementation_type": "same_table_fact|join_path|derived_relation|unknown",
          "support_status": "supported|partially_supported|unsupported|misgrounded|over_broad|needs_rewrite",
          "evidence_tables": [
            "..."
          ],
          "evidence_columns": [
            "..."
          ],
          "notes": "..."
        }
      }
    ],
    "conditions": [
      {
        "condition_name": "...",
        "condition_type": "...",
        "targets": [
          "..."
        ],
        "description": "...",
        "implementation": {
          "support_status": "supported|partially_supported|unsupported",
          "evidence_columns": [
            "..."
          ],
          "notes": "..."
        }
      }
    ],
    "operations": [
      {
        "operation_name": "...",
        "operation_type": "group_by|aggregate|order_by|top_k|window|derive|filter|other",
        "description": "...",
        "required_inputs": [
          "..."
        ],
        "evidence_columns": [
          "..."
        ],
        "support_status": "supported|partially_supported|unsupported"
      }
    ]
  },
  "refinement_report": {
    "global_realizability": "realizable|partially_realizable|not_realizable",
    "kept_units": [
      "..."
    ],
    "rewritten_units": [
      {
        "from": "...",
        "to": "...",
        "reason": "...",
        "evidence_columns": [
          "..."
        ]
      }
    ],
    "added_units": [
      {
        "unit_name": "...",
        "reason": "...",
        "evidence_columns": [
          "..."
        ]
      }
    ],
    "removed_units": [
      {
        "unit_name": "...",
        "reason": "..."
      }
    ],
    "unsupported_semantics": [
      "..."
    ],
    "implementation_notes": [
      "..."
    ]
  }
}
```
