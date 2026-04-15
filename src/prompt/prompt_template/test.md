# Task

You are performing Phase 1 of an Agnostic NL2ER workflow.

Your task is to convert a natural-language analytical question into a schema-agnostic hypothesis draft that can support later ER-structure induction and database verification.

This phase has two tightly coupled goals:

1. Stabilize the problem statement:
   clarify what the user wants, what one result row represents, what should be output, what explicit constraints apply, and what ambiguities remain.

2. Ground the problem logically:
   explain, in natural reasoning form, how the question would need to be solved at a conceptual level, without using any database-specific schema, table names, column names, keys, or implementation paths.

Do not generate SQL.
Do not generate a final ER model.
Do not bind the reasoning to database schema.
Do not over-structure the reasoning into mechanical step lists only.

# Input

You will be given:
- User Question
- Optional external knowledge or task context

# Core requirements

Your output must:

- stay schema-agnostic
- stay problem-driven
- separate stable problem framing from logical solving intuition
- distinguish global conditions from local analytic conditions
- identify intermediate analysis results without turning them into core semantic units too early
- preserve unresolved ambiguities instead of forcing premature decisions

# What to produce

## Part A. Problem Frame

Provide a stable description of the question itself, including:

- Restated question
- Result grain: what one output row represents
- Required outputs
- Explicit constraints and scope
- Ranking / ordering requirements if any
- Possible conflict with external knowledge if any
- Remaining ambiguities requiring later verification

## Part B. Logical Grounding

Write 1–3 coherent paragraphs that naturally explain:

- what the analysis is centered on
- what other objects, relations, contextual references, or comparison anchors must be introduced
- what role distinctions, directions, temporal order, reference points, or semantic anchors are critical
- which conditions define the whole analysis scope
- which conditions only define local subsets, local windows, local metrics, or final retention rules
- which results are only intermediate analytic outcomes rather than core conceptual units

This part should read like a natural conceptual reasoning process, not like SQL planning or schema mapping.

## Part C. Structure-Inducing Hints

Based on the logical grounding, list only high-value hints for later ER hypothesis induction:

- possible focal objects
- possible auxiliary objects
- possible role distinctions
- possible semantic links or link-like dependencies
- possible core attribute origins
- candidate global conditions
- candidate local conditions
- structural ambiguities that should remain open for later validation

# Important boundaries

Do not:
- invent table names, columns, keys, or joins
- collapse local conditions into global structure constraints
- elevate temporary aggregates, local windows, bridge paths, or final retained subsets into core semantic units by default
- force final decisions where the question itself remains ambiguous

# Output format

Return a JSON object with the following fields:

{
  "problem_frame": {
    "restated_question": "",
    "result_grain": "",
    "required_outputs": [],
    "explicit_constraints": [],
    "ordering_or_ranking": [],
    "external_knowledge_conflicts": [],
    "open_ambiguities": []
  },
  "logical_grounding": {
    "natural_reasoning": ""
  },
  "structure_inducing_hints": {
    "possible_focal_objects": [],
    "possible_auxiliary_objects": [],
    "possible_role_distinctions": [],
    "possible_semantic_links": [],
    "possible_attribute_origins": [],
    "candidate_global_conditions": [],
    "candidate_local_conditions": [],
    "open_structural_ambiguities": []
  }
}