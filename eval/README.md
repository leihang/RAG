# eval/ —— 公开基准评测（RGB 中文 + C-MTEB DuRetrieval）

不碰业务库：专用 `config/application.eval.yaml`（`data_dir=var/eval/runtime`）与独立 venv
（`var/eval/venv`），数据/结果都在 `var/eval/`（gitignore）。

## 一、数据准备

| 数据 | 来源 | 放到 |
|---|---|---|
| RGB 中文（4 个 JSONL） | https://github.com/chen700564/RGB （CC BY-NC-SA 4.0，仅非商业） | `var/eval/datasets/rgb/` |
| C-MTEB DuRetrieval（mteb 版，含 qrels） | HF `mteb/DuRetrieval` | `var/eval/datasets/mteb/{corpus,queries,data}/` |

## 二、跑法

```bash
# 0) 评测配置（隔离 data_dir，不入库）
#    config/application.eval.yaml = cloud 配置 + data_dir/log_dir/db.path 指到 var/eval/runtime

# 1) RGB 转语料（JSONL 内嵌文档 → 一篇一个 md；holdout 题文档不入库）
.venv/bin/python eval/rgb_prepare.py

# 2) 灌库（隔离评测库；可断点续跑；并发 8 约 25 分钟）
set -a; . ~/.ekbs/secrets.env; set +a
.venv/bin/python eval/ingest_rgb.py --concurrency 8

# 3) 检索指标（Hit@k / Recall@k / MRR / nDCG@10 + 负例拒答）
.venv/bin/python eval/retrieval_eval.py --k 10

# 4) 答案生成 + 官方口径准确率（阶段 A；产出 answers.jsonl）
.venv/bin/python eval/answer_eval.py

# 5) 向量模型评测（C-MTEB DuRetrieval，nDCG@10 / Recall@100）
var/eval/venv/bin/python eval/embedding_eval.py
```

## 三、口径与注意

- **RGB holdout**：按题号哈希取约 1/5 题，其文档不进语料库 → 这些题应拒答；用于量"负例拒答"。
- **文档级指标**：同一 `doc_title` 只计一次；Recall@k 按 gold slot 覆盖算（信息整合题一个槽位可有多个候选文档）。
- **反事实题**（`fact`）：另报 `wrong_doc_seen_rate`（检索是否召回了被篡改文档）与 `fake_answer_seen_rate`（回答是否被假答案带偏）。
- **可比性**：这是"公开基准题集 + 本项目管道"，与论文数字不可直接横比；报告需附配置/模型指纹。
- **费用**：RGB 灌库 embedding ≈ 0.2 元；DuRetrieval 向量评测 ≈ 2 元；答案生成 + RAGAS judge 另计。
- **隔离**：`var/index` 是业务库，评测全程只读写 `var/eval/runtime`。
