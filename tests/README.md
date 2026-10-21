# 测试说明

当前项目包含 Parser 纯函数、合成 SQLite、HTTP、作业、Writer/恢复与浏览器验收。旧说明“所有测试都不访问数据库”只适用于最早的 Parser 阶段。

## 当前回归

在项目根使用已有 .venv，Windows 子进程编码保持一致：

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = 'D:\codex\13\umalib-ingest\src'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

2026-09-08：624 tests OK（232.294 秒）。日志在 `../umalib-private-state/astra-review-015-20260908/unittest-utf8.log`。这是当轮基线，不是最终产品验收。

注意：部分 operator 测试探测本机 main-copy.db，只读但耗时；缺源库可能 skip。不得用 skip 或缺环境充当 PASS。收尾任务应把可独立合成的单测与真实源回读分开组织。

## 测试层次

- `tests/fixtures/parser/`：UTF-8 原文与完整预期 JSON，Parser 不联网、不写库。
- 其他单元/集成测试：本轮归属明确的合成数据或临时目录；不改真实用户状态。
- `tools/operator_acceptance.py`：当前产品服务场景；现有版本缺真实浏览器 UI phase，TASK-015 必须补齐。
- `tools/demo_acceptance.py` / `tools/night_acceptance.py`：历史 Demo 与旧 Watcher/恢复/UI 回归；不替代 operator 主入口旅程。
- 真 NGA、真实副本回读、实际大书库显示和独立复核分别记录。

完整收尾命令、A001—A112 与 R01—R08 的验收要求见 [TASK-015](../tasks/TASK-015-overnight-closeout.md)。测试必须断言正确行为，不能把诊断脚本的 defect_reproduced=true 误作产品通过。
