
@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in data\input.json.
set "RUN_MODE=all"
set "MODE=er_test_st2"
set "MAX_WORKERS=25"

set "INPUT_PATH=%CD%\data\input.json"
set "INPUT_PATH=%CD%\data\sy_input.json"
set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"
@REM Multiple values can be comma-separated.
set "QUESTION_IDS=sf_bq248"
set "QUESTION_IDS=sf_bq017"
@REM set "QUESTION_IDS=sy02"
@REM set "QUESTION_IDS=sf_bq182"
@REM set "QUESTION_IDS=sf_bq209"
@REM set "QUESTION_IDS=sf_bq341"
@REM set "QUESTION_IDS=sf_bq248"
@REM set "QUESTION_IDS=sf_bq254"
@REM set "QUESTION_IDS=sf_local015"
@REM set "QUESTION_IDS=sf_local030"
@REM set "QUESTION_IDS=sf_local157"


@REM Alternative example for list input:
@REM set "INPUT_PATH=%CD%\data\sy_input.json"
set "QUESTION_IDS=sy00"

set "LLM=deepseek_chat_2"

if /I "%RUN_MODE%"=="all" (
    python -m src.run.nl2er_2_only --mode "%MODE%" --input-path "%INPUT_PATH%" --model-config %LLM% --max-workers %MAX_WORKERS%
) else (
    python -m src.run.nl2er_2_only --mode "%MODE%" --input-path "%INPUT_PATH%" --question-id "%QUESTION_IDS%" --model-config %LLM% --max-workers %MAX_WORKERS%
)

popd
