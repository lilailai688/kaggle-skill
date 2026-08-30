# V2 记忆结构

## 工作区

```text
<workspace>/
  competition.json
  champions.json
  STATE.md                         # 自动生成
  ideas_backlog.md                 # 自动生成
  ledger/events.jsonl
  data/manifests/
  runs/<run_id>/
  artifacts/sha256/
  reports/current/
  reports/archive/state/
  submissions/
  rules/
  release/
  flags/
  validators/
```

## Competition Profile

`competition.json` 使用 `schema_version: 2`，包含：

- `competition`：slug、平台、开放式 `problem_type` 与提交方式；
- `phases`：截止时间、分数范围和提交额度；
- `tracks`：开放式任务类型和一个或多个指标定义；
- `input_contract`：递归文件匹配、表结构、主键、缺失策略、时间上界和 hook；
- `output_contract`：必需产物、ZIP 成员、发布包含/排除项和 hook；
- `dryness`：默认耐心参数；
- `rules`：外部数据、网络、许可证、最大业务日期、外部资产和禁止捷径；
- `extensions`：赛题自己拥有的结构化扩展。

每个 track 必须且只能有一个主指标。平台与任务类型是非空字符串，不是封闭枚举。

## 事件账本

每条事件包含 `schema_version`、`event_id`、固定 `event_type`、`occurred_at` 和
`payload`，并按需携带 `track_id`、`phase_id`、`data_snapshot_id`、`run_id`、
`idea_id` 与 `experiment_family`。

运行状态只能是 `planned/running/completed/failed/invalid`；候选决策只能是
`pending/promoted/rejected/superseded`。账本只追加，纠错必须写新事件。

## 指标

每个指标必须完整记录：

```json
{
  "name": "metric_name",
  "value": 0.0,
  "direction": "higher",
  "source": "local_cv",
  "split": "fold-plan-v1",
  "scope": "overall",
  "comparable_group": "protocol-v1"
}
```

`source` 只能是 `local_cv`、`local_proxy`、`public_lb`、`private_lb` 或
`final_result`。名称相同但上下文不同的两个值仍不可比较。

## 运行清单

`run-manifest.json` 记录命令、源码树、Git commit/patch、配置、比赛画像、规则、
数据、Python 包环境、随机种子、父运行身份、耗时、日志哈希、不变量和产物。
完成后写入独立 SHA256 seal；只有身份、seal 和 CAS 产物都一致时才能 resume。

## 冠军注册表

`champions.json` 使用 `track|phase|data_snapshot` 作为 lane key，记录显式晋升的
冠军、指标、线上锚点、挑战者、回滚候选和更新时间。迁移和榜单反馈都不能自动改表。

## V1 迁移

迁移只能写入新目录。旧 `STATE.md`、backlog 和原始 JSONL 按哈希完整归档；每个
物理账本条目都生成 `legacy_record_imported`，包括空行占位。未知状态保留在
`legacy_status`，完整 JSON 保留在 `legacy_payload`，禁止推断真实语义或冠军。
