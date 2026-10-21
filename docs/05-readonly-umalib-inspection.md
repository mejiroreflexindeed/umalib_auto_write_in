# 05 · 只读大书库数据库检查工具（inspect_db）操作说明

本文面向不熟悉 Python 的录入/审核人员。`inspect_db` 是一个**只读调查工具**，用来在正式开发“写入器（Writer）”之前，先弄清一个大书库数据库文件“长什么样、结构对不对、有没有隐患”。它只读、只看，**不会导入任何作品，也不会修改、复制或写入数据库**。

## 1. 这是什么、不是什么

- **是**：Writer 开发前的只读调查工具。它检查一个 SQLite 数据库副本的结构与健康状态，产出一份摘要或一段 JSON，供主线复核。
- **不是**：它不是导入工具，不是 Writer，不会往数据库里写任何东西，也不会生成“候选库”“交接包”或任何新文件。
- 每运行一次，它只读取你指定的那个文件，跑完即退出，全程保持只读。

## 2. 空白种子 ≠ 实际运行库

大书库发行包（例如 `umalib-win64-v3.0.2-Final`）里 `resources\prisma\main.db` 是一个**首次启动时复制的空白种子**：

- 它里面没有任何真实文章、标签或关联数据（行数基本为 0）。
- 它**不代表**你正在使用的那份实际运行库；真实数据在大书库自己的运行目录（用户数据目录）里。
- 因此，对空白种子跑出来的结果，只能用来**验证工具本身能否正常跑通**，不能用来判断真实库是否“没问题”。

## 3. 检查真实库前：先关闭大书库，只检查副本

真实运行库随时可能被大书库程序打开和写入。直接检查正在使用的库既不安全、结果也不可靠。所以必须：

1. **先完全退出大书库**（确认进程已结束）。
2. 把真实 `main.db` **复制一份**到别处，例如 `D:\example\main-copy.db`。
3. 只对这份**副本**运行检查命令。

> 检查对象永远是一份**副本**，绝不要把正在使用的正式库文件直接交给命令。副本路径请用你自己机器上的位置，`D:\example\main-copy.db` 只是示例。

## 4. 运行人类摘要（PowerShell）

在项目根目录（能看到 `src` 文件夹的地方）打开 PowerShell，依次运行：

```powershell
$env:PYTHONPATH = "src"
python -B -m umalib_ingest.umalib_adapter.inspect_db --db "D:\example\main-copy.db"
```

- `$env:PYTHONPATH = "src"` 让命令能找到项目的源码包，必须先设置。
- `--db` 后面是要检查的**数据库副本路径**（必填）。
- 命令里的 `python` 请替换为你环境里的 Python 3 解释器（本项目使用指定的 Python 3.10）。
- 输出第一行固定是 `只读检查，不会写入数据库`，随后是文件哈希、Schema 指纹、行数、标签审计、完整性等摘要。

想查看命令用法，可以运行：

```powershell
$env:PYTHONPATH = "src"
python -B -m umalib_ingest.umalib_adapter.inspect_db --help
```

## 5. 运行 JSON 输出

在同样的环境下，多加一个 `--json`：

```powershell
$env:PYTHONPATH = "src"
python -B -m umalib_ingest.umalib_adapter.inspect_db --db "D:\example\main-copy.db" --json
```

- 加了 `--json` 后，程序**只**向 stdout 输出一个稳定的 JSON 对象，不再打印人类摘要。
- JSON 键顺序固定，同一份文件重复跑结果一致，方便比对手工核对。
- 想把它保存成文件，可以重定向：把命令结尾加上 `> D:\example\report.json`（重定向后窗口里就没有输出了，属正常现象）。

## 6. 报告包含什么、不包含什么（隐私）

报告是“最小披露”设计：它只暴露结构和统计，**不包含作品内容**。

**不包含**（任何时候都不会出现）：

- 文章标题（`article.name`）
- 文章备注/摘要（`article.note`）
- 正文（`article.content`）
- 作者（`article.author`）
- 译者（`article.translator`）
- 来源（`article.source`）

正文相关的内容，报告只统计“有多少条正文非空、多少条用了内联颜色/图片/`ql-*` 样式/注释定义”这样的**数字**，绝不输出正文文字本身。

**包含**（必要时用于核对）：

- 数据库文件的 SHA-256 哈希、大小、`user_version`、页大小/页数。
- Schema 指纹（SHA-256），用来判断结构是否与预期一致。
- 各表行数（`article`、`tag`、`tagged`、`creator`、`rec`、`dict`）。
- 标签审计：标签类型计数、重名标签组、空名标签 ID、未知类型标签。
- 完整性结果与 issues 列表。

注意一点：**JSON 里的 `tag_audit.duplicate_names` 会列出“重名标签”的实际标签名**（只在出现重名时才有），这是核对标签联动所必需的。人类摘要则只显示“重名 N 组”，不显示具体标签名。除此之外，空名标签和未知类型标签只暴露 ID/类型，不暴露名称；标签类型计数只显示“类型→数量”。

## 7. `core_contract_match` 通过 ≠ 允许写入

