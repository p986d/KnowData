# NL2ER Synthetic SQLite SQL Generation

You are given a user question, an NL2ER structure, a synthetic SQLite schema, and witness rows.

Your job is to produce one complete SQLite SQL query that answers the user question against this synthetic database.

This prompt follows the spirit of ReFoRCE stage-2 NL2SQL self-refine prompting, but it is adapted to a synthetic in-memory SQLite database:

- no real database schema linking
- no column exploration stage
- no few-shot examples after exploration
- only the synthetic schema and witness rows below are available

## Instructions

- Think carefully about the query logic before writing SQL.
- Return exactly one complete SQLite query.
- The SQL must answer the user question with no missing information and no extra information.
- Use only the tables and columns shown below.
- Prefer correct joins and filters over shorter SQL.
- If the previous SQL failed, correct it using the execution error.
- Only output JSON.

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

{% if previous_sql %}
## Previous SQL

```sql
{{ previous_sql }}
```
{% endif %}

{% if execution_error %}
## Previous SQLite Error

{{ execution_error }}
{% endif %}

## Output

Return exactly one JSON object:

```json
{
  "sql": "SELECT ...",
  "reason": "A short summary of the chosen query logic."
}
```

Do not output markdown explanations outside the JSON.
