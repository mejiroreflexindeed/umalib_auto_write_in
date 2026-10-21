# 20 · Operator 产品设计契约（TASK-014）

> 2026-09-08：本文是 TASK-014 设计基线，不是各项已实现证明。当前实现与文档存在差异，例如 launcher 已使用 `last_workspace_by_mode`，补查生命周期和维护互斥仍待修复。按 [TASK-015](../tasks/TASK-015-overnight-closeout.md) 收尾后同步实际协议；当前证据见 [Astra 复核](reports/ASTRA-REVIEW-2026-09-08.md)。

## 1. 持久文件模型（3.3.4 冻结）

所有 JSON 使用 `ensure_ascii=False, sort_keys=True, separators=(",", ":")`、UTF-8、
同目录临时文件 + flush/fsync + os.replace；严格拒绝未知顶层键与未知 schema version。
时间均为 UTC ISO（`+00:00`）；日期筛选另存 YYYY-MM-DD 与 UTC+08:00 下限。

- `workspaces/<id>/source-binding.json`：`{schema_version:"1", source_id, path,
  file_name, sha256, size, bound_at, schema_fingerprint_sha256}`。
- `workspaces/<id>/threads/index.json`：`{schema_version:"1", revision, active_tid,
  entries[]}`；entries 按 (created_at, tid) 稳定排序；active_tid 必须在 entries 中
  或为 null；修改走 expected revision CAS，冲突 409。
- `threads/<tid>/config.json`：revision/tid/canonical_url/name/enabled/start/
  start_confirmed/initialized/时间；start 三形状（now 全空、page 仅正整数 page、
  date 带 date+lower_bound_utc 且后端重算一致），timezone 恒为 UTC+08:00。
- `threads/<tid>/state.json` v2：`{schema_version:"2", tid, canonical_thread_url,
  frontier_page, observed_last_page, next_scan_page, known_floors, date_exclusions,
  baseline_created_at, last_successful_scan_at, last_result_summary, active_rescan}`。
  - frontier_page 为从第 1 页起连续完整提交的最大页（可为 0）；
  - next_scan_page = frontier_page+1，追上活跃尾页后允许等于尾页（重查例外）；
  - v1 适配器只读映射 last_page → frontier/observed/next，不改原文件；
  - date_exclusions[pid] 存 `{pid, content_sha256, posted_at_raw, posted_at_utc,
    last_seen_page, excluded_lower_bound_utc}`；仅已识别 [收录] 且可靠时间早于
    下限的楼层进入；更早日期补查入 Inbox 后删除对应 exclusion；
  - 时间无法解析的 [收录] 不进 exclusion，按候选导入并附 posted_at_unresolved。
- `threads/<tid>/runs/<uuid>.json`：kind ∈ login/initialize_preview/scan/rescan；
  status ∈ queued/running/awaiting_login/cancelling/succeeded/partial/cancelled/
  failed/interrupted；request 只存业务参数与 expected SHA；result 固定计数键
  `pages_scanned,floors_seen,discussion_count,triggered_count,imported_count,
  incomplete_count,updated_count,date_excluded_count,date_unresolved_count,
  remaining_pages,state_sha256`；error `{code,message,retryable}`。
- operator root `launcher.json`：`{schema_version:"1", last_workspace_id,
  updated_at}`；只记最后工作区 UUID。

## 2. 固定默认值（3.3.3）

默认产品数据根 `operator-user`；默认源库 main-copy.db（存在且校验通过才显示）；
默认帖子输入框预填 30099686（不自动添加/访问）；默认起点 now；默认扫描批量 10
正文页；单作业硬导航上限 25 次（首页/重试都计数）；导航最小间隔 3.0s；登录等待
300s；每页 Inbox 50；同时网络作业 1；Web 仅 127.0.0.1 端口 0；楼层时间 UTC+08:00。

## 3. 时间解析（3.3.5）

`operator.state_v2.parse_nga_posted_at(raw)`：HTML-unescape + strip 后只接受
`%Y-%m-%d %H:%M:%S` / `%Y-%m-%d %H:%M` / `%Y-%m-%d` 三种完整匹配，按 UTC+08:00
转 UTC；其他格式返回 None（保守保留，不模糊解析、不用系统 locale）。

## 4. HTTP 包装（3.3.6）

成功 `{"data":...}`，错误 `{"error":{"code","message","retryable"}}`；400 形状/
类型/未知键、404 受控实体、409 状态/CAS/忙碌/未确认、422 业务阻断、503 环境。
mutation 的 `confirmed` 必须 JSON bool true。前端按 code 显示，不解析 message。

页面四顶级 section 顺序：threads → inbox → delivery → settings。主按钮顺序固定：
添加帖子 → 三选一起点 → 确认扫描起点 → 登录 NGA → 检查新增投稿 → 继续下一批；
审核顺序：保存修改 → 批准稿件 → 生成预览 → 勾选确认 → 确认生成候选数据库。

## 5. 不引入

无 Electron/EXE、React/Vue、ORM、后台定时扫描、系统托盘、自动更新、云同步、
账号权限、Cookie 导入导出、验证码绕过、代理、全帖并发、附件下载、自动投稿/批准/
覆盖正式 DB、任意正则规则编辑器。唯一可选依赖 playwright==1.62.0；其余标准库。

## 6. 实际持久协议补充（TASK-014/015 落地）

- `launcher.json` 实际使用 `{schema_version:"1", last_workspace_id, updated_at}`，
  另有 `workspaces/<id>/source-binding.json` 记录已确认副本；
  `workspaces/<id>/previews/<preview_id>.json` 记录独立预览（含 operation_id、
  choices/工作稿指纹、计划/预览/源库 SHA、workspace_revision）。
- 补查进度写在 `state.json.active_rescan`（rescan_id/kind/from_page/to_page/
  lower_bound_utc/next_page/created_at），不触碰 `frontier_page/next_scan_page`。
- 扫描写入阶段（楼层导入 + frontier 提交 + state 原子写）计入共享
  `MaintenanceCoordinator` 活动 mutation；长网络等待不持有门闩。

