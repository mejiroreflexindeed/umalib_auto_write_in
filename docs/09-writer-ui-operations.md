# 09 · Writer UI 集成与操作说明

本文说明 TASK-009 的 Writer UI 集成：API/服务数据流、配置与路径安全、choices 语义、Preview/Approval 失效规则、并发锁、失败恢复、前端状态机，以及实际副本烟雾测试的最小披露结果。

## 1. API / 服务数据流

```text
CLI 固定 --umalib-db（只读源库） + --candidate-dir（输出目录）
        ↓
GET /api/writer/config    → 配置摘要（无绝对路径）
GET /api/writer/catalog   → load_target_tag_catalog()（只读标签目录）
        ↓
POST /api/items/{id}/writer/preview
        → ApprovedPackageService.build() → load_target_tag_catalog()
        → build_umalib_change_plan() → preview_candidate_write()
        → {plan, preview}（blocked 也返回 200 + diagnostics）
        ↓
POST /api/items/{id}/writer/write
        → 共享锁内重建 package/catalog/plan/preview
        → CandidateWriteApproval（绑定 UI 期望哈希）
        → write_candidate_database() → 成功后 transition(exported)
        → {result, item, exported}
```

服务层（`web/writer_ui.py`）只编排现有公共 API，不复制 Writer SQL、哈希算法或数据库校验代码，不从 Adapter/Writer 导入任何私有下划线函数。

## 2. 配置与路径安全

- `--umalib-db` 与 `--candidate-dir` 必须同时提供或同时省略；只给一个时 CLI 拒绝启动（退出码 2）。
- 源库：必须存在、普通文件、非符号链接、与 Inbox 不同、能通过只读 catalog/inspector；启动预检不创建任何文件。
- 候选目录：必须已存在、是普通目录、非符号链接。
- 页面只提交候选**文件名**（单个 basename、`.db` 结尾、不含分隔符/盘符/NUL/遍历）；后端用固定目录组合路径再交给 Writer 复核。
- HTTP 请求永远无法改变源库或候选目录；JSON API/Preview/Result 不含绝对路径（路径只以 `path_sha256` 形式出现）。
- 候选已存在或有 sidecar 时只报告 blocker，绝不覆盖/删除/改名。

## 3. choices 语义

Preview/Write 请求 DTO 顶层为 `candidate_file_name` + `choices`；`choices` 为 `{upload_time_seconds, note, ordered_sources, tag_choices}`；Write 额外要求 `expected_plan_sha256`、`expected_preview_sha256`、`expected_source_database_sha256`、`app_closed_confirmed`。未知键、错误容器、bool 冒充整数、非法 type/ID/hash 一律 `400 writer_invalid_request`。

- `upload_time_seconds`：非 bool 的 `0..2147483647` 整数；前端用 `datetime-local` 人工填写并显示转换后的 Unix 秒，不自动填当前时间。
- `note`：字符串或 `null`。
- `ordered_sources`：人工顺序的 URL 数组；前端从批准包五类可用来源中逐条加入并可上移/下移/移除，不做自动排序。
- `tag_choices`：每项 `requested_kind`（tag/series）、`requested_value`、`existing_tag_id`（正整数）与 `new_tag_type`（0..4）二选一或都为空。唯一命中可留空自动复用；多重同名必须显式选 ID；零匹配必须选新类型或显式现有 ID。最终以 Adapter diagnostics 为准，前端判断不被信任。

## 4. Preview / Approval 失效规则

- 后端每次 Preview/Write 都从**当前 Inbox 与当前源库**完整重建 package/catalog/plan/preview；UI 传来的哈希只是期望值，不能替代后端重算。
- 选择其他 Inbox 项、编辑审核字段、修改 Writer 字段、重载 catalog、状态变化都会在前端清空旧 Plan/Preview/Approval/Result。
- Write 在服务共享锁内重建并再次交给 Writer 核对：任一输入/记录/源库/目录/候选状态变化，写入前拒绝（`writer_approval_mismatch` / `writer_preview_not_ready` / `writer_candidate_exists`）。
- `app_closed_confirmed` 必须为 JSON `true`（拒绝 `1`、字符串、缺失）；关闭确认只能在成功 Preview 之后由用户手动勾选，重新 Preview 自动取消。
- 任何 `ready=true` 都不能绕过 `CandidateWriteApproval`。

## 5. Writer 与 transition 锁

