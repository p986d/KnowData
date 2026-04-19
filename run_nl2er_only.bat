@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1


@REM Choose `single` to run specified question_id values, or `all` to traverse every question in data\input.json.
set "RUN_MODE=single"

set "INPUT_PATH=%CD%\data\input.json"
set "INPUT_PATH=%CD%\data\sy_input.json"

@REM set "QUESTION_IDS=sf_bq017"
@REM set "QUESTION_IDS=sf_bq182"
@REM set "QUESTION_IDS=sf_bq209"
@REM set "QUESTION_IDS=sf_bq248"
@REM set "QUESTION_IDS=sf_bq050"
@REM set "QUESTION_IDS=sf_local015"
@REM set "QUESTION_IDS=sf_bq017"
@REM set "QUESTION_IDS=sf_bq341"
set "QUESTION_IDS=sy00"

@REM set "LLM=qwen3_30B_instruct"
set "LLM=deepseek_chat_2"

@REM set "LLM=qwen_max"

@REM Used only when RUN_MODE=all.
set "MAX_WORKERS=4"

@REM set "LLM=deepseek_chat_personal"
if /I "%RUN_MODE%"=="single" goto run_single
if /I "%RUN_MODE%"=="all" goto run_all

echo [run_nl2er_only.bat] invalid RUN_MODE=%RUN_MODE%. Use `single` or `all`.
popd
exit /b 1

:run_single
python -m src.run.nl2er_only ^
  --input-path "%INPUT_PATH%" ^
  --question-id %QUESTION_IDS% ^
  --model-config "%LLM%"
goto after_run

:run_all
python -m src.run.nl2er_only ^
  --input-path "%INPUT_PATH%" ^
  --model-config "%LLM%" ^
  --max-workers %MAX_WORKERS%

:after_run
if errorlevel 1 (
  echo [run_nl2er_only.bat] nl2er run failed.
  popd
  exit /b 1
)

popd
