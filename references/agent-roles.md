# 可选角色

角色用于让决策可审计；一个 Codex 实例可以承担多个角色，但交接边界仍应明确。

## Orchestrator

读取账本、生成状态、冠军表和 flags，选择一个可审计决策单元，并重新物化派生视图。
不手改 `STATE.md`，不静默提交。

## Researcher

检索相似赛题、writeup、Notebook、论文和讨论，输出有证据与可证伪条件的 idea，
不输出模糊灵感。

## Experimenter

在隔离目录实现已声明假设，记录配置、命令、输入、输出、日志和资源。多个动作只有
共享同一验收标准时才能属于一个决策单元。

## Reviewer

在昂贵运行前后质疑验证独立性、泄漏、指标错位、过期产物和实验是否真正检验假设。
领域检查应通过注册 hook 完成。

## Gatekeeper

运行提交门，验证来源并准备人工审批请求。缺少有效审批时默认拒绝。

## Compliance Auditor

检查比赛规则、外部数据、网络、账户与团队限制、模型许可证、最大业务日期和自动化
是否可能违规。

## 交接字段

- objective
- input files
- allowed actions
- forbidden actions
- expected output
- acceptance criteria
