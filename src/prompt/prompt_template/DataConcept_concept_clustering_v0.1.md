# Task

You are building a database-level concept catalog from table-level data concepts.

Cluster concepts that are semantically similar or grain-similar. This is only a clustering step:

- Do not merge concepts.
- Do not decide that clustered concepts are identical.
- Do not decide that clustered concepts must be separated.
- Do not create new concepts.
- Include singleton clusters when a concept does not have a clear peer.

# Concept Index

```json
{{ concept_index | tojson_pretty }}
```

# Clustering Guidance

Use the complete concept entry, including:

- concept name and definition
- table name
- shape type
- grain kind and grain definition
- identifier sets
- entity or relationship definition
- representative columns

Cluster concepts only when they describe one of the following:

- the same business object type, such as two variants of a person, house, label, address, organization, or dictionary concept
- the same relationship pattern and a comparable grain, such as two label-assignment relationships or two person-house occupancy relationships
- the same lookup/taxonomy family, when the concepts jointly define a comparable controlled vocabulary domain

Do not cluster concepts merely because they are both important, both entities, both related in the application, or one may reference the other. For example, a person concept and a department concept should normally be separate singleton clusters unless the input explicitly says they are variants of the same business object. A parent taxonomy concept and a child item concept may be clustered only when the cluster label is the shared taxonomy family, not when the task would require merging their grains.

Keep relationship concepts and entity concepts in separate clusters unless the input clearly represents the same mixed concept at the same grain.

# Output

Return only valid JSON. Do not wrap it in markdown.

```json
{
  "concept_clusters": [
    {
      "cluster_id": "cluster_1",
      "cluster_label": "Short semantic label",
      "members": [
        {
          "concept_ref": "unit:database.schema.table",
          "table_fullname": "database.schema.table",
          "grain_name": "Concept grain name",
          "grain_kind": "entity|relationship|mixed|unknown",
          "shape_type": "entity_info|relationship|detail_or_event|metric_fact|snapshot_or_history|lookup_or_enum|wide_table|mixed_or_unknown|unknown"
        }
      ],
      "cluster_reason": "Brief reason for placing these concepts together."
    }
  ],
  "analysis_notes": [
    "Optional short note about uncertainty. Maximum 3 notes."
  ]
}
```
