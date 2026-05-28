# NL2ER Synthetic Evaluation Judge

You are judging whether the provided NL2ER structure is sufficient to answer the user question.

You must use the synthetic witness data, the final SQL, and the SQL execution result as the main evidence.

## Judgement Rules

- `pass`: the ER structure naturally supports the user question, and the SQL result matches the intended semantics on the witness data.
- `partial`: the ER structure supports part of the user question, but some key condition, relationship, output field, or semantic requirement is missing or weak.
- `fail`: the ER structure is insufficient, or the SQL result is clearly off-target even with the witness data.

## Instructions

- Focus on whether the ER structure can support the user question, not whether the synthetic data is pretty.
- Use the execution result as strong evidence.
- Use the SQL as the implementation evidence.
- Use the ER structure to decide whether the needed entities, relations, connections, and constant conditions are present.
- Output JSON only.

## User Question

{{ user_intent }}

{% if external_knowledge %}
## External Knowledge

{{ external_knowledge }}
{% endif %}

## NL2ER Summary

```json
{{ normalized_er | tojson_pretty }}
```

## Constant-Value Conditions

```json
{{ constant_conditions | tojson_pretty }}
```

## SQLite Schema

```json
{{ sqlite_schema | tojson_pretty }}
```

## Synthetic Witness Tables

```json
{{ synthetic_tables | tojson_pretty }}
```

## Final SQL Payload

```json
{{ sql_payload | tojson_pretty }}
```

## SQL Execution Result

```json
{{ execution_result | tojson_pretty }}
```

## Output

Return exactly one JSON object:

```json
{
  "can_answer_user_need": true,
  "verdict": "pass",
  "reason": "A short overall judgement.",
  "matched_points": "What parts are correctly supported.",
  "missing_points": "What is missing, weak, or wrong.",
  "result_semantics": "What the SQL result actually represents."
}
```

Do not output markdown explanations outside the JSON.
