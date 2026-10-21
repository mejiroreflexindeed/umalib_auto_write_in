# 大书库半自动录入助手

一个独立、本地运行、规则驱动的 NGA 投稿录入工具。程序负责扫描、筛选、字段提取、审核与候选数据库生成；最终业务判断和交付仍由人工完成。

项目不使用 AI/LLM 作为运行时依赖，也不会直接替换正在使用的大书库数据库。

## 当前状态

这是可运行的开发预览版，已经具备以下主流程：

- 配置并低频扫描指定 NGA 帖子；
- 按 `[收录]` 和保留字段确定性解析投稿；
- 在本地 Inbox 中审核、补全、忽略或批准稿件；
- 处理标题、作者、译者、系列、标签、正文和结构化来源；
- 生成 dry-run 预览；
- 在人工确认后，从选定的大书库 SQLite 副本生成新的候选数据库；
- 保存工作区、扫描状态、交付记录和恢复检查点。

真实 NGA 登录扫描和大书库客户端显示仍需要使用者在自己的账号与数据库副本上验证。成对闭合字段语法和 Pixiv 作者名自动补全仍处于规划阶段，不应视为已经实现。

## 系统要求

- Windows 10/11；
- Python 3.10 或更高版本；
- Microsoft Edge；
- 需要 NGA 登录/扫描时安装 Playwright。

## 安装

```powershell
git clone https://github.com/mejiroreflexindeed/umalib_auto_write_in.git
cd umalib_auto_write_in
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[nga]"
```

安装完成后双击 `启动录入助手.cmd`。也可以先做只读环境检查：

```powershell
.\.venv\Scripts\python.exe -B .\tools\launch_operator.py --check
```

## 数据目录

运行数据不会写进源码仓库。默认使用项目同级的 `umalib-private-state`：

```text
父目录\
├─ umalib_auto_write_in\
└─ umalib-private-state\
```

如果源码放在用户主目录内，请先把私有数据目录指定到一个独立位置；不要指向日常 Edge 的用户数据目录：

```powershell
$env:UMALIB_PRIVATE_STATE_ROOT = "D:\UmaLibIngestData"
```

| 变量 | 用途 |
|---|---|
| `UMALIB_PRIVATE_STATE_ROOT` | 登录 profile、扫描状态和默认工作区的私有根目录 |
| `UMALIB_OPERATOR_ROOT` | 只覆盖录入助手工作区目录 |
| `UMALIB_SOURCE_DB` | 默认检测的大书库只读数据库副本 |

没有设置 `UMALIB_SOURCE_DB` 时，程序会尝试检测项目同级的 `umalib-test-data\main-copy.db`；也可以在界面中选择其他 `.db`/`.sqlite` 副本。

## 安全边界

- 只接受明确选择并通过校验的 SQLite 数据库副本；
- 正式写入目标是新候选副本，不原地覆盖源库；
- Writer 在单一事务中写入 `article`、必要的 `tag` 和 `tagged`；
- 写入后执行完整性、外键和精确回读检查；
- NGA profile 必须与日常 Edge profile 隔离；
- 数据库、登录状态、Cookie、工作区、日志、截图和候选输出均被排除在 Git 之外。

## 投稿格式

当前实现使用独立一行 `[收录]` 作为触发标记，随后是字段块和正文。例如：

```text
[收录]
[标题] 示例标题
[作者] 示例作者
[标签] 标签一、标签二
[原作] https://example.com/original
[正文]
正文内容……
```

完整字段与注意事项见 [保留字段说明](docs/24-reserved-fields-guide.md)。

## 开发与测试

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

| 路径 | 内容 |
|---|---|
| `src/umalib_ingest/` | Parser、Inbox、审核、Watcher、Adapter、Writer、工作区与本地 Web UI |
| `tests/` | 单元测试、集成测试和合成 fixtures |
| `contracts/` | 模块间 JSON Schema 契约 |
| `tools/` | 启动、环境检查、验收和打包工具 |
| `docs/` | 架构、数据库兼容和操作说明 |
| `demo_samples/` | 不含真实账号或数据库内容的演示输入 |

核心设计说明见 [项目简报](docs/00-project-brief.md)、[大书库兼容契约](docs/01-umalib-contract.md) 和 [V1 产品规格](docs/02-v1-spec.md)。
