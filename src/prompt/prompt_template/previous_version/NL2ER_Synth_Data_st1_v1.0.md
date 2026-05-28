# NL2ER Synthetic Witness Data Generation

You are given a user question, an NL2ER structure, and a pre-defined SQLite schema.

Your task is to generate one compact witness dataset that makes it possible to test whether the ER structure can answer the user question.

You must generate data only. Do not generate SQL. Do not explain your reasoning outside the final JSON.

## Rules

- Use only the tables and columns listed in the SQLite schema below.
- Every expected table must appear exactly once in the output.
- Each table must contain between 1 and 3 rows.
- Rows must be realistic and internally consistent.
- The dataset must be joinable across tables.
- For `relation` and `connection` tables, participant anchor columns must match values that appear in the corresponding entity tables.
- Include at least one positive row pattern that supports the user question.
- Include at least one distractor row pattern that would expose a wrong join, a missing filter, or an over-broad query.
- Reflect the constant-value conditions in observable row values whenever possible.
- Do not invent extra columns.
- Prefer JSON numbers and booleans when the value is clearly numeric or boolean.
- Output JSON only.

## User Question

{{ user_intent }}

{% if external_knowledge %}
## External Knowledge

{{ external_knowledge }}
{% endif %}

## Entities

```json
{{ entities | tojson_pretty }}
```

## Relations

```json
{{ relations | tojson_pretty }}
```

## Connections

```json
{{ connections | tojson_pretty }}
```

## Constant-Value Conditions

```json
{{ constant_conditions | tojson_pretty }}
```

## SQLite Schema

```json
{{ sqlite_schema | tojson_pretty }}
```

{% if validation_errors %}
## Previous Validation Errors

```json
{{ validation_errors | tojson_pretty }}
```
{% endif %}

{% if previous_response %}
## Previous Invalid Response

{{ previous_response }}
{% endif %}

## Output

Return exactly one JSON object in the following format:

```json
{
  "tables": [
    {
      "table_name": "entity_example",
      "rows": [
        {
          "column_a": "value",
          "column_b": 1
        }
      ]
    }
  ]
}
```
