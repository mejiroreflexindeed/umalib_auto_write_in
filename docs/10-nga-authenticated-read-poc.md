# 10 · NGA 登录态只读访问 PoC（TASK-010）

## 1. 为什么匿名 403 不能直接等同于登录后不可行

2026-08-31 对目标帖 `tid=30099686` 的匿名只读探测得到：

- `https://bbs.nga.cn/read.php?tid=30099686` → `403`；
- 加 `page` 参数仍 `403`；
- `__output=11` 形态仍 `403`；
- `www.nga.cn/?tid=30099686` 只返回通用首页。

这些只能证明**匿名 HTTP 客户端**不可用，不能证明**登录后的真实浏览器**不可用。
NGA 站点对匿名/非浏览器请求实施风控是常见行为；本 PoC 用系统 Edge + 独立
持久化 profile + 用户在官方页面手工登录来回答这个问题。

## 2. 实际环境

- 浏览器：本机 Microsoft Edge 稳定版（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`），
  仅通过 Playwright `channel="msedge"` 启动，**不下载、不覆盖任何浏览器**。
- Playwright Python：`playwright==1.62.0`（项目可选依赖组 `nga`）。
- Python：`3.10.11`，安装于项目本地虚拟环境 `.venv`。
- 独立 profile：`D:\codex\13\umalib-private-state\nga-profile`（绝不指向日常 Edge `User Data`）。
- 快照目录：`D:\codex\13\umalib-private-state\nga-snapshots`。

## 3. 安装可选依赖（不下载浏览器）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "playwright==1.62.0"
```

不要运行 `playwright install`、`playwright install msedge` 或任何浏览器下载命令。
未安装 playwright 时，`probe` 返回 `playwright_not_installed`；默认审核/Writer 功能完全不依赖它。

## 4. 用户首次手工登录步骤

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -B -m umalib_ingest.nga_access probe `
  --thread-url "https://bbs.nga.cn/read.php?tid=30099686" `
  --profile-dir "D:\codex\13\umalib-private-state\nga-profile" `
  --snapshot-dir "D:\codex\13\umalib-private-state\nga-snapshots"
```

1. 程序打开独立有头 Edge 并导航到 `https://bbs.nga.cn/`；
2. **只在官方 NGA 页面**手工完成登录（账号/验证码/二次验证都由你亲自操作，程序不读取）；
3. 回到终端按 Enter 确认“已完成登录”（非交互启动可用 `--login-wait-file <路径>`：
   登录完成后创建该空文件作为确认信号，程序不读取其内容）；
4. 程序导航到目标帖第一页并保存响应快照与结构化结果；
5. 若可确定最后一页且与第一页不同，间隔 ≥3 秒后再读取最后一页。

`probe` 命令参数边界：

- `--max-pages` 默认 2、上限 2；
- `--navigation-delay-seconds` 默认 3、下限 3（两次帖子导航及可选 JSON 观察前均须间隔 ≥3 秒）；
- `--no-json-probe` 可关闭可选 JSON 观察；
- `--help` 与 `inspect-snapshot` 完全离线、不导入 Playwright；
- 无任意 URL、批量、并发、无限重试、Cookie/代理/UA/验证码绕过参数。

安装后的 console 入口（等价于模块命令）：`nga-probe`、`nga-inspect-snapshot`。

## 5. profile 的敏感性、保管与删除

- `nga-profile` 等价于一个已登录会话；其中的登录态数据等同于账号凭据，必须由你本人保管。
- 程序只验证路径边界，不枚举、不读取、不复制 profile 内容。
- 需要清除登录态时，直接删除整个 `D:\codex\13\umalib-private-state\nga-profile` 目录即可；
  程序从不自动退出 NGA 账号。

## 6. 固定目标、低频、无并发规则

- 在线验收只允许固定目标 `tid=30099686`；
- 一次 `probe` 最多读取第一页与可确定的最后一页（同页只读一次）；
- 页面失败最多一次显式人工重试，不循环重试；
- 两次帖子导航至少间隔 3 秒；
- 不自动翻页扫描历史，不跟随帖内链接，不扫描其他帖子。

## 7. 快照布局、编码、hash 与不覆盖策略

```text
D:\codex\13\umalib-private-state\nga-snapshots\tid-30099686\
  page-1.response.bin        # 目标文档原始字节
  page-1.decoded.html        # 确定性解码后的文本（编码不确定时不生成）
  page-1.meta.json           # 状态/URL/类型/时间/字节数/SHA-256/诊断
  page-173.response.bin      # 实际页码命名（任务示例中的 page-last 语义不稳定，
  page-173.decoded.html      # 改用真实页码保证与内容一一对应）
  page-173.meta.json
probe-result.json            # 本次探测摘要（无正文/作者/Cookie）
```

