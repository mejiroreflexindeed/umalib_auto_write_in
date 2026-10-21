# 11 · NGA Watcher 核心与 Inbox 自动入队（TASK-011）

## 1. Watcher 与 TASK-010 / Parser / Inbox 的数据流

```text
现有 TASK-010 快照（page-173）或未来在线页面
        ↓ baseline-from-snapshot（离线） / scan（未来在线）
Watcher 状态（nga-watcher-state.json，v1）
        ↓ 楼层 diff（新 PID / 内容变化）
仅含独立行 [收录] 的楼层
        ↓ parse_submission(content_text, origin=楼层来源)
        ↓ adapt：raw_text 替换为该楼层原始 UBB 片段；canonical URL 追加到 sources.nga
InboxRepository.import_draft(draft)（现有公共 API，快照幂等）
        ↓ 全部成功 → 原子推进 state
```

Watcher 只复用现有 Parser 与 Inbox 的公开 API，不复制 SQL、不修改 Schema、
不触碰大书库；普通讨论只记 seen，不进入 Inbox。

## 2. 为什么 state 与 Inbox 分离，不迁移 Inbox Schema

- Inbox 是人工审核工作流的数据源，Schema 已冻结并验收（user_version=1）；
- Watcher 状态只描述“看过什么、入过什么”，是审核侧的游标，不是业务数据；
- 两者不是跨文件事务。备份/恢复应成对处理；单独回滚其中之一可能导致绑定不一致，
  必须人工核对，不能认为游标与 Inbox 自动保持一致。

## 3. 命令

```powershell
# 离线首次基线（只读快照，不联网、不 import Playwright、不需要 Inbox）
python -B -m umalib_ingest.watcher baseline-from-snapshot `
  --state-file "D:\codex\13\umalib-private-state\nga-watcher-state.json" `
  --snapshot-dir "D:\codex\13\umalib-private-state\nga-snapshots"

# 只读状态摘要（不联网、不打开 Inbox）
python -B -m umalib_ingest.watcher status --state-file <state>

# 扫描（在线能力；正式扫描必须显式绑定 Inbox）
python -B -m umalib_ingest.watcher scan --state-file <state> --inbox-db <inbox.db> `
  --profile-dir <独立登录 profile> --snapshot-dir <快照目录> `
  [--navigation-delay-seconds 3] [--max-scan-pages 12] [--dry-run] `
  [--login-wait-file <路径>] [--login-timeout-seconds 300]
```

等价 console 入口：`nga-watch`（pyproject `[project.scripts]`）。
`baseline-from-snapshot`、`status`、`--help` 均不 import Playwright；
`scan` 仅在需要启动浏览器会话时延迟导入，未安装可选依赖时报安全错误。

Windows 7 本地审核台也集成了 Watcher 面板（`--watcher-*` 启动参数），
操作说明见 `docs/12-watcher-win7-operations.md`。

## 4. “基线不导入 173 页历史”的语义

`baseline-from-snapshot` 只读最后页（page-173）的响应字节与 metadata，
验证 tid/status/last_page/结构/hash 后，把该页 20 个楼层的 **PID 与内容哈希**
写入 state；**不调用 Parser、不写 Inbox、不导入任何历史楼层**。
基线的意思是“从现在开始关注新内容”，不是回填。

## 5. 页范围从上次 last_page 开始的原因

首次基线记录了最后页的全部 PID。下次扫描必须**重读该页**，才能捕获
“原最后页继续新增楼层后才翻页”的情况；若从 last_page+1 开始会漏掉新楼层。
`start_page = state.last_page`，`end_page = 当前 last_page`，两端包含；
默认单次最多 12 页、硬上限 25 页，超出报 `scan_range_too_large` 且不推进；
当前页回退报 `thread_page_regressed` 且不推进；第一页若在计划内则复用已读结果，
计划内每页恰好读取一次、按页码升序。

## 6. `[收录]` 才入 Inbox；普通讨论只记 seen

- 楼层差异按 PID 与内容 SHA（对原始 UBB 片段的 UTF-8 字节）计算；
- 新 PID 无独立行 `[收录]` → 只写 `trigger_found=false`，不调 Inbox；
- 新 PID 有 `[收录]` → 构造标准 `SubmissionOrigin` 后 import；
- 已知 PID 内容未变 → `unchanged`，不重复解析/导入，仅更新 last_seen_page；
- 普通讨论编辑后新增 `[收录]` → 按新投稿导入；
- 已入队楼层再编辑 → 不覆盖/不新建，保留原 imported hash，置
  `changed_after_import=true`，结果列入人工复查，且后续扫描持续显示。

## 7. candidate / incomplete / changed_after_import

- 完整 `[收录]` 投稿进入 `candidate`；缺标题/作者/标签/正文自然进入 `incomplete`；
- Watcher 不自行改变状态为 suspected_duplicate/approved/ignored/exported；
- `changed_after_import` 只提示人工复查，本任务不提供“确认已处理”命令。

## 8. 原始 UBB、规范文本和 Inbox raw_text/body_text

- `parse_submission` 的输入是楼层**规范文本**（UBB 已知标签已剥离，
  未知方括号注释如 `[术语]注释` 保留）；
- import 前仅把 `draft.content.raw_text` 替换为该楼层**原始 UBB 片段**，
  使颜色、链接、表情与注释可追溯；不改 Parser 的 metadata/body_text/diagnostics；
- canonical 楼层 URL（`https://bbs.nga.cn/read.php?tid=<tid>&pid=<pid>`，
  不含 `&to=1`）同时追加到 `metadata.sources.nga`。

