# 13 · Watcher 发布候选验收（TASK-011R2+012A）

更新时间：2026-09-01。本文件是 TASK-011R2+012A 的发布候选验收记录，供 Codex 主线独立验收。

后续记录（2026-09-02）：本页历史在线结果不代表当前修复版本重新在线通过。
TASK-012B 独立验收发现的问题及 Codex 直接修复的最新结果，见
`docs/reports/TASK-012B-final.md` 的“Codex 直接修复复验”附录；当前行为以 docs/11、12、15 为准。

## 1. 为什么已有 437 项全绿仍需要本轮修复

上一轮 TASK-011R+012 的主体功能（Watcher 核心、Windows 7 操作台、正常登录态扫描、
隔离在线 E2E happy path）已经完成，437 项测试全绿。但 Codex 的行为级补充验收稳定复现
4 个缺陷，全部属于"测试盲区"，不是回归：

1. **F1 官方登录重定向被当作通用意外重定向**：旧实现对 `https://bbs.nga.cn/login.php`
   走 `validate_final_url` → `unexpected_redirect`，Watcher 永远进不了
   `manual_login_required → 用户确认 → 一次重试` 的流程；
2. **F2 登录重试不经过统一三秒导航门**：旧实现只对"进入计划页"前 sleep，登录确认后的
   重试导航前没有 sleep（观测 `LOGIN_RETRY_SLEEP_CALLS=[3.0]`）；
3. **F3 Web 的 expected state SHA 没有绑定到 run_scan**：旧实现只在调用 runner 前比较
   一次，页面确认 state A 后、runner 真正加载 state 前若被改为 B，竞态被接受
   （观测 `EXPECTED_SHA_RACE_ACCEPTED=True`）；
4. **F4 shutdown 被解释成"用户已登录"**：旧实现登录确认与服务取消共用同一个 Event，
   关闭服务会"唤醒"等待登录的 callback 并继续重试（观测
   `SHUTDOWN_INTERPRETED_AS_SUCCESS=True`）；同时 `web.app.run()` 退出只调
   `server_close()`，不通知 Watcher service。

因此上一轮结论是 `BLOCKED_CORE`，不能把"437 项全绿"解释为可以正式使用。

## 2. 官方登录重定向与外部意外重定向的区别

| 场景 | 判定 | 结果 |
|---|---|---|
| HTTPS + 官方 NGA host（bbs.nga.cn 等）+ 明确登录路径（`/login.php`、`/login`、`/login/`、`nuke.php?func=login`） | `is_official_login_url()` 精确匹配 | `manual_login_required`，只落无正文诊断 meta，Edge 停在官方页面等待用户手工登录 |
| 官方 host 但非登录路径（如 `/other.php`） | 仍走 `validate_final_url()` | `unexpected_redirect`，写正文前阻断 |
| 非 NGA host、HTTP 降级、错误 tid | 仍走 `validate_final_url()` | `unexpected_redirect`，写正文前阻断 |

登录页的正文、标题和用户内容绝不写入正常 response/decoded 快照；只允许既有的无正文
诊断 meta（TASK-010 `nga-probe` 语义继续兼容）。没有放宽 `validate_final_url()`，也没有
把任意 NGA 页面放行。

## 3. 登录确认只重试一次，且每次顶层导航统一限速

- 第一次顶层导航前不 sleep；从第二次开始，每一次 `goto/fetch_thread_page` 前
  sleep `navigation_delay_seconds`（默认 3.0，可注入，测试断言调用顺序与值，
  不依赖真实时间）；
- 登录等待本身持续多久都不能替代确定性导航限速；用户确认后重试仍经过导航门；
- 确认后只重试第一页一次；仍失败返回稳定 `login_required`，无第三次请求；
- 第一页没有登录问题且属于计划时只请求一次；禁止并发抓页。

测试证据：`[1,173]` sleeps `[3.0]`；登录重试 `[1,1,173]` sleeps `[3.0,3.0]`；
`[1,173,174]` sleeps `[3.0,3.0]`；重试后立即失败仍有重试前一次 `[3.0]`；
自定义 4.5 时使用 4.5。

