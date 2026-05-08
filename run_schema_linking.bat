@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

@REM Existing NL2ER metadata batch. Each case should contain nl2er_output.json and input.json.
set "METADATA_DIR=D:\Workspace\Ace\Knowdata\metadata\20260507-120109"
@REM Choose `single` to run specified question_id values, or `all` to traverse every question in METADATA_DIR.
set "RUN_MODE=all"
set "MAX_WORKERS=64"
set "MAX_BATCH_WORKERS=16"
set "MAX_LINKING_WORKERS=64"

set "INPUT_PATH=%CD%\data\input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"
@REM Multiple values can be comma-separated.
set "QUESTION_IDS=sf_bq050"

set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"
set "NL2SQL_ENGINE=reforce_gen_sl_m1"
@REM Supported values: linked_columns, linked_table_all_columns
set "SNAPSHOT_MODE=linked_columns"
set "GROUND_TRUTH_DIR=%CD%\data\ground_truth"
set "QUESTION_MODEL_CONFIG=%LLM%"
set "SCHEMA_LINK_MODEL_CONFIG=%LLM%"

if /I "%RUN_MODE%"=="all" (
    @REM python -m src.run.er2data_2 --metadata-dir "%METADATA_DIR%" --engine-provider "%NL2SQL_ENGINE%" --question-model-config "%QUESTION_MODEL_CONFIG%" --schema-link-model-config "%SCHEMA_LINK_MODEL_CONFIG%" --reasoning-mode "%REASONING_MODE%" --include-conditions-in-er2query --max-batch-workers %MAX_BATCH_WORKERS% --max-linking-workers %MAX_LINKING_WORKERS%
    python -m src.run.schema_linking_only --metadata-dir "%METADATA_DIR%" --output-filename "schema_linking_2.json" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --engine-provider "%NL2SQL_ENGINE%" --snapshot-mode "%SNAPSHOT_MODE%" --max-workers %MAX_WORKERS% --ground-truth-dir "%GROUND_TRUTH_DIR%" --coverage-schema-linking-filename "schema_linking_2.json" --quiet
) else (
    python -m src.run.er2data_2 --metadata-dir "%METADATA_DIR%" --engine-provider "%NL2SQL_ENGINE%" --question-model-config "%QUESTION_MODEL_CONFIG%" --schema-link-model-config "%SCHEMA_LINK_MODEL_CONFIG%" --reasoning-mode "%REASONING_MODE%" --include-conditions-in-er2query --max-batch-workers %MAX_BATCH_WORKERS% --max-linking-workers %MAX_LINKING_WORKERS%
    python -m src.run.schema_linking_only --metadata-dir "%METADATA_DIR%" --question-id "%QUESTION_IDS%" --output-filename "schema_linking_2.json" --merge-unit-schema-linking --unit-schema-linking-filename "schema_linking.json" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --engine-provider "%NL2SQL_ENGINE%" --snapshot-mode "%SNAPSHOT_MODE%" --max-workers %MAX_WORKERS% --ground-truth-dir "%GROUND_TRUTH_DIR%" --coverage-schema-linking-filename "schema_linking_2.json" --quiet
)

popd
