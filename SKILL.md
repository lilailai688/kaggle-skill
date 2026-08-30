---
name: kaggle-skill
description: 面向 Kaggle、天池和自定义评测的通用竞赛研究工作流。用于赛题画像、验证设计、隔离实验、可比指标与冠军管理、提交预算、榜单反馈和可复现最终发布；不绑定具体平台、数据模态或业务领域。
---

# Kaggle Skill

把竞赛当作有证据链的研究项目，而不是一组互不关联的 Notebook。核心层与平台、
模态和领域无关：分类、回归、排序、预测、视觉、文本、多模态、无监督、仿真、
优化和代码赛共享同一套来源、隔离和决策规则，赛题差异通过契约与 hook 注入。

## 工作模型

1. 优化前先完成赛题画像：任务语义、指标、推理时可用信息、独立性结构、提交
   方式、规则、阶段、算力和提交额度。
2. 用 `scripts/kaggle_ops.py bootstrap` 建立 V2 工作区，再配置
   `competition.json`。把目标、流水线、模态或独立研究方向建成通用 `track`，
   不把某场比赛的领域词写进核心。
3. 搜索模型前先建立可信的端到端 baseline 和验证契约。代理指标必须标为
   `local_proxy`，不能冒充正式 CV。
4. 每个实验声明假设、预期信号、失败模式、成本和停止条件。执行一个可审计决策
   单元：计划、隔离运行、验证、证据记录和明确决策；不机械限制为一条命令。
5. 只有 `track`、阶段、数据快照、来源、split、scope、方向和
   `comparable_group` 全部一致的指标才能比较。
6. 冠军只能显式晋升，并保留线上锚点、挑战者和回滚版本。受控差异中声明为
   不变的组件必须通过字节或注册校验器验证。
7. 把提交分为 `probe`、`candidate` 和 `final`。审批必须绑定候选 ID、产物哈希、
   意图、请求和有效期；Skill 只准备 `ready_for_human_submission`，永不自动提交。
8. 榜单反馈只说明整个候选的结果，不提供逐样本真值。只有受控差异才能支持有限
   归因，反馈不会自动晋升冠军。
9. 某条可比赛道枯竭时，停止局部微调，复盘被证伪的假设，补充新证据或关闭赛道。
10. 以冷启动复现、确定性发布包和赛后复盘结束比赛。

## 确定性入口

所有会改变工作区状态的操作优先使用统一 CLI：

```bash
python scripts/kaggle_ops.py --help
```

命令包括 `bootstrap`、`migrate`、`ingest`、`idea`、`run`、`validate`、
`status`、`dryness`、`gate`、`feedback`、`promote`、`release` 和
`postmortem`。机器真相位于 `competition.json`、`champions.json` 和
`ledger/events.jsonl`；`STATE.md` 与 `ideas_backlog.md` 只是自动生成视图。

## 不可违反的规则

- 原始输入按只读契约使用，每个运行拥有独立可写目录和锁；native 模式做前后哈希
  检测，最终候选必须使用容器只读挂载。
- 不因指标名称相似就推断其可比，不混合任务、日期、代理指标或榜单来源。
- 不伪造分数、来源、合规状态、外部数据许可或人工审批。
- 不复用不同产物、不同意图或已过期的审批。
- 不自动下载需登录的数据，不自动执行榜单提交，不允许系统自我审批。
- 不晋升无效运行；旧工作区迁移时不猜测状态，也不推断冠军。
- 本地验证和榜单矛盾时，记录矛盾并缩小下一次实验范围，不改写历史。
- 领域规则只能进入 `competition.json`、规则快照或 validator hook，不能进入通用
  核心。

## 按需参考

- `references/competition-strategy.md`：跨赛题类型的画像、验证与实验选择。
- `references/architecture.md`：V2 分层、运行、冠军、适配器与发布边界。
- `references/memory-schema.md`：机器结构、事件、指标和只读迁移。
- `references/v2-cli.md`：命令、输入、运行环境和退出码。
- `references/submission-gates.md`：三种提交意图与审批约束。
- `references/gate-report.example.json`：候选门控报告的通用字段模板。
- `references/dryness-and-reflection.md`：分赛道枯竭检测与研究补充。
- `references/agent-roles.md`：大型项目中的可选角色边界。