报告里有一项 `核心契约 (core_contract_match)`。它**通过**只表示：这个数据库的 `article`、`tag`、`tagged` 三张核心表的结构与已知契约一致，适合作为调查对象。

它**不等于**“Writer 现在可以写库了”。是否允许写入由未来的 Writer 自己把关（还需要人工确认、关闭正式库、写前校验、写后回读等）。摘要末尾固定会打印这一句提醒：`注意: core_contract_match 通过也不代表已允许 Writer 写入。`

## 8. blocker / warning / info 与退出码

检查会产出带级别的 issues，级别含义：

| 级别 | 含义 |
|---|---|
| `blocker` | **阻断性问题**：核心表缺失/缺列、主键或外键不符、完整性检查失败、外键违规、重复关联等。这类问题会阻止后续写入，必须解决后再谈写库。 |
| `warning` | **需人工审查的异常**：例如缺少非核心表、发现重名标签、空名标签、未知类型标签等，需要人工判断是否影响。 |
| `info` | **可审查差异**：非致命、需要人工留意的差异项。当前版本主要产生 blocker 与 warning，info 作为保留级别。 |

命令的退出码（在 PowerShell 里可以用 `echo $LASTEXITCODE` 查看）：

| 退出码 | 含义 |
|---|---|
| `0` | 检查完成，且**没有 blocker**。 |
| `1` | 检查完成，但**存在 blocker**。 |
| `2` | 参数错误、路径错误或打开数据库失败（此时不会产出报告）。 |

## 9. 红线：不要把唯一正式库拿来开发或测试

- **不要**把唯一的正式 `main.db` 直接当作检查目标、测试样本或开发数据。
- 任何调查、测试、开发都要用**副本**，并且确认正式大书库已经关闭。
- 本工具本身只读、不会写坏文件，但“拿正式库冒险”的习惯会带来误操作风险；请始终用副本。

## 10. 如何把 JSON 结果交给主线复核

`inspect_db` 是本地工具，**不会自动上传任何内容，也不会联网发送任何数据**。把结果交给主线复核，采用**人工传递**：

1. 用第 5 节的方法生成 JSON（或把人类摘要一起截下来）。
2. **手动复制 JSON 文本**，粘贴给负责人（例如粘贴到聊天、邮件或本地文档里）。
3. 不要配置任何“自动上传”、不要点任何“发送到云端”的按钮——本工具没有也不应有联网发送功能。

负责人拿到 JSON 后，可据此判断 Schema 是否一致、有哪些 blocker/warning 需要处理。

## 附：对空白种子的实测输出（参考）

下面是对相邻发行包空白种子 `main.db`（32 KB、8 页）运行后得到的**真实输出**，只用于对照格式。换成你自己的真实库副本后，数字和 issues 会不同。

人类摘要：

```text
只读检查，不会写入数据库
文件: main.db (32768 字节)
文件 SHA-256: e0cef2348d6c73247b30a6079a9e8d19bc0d20b3aeff1728d0644061f1cdf0f4
user_version: 0, page_size: 4096, page_count: 8
Schema 指纹: d7b1966992e75819e37d41ae9c091ec21c2cdf9c5b6727af78f95398d0179aa8
核心契约 (core_contract_match): 通过
行数: article=0 tag=0 tagged=0 creator=1 rec=0 dict=0
标签类型计数: 无
标签审计: 重名 0 组, 空名 0 个, 未知类型 0 个
完整性: integrity_check=ok, 外键违规 0, 重复关联 0
issues: 无
结论: 0 个 blocker, 0 个 warning, 0 个 info
注意: core_contract_match 通过也不代表已允许 Writer 写入。
```

`--json` 输出实际是**单行**，下面按字段折行只是为了便于阅读：

```json
{
  "article_features": {
    "annotation_definition": 0, "background_color": 0, "img": 0,
    "inline_color": 0, "nonempty_content": 0, "ql_class": 0
  },
  "counts": {"article": 0, "creator": 1, "dict": 0, "rec": 0, "tag": 0, "tagged": 0},
  "database": {
    "file_name": "main.db", "page_count": 8, "page_size": 4096,
    "sha256": "e0cef2348d6c73247b30a6079a9e8d19bc0d20b3aeff1728d0644061f1cdf0f4",
    "size_bytes": 32768, "user_version": 0
  },
  "integrity": {
    "duplicate_tagged_pair_count": 0, "foreign_key_violation_count": 0,
    "foreign_key_violations_by_table": {}, "integrity_check": true
  },
  "issues": [],
  "report_schema_version": "1.0",
  "schema": {
    "core_contract_match": true,
    "schema_fingerprint_sha256": "d7b1966992e75819e37d41ae9c091ec21c2cdf9c5b6727af78f95398d0179aa8",
    "user_tables": ["article", "creator", "dict", "rec", "tag", "tagged"]
  },
  "tag_audit": {
    "blank_name_ids": [], "duplicate_names": [],
    "type_counts": {}, "unknown_type_ids": []
  }
}
```

可以看到：空白种子行数几乎全为 0、`issues` 为空、无 blocker，这正好印证第 2 节——它是空白种子，不是实际运行库。
