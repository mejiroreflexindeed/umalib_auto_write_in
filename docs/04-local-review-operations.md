# 本地审核台操作说明

本文面向不熟悉 Python 的录入人员。审核台是一个**完全本地**的工具：日常审核只操作独立 Inbox；启用 Writer 后会只读检查你在命令行明确指定的数据库副本，并只生成新的候选数据库。它**不会**访问或写入“大书库”正在使用的正式 `main.db`。

## 1. 重要概念

- 日常审核只操作独立 Inbox 数据库；Writer 只读使用你明确指定的源数据库副本，与大书库正在使用的正式 `main.db` 分离。
- 请**不要**选择、复制或填写大书库安装目录里的 `main.db` 作为 Inbox 路径。
- 页面里的正文预览只是“审核文本预览”，不是最终大书库 HTML 预览。
- “批准交接包”是交给 Writer 的输入预览；**Writer 只会生成新的候选数据库文件，绝不会写源库或替换正式库**。
- 启用 Writer 时（启动命令带 `--umalib-db` 与 `--candidate-dir`），源数据库全程**只读**，候选只写入你预先建好的输出目录。

## 2. 首次创建 Inbox 并启动（仅审核模式）

在 PowerShell 中进入项目目录后运行（路径只是示例，请换成你自己的位置）：

```powershell
$env:PYTHONPATH = "src"
New-Item -ItemType Directory -Force .\inbox | Out-Null
python -B -m umalib_ingest.web --db .\inbox\review.db --init
```

- `--db` 指定 Inbox 文件位置（必填）；
- `--init` 表示该文件不存在时创建并初始化；
- 默认端口 8765，可加 `--port 9000` 换端口（0-65535）；
- 不提供 `--umalib-db` / `--candidate-dir` 时只有审核功能，页面 Writer 区显示“启动时未配置”。

## 2.1 启用 Writer 的启动方式

```powershell
$env:PYTHONPATH = "src"
New-Item -ItemType Directory -Force .\candidate-out | Out-Null
python -B -m umalib_ingest.web --db .\inbox\review.db `
  --umalib-db "D:\example\main-copy.db" `
  --candidate-dir .\candidate-out
```

- `--umalib-db` 与 `--candidate-dir` 必须**同时提供**；源库只读、绝不能是 Inbox 本身；
- 候选输出目录必须**预先存在**（程序不会自动创建）；候选文件名在页面填写，只会写新文件、绝不覆盖已有文件；
- 源库与输出目录在启动时固定，页面或 HTTP 请求不能改。

## 3. 再次打开已有 Inbox

```powershell
$env:PYTHONPATH = "src"
python -B -m umalib_ingest.web --db .\inbox\review.db
```

- 不需要 `--init`；程序会检查数据库 Schema 是否合法。
- 如果路径不存在（且没带 `--init`）、文件不是合法 Inbox、端口被占用或端口号非法，程序会明确报错退出，不会覆盖任何文件。

## 4. 打开页面与关闭

- 启动后终端会打印形如 `http://127.0.0.1:8765` 的地址，用浏览器打开即可。
- 服务只监听本机 `127.0.0.1`，局域网内其他电脑无法访问。
- 关闭：回到 PowerShell 窗口按 `Ctrl+C`。

## 5. 日常操作顺序

1. **粘贴投稿**：在页面顶部“导入投稿”区粘贴 NGA 楼层原文（包含 `[收录]` 标记）；NGA 楼层 URL 可填可不填，填了会同时保存到采集上下文和可编辑的“NGA 来源”。
2. **检查字段**：在 Inbox 列表点开记录，核对标题、作者、译者、系列、标签、五类来源和正文；缺失字段、warnings、unknown_fields 会在“诊断”区显示。
3. **保存**：修改后点击“保存修改”。正文逐字保存，不会改动换行或注释。
4. **填原因并批准**：在“改变状态”里选择目标状态、填写原因（必填），点“改变状态”。必要字段缺失时不能批准（页面会提示）。
5. **查看交接包**：批准后详情区会显示“批准交接包”的哈希和只读 JSON 预览。

## 6. 状态含义

| 状态 | 含义 |
|---|---|
| `candidate` | 已识别投稿，等待审核 |
| `incomplete` | 缺少必要字段 |
| `suspected_duplicate` | 人工标记为疑似重复 |
| `approved` | 人工确认通过 |
| `exported` | 已导出（本版本不会出现在可选状态里） |
| `ignored` | 人工忽略或不是投稿 |

- `approved`、`exported`、`ignored` 记录为只读，不能直接修改；`approved`/`ignored` 可填写原因后退回，`exported` 是终态，不能再转换。
- 页面不提供 `exported` 选项；只有 Writer 成功生成候选副本并验证通过后，Inbox 状态才会自动变为 `exported`。

## 7. 未保存修改保护

- 修改任一审核字段后会出现“有未保存修改”提示。
- 有未保存修改时，点击其他记录、导入新投稿或改变状态会被阻止，避免静默丢失。
- 可以点“保存修改”保存，或点“放弃未保存修改”恢复最近一次后端数据后继续。
- 保存失败时提示会保留，你的输入不会被清空。

## 8. 列表搜索

- 状态筛选旁有“本地搜索”框，可按标题、作者模糊过滤当前列表。
- 搜索只影响显示，不访问网络、不修改数据库；清空即恢复全部。

## 9. 常见错误

- **Inbox 数据库不存在**：路径写错，或首次启动忘了加 `--init`。
- **错误数据库**：指定的文件不是本工具创建的 Inbox（例如选成了大书库的 `main.db`），程序会拒绝启动。
- **端口被占用**：换一个端口，例如 `--port 9000`。
- **字段缺失不能批准**：先把标题、作者、标签和正文补全再批准。
- **有未保存修改时操作被阻止**：先保存或放弃。
- **Writer 未配置 / 源库不可用**：启动命令缺 `--umalib-db`/`--candidate-dir`，或源库路径/状态不对（不存在、不是数据库、有 sidecar、Schema 不符），程序会拒绝启动或拒绝生成预览。
- **候选已存在**：页面会阻止并提示，绝不覆盖已有文件；换个候选文件名即可。
- **提示“正式库未被替换”**：这是正常提醒——候选文件生成后，需要你自己用大书库**打开候选文件**人工验收，本工具不会替你做这一步。

## 10. 红线

- 不要把大书库的 `main.db` 当作 Inbox 使用。
- 源库永远只读；候选只写新文件；本工具**不会**替换、移动或覆盖任何正式数据库。
- Watcher（扫描 NGA 固定帖子）已集成到审核台：带 `--watcher-state / --watcher-profile / --watcher-snapshot-dir` 启动后，页面出现“扫描 NGA”面板；操作与恢复见 `docs/12-watcher-win7-operations.md`。扫描只把含 `[收录]` 的楼层放进 Inbox，不批准、不写大书库。
