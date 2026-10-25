#!/usr/bin/env bash
# CRUD 全量 80k 口径收尾：补失败 → 无门禁全量评测 → 门禁全量评测 → 两次判分
# 用法：bash eval/crud_full_run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . "$HOME/.ekbs/secrets.env"; set +a

echo "== 1) 补灌失败文档 =="
.venv/bin/python eval/retry_failed.py --root var/eval/crud --kb-code crud_bench \
  --config config/application.eval-crud.yaml --source-prefix "bench://crud/" \
  --defer-faiss-save || true

echo "== 2) 全量基线评测（门禁关闭） =="
.venv/bin/python eval/crud_eval.py --stage run \
  --config config/application.eval-crud.yaml \
  --out var/eval/crud/results_full.jsonl \
  --summary-out var/eval/crud/summary_full.json --concurrency 6
zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/crud_eval.py --stage judge \
  --results var/eval/crud/results_full.jsonl \
  --judge-out var/eval/crud/judge_full.jsonl --concurrency 8'

echo "== 3) 全量门禁评测（同一语料快照，门禁开启） =="
.venv/bin/python eval/crud_eval.py --stage run \
  --config config/application.eval-crud-gate.yaml \
  --out var/eval/crud/results_full_gate.jsonl \
  --summary-out var/eval/crud/summary_full_gate.json --concurrency 6
zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/crud_eval.py --stage judge \
  --results var/eval/crud/results_full_gate.jsonl \
  --judge-out var/eval/crud/judge_full_gate.jsonl --concurrency 8'

echo "== 完成 =="
echo "基线：var/eval/crud/{summary_full.json,judge_summary_judge_full.json}"
echo "门禁：var/eval/crud/{summary_full_gate.json,judge_summary_judge_full_gate.json}"
