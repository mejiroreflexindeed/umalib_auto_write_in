# 12 · Watcher Windows 7 操作台（TASK-011R+012 门 B）

## 1. 带 Watcher 配置启动本地审核台

```powershell
$env:PYTHONPATH = "src"
python -B -m umalib_ingest.web `
  --db .\inbox\review.db `
  --watcher-state "D:\codex\13\umalib-private-state\nga-watcher-state.json" `
  --watcher-profile "D:\codex\13\umalib-private-state\nga-profile" `
  --watcher-snapshot-dir "D:\codex\13\umalib-private-state\nga-snapshots"
```

- `--watcher-state` / `--watcher-profile` / `--watcher-snapshot-dir` 必须**同时提供或同时省略**；
  只给一部分会在启动阶段直接报错退出（fail closed），不会创建任何文件；
- 当前 `--db` 就是 Watcher 正式导入要写入的 Inbox，HTTP 请求不能指定路径；
- 未配置 Watcher 时，审核与 Writer 全部照常工作，页面只显示"启动时未配置 Watcher"；
- 可选：`--watcher-login-timeout-seconds`（默认 300）、`--watcher-max-scan-pages`（默认 12，上限 25）。

## 2. 状态刷新、dry-run、正式加入 Inbox 的区别

| 操作 | 访问 NGA | 写快照 | 写 Inbox | 推进 state |
|---|---|---|---|---|
| 刷新状态（只读） | 否 | 否 | 否 | 否 |
| 只检查，不导入（dry-run） | 是 | 是 | 否 | 否 |
| 扫描并加入当前 Inbox | 是 | 是 | 是（仅触发楼层） | 是 |

- 正式扫描必须：勾选确认框 + 后端校验“页面刚读取的 expected_state_sha256”与当前 state 一致；
- 扫描只把含独立行 `[收录]` 的楼层导入当前 Inbox（candidate/incomplete），普通讨论只记 seen；
- 扫描不批准、不忽略、不标记重复、不生成候选数据库、不写大书库；导入后刷新 Inbox 列表由人工审核。

## 3. 登录过期时的官方页面操作

1. 扫描中检测到登录页/官方登录重定向，页面显示"检测到需要登录"提示，Edge 停在官方 NGA 页面；
2. 你只在官方页面手工完成登录（账号/验证码都由你亲自操作，程序不读取凭证）；
3. 回到页面点击"我已在官方 NGA 页面完成登录，重试一次"；
4. 程序只重试第一页一次；仍失败则关闭 Edge 并显示稳定错误，不会第三次请求。

等待确认有超时（默认 300 秒，可配置）；超时后扫描结束并回到空闲状态。

## 4. checkbox 和 expected state hash 的意义

- checkbox 是人工确认"允许把本次发现的投稿加入当前 Inbox 并推进扫描状态"；
- `expected_state_sha256` 是页面最后一次刷新状态时拿到的 state SHA；正式扫描时后端
  会与当前磁盘 state 比较，不一致就拒绝（409 watcher_state_stale），避免"确认的是旧状态、
  写的是新状态"的错位。此时先点"刷新状态"再重新确认。
- 状态 SHA 改变或读取失败时自动撤销旧勾选；发起扫描后也消耗本次确认。

## 5. 安全边界

- 同一进程同一时间只允许一个扫描；重复点击/并发请求返回 409；
- 有未保存审核修改时扫描按钮被阻止（沿用 dirty 保护）；
- 请求/响应不含账号、密码、Cookie、验证码、任意 URL、profile/snapshot 路径；
- Watcher 状态摘要不显示正文、标题、作者、PID 列表；Inbox 审核区仍显示稿件内容；
- 扫描期间不冻结列表与审核，仅扫描相关按钮禁用。

## 5b. 服务关闭/取消 ≠ 登录确认

- 关闭审核台（Ctrl+C/关窗口）会取消正在等待登录的扫描：页面显示"已取消：…"，
  `last_error` 为 `watcher_shutdown`，本次扫描会话恰好关闭一次；
- 取消绝不等于"用户已完成登录"。首次入队前取消不推进 Inbox/state；已有条目提交后，
  显示 `watcher_partial_import`（已部分入队、state 未推进），不可视为完全未写入；
- 取消/失败不会清空你未保存的审核表单；成功或部分入队会刷新列表，仍保留未保存编辑；
- 仅关闭浏览器标签页不等于关闭服务；Ctrl+C 或关闭服务终端才会通知扫描取消。

## 6. 每月手工运行一次即可

Watcher 没有计划任务、后台常驻、服务、托盘或开机自启。每月打开一次审核台，
点"扫描并加入当前 Inbox"，其余交给人工审核流程。

## 7. 常见错误及恢复

| 现象 | 原因与恢复 |
|---|---|
| 面板显示"启动时未配置 Watcher" | 启动命令缺 watcher 参数；重启并带全三个参数 |
| 提示 watcher_state_stale | 状态在别处被修改；点"刷新状态"后重新勾选确认 |
| 提示 watcher_scan_busy | 已有扫描在运行；等待其结束或重启审核台 |
| 登录等待超时 | 未在官方页面完成登录；完成后重新扫描 |
| 提示 watcher_state_missing | 尚未建立基线；先运行 `nga-watch baseline-from-snapshot` |
| 提示 watcher_not_awaiting_login | 没有等待登录的扫描；忽略或重新扫描 |
| 显示"已取消：…"（watcher_shutdown） | 服务关闭取消了本次扫描；不是登录确认成功，重新打开审核台再扫 |
| 已部分入队（watcher_partial_import） | Inbox 已有提交但 state 未推进；不要删条目，重扫恢复后核对 |
| state_import_recovery_ambiguous | 原帖在中断后又修改过；人工核对已有 Inbox 与原帖，不自动覆盖 |

## 8. 门 E（在线隔离 E2E）结果

**ONLINE_E2E_PASS**（2026-09-01 UTC 13:17，隔离目录
`D:\codex\13\umalib-private-state\nga-e2e-20260901-20260901-131624`，保留供 Codex 验收）：

- 登录态仍有效（TASK-010 的独立 profile 复用成功，无需人工重新登录）；
- 扫描页范围 `[173]`（当前最后一页仍为 173，与基线一致）；
- 20 个已知楼层全部 unchanged，新增/变化/普通讨论/触发均为 0；
- 未产生任何 Inbox 导入（inbox-copy 0 项，state-copy imported=0，绑定一致）；
- state-copy 由 `c31a71e6…` 推进为 `4f0d28bc…`（仅更新 last_successful_scan_at）；
- canonical state、现有快照、真实 Inbox、main-copy、候选库的 hash/size/mtime 全部未变；
- 未输出正文/作者/标题/PID 列表或凭证；E2E 目录含 state 副本、空 Inbox 副本、
  该次快照（page-1/page-173 完整三件套）与最小结果摘要 `result-summary.json`。

### TASK-011R2+012A 门 E 复验（2026-09-01 UTC 15:53）

**ONLINE_E2E_PASS**（隔离目录
`D:\codex\13\umalib-private-state\nga-e2e-r2-20260901-155306`，保留供 Codex 验收）：

- 登录态仍有效，未出现登录页，全程无人工交互；
- 页范围 `173 → 173`，20 楼层全 unchanged，new/trigger/import 均为 0；
- inbox-copy 0 项；state-copy 由 `c31a71e6…` 推进为 `8f5621e1…`；
- canonical state、原 snapshots、真实 Inbox、main-copy、候选库不变；
- 详细记录见 `docs/13-watcher-release-acceptance.md`。
