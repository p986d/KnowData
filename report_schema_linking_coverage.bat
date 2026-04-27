@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "PYTHON_EXE=D:\Tools\MiniConda\envs\spider2\python.exe"

if not exist "%PYTHON_EXE%" (
    echo Python executable not found: %PYTHON_EXE%
    popd
    exit /b 1
)

if "%~1"=="" (
    echo Usage:
    echo   report_schema_linking_coverage.bat metadata\20260426-181526 data\ground_truth [--json] [--show-covered-details] [--dialect DIALECT]
    popd
    exit /b 1
)

if "%~2"=="" (
    echo Usage:
    echo   report_schema_linking_coverage.bat metadata\20260426-181526 data\ground_truth [--json] [--show-covered-details] [--dialect DIALECT]
    popd
    exit /b 1
)

set "METADATA_DIR=%~1"
set "GROUND_TRUTH_DIR=%~2"
shift
shift

set "EXTRA_ARGS="
:collect_args
if "%~1"=="" goto run_command
set "EXTRA_ARGS=%EXTRA_ARGS% "%~1""
shift
goto collect_args

:run_command
"%PYTHON_EXE%" -m src.validation.schema_linking_coverage --metadata-dir "%METADATA_DIR%" --ground-truth-dir "%GROUND_TRUTH_DIR%" %EXTRA_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

popd
exit /b %EXIT_CODE%
