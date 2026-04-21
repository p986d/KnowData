@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

@REM Choose the source input file.
set "INPUT_PATH=%CD%\data\input.json"
set "INPUT_PATH=%CD%\data\sy_input.json"
set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in INPUT_PATH.
set "RUN_MODE=single"


set "QUESTION_IDS=sy13"

@REM set "QUESTION_IDS=sf_bq050"
@REM set "QUESTION_IDS=sf_bq017"
@REM set "QUESTION_IDS=sf_bq182"
@REM set "QUESTION_IDS=sf_bq209"
@REM set "QUESTION_IDS=sf_bq341"
@REM set "QUESTION_IDS=sf_bq248"
@REM set "QUESTION_IDS=sf_bq254"
@REM set "QUESTION_IDS=sf_local015"
@REM set "QUESTION_IDS=sf_local030"
@REM set "QUESTION_IDS=sf_local157"



@REM set "LLM=qwen3_30B_instruct"
set "LLM=deepseek_chat_2"
@REM set "LLM=qwen_max"

set "NL2SQL_ENGINE=reforce_gen_sl_m1"

if /I "%RUN_MODE%"=="single" goto run_single
if /I "%RUN_MODE%"=="all" goto run_all

echo [run.bat] invalid RUN_MODE=%RUN_MODE%. Use `single` or `all`.
popd
exit /b 1

:run_single
python -m src.run.pipeline ^
  --input-path "%INPUT_PATH%" ^
  --question-id %QUESTION_IDS% ^
  --engine-provider "%NL2SQL_ENGINE%" ^
  --nl2er-model-config "%LLM%" ^
  --question-model-config "%LLM%" ^
  --nl2sql-model-config "%LLM%" ^
  --include-conditions-in-er2query ^
  --include-conditions-in-sql2nl ^
  --enable-nl2er-db-hint true ^
  --enable-er2data-db-hint true ^
  --enable-er2query-db-hint false ^
  --exclude-desc-in-er2query ^
  --nl2sql-timeout-seconds 1800 ^
  --max-nl2sql-workers 64
goto after_run

:run_all
python -m src.run.pipeline ^
  --input-path "%INPUT_PATH%" ^
  --engine-provider "%NL2SQL_ENGINE%" ^
  --nl2er-model-config "%LLM%" ^
  --question-model-config "%LLM%" ^
  --nl2sql-model-config "%LLM%" ^
  --include-conditions-in-er2query ^
  --include-conditions-in-sql2nl ^
  --enable-nl2er-db-hint true ^
  --enable-er2data-db-hint true ^
  --enable-er2query-db-hint false ^
  --exclude-desc-in-er2query ^
  --max-batch-workers 6 ^
  --nl2sql-timeout-seconds 1800 ^
  --max-nl2sql-workers 64

  @REM --exclude-desc-in-er2query ^
:after_run
if errorlevel 1 (
  echo [run.bat] pipeline failed.
  popd
  exit /b 1
)

popd
