@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "NL2ER_OUTPUT_PATH=D:\Workspace\Knowdata\metadata\sf_bq050_20260408-174639\nl2er_output.json"
set "ENGINE_PROVIDER=reforce_gen_sl_m1"
set "MODEL_CONFIG=deepseek_chat"
set "MAX_NL2SQL_WORKERS=64"


python -m src.run.er2data ^
  --input-path "%NL2ER_OUTPUT_PATH%" ^
  --engine-provider "%ENGINE_PROVIDER%" ^
  --question-model-config "%MODEL_CONFIG%" ^
  --nl2sql-model-config "%MODEL_CONFIG%" ^
  --include-conditions-in-er2query ^
  --max-nl2sql-workers %MAX_NL2SQL_WORKERS%
if errorlevel 1 (
  echo [run_er2data_only.bat] er2data run failed.
  popd
  exit /b 1
)

popd
