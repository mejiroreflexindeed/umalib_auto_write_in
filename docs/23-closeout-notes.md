# 23 · TASK-015 收尾修复说明（R01—R08）

本轮修复 8 个阻断缺陷；全部先写正式红灯（`tests/test_operator_closeout.py` 9 项 +
`tests/test_operator_ui_flow.py` 1 项真实浏览器），修复后全绿。基线 624 → 最终 634。

## R01 补查页范围破坏日常游标

- 现象：`scan?kind=pages` 后 state 出现 `frontier=2,next_scan_page=1`，读取直接
  抛 `next_scan_page 与 frontier_page 关系非法`。
- 根因：`scan_service.run_batch_scan` 无论是否补查都重算 `next_scan_page`。
- 修复：`rescan=True` 时 `next_scan_page/frontier_page/last_successful_scan_at`
  一律沿用原 state；补查进度改写入 `active_rescan`（rescan_id/kind/范围/日期下限/
  next_page），完成时清空。
- 测试：`R01PageRescanCursorTests`。

## R02 较早日期补查从尾页开始

- 现象：`kind=date&lower_bound_date=2026-07-01` 仍只扫尾页，七月稿补不回来。
- 根因：`_exec_rescan` 未给 date 模式指定起点，落回 `state.next_scan_page`。
- 修复：date 补查固定 `from_page=1`、`to_page=observed_last_page`；若存在同类型
  同下限的 `active_rescan` 则从 `next_page` 续做；`run_batch_scan` 对已识别
  `[收录]` 且时间晚于新下限的楼层 `exclusions.pop` 并导入。
- 测试：`R02EarlierDateRescanTests`。

## R03 403/验证码被报告为登录成功

- 现象：HTTP 403 也 `succeeded` + `login_verified=true`。
- 根因：`_exec_login` 只检查 `manual_login_required`，忽略其它阻断诊断。
- 修复：登录读取后统一检查 `thread_forbidden/thread_not_found/captcha_or_challenge/
  manual_login_required/unexpected_redirect/response_not_captured/
  floor_structure_unrecognized/encoding_uncertain` 与 tid 一致性；等待超时单独
  `failed/login_timeout`（不冒充成功，也不等同取消）。
- 测试：`R03LoginTruthTests`（403 与验证码两例）。

## R04 跨帖预览确认写入错误 tid

- 现象：用 A 帖的 `run_id/preview_sha256` 确认 B 帖，B 路径写入 tid=A 的 state。
- 根因：`initialize_confirm` 只校验预览 hash，未绑定 workspace/tid。
- 修复：预览记录写入 `workspace_id`，确认时校验 `preview.tid == tid` 与
  workspace 一致；并改为先写 state 再 CAS config，CAS 失败回滚 state，避免
  "已初始化但无 state"的半套配置。
- 测试：`R04CrossThreadConfirmTests`。

## R05 维护期间仍可写入

- 现象：备份维护中 HTTP `POST /api/operator/import` 仍 200 落盘。
- 根因：`manual_import/review/transition` 未经过共享 `MaintenanceCoordinator`。
- 修复：`OperatorService.mutation_scope()` 统一门闩（忙碌转 409，`__enter__` 也
  覆盖）；`manual_import`、operator_ui 的 `review/transition` 全部走门闩；扫描的
  写入阶段（楼层导入 + frontier 提交 + state 原子写）用 `write_gate` 计入活动
  mutation，长网络等待不持有门闩。
- 测试：`R05MaintenanceImportTests`。

## R06 切换稿件丢失未保存修改

- 现象：编辑稿一后点稿二，输入被静默替换。
- 修复：`dirtyGuard` 覆盖切稿/切页签/切帖/筛选/翻页；`beforeunload` 保留；
  `openReview` 增加请求序号，迟到响应不得覆盖当前稿件。
- 测试：`tests/test_operator_ui_flow.py`（真实浏览器：留在当前页保留输入 /
  确认放弃后切换）。

## R07 字段块后直接 UBB 首段被吞成 unknown_fields

- 现象：`[标签] …` 后直接 `[b]首段正文[/b]`，正文为空且该行进入 unknown_fields。
- 根因：Parser 元数据块把任何 `[名称]值` 都当字段。
- 修复：复用 `formatting.models.is_format_tag`，已知 UBB 起始标记（b/i/u/s/
  color=…）视为正文起点；未知 `[字段]` 仍诊断保留。
- 测试：`R07ParserBodyStartTests`（含未知字段仍诊断的对照）。

## R08 补查 UI/中文状态/翻页缺失

- 修复：`operator.html/js/css` 增加「补查历史」区（页范围/较早日期互斥、代价提示、
  确认、开始/继续/取消）；列表状态显示中文；增加「上一页」；布局按两档（列表
  300—360px、编辑区铺满、来源折叠显示已填数、操作条、底部状态条）并全页截图。
- 另修复两处由 UI 旅程暴露的真实缺陷：`fields.tags` 字符串调用 `.map()` 导致
  标签决定区永不渲染；切帖后旧提示/旧预览未清空导致 `preview_stale`；
  `writer_write` 返回 `InboxItem` 不可 JSON 序列化导致 HTTP 写入 500。
- 测试：`tests/test_operator_ui_flow.py` + `operator_acceptance --phase ui`。

## 证据

- 证据根：`D:\codex\13\umalib-private-state\operator-015-85221580`
  （`preflight/`、`acceptance-all/`、`bundle/`）。
- 全量回归：`Ran 634 tests OK`（exit 0，0 skip）。
- `tools/operator_acceptance.py --phase all`：launch/threads/scan/review/writer/
  recovery/real-copy/ui 八阶段全 PASS。
- 保护文件 hash 全部未变；无遗留进程。
