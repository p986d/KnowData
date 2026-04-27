@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "METADATA_DIR=D:\Workspace\Knowdata\metadata\20260426-181526"
set "NL2SQL_ENGINE=reforce_gen_sl_m1"
set "MODEL_CONFIG=deepseek_chat_personal"
set "MAX_BATCH_WORKERS=4"
set "MAX_LINKING_WORKERS=16"

python -m src.run.er2data_2 ^
  --metadata-dir "%METADATA_DIR%" ^
  --engine-provider "%NL2SQL_ENGINE%" ^
  --question-model-config "%MODEL_CONFIG%" ^
  --schema-link-model-config "%MODEL_CONFIG%" ^
  --include-conditions-in-er2query ^
  --max-batch-workers %MAX_BATCH_WORKERS% ^
  --max-linking-workers %MAX_LINKING_WORKERS%
if errorlevel 1 (
  echo [run_er2data_only.bat] er2data_2 run failed.
  popd
  exit /b 1
)

popd
