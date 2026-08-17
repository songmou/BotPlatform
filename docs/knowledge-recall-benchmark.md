# 知识库双语召回评测

本流程使用固定抽样的 T²Ranking 中文检索集与 BEIR SciFact 英文检索集，评测对象是平台真实的“文件管理 → 知识库索引 → 会话自动召回”链路。

## 1. 准备评测包

评测依赖不属于默认运行时：

```bash
python -m pip install -r requirements-benchmark.txt
python scripts/knowledge_recall_benchmark.py prepare \
  --output /tmp/botplatform-knowledge-benchmark
```

固定参数为随机种子 `20260810`、每个基准 60 个问题（前 30 个调优、后 30 个锁定测试）和最多 800 个文档。生成目录包含独立 Markdown 文档、`questions.jsonl`、`manifest.json` 与许可说明。

## 2. 文件管理和知识库

1. 在公共文件区上传整个生成目录，目标路径为 `knowledge-benchmark/`。
2. 新建公共知识库“召回评测-T2Ranking”和“召回评测-SciFact”。
3. 从文件管理分别选择 `t2ranking/docs` 与 `scifact/docs` 下的全部 Markdown 文件并加入对应知识库。
4. 新建“知识库评测助手”，只绑定上述两个知识库；不要给现有日常智能体增加绑定。

若现有数据库仍是旧格式，可用 `BOTPLATFORM_DATA_DIR=/绝对路径` 启动一个全新评测实例。未设置该变量时仍使用仓库的 `data/`，不会改变现有部署行为。文件夹上传与“从网盘加入知识库”均支持本基准的 800 文件规模。

## 3. 基线与向量评测

FTS5 基线不加载配置中的 embedding/rerank：

```bash
python scripts/knowledge_recall_benchmark.py evaluate \
  --questions /tmp/botplatform-knowledge-benchmark/questions.jsonl \
  --split tuning \
  --output data/public/knowledge-benchmark/reports/fts-tuning
```

启用 `bge_m3_local`、完整重启并强制重建向量后，加上 `--configured-models`：

```bash
python scripts/knowledge_recall_benchmark.py evaluate \
  --questions /tmp/botplatform-knowledge-benchmark/questions.jsonl \
  --split tuning --configured-models \
  --output data/public/knowledge-benchmark/reports/bge-m3-tuning
```

只允许使用 `tuning` 结果选择 RRF 参数。固定网格为：`candidate_pool ∈ {100, 200}`、`rrf_k ∈ {20, 60}`、`lexical_weight ∈ {0.25, 0.5, 1.0, 2.0}`，`vector_weight=1.0`。选择顺序固定为：最低单语 `HitRate@6` 最大、两种语言平均 `MRR@6` 最大、P95 延迟更低。每个组合都用 `evaluate` 写入独立报告，不得人工挑选单题结果。

参数冻结后将 `--split` 改为 `test`，锁定测试集只执行一次。每种语言需达到 `HitRate@6 ≥ 95%` 且 `MRR@6 ≥ 0.75`。

本轮固定调优集按上述规则选择出的融合参数为 `candidate_pool=100`、`rrf_k=20`、`lexical_weight=1.0`、`vector_weight=1.0`。若更换语料版本或向量模型，必须重新运行整个固定网格，不能沿用该结论。

可选重排不需要修改默认应用绑定。安装独立依赖后，在评测命令中显式指定模型：

```bash
python -m pip install -r requirements-rerank.txt
python scripts/knowledge_recall_benchmark.py evaluate \
  --questions /tmp/botplatform-knowledge-benchmark/questions.jsonl \
  --split tuning --configured-models \
  --rerank-model bge_reranker_v2_m3_local \
  --output data/knowledge-benchmark/reports/rerank-tuning
```

本轮重排固定为融合后的前 32 个分块，最终 Top-6 按来源文档去重。本地适配器输入包含标题与正文，最大长度固定为 1024 token；模型档案可启用，但 `config/app.json` 的默认 `rerank_model` 保持为空，避免未安装可选依赖时影响默认运行。

## 4. 会话验收

从每种语言的锁定测试集按清单顺序取前 20 题。每题新建会话并选择“知识库评测助手”，记录页面参考来源中的文档 ID；每种语言至少 19/20 题必须包含一个 qrels 文档，且来源链接可打开。

若混合召回仍不达标，可安装 `requirements-rerank.txt`，在隔离评测实例的模型管理页面启用 `bge_reranker_v2_m3_local` 并绑定“重排模型”角色。该依赖和模型均为可选项，不影响默认安装。

## 5. 本轮锁定结果

锁定集仅执行一次。T²Ranking 达到 `HitRate@6=100%`、`MRR@6=0.978`；SciFact 为 `HitRate@6=90%`、`MRR@6=0.712`，未达到严格门槛，差距分别为 5 个百分点与 0.038。两个基准的空结果率、向量降级和重排降级均为 0。不得依据该结果更换样本或继续调参，失败题及完整排名见 `data/knowledge-benchmark/reports/final-locked-test.{json,csv,md}`。