- 一个进程内共享 `threading.RLock`：普通状态转换 API 与 Writer 写入共用同一把锁。
- Write 在锁内重新读取 item 并确认仍为 `approved`，然后执行写入与 `exported` 转换——同一进程两次并发 write 只有一次成功，另一次看到 `exported` 或候选已存在而拒绝。
- Writer 成功以前绝不改变 Inbox 状态；成功后以固定审计文本 `候选写入成功: <candidate文件名> result_sha256=<hash>` 转换到 `exported`。

## 6. post-write transition 失败恢复

极少数情况：候选已成功发布并验证，但 Inbox 转换随后失败。

- 绝不删除已成功验证的候选。
- API 返回 `500 writer_post_write_state_failed`，响应只含候选文件名与 `result_sha256` 的恢复说明（无正文/绝对路径/SQL）。
- 人工处理：立即记录错误中显示的候选文件名与 `result_sha256`，确认候选文件存在并保留原样；不要重试 Write、不要删除或移动候选，也不要尝试通过普通状态按钮设置 `exported`。先用大书库人工验收候选，再由负责人核对候选哈希与 Inbox 状态并执行专门恢复。当前版本尚未提供这个低频恢复入口。

## 7. Windows 7 风格前端状态机

- 布局：Aero 渐变标题栏 → 命令/导入区 → Inbox 列表 → 审核详情（含批准包区）→ “生成大书库候选”分组（仅 approved 时显示）→ 底部状态栏（仅本机 / Writer 配置 / 未保存修改 / 当前选中）。
- 样式：Segoe UI/Microsoft YaHei UI 字体、浅灰蓝桌面、冷灰边框小圆角面板、灰白渐变按钮（主操作浅蓝）、表头浅灰渐变、选中行浅蓝高亮、focus 蓝外框、disabled 变灰；blocker 红/warning 琥珀/success 绿且始终带文字与 Unicode 图标；1024×768 可完成核心操作，窄屏单列退化；无外部字体/图片/CDN。
- 状态机要点：dirty 审核字段禁止 Preview/Write（沿用保存/放弃保护）；Preview 变化清除 approval 与确认勾选；写入中禁用控件防双击；异步返回带请求版本防止旧响应覆盖新记录；已 exported 只显示历史与结果、不可再写；所有不可信字符串只经 `textContent`/`value`/安全 DOM 写入。
- 结果区明确提示“候选已生成；正式库未被替换，请用大书库人工打开验收”。

## 8. 实际副本 HTTP 烟雾测试（最小披露）

对实际副本（SHA `4e152ea3…a54dc`、Schema 指纹 `d7b19669…79aa8`、catalog `7759f03c…89fd`）通过 HTTP API 完成一次端到端闭环（合成数据）：

- Writer 配置 API 正常；catalog 405 个标签，复用唯一名现有标签 id=1，新建合成标签 id=451（type=0）；
- Preview ready（plan/preview 哈希与 catalog 哈希均与后端重算一致）；
- Write 成功：article 6983→6984（新 id=7015）、tag 405→406、tagged 17471→17473（+1/+1/+2）；
- Inbox 状态 exported，历史 reason 含候选文件名与 result_sha256（长度 113，不含绝对路径/正文/SQL）；
- 候选 integrity ok、外键违规 0、重复关联 0、inspector blocker 0；独立只读 SQL 精确回读合成 article/新 tag/tagged；
- 源库 SHA/大小/mtime/目录前后完全一致；项目与 `umalib-test-data` 目录无新增文件；临时 Inbox/候选随 TemporaryDirectory 自动删除。

以上数字只证明 Writer UI 闭环可用，不构成对生产库写入的批准。

Codex 主线验收又独立执行了一次同边界 HTTP 闭环：真实源库 SHA/大小/mtime/目录保持不变，候选增量仍为 `article/tag/tagged = +1/+1/+2`，完整性、外键、重复关联与 inspector 检查全部通过，临时目录已删除。

## 9. 当前限制与下一步人工验收

- 仍只支持向候选副本新增单篇文章；不编辑已有文章、不批量、不替换正式库。
- 大书库关闭状态依赖人工确认（程序无法可靠证明 Electron 已关闭）。
- 没有一键启动器/安装包/NGA Watcher；纯文本 Quill 不保留尚未进入批准包的颜色与图片。
- Codex 已在真实本地浏览器中检查约 `1280×720` 与 `1024×768` 两种尺寸，并实测导入、批准、标签目录、来源排序、Preview、关闭确认和成功 Result；后续仍可根据录入人员实际使用反馈微调密度与文案。
- 下一步人工验收：用大书库打开一次生成的候选文件，确认文章、标签、关联与正文显示正确。
