You are writing the final SQL query for a user request using prepared ER-derived CTEs.
Database ID: {{ db_id }}

User request:
{{ user_question }}

External knowledge:
{{ external_knowledge }}

Return JSON only with the following shape:
{
  "query_body": "SELECT ...",
  "used_ctes": ["CTE_NAME"],
  "mode": "{{ response_mode }}",
  "notes": "short note"
}

Requirements:
- query_body should normally start with SELECT.
- Do not use markdown fences.
- Do not repeat any prepared CTE definitions.
- Use the listed CTE names and columns exactly as provided.
- {{ mode_requirement }}
