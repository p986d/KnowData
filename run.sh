#!/usr/bin/env bash

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Choose the source input file.
# INPUT_PATH="$PWD/data/input.json"
INPUT_PATH="$PWD/data/sy_input.json"
INPUT_PATH="$PWD/data/sy_input_with_hint.json"

# Choose `single` to run specified question_id values, or `all` to traverse every question in INPUT_PATH.
RUN_MODE="all"

QUESTION_IDS="sy13"

# LLM config.
# LLM="qwen3_30B_instruct"
LLM="deepseek_chat"
# LLM="qwen_max"

NL2SQL_ENGINE="reforce_gen_sl_m1"

if [ "${RUN_MODE,,}" = "single" ]; then
  python -m src.run.pipeline \
    --input-path "$INPUT_PATH" \
    --question-id "$QUESTION_IDS" \
    --nl2er-model-config "$LLM" \
    --question-model-config "$LLM" \
    --nl2sql-model-config "$LLM" \
    --include-conditions-in-er2query \
    --include-conditions-in-sql2nl \
    --enable-nl2er-db-hint false \
    --enable-er2data-db-hint true \
    --max-nl2sql-workers 64
elif [ "${RUN_MODE,,}" = "all" ]; then
  python -m src.run.pipeline \
    --input-path "$INPUT_PATH" \
    --engine-provider "$NL2SQL_ENGINE" \
    --nl2er-model-config "$LLM" \
    --question-model-config "$LLM" \
    --nl2sql-model-config "$LLM" \
    --include-conditions-in-er2query \
    --include-conditions-in-sql2nl \
    --enable-nl2er-db-hint true \
    --enable-er2data-db-hint true \
    --enable-er2query-db-hint false \
    --exclude-desc-in-er2query \
    --max-batch-workers 8 \
    --nl2sql-timeout-seconds 800 \
    --max-nl2sql-workers 64
else
  echo "[run.sh] invalid RUN_MODE=$RUN_MODE. Use 'single' or 'all'." >&2
  exit 1
fi
