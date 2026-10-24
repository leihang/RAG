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

## 四、RAGAS 说明（2026-10-24 实测）

RAGAS 0.4.3 直连 DashScope 兼容端点时**不可用**：
- `LangchainLLMWrapper` → 结构化输出 schema 校验失败（instructor 把对象解成字符串）；
- `llm_factory` 原生路径 → `OpenAIEmbeddings` 缺 `embed_query` / 同样有 schema 问题；
- 结论：RAGAS 需要 OpenAI 兼容且支持 structured output 的 judge（如 OpenAI、DeepSeek 官方 API），
  或改用自研 judge（同一套 RAGAS 指标定义）。本仓库先以「RGB 官方 checkanswer + 项目内置
  rule-based faithfulness」作为答案侧指标，见 `var/eval/rgb/answer_summary.json`。

## 五、负例拒答 + RAGAS（DeepSeek judge）

```bash
# 负例-only 语料（官方 noise_rate=1 口径；7,604 篇 / 962k tokens）
.venv/bin/python eval/negative_prepare.py
set -a; . ~/.ekbs/secrets.env; set +a
.venv/bin/python eval/ingest_rgb.py --root var/eval/rgb-neg --kb-code rgb_neg \
  --source-prefix "bench://rgbneg/" --config config/application.eval-neg.yaml --concurrency 16
.venv/bin/python eval/retrieval_eval.py --root var/eval/rgb-neg --kb-code rgb_neg \
  --config config/application.eval-neg.yaml --subsets refine,int,fact

# RAGAS（judge 用 DeepSeek：DEEPSEEK_API_KEY 在 ~/.zshrc，用 zsh -ic 运行）
zsh -ic 'cd "$PWD" && set -a; . "$HOME/.ekbs/secrets.env"; set +a; \
  var/eval/venv/bin/python eval/ragas_judge.py --per-subset 10 --no-embeddings'
```

说明：`--no-embeddings` 只跑 Faithfulness / ContextPrecision / ContextRecall；
AnswerRelevancy、AnswerCorrectness 依赖 embedding，当前 RAGAS 的 wrapper 与 DashScope
兼容端点不匹配（`embed_query` 缺失 / 请求体被拒），暂缺。

## 六、回答侧 no-answer 判官（动作1）

```bash
# 1) 采集负例库上下文
set -a; . ~/.ekbs/secrets.env; set +a
.venv/bin/python eval/collect_contexts.py

# 2) DeepSeek 判官（basic / strict 两版；DEEPSEEK_API_KEY 在 ~/.zshrc）
zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/no_answer_judge.py --concurrency 8'
zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/no_answer_judge.py --concurrency 8 \
  --prompt strict --out var/eval/rgb/no_answer_judge_strict.jsonl'
```

结果：见 `var/eval/rgb/no_answer_judge*_summary.json`；basic 判官把负例拒答从 7.6% 提到
46.4%（可答误拒 5.2%），strict 提到 58.1%（误拒 7.1%）。

## 七、动作1续：answer-then-verify

```bash
# 1) 负例库先生成候选答案（qwen-max）
set -a; . ~/.ekbs/secrets.env; set +a
.venv/bin/python eval/answer_eval.py --root var/eval/rgb-neg --kb-code rgb_neg \
  --config config/application.eval-neg.yaml --concurrency 6

# 2) DeepSeek 逐条核验
zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/answer_verify.py --concurrency 8'
```

结果：可答误拒 2.9% / 负例拒答 35.4%；与阈值 0.80 级联后 2.9% / 46.4%。

## 八、RAGAS 5 项指标（DeepSeek judge）

```bash
zsh -ic 'cd "$PWD" && set -a; . "$HOME/.ekbs/secrets.env"; set +a; \
  var/eval/venv/bin/python eval/ragas_judge.py --per-subset 10 --out var/eval/rgb/ragas_report_5m.json'
```

要点：DashScope embedding 必须关掉 langchain 的 tiktoken 预分词
（`tiktoken_enabled=False, check_embedding_ctx_length=False`），否则会报
`contents is neither str nor list of str`。30 题分层结果：Faithfulness 0.937 /
Answer Relevancy 0.904 / Context Recall 0.947 / Context Precision 0.714 /
Answer Correctness 0.641。

## 九、claim 分解核验

```bash
zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/answer_verify.py --concurrency 8 \
  --prompt claim --out var/eval/rgb/answer_verify_claim2.jsonl'
```

`--prompt claim` 要求逐条 claim 找原文引用（max_tokens=800 才能完整输出）；
结果：误拒 1.8% / 负例拒答 27.8%，与阈值 0.85 级联后 2.6% / 49.4%。

## 十、CRUD-RAG（第二阶段）

```bash
# 1) 语料：48 个分片（186MB）→ 去重 83,814 篇；题集抽样 773 题
#    数据下载到 var/eval/datasets/crud/{corpus,QuestAnswer*.json}
.venv/bin/python eval/crud_prepare.py --entries-1doc 100 --entries-2docs 50 --entries-3docs 50

# 2) 灌库（隔离 data_dir=var/eval/runtime-crud；约 40–60 分钟）
set -a; . ~/.ekbs/secrets.env; set +a
.venv/bin/python eval/ingest_rgb.py --root var/eval/crud --kb-code crud_bench \
  --source-prefix "bench://crud/" --config config/application.eval-crud.yaml --concurrency 24

# 3) 检索 key_info 覆盖 + 答案生成
.venv/bin/python eval/crud_eval.py --stage run --concurrency 4

# 4) DeepSeek 判官（答案覆盖 key_info / 与参考答案一致性）
zsh -ic 'cd "$PWD" && var/eval/venv/bin/python eval/crud_eval.py --stage judge --concurrency 8'
```

指标：`retrieval_keyinfo_recall@10`、`retrieval_hit_rate`、`answer_keyinfo_recall`、
`answer_correct_rate`、`refused_rate`；结果在 `var/eval/crud/{summary,judge_summary}.json`。

## 十一、CRUD-RAG 收窄口径说明

全量 83,814 篇灌库在 13.6k 篇时被 API 限流/欠费打断；评测改用「13,608 篇干扰池 +
全部可定位源文档（1,627 篇）」的收窄口径，并用 `eval/crud_sources.py` 定位源文档。
该口径用于跨语料回归，不与官方 80k 全库成绩对比。完整结果见 `var/eval/rgb/REPORT.md` 第十二节。
