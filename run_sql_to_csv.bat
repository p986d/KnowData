@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "PYTHON_CMD="
if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "PYTHON_CMD=%CONDA_PREFIX%\python.exe"
if not defined PYTHON_CMD if exist "D:\Tools\MiniConda\python.exe" set "PYTHON_CMD=D:\Tools\MiniConda\python.exe"
if not defined PYTHON_CMD set "PYTHON_CMD=python"

if "%~1"=="" goto show_usage
if /I "%~1"=="--stdin" goto run_stdin
if exist "%~1" goto run_sql_file

:run_single_line
set "KNOWDATA_SQL_INPUT=%*"
"%PYTHON_CMD%" -m src.utils.sql_to_csv --sql-from-env KNOWDATA_SQL_INPUT
set "EXIT_CODE=%ERRORLEVEL%"
goto finalize

:run_sql_file
"%PYTHON_CMD%" -m src.utils.sql_to_csv --sql-file "%~1"
set "EXIT_CODE=%ERRORLEVEL%"
goto finalize

:run_stdin
"%PYTHON_CMD%" -m src.utils.sql_to_csv --stdin
set "EXIT_CODE=%ERRORLEVEL%"
goto finalize

:show_usage
echo Usage:
echo   run_sql_to_csv.bat "SELECT * FROM DB.SCHEMA.TABLE"
echo   run_sql_to_csv.bat path\to\query.sql
echo   type path\to\query.sql ^| run_sql_to_csv.bat --stdin
echo.
echo Notes:
echo   1. Single-line string mode supports escaped sequences such as \n, \t, \", and \'.
echo   2. Multi-line SQL is recommended via a .sql file or stdin pipe.
echo   3. The output CSV defaults to metadata\sql_to_csv\query_result_*.csv
set "EXIT_CODE=1"

:finalize
if errorlevel 1 (
  echo [run_sql_to_csv.bat] execution failed.
)

popd
exit /b %EXIT_CODE%