## 4. expected state SHA 在锁内绑定的意义

- `expected_state_sha256` 由 Web 在调用 `run_scan` 边界传入；
- 正式扫描取得 state lock 后**锁内**加载 state、计算当前 SHA 并与 expected SHA 比较；
- 不一致返回稳定 `watcher_state_stale`，且发生在 session 创建、网络访问、Inbox
  初始化和 state 写入**之前**（测试断言 session 未创建、fetch 未调用、Inbox 未建）；
- Web 前置检查仍保留（缺确认/缺 SHA/格式非法/静态 stale 都 409），但它不是唯一的门；
- import 前/后的外部变化检查保留，没有因为锁内绑定而删除。

这消除"页面确认的是状态 A、runner 写的是状态 B"的竞态窗口。

## 5. Ctrl+C / 服务关闭会取消扫描并关闭本次 session，不等于登录确认

- 登录确认与服务取消是不同语义：`login_confirm()` 只设置一次性确认事件；
  `shutdown()` 先置取消标志再唤醒等待者，等待 callback 抛稳定
  `watcher_shutdown`（"服务关闭，扫描已取消（不等于登录确认）"），绝不继续重试、
  绝不返回成功；
- 关闭后 `login_confirm()` 被拒绝（`watcher_shutdown` 或 `watcher_not_awaiting_login`）；
- `run_scan` 的 finally 使本次扫描创建的 session 恰好关闭一次；
- 扫描页之间检查取消，收到取消后不再 fetch 下一页、不 import、不推进 Inbox/state；
- 重复 shutdown 幂等；busy/awaiting_login 回到稳定状态，新 service 实例仍可用；
- `create_server()` 把 Watcher service 挂在 server 对象上，退出路径可安全取得；
  `web.app.run()` 在 `server_close()` 前 best-effort 通知 service 取消；
- 前端识别 `watcher_shutdown` 显示"已取消：…"（警示色），与一般失败区分，不清空
  用户未保存的表单。

## 6. 每月手工点击一次，不是后台调度器

没有计划任务、后台常驻、服务、托盘或开机自启。每月打开一次审核台，点
"扫描并加入当前 Inbox"，其余交给人工审核。

## 7. 扫描只进入 Inbox，不批准、不写大书库

- 普通讨论只记 seen；含独立行 `[收录]` 的楼层导入 Inbox（candidate/incomplete）；
- 已入队楼层再编辑不覆盖人工审核内容，只报 `changed_after_import` 人工复查；
- 扫描不批准、不忽略、不去重、不生成候选数据库、不打开或替换大书库；
- Watcher → Inbox 离线联调（门 D3）：普通讨论不入队；完整稿 candidate；缺字段
  incomplete；重复运行幂等；变化不自动覆盖；state 与 Inbox item ID 绑定一致。

## 8. 门 E：隔离在线复验结果

**ONLINE_E2E_PASS**（2026-09-01 UTC 15:53 执行，隔离目录
`D:\codex\13\umalib-private-state\nga-e2e-r2-20260901-155306`，保留供 Codex 验收）：

- 登录态仍有效，未出现登录页，全程无人工交互、无 wait-file；
- 固定 tid=30099686，串行一次非 dry-run 扫描，页范围 `173 → 173`（与基线一致）；
- 20 个已知楼层全部 unchanged；new=0、changed=0、普通讨论=0、触发=0、导入=0；
- inbox-copy 0 项；state-copy 由 `c31a71e6…` 推进为 `8f5621e1…`
  （仅更新 last_successful_scan_at，safe 字段 tid/last_page/known_floors 不变）；
- 隔离快照 6 件（page-1、page-173 完整三件套）；
- 不变性：canonical state、原 nga-snapshots（11 件）、真实 Inbox `review.db`、
  `main-copy.db`、已有候选库的 hash/size/mtime 全部未变；前一轮 E2E 目录未动；
- 未输出正文、作者、标题、PID 列表或凭证；结果摘要见该目录
  `result-summary.json`。

## 9. 测试命令与结果（2026-09-01，全部离线、无真实 Edge/网络）

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONDONTWRITEBYTECODE = "1"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

