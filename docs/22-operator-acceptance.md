# 22 · 当前验收记录

**2026-09-26 当前：先交付可用软件，非阻断问题后续集中修复。** 核心旅程 24 项功能检查通过，本机启动环境已核对可用；全量和清理失败如实保留，尚未全面验收。真实 NGA 与大书库显示待现场验证。 证据和后续安排见 [当前复核](reports/ASTRA-REVIEW-2026-09-26.md)。下方旧状态为历史，当前执行以本段及 TASK-015 第 8 节为准。

## 2026-09-24 收口结论（DeepSeek；待 Astra 独立复核）

**对 Astra 09-22 RETURN_FOR_REWORK 的集中修复已完成**（F021 批准后编辑死路、F019/A053 纯讨论重扫误报、A068 精度、测试清理、矩阵/文档）。本轮证据根：`D:\codex\13\umalib-private-state\operator-015-rework-20260924`。

| 检查 | 本轮事实（同一最终源码） |
|---|---|
| entrypoint | **PASS**：中文 CMD 外部 cwd/无 PYTHONPATH 实跑；解包到中文含空格路径实际启动并优雅退出 |
| operator_flow_offline | **PASS**：operator 十阶段，ui-journey **25/25**（含恢复新区与 A068 三阶段 DOM 比对、清理后无残留） |
| full_regression | **PASS**：unittest `Ran 696 OK`；Demo all 4/4（exit 0）；night all replay 44+13 + recovery + ui（exit 0）；exit-codes.txt 记录 OPERATOR/DEMO/NIGHT=0 |
| acceptance-matrix | **121 PASS / 2 PARTIAL / 0 FAIL**；旧相位证据改指本轮并注明适用性；A095/A097 按 Astra 要求重映射 |
| nga_online_ui / umalib_visual | WAITING_USER（现场项，不作为本地收口理由） |
| independent_review | **PENDING** |

修复与回归明细见 [defects](reports/TASK-015-defects.md) 的「2026-09-24 收口修复」。

## 2026-09-22 Astra 整体复核（历史：RETURN_FOR_REWORK）

F021、F019 实际复现并要求集中修复；当时旅程 21/21、包与源码一致。见 [复核报告](reports/ASTRA-REVIEW-2026-09-22.md)。两项均已在本轮修复并有真实 DOM 回归。

## 2026-09-21 提交记录（DeepSeek 原声明，非当前验收结论）

**LOCAL_OPERATOR_READY（执行者声明，证据根 `D:\codex\13\umalib-private-state\operator-015-final-20260921`）**：

| 检查 | 本轮事实（同一最终源码） |
|---|---|
| entrypoint | **PASS**：中文 CMD 在外部 cwd/无 PYTHONPATH 实跑 `--check`；源码包解到中文含空格目录后实际启动并读取产品页（`test_entry_assertions`、bundle smoke、launch 阶段） |
| operator_flow_offline | **PASS**：operator 十阶段，含增强版 ui-journey 21/21（两帖三篇→退出重开→恢复新区第四篇、A068 多来源回填、四篇逐字段+有序来源+标签精确回读、旧区不变） |
| real_source_readback | **PASS**：real-copy 8/8 checks（源 SHA/旁侧不变、候选逐字段/标签关联/FK/旧行/非目标表/schema） |
| full_regression | 见 `review-index.json` 的 test_runs（unittest / operator all / Demo all / night all，含退出码与日志） |
| nga_online_ui | WAITING_USER：本轮无在线授权，未访问真实 NGA |
| umalib_visual | WAITING_USER：需用交付中心“生成单独查看副本”现场核对 |
| independent_review | **PENDING**：交 Astra 抽验 A053/A054/A067/F018—F021 与 Q1 旅程 |

修复历史及新反例见 [defects](reports/TASK-015-defects.md)；A059/A062/A068 的原独立证据按 SHA 引用（`referenced/astra-20260921/`），适用性仍须结合当前源码。

## 2026-09-18 Astra 复核（历史）

F011/F012 原反例与相关 5 项回归独立通过；当时 real-copy 8/8 PASS。13 项 DOM/入口断言已在本轮 2026-09-21 补齐。见 [Astra 复核](reports/ASTRA-REVIEW-2026-09-18.md)。

## 2026-09-15 收尾结论（DeepSeek 执行，待 Astra 独立复核）

**LOCAL_OPERATOR_READY（执行者声明）**：本地全部必需项已用最终源码复跑通过。F012（换副本新建工作区 Inbox 未初始化）、F011（同楼旧版本被分页截断）、F013（切帖残留“预览完成”致初始化误判）均已修复并有定向回归；real-copy 已按字段精确回读。证据根 `D:\codex\13\umalib-private-state\operator-015-closeout-20260915`。