- 路径不含标题、作者、UID、Cookie 或查询字符串；
- 写入全部走“同目录临时文件 + 原子替换”；
- **版本成组保存**：同一版本的 `response.bin`、`decoded.html`、`meta.json` 使用相同基名
  （首次为 `page-{N}`，内容变化后为 `page-{N}.{body_sha8}`），绝不覆盖任何旧版本；
- 登录/权限/验证码/不可识别页面**不落盘正文**，只保存 `page-{N}.diag.json` 无正文诊断元数据；
- 导航后对最终 URL 做严格 scheme/host/path/tid 校验，异常重定向在保存正文前停止；
- 子资源限制：拦截图片/媒体/字体及一切非 NGA 官方站点（nga.cn / nga.178.com / 178.com）请求，
  顶层文档导航不受影响；
- 编码判定顺序：Content-Type charset → HTML meta charset → UTF-8 严格解码 → GBK/GB18030；
  仍无法无损判定时诊断 `encoding_uncertain`，不静默丢字符。

## 8. 在线验收结果（2026-08-31 实测）

结论：**PASS**（登录态可复用；目标帖成功读取；第一页楼层 pid/正文容器可稳定识别；重复复核无安全异常）。

### 8.1 环境与登录

- 系统 Edge 稳定版 channel 可用（`channel="msedge"`，有头），未下载/覆盖任何浏览器；
- 用户在官方 NGA 页面手工完成登录（程序不读取任何凭证）；
- 独立 profile 持久化复用：两次独立启动均无需重新登录。

### 8.2 目标帖读取证据（tid=30099686，最小披露）

- 第一页 `https://bbs.nga.cn/read.php?tid=30099686`：HTTP 200，非 403；
  - 页面形态：HTML（`text/html; charset=GBK` 声明，实测内容为 UTF-8，解码确定性回退）；
  - 分页判断：`__PAGE = {0: base, 1:173, 2:1, 3:20}` → 总页数 173、当前页 1、每页 20 行；
  - 楼层计数：19 个真实楼层（楼主行 pid=0 占位被排除），pid 全部唯一；
  - 正文容器：`<tr id='post1strowN'>` 行内 `<a id='pid{PID}Anchor'>` + `postdateN` + `postcontentN`（UBBCode）；
  - 两次读取楼层结构计数稳定（19/19），body hash 因页面动态内容变化 → 按版本保留、不覆盖。
- 最后一页（page=173）：HTTP 200，20 个楼层，pid 全部唯一，与第一页间隔 ≥3 秒读取。
- 可选 JSON 观察（`__output=11`，一次性）：HTTP 200，`text/json;charset=UTF-8`，顶层对象
  `{data, time}`，`data.__R` 含楼层行（`pid`/`lou`/`postdate`/`content`）；仅作实验观察，非唯一成功路径。
- 快照与 manifest：`page-1.response.bin/.decoded.html/.meta.json`、
  `page-173.response.bin/.decoded.html/.meta.json`、`probe-result.json`；
  同一页面内容变化时生成带短 hash 的新版本（`page-1.cf759592.response.bin`），绝不覆盖。
- 未发现验证码、权限页或风控阻断；全程低频（两次帖子导航间隔 ≥3 秒）、无写操作。

### 8.3 编码取证

NGA 响应头声明 `charset=GBK`，但实测原始字节为 UTF-8。确定性解码按
Content-Type → meta → UTF-8 严格 → GBK/GB18030 的优先级正确回退到 UTF-8，
未出现 `encoding_uncertain`，未丢字符。

## 9. 本 PoC 的边界

- 没有接入 Inbox、Parser、审核、Adapter 或 Writer；
- 没有月度调度、定时器、后台服务、开机自启；
- 没有自动发布任何内容（不发帖/回复/点赞/编辑/上传）；
- 没有自动登录、不保存账号密码、不绕过验证码；
- 不依赖 NGA 未公开 JSON 接口（`__output=11` 只是同上下文的一次性可选观察）；
- 不下载图片、头像、附件、表情或站外页面；
- 不保存 Cookie、Storage State、header、截图、HAR、trace、视频。

## 10. TASK-011 前置条件

只有在主线验收本 PoC 的在线证据、楼层结构与持久化登录态结论之后，才允许设计
TASK-011（NGA Watcher）。本 PoC 的结论等级（PASS/PARTIAL/BLOCKED）见第 8 节。
