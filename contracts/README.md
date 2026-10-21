# 机器契约

`contracts/` 定义模块之间必须共同遵守的数据结构。

普通编码任务不得自行修改这些文件。发现契约无法满足任务时，应停止并报告：

- 当前契约位置；
- 具体冲突；
- 建议改动；
- 会受影响的模块和测试。

当前契约：

- `submission.schema.json`：Parser 输出和 Inbox 输入的 `SubmissionDraft`。
- `approved-package.schema.json`：人工批准后交给未来 Adapter/Writer 的不可变审核快照。
- `umalib-change-plan.schema.json`：`UmaLibChangePlan`，UmaLib Adapter 纯变更计划核心的输出。字段语义要点：
  - `input`：绑定证据来源（Inbox item_id、批准包哈希、目标数据库哈希、已确认 Schema 指纹、目标标签目录哈希 `{id,name,type}` 数组的稳定哈希；`usage_count` 不参与）。
  - `article`：字段名与实际 `article` 表列名一致，但只是计划数据；不预分配 `article.id`。
  - `tags.existing` 只含准备复用的现有标签元数据；`tags.create` 只含经人工明确类型选择的新标签。
  - `ready=true` 当且仅当 blockers 为空，且只表示 Adapter 层人工选择齐全。
  - `plan_sha256` 为排除自身后对规范 JSON 计算的 SHA-256。
  - 所有参与哈希的内部计划数据均为深度不可变；`to_dict()` 每次返回独立副本。
  - 带 blocker 的预览仍必须符合 Schema；格式错误的输入哈希和负上传时间使用契约安全占位，并由 diagnostics 明确阻断。

  该计划**不是可执行 SQL，不是 Writer**；即使 `ready=true` 也不代表 Writer 已获准执行或数据库已被写入。
- `umalib-writer-preview.schema.json`：`CandidateWritePreview`，候选 Writer 的确定性 dry-run 预览。`ready_to_write=true` 只表示没有 blockers，**不是写入授权**；实际写入还必须提供绑定 plan/preview/source 哈希且 `app_closed_confirmed=true` 的 `CandidateWriteApproval`。
- `umalib-writer-result.schema.json`：`CandidateWriteResult`，候选副本写入完成并通过回读/完整性复核的证据。它证明的是**候选副本**已写好，**不代表正式库已被替换**。