## 9. dry-run、失败不推进和崩溃恢复

- `--dry-run` 仍读取页面（可保留快照），但不写 Inbox、不写 state；
- 任一页读取失败、PID 跨页 hash 不一致、范围超限或首次 import 前 state 变化 →
  保留已生成快照证据，但 state 与 Inbox 不推进；
- 一旦已有 import 提交或旧条目复用，之后取消、state 变化或写 state 失败返回
  `watcher_partial_import`，明确提示已处理数量与原因；保留 Inbox 条目、state 未推进。
  不回滚已提交条目，不把部分写入误报成完全取消。重新扫描恢复后人工核对；
- 正式 scan 必须绑定 Inbox：`--inbox-db` 为必填参数；缺失时在创建 session、
  访问网络、创建 Inbox 或修改 state 之前 fail closed（`watcher_inbox_required`）；
- 登录失效时停在官方 NGA 页面等待人工确认，确认后只重试第一页一次；仍失败
  返回稳定 `login_required`，不进行第三次请求；等待超时返回 `login_not_confirmed`
  （可配置有限超时，默认 300 秒）；
- 所有顶层帖子导航统一限速：第一页元数据读取后到计划页、以及计划页之间
  均至少间隔 3 秒（测试用注入 sleep/clock 断言调用顺序）；
- profile/snapshot/state 路径在配置阶段即严格校验（拒绝日常 Edge User Data、
  项目目录、相对路径、根路径、symlink 与 profile=snapshot），在启动 Edge 前 fail closed；
- 崩溃窗口：Inbox 已导入、state 未保存 → 重试时比较不可变原稿的完整
  `content.raw_text` 与当前楼层原始片段，相同才复用同一 item 并补写 state。
  标题、作者、标签、来源或正文任一处变化都不静默绑定；无法安全绑定 →
  报告 `state_import_recovery_ambiguous`，由人工处理；
- 多个触发楼层按“页码 + 页面/楼层顺序”导入，不以 PID 字符串排序代替楼层顺序；
- import 前与 import 后各校验一次磁盘 state SHA，任一不一致即中止且不推进；
- state 写入：同目录临时文件 + flush/fsync + `os.replace`，失败无半文件；
- 运行期独占 `.lock`，防止两个 scan 并发，`finally` 释放。

## 9b. TASK-011R2+012A 最终收敛修复

Codex 行为级验收复现的 4 个缺陷已修复（详见 `docs/13-watcher-release-acceptance.md`）：

- **F1 官方登录重定向**：HTTPS + 官方 host + 明确登录路径精确识别为
  `manual_login_required`（无正文诊断），在 `validate_final_url()` 之前判定；
  非登录路径/外部 host/HTTP/错误 tid 仍在写正文前 `unexpected_redirect`；
- **F2 统一导航限速**：登录确认后的重试也经过导航门，第二次起每次顶层导航前
  sleep `navigation_delay_seconds`（≥3.0）；登录等待时长不替代导航限速；
- **F3 expected SHA 锁内绑定**：`expected_state_sha256` 由 Web 传入 `run_scan`
  边界，在取得 state lock 后只读取一次文件字节，同一份字节同时解析和计算 SHA；
  不一致在 session/网络/Inbox/state
  副作用之前返回 `watcher_state_stale`；import 前/后检查保留；
- **F4 shutdown 语义分离**：登录确认与服务取消不再共用 Event；shutdown 使等待
  callback 抛 `watcher_shutdown`，不重试、不返回成功、不写 Inbox/state；
  `run_scan` finally 关闭本次 session 恰好一次；`web.app.run()` 退出在
  `server_close()` 前通知 service 取消；前端显示"已取消：…"。

2026-09-02 补充：启动/关闭由同一生命周期锁协调，关闭标志不可被新扫描复位。
最后一次 fetch 后、diff 后、每次 import 前以及 state 写入前均检查取消。
取消是协作式的：已开始的导航/单条事务会返回后再处理；若已入队则按上文部分提交处理。
无法解码、中文验证页仅保留无正文诊断，不保存正常正文快照。

## 10. state / profile / snapshot 各包含什么及敏感性

- `nga-watcher-state.json`：tid、last_page、`pid → {content_sha256, last_seen_page,
  trigger_found, inbox_item_id, imported_content_sha256, changed_after_import}`、
  基线与上次扫描时间、结果摘要；**不含**正文、作者、标题、Cookie、header、绝对 profile 内容；
- `nga-profile`：独立登录会话，等价账号凭据，由用户保管，删除即退出登录；
- `nga-snapshots`：目标页响应字节/解码文本/meta 与 manifest，供追溯。

## 11. 在线运行状态

离线基线（page-173，20 楼层）已建立；核心与 Windows 7 操作台已完成并通过
离线测试。首次实际在线 scan 需在下一次人工登录态测试中执行（在官方页面确认
登录后点击面板按钮；失败只允许一次人工重试）。本任务是否完成在线 E2E 以
`docs/12` 第 8 节记录为准，不伪称在线通过。

## 12. 当前限制

- 单一固定帖子（tid=30099686）；
- 无计划任务/调度，每月人工点击一次；
- 不回填历史；
- 已入队楼层的编辑需人工处理；
- 多楼层作品、同一作品多来源等业务冲突仍由人工判断。
