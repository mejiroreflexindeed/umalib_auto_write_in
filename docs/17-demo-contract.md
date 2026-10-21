# 17 · Demo 契约（TASK-013）

## 1. 工作区（workspace）

- 工作区根：`D:\codex\13\umalib-private-state\demo-013-user\`（测试可用
  `UMALIB_DEMO_ROOT` 覆盖）；一进程只打开一个工作区，切换需退出重启。
- `workspaces/<uuid>/workspace.json`（schema_version "1.0"）字段：
  schema_version、workspace_id、name、mode（synthetic/real_copy）、
  base_source_id（synthetic/main-copy）、base_sha256、revision（仅 HEAD 成功
  前进时 +1）、head（`{operation_id, candidate_file_name, sha256}` 或 null）、
  pending_operation_id、created_at/updated_at、restored_from、
  history_workspace_ids。严格类型校验，拒绝未知键/非整数 revision。
- 目录：inbox.db、base.db（仅 synthetic）、choices/、operations/、
  candidates/、deliveries/、watcher/state.json、watcher/snapshots/、
  checkpoints/、instance.lock（系统文件锁，进程退出由 OS 释放，不靠删文件修复）、
  restore-provenance.json（仅恢复工作区）。
- 所有持久 JSON：UTF-8、固定键序规范序列化；原子替换只用于本任务自有元数据；
  失败保留旧版本。

## 2. 累计候选 lineage

```text
只读基础库 -> 候选 A（+稿1）-> 候选 B（+稿2）-> 候选 C（+稿3）
               永不覆盖         永不覆盖         最新交付库
```

- 每次写入：operation UUID → `candidate-<operation_id>.db`；HEAD 指向最新已验证
  候选；历史候选不变；候选 basename 后端重算核对。
- 操作记录阶段：PREPARED → CANDIDATE_VERIFIED → COMMITTED（/ABANDONED）；
  记录保存规范请求指纹（含全部人工 choices）、计划/预览/结果 hash 与完整
  Writer result；重试按原请求规范字段比对（不重建批准包）。
- 恢复动作仅 `complete` 与 `abandon_unverified`；`abandon_unverified` 保留孤立
  文件、只适用于未验证且未 exported 的操作。
- 检查点：明确清单 + 逐文件 SHA；Inbox 用 SQLite backup；恢复新建
  workspace_uuid，原字节不改（历史 operation/choices/result 原样），
  `history_workspace_ids` 由已验证清单派生。

## 3. 格式协议（formatting）

- 支持：普通文本、空段、`[b][i][u][s][color=#RGB/#RRGGBB/命名色]` 及正确嵌套；
  深度上限 16；未知 `[关键词]注释` 与不支持格式（图片/附件/表格等）保留原文并
  报 diagnostic；非法颜色/交叉/未闭合为 blocker。
- 渲染：`<p>`、`<p><br></p>`、`<strong><em><u><s>`、
  `<span style="color: #rrggbb;">`，固定嵌套顺序 strong/em/u/s/span；文本转义；
  结尾连续 `[关键词]注释` 段输出朴素 `<p>`，段内含格式则 blocker
  `annotation_format_unsupported`。Writer 独立校验同一子集（保留旧纯文本语义）。
- `AdapterChoices` 扩展：`content_mode`（plain/nga_ubb，默认 plain）+
  `formatted_body`；nga_ubb 模式 plain_text 必须与批准正文等价（仅 CRLF 归一化），
  否则 blocker `formatted_body_mismatch`；旧调用不传新字段行为不变。

## 4. Demo API（仅在 Demo 模式注册）

| 方法/路径 | 说明 |
|---|---|
| GET /api/demo/status | 工作区/HEAD/维护/恢复摘要（无凭证/路径） |
| GET /api/demo/samples | 样例清单 |
| POST /api/demo/samples/import | 导入样例（真实 Parser/Inbox） |
| POST /api/demo/import | 粘贴原文导入 |
| GET/PUT /api/demo/items/<id>/choices | 人工决定（绑定工作稿指纹） |
| POST /api/demo/format-preview | 原文提取/格式文本纯预览 |
| POST /api/demo/source-preview | 来源规范化纯预览 |
| GET /api/demo/deliveries | 持久交付列表 |
| GET /api/demo/operations/<uuid> | 操作状态与可执行恢复动作 |
| POST /api/demo/operations/<uuid>/recover | 恢复/放弃（confirmed=true） |
| POST /api/demo/checkpoints | 创建检查点 |
| GET /api/demo/checkpoints | 检查点列表 |
| POST /api/demo/checkpoints/<uuid>/restore | 恢复到新工作区 |
| POST /api/demo/simulated-scan | 离线模拟扫描（仅 synthetic） |
| POST /api/demo/nga/baseline-preview | 真实 NGA 基线建议（需 --enable-nga） |
| POST /api/demo/nga/baseline-confirm | 确认"从现在开始" |

## 5. 边界与不支持

- 真实 NGA 仅 fixed tid=30099686；基线"从现在开始"不补历史；每次程序导航 ≥3 秒；
  本轮在线检查预算 6 次顶层导航。
- 不自动合并多来源、不猜标签/系列类型、不自动替换正式库、无调度器。
- 交付包是"当前环境源码运行包"，不是独立 EXE；解压后仍需本机 Python 3.10+ 与
  项目 .venv；路径校验绑定本机 private-state 目录。
