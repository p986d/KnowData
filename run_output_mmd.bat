@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "PYTHON_EXE=D:\Tools\MiniConda\envs\spider2\python.exe"
set "INPUT_ROOT=%CD%\metadata"

if not exist "%PYTHON_EXE%" (
  echo [run_output_mmd.bat] Python executable not found: %PYTHON_EXE%
  popd
  exit /b 1
)

if not exist "%INPUT_ROOT%" (
  echo [run_output_mmd.bat] Metadata directory not found: %INPUT_ROOT%
  popd
  exit /b 1
)

"%PYTHON_EXE%" -B -m src.utils.er_json_to_mermaid "%INPUT_ROOT%" 
if errorlevel 1 (
  echo [run_output_mmd.bat] Mermaid generation failed.
  popd
  exit /b 1
)

echo [run_output_mmd.bat] Mermaid files generated for all metadata cases under %INPUT_ROOT%.

popd
