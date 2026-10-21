# 15 · 录入人员快速上手（约 10 分钟）

面向每月操作审核台的录入人员。本页只讲"怎么用"；设计依据见 docs/11、docs/12、
docs/13。**区分两个概念**：

- **Inbox（审核台数据库）**：扫描进来的待审投稿，人工在这里补字段、保存、改变状态；
- **候选数据库（大书库候选）**：只有人工批准后才由 Writer 生成，扫描不会写它。

## 1. 第一次使用：只读检查

打开 PowerShell，运行一条命令即可判断能否启动（不写任何文件、不联网、不登录）：

```powershell
cd D:\codex\13\umalib-ingest
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -B tools/check_local_setup.py `
  --inbox-db <你的 Inbox.db 路径> `
  --watcher-state "D:\codex\13\umalib-private-state\nga-watcher-state.json" `
  --watcher-profile "D:\codex\13\umalib-private-state\nga-profile" `
  --watcher-snapshot-dir "D:\codex\13\umalib-private-state\nga-snapshots"
```

- 每行 `[OK]`/`[WARN]`/`[BLOCKED]` + 下一步提示；
- 出现 `[BLOCKED]` 按提示修正后再启动；`[WARN]` 一般可继续；
- 输出永远包含"本次未验证登录"——这个工具不检查也不读取你的登录资料。
- 必须检查已关闭、无 `-wal/-shm/-journal` 的干净数据库；WAL 模式（即使暂时没有
  sidecar）也会阻断，避免只读检查反而创建 sidecar。需另行准备正常关闭并转为
  DELETE 模式的副本；不要手工删日志、不要直接转换正在使用的原库。
- 本工具检查完整 Inbox Schema，而不只检查版本号；OK 不代表已验证 Edge 启动、
  NGA 登录或在线访问。它不修改或修复数据库。

## 2. 启动审核台

```powershell
.\.venv\Scripts\python.exe -B -m umalib_ingest.web `
  --db <你的 Inbox.db 路径> `
  --watcher-state "D:\codex\13\umalib-private-state\nga-watcher-state.json" `
  --watcher-profile "D:\codex\13\umalib-private-state\nga-profile" `
  --watcher-snapshot-dir "D:\codex\13\umalib-private-state\nga-snapshots"
```

浏览器打开打印出的 `http://127.0.0.1:<端口>`。watcher 三个参数必须同时给或同时
不给；不给时审核台照常工作，只是没有"扫描 NGA"面板。

## 3. 每月扫描一次

1. 点 **刷新状态**：确认"上次页码/已知楼层/state SHA"显示正常；
2. 点 **只检查，不导入（dry-run）**：会联网并可保存快照，但不写 Inbox、不推进 state；
3. 正式导入：勾选"我确认把本次发现的投稿加入当前 Inbox，并推进扫描状态"，
   点 **扫描并加入当前 Inbox**；
4. 出现"检测到需要登录"：在已打开的官方 NGA 页面手工登录，回来点
   "我已在官方 NGA 页面完成登录，重试一次"（只重试一次）；
5. 出现"已取消：…"：这是上次关闭服务取消了扫描，不是成功；重新打开再扫；
6. 出现 `watcher_state_stale`：点刷新状态后重新勾选确认。
7. 出现“已部分入队”：已有稿件提交、state 未推进；不要删条目，按第 7 节恢复。

扫描只把含独立一行 `[收录]` 的楼层放进 Inbox；普通讨论只记录、不入队。
扫描不批准任何东西。

## 4. 缺字段的稿件

- 状态 `incomplete` 表示缺 title/author/tags/body 之一；
- 打开该条，按原帖补全字段，点 **保存修改**；有未保存修改时不能发起新扫描
  （这是保护，不是故障）；
- `changed_after_import` 的条目表示原帖后来改过，需要人工核对后决定是否同步。

## 5. 标签与来源由人工决定

- 标签按原帖内容人工选择；程序不自动联动或猜测；
- 同一作品出现多个来源（多楼层/多次投稿）时**不自动合并**：由你判断如何处理；
- 已入队稿件再被修改，程序不会覆盖你保存过的人工内容。

## 6. 登录过期 / 验证码

- 账号与验证码永远由你本人操作，程序不读取任何凭证；
- 登录等待有超时（默认 300 秒），超时后扫描结束回到空闲，重新扫描即可；
- 验证码/风控页会让扫描以 `page_fetch_failed` 结束，稍后重试。

## 7. 取消与中断恢复

- Ctrl+C/关闭服务终端会请求取消；仅关闭浏览器标签页不保证取消后台请求。
  首次入队前取消不推进 Inbox/state；已有提交则显示“已部分入队”，保留已提交条目；
- 若扫描在导入后被强制中断（如断电）：Inbox 可能已有部分条目、state 未更新。
  重新扫描仅在完整原始片段一致时复用条目，不覆盖人工编辑。
  若中断后原帖连标题/标签/来源等也改过，会提示 `state_import_recovery_ambiguous`，
  需人工核对，不会静默认定新版已录入；
- 残留 `.lock` 文件会阻止新扫描（`watcher_locked`）。确认没有其他扫描在跑后，
  手动删除 state 文件旁边的 `<state>.lock` 再重试；不要当作垃圾自动清理别人的锁。

## 8. 收工确认清单

1. Inbox 列表没有意外条目；该保存的修改已保存；
2. 页面没有"扫描中…"或等待登录提示；
3. 关闭浏览器页面，Ctrl+C 关闭审核台（会安全取消未完成的扫描）；
4. 建议关闭服务后成对备份 Inbox 与 Watcher state；Inbox 含人工审核成果，不能
   因“已存为 .db”就当成已有备份。程序目前不自动备份 Inbox。

## 9. 常见错误速查

| 提示 | 含义与处理 |
|---|---|
| 启动时未配置 Watcher | 启动命令缺 watcher 参数；不带即可继续审核 |
| watcher_state_missing | 还没有基线；先跑 `baseline-from-snapshot` |
| watcher_state_stale | 状态被别处修改；刷新状态后重新确认 |
| watcher_scan_busy | 已有扫描在运行；等它结束 |
| watcher_locked | 有 `.lock` 残留；确认无其他扫描后手动删除 |
| 已取消：… | 服务关闭取消了扫描，不等于登录确认 |
