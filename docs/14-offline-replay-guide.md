# 14 · 离线回放实验室使用指南（TASK-012B W3/W4/W5 工具）

本文说明如何从干净环境重跑离线回放实验室、如何读失败结果、如何找到证据。
S 系列 44 场景注入已解析页面，执行真实 Watcher/Parser/Inbox；新增 H 系列 13 场景
注入 HTTP 响应字节，执行真实 fetch/codec/HTML 解析/Watcher/Parser/Inbox 链。
UI 使用真实无头 Edge 和本地接口（多数旅程注入 runner，部分入队旅程运行真实扫描核心）。
**不访问真实 NGA、不读取现有 profile、不写现有数据库。**

## 1. 前置条件

- Python 3.10+ 与项目 `.venv`（不需要安装任何新依赖）；
- `--phase ui` 额外需要已有 Playwright 与系统 Edge（msedge.exe），缺少时报告
  `UNAVAILABLE`，不阻止其他阶段收集证据，但 `--phase all` 总结果为 FAIL、退出码非 0；
- 全部命令从项目根运行，测试内自行设置 PYTHONPATH：

```powershell
cd D:\codex\13\umalib-ingest
$env:PYTHONPATH = "src"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
```

## 2. 命令

```powershell
.\.venv\Scripts\python.exe -B tools/night_acceptance.py --check-environment
.\.venv\Scripts\python.exe -B tools/night_acceptance.py --phase replay --output <新证据根>
.\.venv\Scripts\python.exe -B tools/night_acceptance.py --phase recovery --output <证据根>
.\.venv\Scripts\python.exe -B tools/night_acceptance.py --phase ui --output <证据根>
.\.venv\Scripts\python.exe -B tools/night_acceptance.py --phase all --output <新证据根>
```

- `--check-environment`：只读检查（Python 版本、模块导入、场景语料、playwright、
  Edge），不写库、不启浏览器、不联网；
- `--phase all` 的 `--output` 必须是**新目录或空目录**；分阶段运行可共用证据根，
  但每阶段拒绝覆盖其已有非空子目录；
- `--scenarios <文件>` 对 replay/all 均生效；不存在或非法就阻断，不偷偷回退默认语料；
- 退出码：0 通过；1 存在失败场景；2 阻断（参数/证据根冲突/环境核心缺失）。

## 3. 证据根结构

```text
<证据根>/
  replay/                  44 个语义场景 + 13 个响应链场景
    S01/result.json        结构化判定（checks、events、error_code）
    S01/summary.txt        人类可读摘要
    H01/result.json        HTTP 字节到 Inbox 的真实链判定
    summary.json           总览（passed/failed/overall）
  recovery/
    <point>.json           每个中断点记录（checks、verdict）
    summary.json           12 中断点映射表 + 总览
  ui/
    journeys.json          20 条 DOM 旅程记录
    screenshots/*.png      精选假数据截图
    index.txt              保留截图的相对路径索引
    failure-snapshot.json  仅失败时存在：失败现场页面状态
  all-summary.json         仅 --phase all 时
```

临时 state/Inbox/快照/profile 位于 `umalib-private-state` 下本轮创建的随机子目录，
正常结束及异常收尾时清理；证据根保留假数据结果、截图、摘要与相对路径索引。
强制终止父进程等无法执行 finally 的情形仍可能留下临时目录，需核对归属后清理。

## 4. 如何读失败

1. 看 `summary.json` 的 `overall` 与 `failed` 列表；
2. 打开对应 `result.json`，逐条看 `checks`：每条含 `check`（断言名）、`ok`、
   `detail`（期望值 vs 实际值）；`events` 是实际事件顺序
   （`fetch:页码`、`sleep:秒`、`confirm`、`import:PID`）；
3. `summary.txt` 是该记录的纯文本版本；
4. ui 阶段失败时看 `failure-snapshot.json`（失败时的页面文字与行数快照）；
5. 常见失败含义：
   - `events` 不一致 → 导航/取消/登录重试顺序与契约不符；
   - `counts` 不一致 → diff/导入计数与契约不符；
   - `inbox_items` 不一致 → 入队状态（candidate/incomplete）不符；
   - `error` 不一致 → 该失败场景的错误码与契约不符。

## 5. 场景语料确实被消费

- 语料在 `tests/fixtures/night/scenarios.json`（44 个场景，预期全部按
  docs/11/docs/12 契约手工撰写，不由实现生成）；
- 响应语料在 `tests/fixtures/night/responses.json`，包括 UTF-8/GB18030、投稿/讨论/
  缺字段、颜色注释、登录重试失败、外域重定向、403/404、中文验证页、结构及编码失败；
- 修改任意场景的一项预期（如计数 +1），对应场景必须 FAIL；
- 删除语料文件或某个场景的输入页，实验室必须报错/FAIL，绝不静默跳过；
- 同一个输入在两个独立证据根各跑一次，除时间与目录外结果字节一致。

## 6. 恢复矩阵（recovery）

12 个中断点的映射见 `recovery/summary.json` 的 `point_mapping`。其中
`kill-point2/3/9/10` 会真实启动并终止本实验室自己的子进程（登记过的 PID），
point9 恰好提交第一条后终止，point10 提交全部两条后终止，分别验证恢复和 ID 复用。
不会按进程名终止用户应用。断电残留的 `.lock` 文件**不会**被自动删除：
先确认没有其他扫描在运行，再手动删除（见 docs/15 第 7 节）。

## 7. 注意事项

- 本工具是验收工具，不是权限包装器：参数使用 allowlist，不接受 shell/eval
  或任意命令；
- 125% 布局采用 1280×720 物理像素等效的 1024×576 CSS 视口、DPR 1.25；
  这是可重复的等效布局检查，不声称实际操作了 Windows 系统缩放设置；
- 真实 NGA 登录态验证不属于本工具范围（在线状态固定
  `ONLINE_NOT_RUN_BY_DESIGN`）。
