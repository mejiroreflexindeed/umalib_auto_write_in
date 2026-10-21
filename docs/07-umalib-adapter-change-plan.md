# 07 · UmaLib Adapter 纯变更计划核心（ChangePlan）说明

本文说明 TASK-007 实现的 `UmaLibChangePlan`：它在完整数据流中的位置、`ready=true` 的准确含义，以及它**不是**什么。

## 1. ChangePlan 在完整数据流中的位置

```text
NGA 楼层 → Parser → Inbox → 人工审核 → ApprovedSubmissionPackage（不可变快照）
                                           │
                  + 人工 AdapterChoices（uploadTime / note / 来源顺序 / 标签选择）
                  + TargetTagCatalog（目标数据库标签元数据快照）
                                           ↓
                             UmaLibChangePlan（纯内存、可预览、可哈希）
                                           ↓
                      Writer Preview → 人工批准 → 候选副本内事务执行与回读
```

ChangePlan 只依赖三个输入，全部由调用方显式传入：批准包、人工选择、目标标签目录。它自身**不打开任何数据库、不生成 SQL、不读写文件、不看当前时间、不发网络请求**。

## 2. `ready=true` 的准确含义

- `ready` 当且仅当 `diagnostics.blockers` 为空。
- 它只表示 **Adapter 层的人工选择已经齐全、输入绑定核验通过**，可以交给 Writer 预览和复核。
- 它**不代表** Writer 已获准执行、数据库已被写入、或写入一定成功。

## 3. 为什么它不是 SQL、不是 Writer、不会打开数据库

- 计划输出是纯 JSON 数据对象（契约：`contracts/umalib-change-plan.schema.json`），没有任何 SQL 文本，没有预分配的 `article.id`，没有执行时间戳。
- `build_umalib_change_plan` 与 `render_plain_text_quill_html` 都是纯函数/值对象；重复调用得到逐字节相同的 `to_dict()` 和 `plan_sha256`。
- `UmaLibChangePlan` 内部所有参与哈希的映射和数组都会深度冻结；调用方只能修改 `to_dict()` 返回的新副本，不能在哈希不变的情况下篡改计划本体。
- 目标数据库只以 SHA-256 形式绑定在 `input.target_database_sha256` 中；Adapter 从不接触该文件，Writer 必须对候选副本重新核对。
- 计划中 `article.id` 刻意缺失——该 ID 只能由 Writer 在候选副本事务内取得。

## 4. 人工必须选择什么

| 选择项 | 对应字段 | 缺失/非法时的行为 |
|---|---|---|
| 上传时间（非负整数秒） | `AdapterChoices.upload_time_seconds` | 负数产生 `invalid_upload_time` blocker；不读取系统当前时间 |
| 备注 | `AdapterChoices.note` | 可空；`None` 保持 JSON `null`，不自动发明摘要 |
| 来源顺序 | `AdapterChoices.ordered_sources` | 批准包有来源但未选择 → `source_selection_required` blocker |
| 歧义/新标签 | `AdapterChoices.tag_choices` | 见第 5 节 |

来源压平规则：只从批准包的五个结构化来源（nga、original_series、original、translation_series、translation）构成可选集合；`ordered_sources` 每个条目必须逐字命中可选集合，并且必须是带有效主机名、端口和无用户名/密码的绝对 HTTP(S) URL；按人工顺序用一个 ASCII 空格连接；重复选择去重并保留第一次位置（产生 warning）；选择不存在的 URL、非 HTTP(S) URL 或含 ASCII 空白的 token 产生 blocker；批准包完全没有来源时允许空字符串。**不自动决定原作/译作优先级，不修改 URL。**

## 5. 标签与系列的三种行为

请求项来自批准包的 `metadata.tags`（kind=tag）与非空 `metadata.series`（kind=series）。

1. **唯一精确匹配**：目标目录中名称唯一命中 → 自动加入 `tags.existing`。
2. **多重匹配**：同名命中多个 → `ambiguous_tag_name` / `ambiguous_series_name` blocker，message 只列出候选 ID、类型与 usage_count；人工通过 `existing_tag_id` 显式指定其中一个即可解除。
3. **零匹配**：产生 `tag_resolution_required` / `series_resolution_required` blocker；只有人工显式给出 `new_tag_type`（仅允许 0..4）才进入 `tags.create`。若目录已存在同名标签（无论一个还是多个），禁止再计划新建（`new_tag_name_exists`）。

系列不默认猜类型 2 或 3，普通标签也不默认猜类型。显式把请求值映射到不同名称的现有 ID 会产生 `tag_alias_mapping` warning，供人工复核。

### 为什么必须人工选 ID：真实同名标签形态

