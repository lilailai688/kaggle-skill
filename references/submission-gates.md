# 提交门控

Skill 只准备和验证候选，永远不执行提交。

## 共同要求

所有提交意图都必须满足：

- 当前产物字节与声明 SHA256 一致；
- 产物来自一个 `completed` 运行并已进入内容寻址存储；
- `format`、`compliance`、`artifact_integrity`、`leakage`、
  `temporal_integrity`、`training_inference_boundary`、`prohibited_shortcuts`
  和 `artifact_freshness` 门通过；不适用项也要以带证据的 `passed: true` 明确说明；
- 审批绑定 candidate ID、产物哈希、意图、审批人、请求 ID、时间和失效时间；
- 审批仍有效，且审批人不是 Codex、agent、system 或其他自我审批身份；
- 对应 phase 仍有提交额度。

`competition.json.rules` 中的外部数据、网络和模型许可证策略不能继续为 `unknown`；
报告里手写 `compliance: true` 不能替代已完成的规则清单。

配置了 ZIP 契约时，检查安全路径、CRC 和精确成员。只有 `ensemble: true` 才要求
`diversity` 门。

## Probe

`probe` 可以不通过本地指标门，但必须声明：

- `information_hypothesis`：这次额度购买什么信息；
- `controlled_difference`：相对已知候选只改变了什么；
- `rollback_candidate_id`：结果不理想时回到哪里；
- 剩余提交额度。

Probe 不能绕过格式、来源、合规、完整性或审批。

## Candidate

`candidate` 必须额外通过 `metric` 门。证据要说明它是正式 CV 还是 proxy，并与冠军
使用相同协议。候选 ID 一旦建立，不能重新绑定到不同运行、产物、track、phase、
数据快照或意图。

## Final

`final` 还必须通过 `cold_start`、`release` 和 `final_deliverable` 门。核验环境、输入
清单、确定性输出、规则快照、依赖、发布成员、排除项和回滚包，再请求人工批准。
报告必须以 `release_manifest` 绑定工作区 `release/` 内已 seal 的 manifest；ZIP 哈希
和依赖来源会再次核验。最终候选的运行必须来自已解析镜像 ID 的容器执行，且该
`run_id` 必须出现在 release runtime provenance 中。

## 反馈与晋升

榜单反馈绑定已准备候选、运行和候选产物哈希，记录来源、split、scope、
`comparable_group`、相对基线增量与有限归因范围。`feedback` 永远不会自动晋升；
`promote` 会重新检查审批有效期和产物哈希，并把旧冠军保存为 rollback。
