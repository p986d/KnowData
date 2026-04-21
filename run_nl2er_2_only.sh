#!/usr/bin/env bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Choose `single` to run specified question_id values, or `all` to traverse every question in data/input.json.
RUN_MODE="all"
MODE="er_test_st2"
MAX_WORKERS="25"

INPUT_PATH="$PWD/data/input.json"
INPUT_PATH="$PWD/data/sy_input.json"
# INPUT_PATH="$PWD/data/sy_input_with_hint.json"

# Multiple values can be comma-separated.
QUESTION_IDS="sf_bq248"
QUESTION_IDS="sf_bq017"
# QUESTION_IDS="sy02"
# QUESTION_IDS="sf_bq182"
# QUESTION_IDS="sf_bq209"
# QUESTION_IDS="sf_bq341"
# QUESTION_IDS="sf_bq248"
# QUESTION_IDS="sf_bq254"
# QUESTION_IDS="sf_local015"
# QUESTION_IDS="sf_local030"
# QUESTION_IDS="sf_local157"

# Alternative example for list input:
# INPUT_PATH="$PWD/data/sy_input.json"
QUESTION_IDS="sy00"

LLM="deepseek_chat_2"

if [ "${RUN_MODE,,}" = "all" ]; then
  python -m src.run.nl2er_2_only \
    --mode "$MODE" \
    --input-path "$INPUT_PATH" \
    --model-config "$LLM" \
    --max-workers "$MAX_WORKERS"
else
  python -m src.run.nl2er_2_only \
    --mode "$MODE" \
    --input-path "$INPUT_PATH" \
    --question-id "$QUESTION_IDS" \
    --model-config "$LLM" \
    --max-workers "$MAX_WORKERS"
fi
