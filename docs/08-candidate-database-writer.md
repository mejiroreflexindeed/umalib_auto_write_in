# 08 · 候选数据库 Writer 核心说明

本文面向后续 UI/CLI 开发者与主线验收，说明 TASK-008 实现的候选数据库 Writer（`umalib_writer`）的数据流、边界与已知限制。

## 1. 数据流

```text
TargetTagCatalog（只读加载）
        + UmaLibChangePlan（ready=true）
        + 候选路径
        ↓
preview_candidate_write()  → CandidateWritePreview（dry-run，绝不创建文件）
        ↓ 人工确认 app_closed_confirmed=True
        + CandidateWriteApproval（绑定 plan/preview/source 三个哈希）
        ↓
write_candidate_database() → 候选副本单事务写入 → CandidateWriteResult
```

三个公开 API：`load_target_tag_catalog`、`preview_candidate_write`、`write_candidate_database`，全部从 `umalib_ingest.umalib_writer` 包根导出。

## 2. dry-run 绝不创建文件

`preview_candidate_write` 只做：源库严格只读检查（复用 `inspect_umalib_database`）、计划二次验证、标签目录哈希复核、正文/来源/字段边界检查、重复证据查询（只读）。它不创建候选文件、临时文件、锁文件或目录，执行前后源库与目录无变化（单元测试以文件哈希/大小/mtime/目录集合证明）。

## 3. 为什么必须关闭大书库

Electron/SQLite 进程是否持有写句柄无法从文件系统可靠证明（没有跨进程锁查询 API）。因此 Writer：

- 用 `mode=ro` + `PRAGMA query_only=ON` 只读打开源库；
- 在复制前、复制后、事务后、发布后四次核对源库 SHA/大小/mtime/文件身份/sidecar，任何变化立即中止；
- 最终依赖人工明确确认：`CandidateWriteApproval.app_closed_confirmed` 必须 `is True`（不接受 `1` 或字符串）。

## 4. 为什么源库不会被写、候选如何在临时文件完成

- 源库自始至终只被 `mode=ro` 连接和 `open("rb")` 读取，绝无可写连接。
- 写入流程：把源库字节 `shutil.copyfile` 到候选父目录内的本次调用专属临时文件（`tempfile.mkstemp`，前缀 `.umalib-writer-`）→ 校验临时副本 SHA/大小与源一致 → 只对临时文件开可写连接并强制 `PRAGMA foreign_keys=ON` → `BEGIN IMMEDIATE` 单事务写入并校验 → `COMMIT` → 只读重开复检 → 全通过后发布。

## 5. Preview/Approval 的哈希绑定

- `CandidateWritePreview` 输出 `preview_sha256`（排除自身后的规范 JSON SHA-256），并绑定 `plan_sha256`、源库 `database_sha256`、Schema 指纹、标签目录哈希、候选 `path_sha256`。
- `CandidateWriteApproval` 必须同时匹配：`plan_sha256`（当前计划）、`preview_sha256`（写入前立即重新生成的 Preview）、`source_database_sha256`（当前源库且与计划绑定一致）、`app_closed_confirmed is True`。任一不一致在创建任何文件前抛 `WriterApprovalError`。
- Preview 有 blockers 时 `write_candidate_database` 抛 `WriterNotReadyError`（携带 preview）。没有 `force/overwrite/skip-checks` 之类的参数。

## 6. 单事务写入与失败回滚

- 一个事务内：事务内重复/标签复核 → 参数化 INSERT `article(name,note,content,author,translator,uploadTime,source)` → 逐条参数化 INSERT 新 `tag(name,type,cover=NULL,description=NULL)` → 合并现有+新标签 ID（去重、稳定顺序）逐条 INSERT `tagged(artId,tagId)` → 事务内精确回读 article/新 tag/全部 tagged → `PRAGMA foreign_key_check`、重复关联检查、`PRAGMA integrity_check`。
- 任何一步失败立即 `ROLLBACK`，关闭连接，只删除本次调用创建的临时文件；源库与已有外部文件保持不变。使用实际 `lastrowid`，绝不预测 ID 或手工写 `sqlite_sequence`。

