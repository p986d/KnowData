Task: data snapshot shape and grain analysis

You are provided with one already-generated data snapshot. The snapshot contains
table schema, column and table descriptions, sampled rows, sample statistics,
and any constraint metadata that the snapshot provider returned.

Do not assume any user question.

Your job is to judge:

1. The data unit shape: what kind of data unit this snapshot appears to be.
2. The row grain: whether one row/logical record represents an entity or a
   relationship, and the definition of that entity or relationship.

The snapshot may contain `provided_constraints`. These are only constraints
returned by the upstream snapshot provider. They may be empty or incomplete.
Use them when present, but do not assume a table has a primary key, unique key,
foreign key, or index when the snapshot does not provide one.

Use the full snapshot evidence: table names, descriptions, column names, column
types, column descriptions, sampled values, sample statistics, provided
constraints, relation clues, and snapshot warnings. Do not apply fixed suffix or
keyword rules as deterministic logic. If the evidence is insufficient or
conflicting, state that in `analysis_notes`.

# Data Snapshot

```json
{{ data_snapshot | tojson_pretty }}
```

# Shape Types

Use one of the following shape types. These descriptions define the intended
meaning of each type; they are not keyword rules.

```text
entity_info
  The data unit mainly stores descriptive or identifying attributes for one
  class of business object. A row usually describes one object instance or one
  stable object record.

relationship
  The data unit mainly materializes a relationship among two or more
  participant entities. The relationship itself is the row-level subject. This
  includes binary relationships and multi-participant role sets, such as
  membership, assignment, ownership, labeling, enrollment, residence, contact
  association, or role assignment.

detail_or_event
  The data unit mainly records occurrences, transactions, operations, logs, or
  line-level details. The row is an event/detail record rather than a stable
  object definition.

metric_fact
  The data unit mainly stores measured values or statistics over dimensions.
  The row is an analytical fact such as object-period, object-category,
  region-period, or similar.

snapshot_or_history
  The data unit mainly stores a state at a time, a historical version, or a
  validity interval. The row should not be collapsed into the underlying object
  when version, snapshot date, or validity time is part of the record meaning.

lookup_or_enum
  The data unit mainly defines a controlled vocabulary, code table, enum value,
  or dictionary entry used to interpret other data.

wide_table
  The data unit denormalizes several attribute groups, dimensions, or measures
  into one physical table. Use this when one simple entity/relationship/fact
  interpretation is too narrow but the rows still appear structurally
  homogeneous.

mixed_or_unknown
  The evidence suggests mixed row meanings, conflicting patterns, or multiple
  logical subsets that should not be forced into one shape.

unknown
  Evidence is too weak to make a useful shape judgment.
```

# Grain Requirements

Grain must be described as one of:

```text
entity
relationship
mixed
unknown
```

For an entity grain, define the entity represented by one logical business
instance. For a relationship grain, define the relationship represented by one
logical business instance and the participant entities. Do not output a generic
grain type such as "metric_record" or "business_object".

Entity and relationship definitions must describe semantic business content,
not physical table mechanics. Phrase them in terms of "each instance" or "one
instance" rather than "each row". Do not include data factory or processing
pipeline fields in the semantic definition, such as ETL/source-system tracking,
load time, batch id, partition fields, sync markers, or other technical
ingestion/audit columns. This does not prohibit business-valid time, effective
periods, business status, or business source attributes when they are part of
the entity or relationship meaning.

You must list every identifiable identifier set for the entity or relationship,
including both technical keys and natural keys when evidence supports them.
Identifier sets can come from provided constraints, column descriptions,
sampled values, or the table/column semantics inferred from the whole snapshot.
If no reliable identifier is visible, return an empty `identifier_sets` list and
explain the limitation in `analysis_notes`.

# Final Output

Return one JSON object with this shape:

```json
{
  "snapshot_id": "...",
  "table_fullname": "...",
  "shape": {
    "shape_type": "entity_info|relationship|detail_or_event|metric_fact|snapshot_or_history|lookup_or_enum|wide_table|mixed_or_unknown|unknown",
    "description": "Why this shape best describes the data unit.",
    "confidence": "high|medium|low"
  },
  "grain": {
    "grain_kind": "entity|relationship|mixed|unknown",
    "grain_name": "Name of the entity or relationship represented by one logical instance.",
    "definition": "Precise semantic definition of what one logical business instance represents.",
    "identifier_sets": [
      {
        "identifier_type": "technical_key|natural_key|declared_primary_key|declared_unique_key|composite_key|surrogate_key|unknown",
        "columns": [
          "column_name_or_full_column_name"
        ],
        "definition": "What this identifier identifies and why it is an identifier."
      }
    ],
    "entity": {
      "entity_name": "...",
      "definition": "...",
      "representative_columns": [
        "column_name_or_full_column_name"
      ]
    },
    "relationship": {
      "relationship_name": "...",
      "definition": "...",
      "participant_entities": [
        {
          "entity_name": "...",
          "role": "...",
          "identifier_columns": [
            "column_name_or_full_column_name"
          ]
        }
      ],
      "representative_columns": [
        "column_name_or_full_column_name"
      ]
    }
  },
  "analysis_notes": [
    "Optional note. At most 3 notes. Only include uncertainty, missing constraint metadata, mixed-grain concern, or another limitation not already stated above."
  ]
}
```

Rules:

- Shape and grain are judgments, not raw facts.
- Grain is the entity or relationship represented by one logical business
  instance, not a restatement of the physical row.
- A provided primary key proves record addressability, but it does not by itself
  prove business grain.
- Do not collapse a relationship, metric, event, snapshot, or history row into a
  simple entity just because one participant entity appears in the columns.
- Do not output referenced concepts as separate concepts in this stage except as
  relationship participants or entity context inside the grain definition.
- Use `relationship`, not `relationship_bridge`, for any row-level relationship
  among two or more participant entities.
- Use column_fullname values exactly as provided in the snapshot for all columns
  in `identifier_sets`, `entity.representative_columns`,
  `relationship.representative_columns`, and participant `identifier_columns`.
- Keep `analysis_notes` short. Omit it or return an empty list when there is no
  important uncertainty, missing metadata, or mixed-grain concern.
