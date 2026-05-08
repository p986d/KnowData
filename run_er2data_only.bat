@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "METADATA_DIR=D:\Workspace\Ace\Knowdata\metadata\20260507-120109"
set "GROUND_TRUTH_DIR=%CD%\data\ground_truth"
set "NL2SQL_ENGINE=reforce_gen_sl_m1"
set "MAX_BATCH_WORKERS=16"
set "MAX_LINKING_WORKERS=64"
set "MAX_PROFILE_WORKERS=1"
set "MAX_TABLE_PROFILE_CONCURRENCY=64"
set "SAMPLE_ROW_LIMIT=2"
set "SAMPLE_VALUE_MAX_CHARS=300"
set "COVERAGE_REPORT_PATH=%METADATA_DIR%\schema_linking_coverage_report.txt"

set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"

set "QUESTION_MODEL_CONFIG=%LLM%"
set "SCHEMA_LINK_MODEL_CONFIG=%LLM%"


python -m src.run.er2data_2 --metadata-dir "%METADATA_DIR%" --engine-provider "%NL2SQL_ENGINE%" --question-model-config "%QUESTION_MODEL_CONFIG%" --schema-link-model-config "%SCHEMA_LINK_MODEL_CONFIG%" --reasoning-mode "%REASONING_MODE%" --include-conditions-in-er2query --max-batch-workers %MAX_BATCH_WORKERS% --max-linking-workers %MAX_LINKING_WORKERS%

python -m src.run.schema_linking_table_semantic_sketch --metadata-dir "%METADATA_DIR%" --schema-linking-filename schema_linking.json --output-filename linked_table_profile.json --model-config "%SCHEMA_LINK_MODEL_CONFIG%" --reasoning-mode "%REASONING_MODE%" --sample-row-limit %SAMPLE_ROW_LIMIT% --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% --max-workers %MAX_PROFILE_WORKERS% --max-table-concurrency %MAX_TABLE_PROFILE_CONCURRENCY% --write-enriched-schema-linking

python -m src.validation.schema_linking_coverage --metadata-dir "%METADATA_DIR%" --ground-truth-dir "%GROUND_TRUTH_DIR%" --output-path "%COVERAGE_REPORT_PATH%"

echo [run_er2data_only.bat] coverage report wrote to %COVERAGE_REPORT_PATH%

popd