## 7. 提交后失败为什么仍不会发布候选

`COMMIT` 之后、发布之前还有一轮只读复检：sidecar 是否残留、只读精确回读、完整 inspector（无 blocker、行数精确等于基线+增量）、源库未变。任一失败即中止，临时文件被清理，最终候选路径保持不存在。

## 8. 目标已存在时为何拒绝而不是覆盖

最终发布使用 `os.link(temp, candidate)` 硬链接（同文件系统、无覆盖语义）：目标已存在时 `os.link` 直接失败。Writer 在 Preview 阶段就会因 `candidate_exists` 产生 blocker；写入阶段对竞态中新出现的文件同样拒绝。清理只针对本次调用明确创建并记录了身份（dev/ino）的文件，绝不删除其他进程的文件。

## 9. Result 证明什么、不证明什么

`CandidateWriteResult` 证明：**候选副本**（新文件）写入完成、事务内与提交后回读精确一致、完整性/外键/重复关联检查通过、源库未变。它**不证明**正式运行库已被替换——本任务根本没有替换正式库的能力，也没有生成任何 SQL 或批量操作。

## 10. 未来 UI 如何调用

1. `load_target_tag_catalog(源库)` 获取标签目录（供 Adapter 生成计划）；
2. `preview_candidate_write(plan, 源库, 候选路径)` 展示 blockers/warnings 与重复证据；
3. 人工确认大书库已关闭后，构造 `CandidateWriteApproval(...)` 并调用 `write_candidate_database(...)`；
4. 展示 `CandidateWriteResult`（或错误），提示用户用大书库打开候选文件人工验收。

## 11. 当前范围

- 只支持向候选副本**新增单篇文章**（含必要新标签与关联）；
- 不支持编辑已有文章、批量写入、正式库替换；
- 不提供最终用户 UI/CLI，不修改 Inbox，不读取 NGA。

## 12. 纯文本 Quill 限制仍然存在

Writer 只接受 TASK-007 渲染器产出的最小 Quill HTML（确定性结构校验，拒绝属性/嵌套标签/脚本/图片/未知实体）。上游 NGA 颜色、图片和富文本尚未进入批准包，仍待未来采集/交接契约扩展。

## 13. 强制终止的已知限制

进程被强制终止可能留下带 `.umalib-writer-` 前缀的临时文件。人工识别方式：文件位于候选目录内且前缀固定；它可能是源库的字节副本，也可能是已完成部分候选写入的工作副本。Writer 不会扫描或自动删除这些文件；由人工确认后手动清理，防止误删其他进程的文件。

## 14. 实际副本烟雾测试（最小披露）

对用户提供的实际副本（82,296,832 字节，SHA `4e152ea3…a54dc`，Schema 指纹 `d7b19669…79aa8`，标签目录哈希 `7759f03c…89fd`）在 `TemporaryDirectory` 中做了一次端到端写入（合成数据）：

- 复用现有标签 id=1（唯一名，未使用重名组 428/429）；新建合成标签 id=451（type=0）；文章 id=7015；
- 计数 article 6983→6984、tag 405→406、tagged 17471→17473（+1/+1/+2）；
- 事务内/提交后回读、integrity_check、外键违规 0、重复关联 0、候选 inspector blocker 0；
- 候选 SHA `791de99f…f100`；
- 源库 SHA/大小/mtime/文件身份/sidecar 前后完全一致；项目与 `umalib-test-data` 目录无新增文件；临时候选随 TemporaryDirectory 自动删除。

验收回归：Writer 定向 `49` 项、项目完整 `293` 项全部通过，无跳过项。验收还覆盖了字符串路径、哈希一致但带预分配 ID 的伪造计划、同标题作者 warning 经批准写入、Preview 末段源库变化、发布竞态、发布后校验失败，以及临时路径被外部文件替换时不误删。

以上数字只证明候选副本写入路径可用，不构成对生产库写入的批准。
