# V2 CLI

使用 Python 3.11+，从 Skill 根目录执行。

## 生命周期

```bash
python scripts/kaggle_ops.py bootstrap work/demo --slug demo --problem-type classification --metric auc
python scripts/kaggle_ops.py ingest work/demo /path/to/read-only-data
python scripts/kaggle_ops.py idea work/demo --id idea-001 --track primary --phase active --family baseline --priority high --hypothesis "Trusted baseline"
python scripts/kaggle_ops.py run work/demo --track primary --phase active --data-snapshot SNAPSHOT --family baseline --idea-id idea-001 --command "python /path/to/code/train.py" --source-root /path/to/code --artifact output.csv
python scripts/kaggle_ops.py validate work/demo --deep
python scripts/kaggle_ops.py status work/demo
```

`bootstrap` 只生成通用默认值。运行 `ingest` 前，应根据赛题补充
`competition.json` 的文件、表结构、主键、缺失、时间和输出契约，并把正式规则
快照写入 `rules/`。

Runner 导出 `KAGGLE_SKILL_WORKSPACE`、`KAGGLE_SKILL_RUN_ID`、
`KAGGLE_SKILL_RUN_DIR`、`KAGGLE_SKILL_DATA_MANIFEST` 和
`KAGGLE_SKILL_SEED`。相对产物路径以独立 run 目录为根；
`--invariant OUTPUT=REFERENCE` 要求两者字节一致。

命令的工作目录固定为独立 `runs/<run_id>/`，因此源码入口应使用绝对路径；CLI 不提供
共享可写 `workdir`。本地运行会在执行前后核验原始数据和源码哈希，发现增删改即把
运行标为 `invalid`；需要操作系统级只读挂载时，应在外层使用容器或受限执行环境。

传入 `--container-image IMAGE_OR_DIGEST` 时，runner 会先解析镜像 ID，再以 Docker
`--network none --read-only` 运行，输入、源码、配置和数据 manifest 只读挂载，只有
`/workspace/run` 可写。容器内源码入口使用 `/workspace/source`，数据使用
`/workspace/input`；镜像必须提供 `/bin/sh`。镜像 ID 进入 checkpoint identity。

相同 `run_id` 只有带 `--resume` 且命令、代码、patch、配置、比赛画像、规则、数据、
环境、seed、父运行身份、不变量、manifest seal 和产物哈希全都一致时才会跳过。

## 指标与决策

`feedback` 记录完整 typed metric。本地指标要求已完成运行；榜单和最终指标还要求
`candidate_id`。`dryness` 必须传完整可比 lane。`gate` 读取 V2 JSON 报告，输出
`ready_for_human_submission`；之后仍需人工提交，榜单结果回来后再执行 `feedback`，
最后由人决定是否 `promote`。

门控字段模板见 `references/gate-report.example.json`。模板中的审批时间和哈希只是
占位符，必须替换为真实的人类审批与当前产物 SHA256。

## 迁移

```bash
python scripts/kaggle_ops.py migrate OLD_WORKSPACE NEW_WORKSPACE --mapping track_mapping.json
```

源目录只读，目标必须为空。映射文件只做通用正则到 track 的显式映射；无法可靠
映射的条目保持 `legacy-unmapped`，旧状态和完整 payload 不做语义猜测。

## 发布

`release build` 按包含和排除规则创建确定性 ZIP；`release verify` 检查安全成员、
重复成员、symlink、CRC、大小和哈希。`postmortem` 汇总运行结果、提交利用率、冠军
路线、实验族和成对 proxy/LB 证据。

## 退出码

- `0`：成功，或 gate 已准备好；
- `1`：结构、运行、验证或发布失败；
- `2`：lane 已枯竭，或 gate 未准备好；
- `130`：用户中断。

旧四个脚本只作为兼容包装器保留，并输出弃用提示。新流程调用 `kaggle_ops.py`。