| 检查 | 本轮事实（最终源码） |
|---|---|
| entrypoint | **PASS**：源码包解到含中文/空格的 `unpack 中文 目录`，以既有解释器实际启动，`/operator.html` 200 且含标题与 operator.js、`/api/operator/status` 200 |
| operator_flow_offline | **PASS**：operator 十阶段（launch/threads/scan/review/writer/recovery/real-copy/ui/lifecycle/ui-journey）全 PASS，exit 0 |
| real_source_readback | **PASS**：real-copy 8/8 checks（主副本 SHA/旁侧文件不变、候选逐字段/标签关联/FK/旧行/非目标表/schema 精确核对） |
| full_regression | **PASS**：`Ran 658 tests OK`；Demo all、night all 均 PASS |
| acceptance-matrix | 11 PASS（逐用例+证据校验）/ 112 PARTIAL 待抽验 / 0 FAIL（`acceptance-matrix.json`） |
| bundle | `operator-bundle-final.zip` 173 文件 sha256 `52cf4c36…`，已解包实际启停 |
| nga_online_ui | WAITING_USER：本轮无在线预算 |
| umalib_visual | WAITING_USER：需用独立查看副本现场核对 |
| independent_review | **PENDING**：交 Astra 抽验 F011/F012/F013 与一条核心旅程 |

## 2026-09-13 历史结论

**VALIDATION_INCOMPLETE：保留完成的开发成果，集中调试/候选版验收尚未结束。** 647 OK/九阶段 PASS 的交付日志已核到；不等于全部 A 条目或完整中文入口 UI 旅程通过。真实退回编辑缺陷、日期回填反例、范围级矩阵、服务层 lifecycle 与剩余交付验证见 [Astra 最新核对](reports/ASTRA-REVIEW-2026-09-13.md)。

当前执行：TASK-015 第 9 节 Q0—Q3。未改产品代码；只做规划核对及有限浏览器验证。用户现场仅保留真实 NGA 与大书库显示，本地剩余项不推给用户。

以下为历史阶段记录；旧 LOCAL_OPERATOR_READY 是执行者声明，当前尚未通过独立确认。

更新：2026-09-11。**总状态：INCOMPLETE；先完成全流程开发，再统一 debug。**

| 检查 | 本轮事实 | 限制 |
|---|---|---|
| 完整 unittest | 647 项 OK，249.218 秒 | 现有自动化覆盖，尚非全部 A001—A112 |
| operator all | 八阶段 PASS，含实际浏览器 UI | 部分 phase 为别名；未完成同中文入口重开/恢复第四篇的全旅程 |
| 新增边界 | 13 项，包含扫描恢复、日期、异步编辑及启动失败清理 | 证据见本轮交接，不要求开发开工重复重跑 |
| 源库身份 | main-copy SHA/size 前后一致 | 不扩大为全部保护目录的独立比对 |
| 最终 Demo/night、当前包实际启动 | 本轮未执行 | 留开发完成后 Q2 集中完成 |
| NGA 在线 UI | NOT_RUN | 本轮没有新现场预算 |
| 大书库实际显示 | NOT_RUN / WAITING_USER | 必须用独立查看副本核对 |
| 独立验收 | 尚未完成最终全流程验收 | DeepSeek 完成开发及集中调试后再交 Astra |

证据根：`D:\codex\13\umalib-private-state\operator-015-astra-20260910`。

- [本轮交接与改动](reports/ASTRA-REVIEW-2026-09-11.md)
- [完整任务书与完成标准](../tasks/TASK-015-overnight-closeout.md)
- [缺口/延后调试清单](reports/TASK-015-defects.md)
- [恢复点](reports/TASK-015-progress.md)

2026-09-08 的 R01—R08 检查与旧测试数见历史 Astra 报告；DeepSeek 上次结论见 TASK-015-final.md。历史结果不作为当前总状态。

最后交付必须分别给出 entrypoint、operator_flow_offline、real_source_readback、nga_online_ui、umalib_visual、independent_review。全部本地必需项通过才 LOCAL_OPERATOR_READY；现场两项也通过才 OPERATOR_ACCEPTED。开发期登记 bug 不影响继续开发，交付前仍须统一闭环。

## TASK-015 第二轮结果（2026-09-12，DeepSeek 执行）

| 字段 | 值 |
|---|---|
| entrypoint | **PASS**：中文 CMD 非项目 cwd、无 PYTHONPATH 实启动（status 200 + operator.html） |
| operator_flow_offline | **PASS**：`operator_acceptance --phase all` 九阶段（旧八 + **lifecycle**）全 PASS |
| real_source_readback | **PASS**：real-copy 阶段（main-copy 只读派生，源 SHA/size 不变） |
| full_regression | **PASS**：`Ran 647 tests OK`（unittest-full.log，exit 0） |
| acceptance-matrix | 107 PASS / 11 PARTIAL / 2 NOT_RUN（范围级 PASS 需 Astra 抽验） |
| bundle | 168 文件，sha256 `0f43f473…`；解包 `--check` exit 0 |
| nga_online_ui | NOT_RUN（本任务未安排在线预算） |
| umalib_visual | WAITING_USER |
| independent_review | PENDING（本报告交 Astra） |
| overall | **LOCAL_OPERATOR_READY**（待 Astra 复核与现场两项） |

- 本轮补齐：F001（忽略理由/退回待审/锁定态）、F002（补查恢复）、F004（lifecycle：
  三篇→检查点→退出→重开→恢复新工作区→第四篇，来源链核对）、恢复后 launcher 更新。
- 证据根：`D:\codex\13\umalib-private-state\operator-015-20260912-111232`。
- 缺陷状态：见 `reports/TASK-015-defects.md`（F004 闭环；F001/F002 待抽验；F003/F006 外部；
  F007 偶发记录）。
- 报告：`reports/TASK-015-final.md`（本版）。