| 套件 | 结果 |
|---|---|
| tests.test_nga_access | Ran 52 tests … OK |
| tests.test_watcher | Ran 50 tests … OK |
| tests.test_watcher_web | Ran 15 tests … OK |
| tests.test_watcher_release_acceptance | Ran 29 tests … OK |
| tests.test_web + tests.test_writer_web | Ran 79 tests … OK |
| tests.test_inbox + tests.test_parser | Ran 62 tests … OK |
| 完整回归 | Ran 466 tests in 87.670s … OK |

- 修改前基线：Ran 457 tests … OK（>437）；验收测试 25 → 29 项、完整回归 462 → 466 项
  为本轮新增/强化的行为测试，全部未弱化旧断言；
- skip/xpass：0；测试不访问公网、不打开真实 Edge、不读取真实 profile；
- 测试创建的 DB/sidecar/lock/wait-file/快照/缓存全部清理。

## 10. 门 D 离线发布候选验收摘要

- **D1 loopback**：config 只返回 enabled+tid；status 离线只读；dry-run 不写
  Inbox/state；正式 scan 缺确认 409、静态/竞态 stale 409；登录等待 status
  awaiting_login、确认只重试一次；shutdown 取消等待且不显示为成功、last_error
  `watcher_shutdown`；foreign Origin 403、非 JSON/未知键 400、超大 body 413
  `payload_too_large`；关闭 server 后端口释放、线程结束、session close 恰好一次。
- **D2 Win7 UI**：1024×768 与 1280×720 下 Watcher 面板可见、与 Inbox 不重叠、
  页面 0 JS 错误（headless 系统 Edge + 假数据截图，截图随临时目录清理）；
  七状态文案明确（未配置/扫描中/等待登录/成功/失败/取消/确认文案）；正式扫描未勾选
  前端阻断且后端独立校验；waiting 时登录按钮可用、扫描按钮禁用；dirty 阻止扫描；
  扫描成功刷新 Inbox、取消/失败不清空未保存表单；无 innerHTML/eval/new Function、
  无 CDN/图片/前端框架/账号登录页。
- **D3 离线联调**：脱敏 fixture 经真实 Parser/Inbox 公共 API，验证见第 7 节。

## 11. 不变性摘要（前后一致）

| 对象 | 值 |
|---|---|
| canonical state SHA | `c31a71e6d14486b7ec57757d72da1438063de8650bdc46749d853437415fe6ba`（不变） |
| review.db SHA | `bcc41160bb6bb136368af69c968af1c5edef608118b49e0d90600a5104ace29b`（不变） |
| main-copy.db SHA | `4e152ea33e046b1bd238387c39fa32ad4dc6fd584d5fc1f6c1ff10431a6a54dc`（不变） |
| manual-acceptance-20260831.db SHA | `4081a732977d76b570de32fd9aa7a2814e057beef6c20b0584254ed5e1fc7f0c`（不变） |
| 原 nga-snapshots | 11 件，max mtime `2026-08-31T11:57:17.917970Z`（不变） |
| 前一轮 E2E 目录 | 9 件，未修改 |
| 项目残留 | 无 DB/-wal/-shm/lock/wait-file/snapshot/__pycache__/.pyc |
| 遗留进程 | 无 loopback server、无任务 Watcher、无任务 Edge 会话 |
| pyproject.toml | 未修改 |

## 12. 已知限制

- 单一固定帖子（tid=30099686），不回填历史 173 页；
- 无计划任务/调度，每月人工点击一次；
- 登录、验证码由用户手工完成，程序不读取凭证；在线复验依赖登录态有效；
- 已入队楼层的编辑、多楼层作品、同一作品多来源等仍由人工判断；
- 人工审核在本地 Inbox 审核台进行（保存/放弃/改变状态），本项目没有 review.csv
  导出或自动翻译功能（2026-09-02 TASK-012B W0 复核修正：无实现证据）。

## 13. 结论

门 0→A→B→C→D 全部通过，门 E 在线复验通过：

```text
PASS_OFFLINE + ONLINE_E2E_PASS
```

等待 Codex 主线独立验收；DeepSeek 不自行宣布正式发布。
