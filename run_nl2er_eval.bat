@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

if "%~1"=="" (
    echo Usage:
    echo   run_nl2er_synth_eval.bat path\to\nl2er_output.json
    popd
    exit /b 1
)

set "INPUT_JSON=%~1"

REM 如果你用的是 conda，可以取消下面两行注释
REM call D:\Tools\MiniConda\Scripts\activate.bat your_env_name
REM if errorlevel 1 exit /b 1

python -m src.run.nl2er_synth_eval "%INPUT_JSON%" --model-config deepseek_chat --max-retry 1

set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%