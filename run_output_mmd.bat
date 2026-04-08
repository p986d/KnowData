@echo off
setlocal EnableExtensions

set "file=D:\Workspace\Knowdata\metadata\sf_local030_20260401-160944\nl2er_output.json"

python -m src.utils.er_json_to_mermaid %file% --show-relationship-labels