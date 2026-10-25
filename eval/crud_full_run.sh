#!/usr/bin/env bash
# CRUD 全量 80k 口径的收尾流程（灌库完成后执行）
# 用法：bash eval/crud_full_run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . "$HOME/.ekbs/secrets.env"; set +a

echo "== 1) 补灌失败文档 =="
.venv/bin/python eval/retry_failed.py --root var/eval/crud --kb-code crud_bench \
  --config config/application.eval-crud.yaml --source-prefix "bench://crud/" || true

echo "== 2) 全量检索+答案（输出到 full 文件，不动收窄口径结果） =="
.venv/bin/python eval/crud_eval.py --stage run \
  --config config/application.eval-crud.yaml \
  --out var/eval/crud/results_full.jsonl \
  --summary-out var/eval/crud/summary_full.json --concurrency 6

echo "== 3) DeepSeek 判分 =="
zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/crud_eval.py --stage judge \
  --results var/eval/crud/results_full.jsonl \
  --judge-out var/eval/crud/judge_full.jsonl --concurrency 8'

echo "== 完成：var/eval/crud/{summary_full,judge_summary_judge_full}.json =="