实际数据库副本证据（`docs/06-production-db-evidence.md`）已确认存在同名标签：同一名称、同一类型、不同 ID、不同关联数（如名称相同而 ID 分别为 428 与 429、关联数分别为 2 与 0）。按名称盲选会把新关联挂到错误的 ID 上。因此多重命中一律阻断并要求人工指定 ID。本实现测试中使用**合成**同名标签复现该形态，不复制任何真实文章内容。

## 6. 纯文本到 Quill HTML 的确切转换

`render_plain_text_quill_html` 只支持当前批准包能够证明存在的纯文本：

1. 规范化 CRLF 和单独 CR 为 LF；
2. 转义 `&`、`<`、`>`，绝不把输入中的 HTML 当标记解释；
3. 每个非空逻辑行输出一个 `<p>...</p>`；
4. 空行输出 `<p><br></p>`；
5. 保留普通空格、`[关键词]注释` 文字和末尾换行所代表的空行；
6. 空正文、NUL 与不允许的 C0 控制字符 → blocker（直接调用渲染器抛 `AdapterContentError`）；tab 与 LF 允许；
7. 不插入图片、链接、颜色、class、style 或未被输入证明的格式。

输入（末尾带一个换行）：

```text
第一行

[关键词]注释
```

实际运行得到的输出（已与本实现核对）：

```html
<p>第一行</p><p><br></p><p>[关键词]注释</p><p><br></p>
```

## 7. 为什么现在不能保留 NGA 颜色/图片

实际副本证据显示正文是 Quill HTML：6983 篇中 472 篇含内联颜色、1692 篇含图片、840 篇含 `ql-*` class。但当前 `ApprovedSubmissionPackage` 只携带 `body_text` 纯文本，不携带 NGA 原始 HTML、BBCode 或结构化富文本。因此 V1 渲染器**只能**输出转义后的最小 `p/br` 结构，不能凭空恢复已丢失的格式。

要保留这些格式，必须先在**上游采集/交接契约**层扩展（在批准包中携带经允许列表校验的结构化富文本），再用真实 NGA 格式样本建立转换测试。这不是本任务范围，也不能用"通用 HTML sanitizer"冒充。

## 8. `plan_sha256` 的用途与不代表什么

- 计算方式：排除 `plan_sha256` 自身后，对规范 JSON（UTF-8、`sort_keys=True`、紧凑分隔符、`ensure_ascii=False`）计算 SHA-256。
- 用途：同一计划可审计、可跨进程比对；`input` 内还绑定 `package_sha256`（沿用批准包哈希，Adapter 会按相同算法复算核对）、`target_database_sha256`、已确认 Schema 指纹与 `target_tag_catalog_sha256`（按 ID 稳定排序的 `{id,name,type}` 数组哈希，`usage_count` 不参与）。
- 不代表：哈希匹配 ≠ 内容已写入；它只是计划数据的指纹。

实际运行示例（合成 fixtures，ready=true）：`plan_sha256 = ad0a7f72223224709a6a3394948d53e80b462cd0d49e25a666734f695eb62d3b`，`article.source = "https://example.com/original-1 https://example.com/translation-1 https://example.com/os-1 https://example.com/ts-1"`，`tags.existing` 为 id 1、2、3 三条合成标签，`tags.create` 为空。

## 9. blocker 与 warning

- `blocker`：阻断计划进入 ready。常见：`approved_package_hash_mismatch`、`target_database_sha256_malformed`、`target_schema_fingerprint_mismatch`、`invalid_body_text`、`source_selection_required`、`source_not_available`、`tag_resolution_required`、`series_resolution_required`、`ambiguous_tag_name`、`new_tag_name_exists`、`catalog_duplicate_id` 等。
- `warning`：可审查但非阻断。常见：`duplicate_source_selection`、`tag_alias_mapping`、`duplicate_target_resolution`。
- diagnostics 按稳定顺序输出（同级别内按 code、field 排序）；机器判断只看 `code`，`message` 只面向人类。
- 为保证带 blocker 的预览仍符合机器契约，格式错误的输入哈希在计划中使用 64 位全零占位，负的上传时间使用 `0` 占位；原错误通过稳定 diagnostic code 表达，且 `ready` 必为 false。这些占位值不能被当成有效绑定。

## 10. Writer 已实现后的当前边界

- TASK-008 已实现候选副本生成、单事务写入、写后回读、完整性校验与失败清理；只写新候选文件，不原地修改源库。
- 已能按计划在候选库内创建新标签、复用现有标签、插入文章并建立关联。
- `core_contract_match=true` 与 `ready=true` 仍不是执行许可；真正写入还必须通过独立的 `CandidateWriteApproval` 哈希绑定。
- TASK-009 负责把 Writer Preview、人工批准和候选结果接入本地 UI，但不得改变上述授权边界。
- NGA 富文本采集与结构化交接契约扩展仍未实现。

本工具只生成经过验证的新候选数据库副本，不覆盖源库或正式库。
