# 大书库 3.0.2-Final 兼容契约

本文记录已经从当前发行包代码和 SQLite Schema 中确认的事实。除非出现新的代码或真实数据库证据，不得把推测写成既定规则。

## 运行架构

- 大书库是 Electron 19.1.9 桌面应用。
- 前端为 Vue 2.7.16、Element UI 2.15.14、Quill 1.3.7。
- Electron 主进程通过私有 `artChannel` IPC 调用 Prisma Client 3.15.2。
- 数据库为 SQLite。
- 没有公开 HTTP API，也没有文章 JSON/CSV 导入器。
- `resources/prisma/main.db` 是首次复制用的空白种子；默认运行库位于 `app.getPath("userData")/main.db`。
- 用户选择外部 DB 后，发布会直接修改该文件；下次启动重新连接默认运行库。

## 业务表

### `article`

| 字段 | 已确认语义 |
|---|---|
| `id` | 自增作品 ID |
| `name` | 标题 |
| `note` | 备注/摘要，不是正文注释 |
| `content` | Quill HTML 正文 |
| `author` | 作者字符串；多人约定使用 `/` |
| `translator` | 译者字符串；多人约定使用 `/` |
| `uploadTime` | Unix 秒；UI 使用毫秒并在保存时换算 |
| `source` | 多个来源按 UI 顺序用单个空格拼接 |

### `tag`

| 字段 | 已确认语义 |
|---|---|
| `id` | 自增标签 ID |
| `name` | 标签名；数据库没有唯一约束 |
| `type` | 0 其他、1 角色、2 系列、3 长篇/合集、4 争议/不适 |
| `cover` | 长篇/合集显示封面 |
| `description` | 长篇/合集描述 |

### `tagged`

- `id`：自增关联 ID。
- `artId`：外键到 `article.id`，`NO ACTION`。
- `tagId`：外键到 `tag.id`，`NO ACTION`。
- 数据库没有 `UNIQUE(artId, tagId)`。

### 其他表

- `creator`：版权贡献者列表和隐藏口令，不是作者主数据表。
- `rec`：推荐数据。
- `dict`：用语解释和扩展阅读。
- 新增普通文章不应修改 `creator`、`rec` 或 `dict`。

## 现有发布逻辑

当前 `pubArticle` 会：

1. 空作者改为 `匿名`。
2. 毫秒时间四舍五入转换为 Unix 秒。
3. 对来源做少量 Pixiv/tsumanne 修正后，以空格拼成 `article.source`。
4. 空备注从正文去标签后自动生成摘要。
5. 新建时写 `article`，编辑时更新 `article`。
6. 按标签名称 `findFirst`；不存在则创建 `type=0` 标签。
7. 删除不再选择的 `tagged`，创建新增关联。

这组操作没有跨表事务、发布前备份、重复检查或完整性检查。新工具只复刻兼容语义，不复刻这些安全缺陷。

## 正文契约

- `article.content` 保存 Quill HTML，不是 Markdown、纯文本或 Delta。
- 颜色通常保存为内联 `color`、`background-color`。
- 字号、对齐等可能使用 `ql-*` class。
- 阅读端以 `v-html` 渲染，因此外部 HTML 必须经过严格白名单转换。
- `[关键词]注释` 定义保存在正文末尾的连续段落中，形式近似 `<p>[关键词]注释文本</p>`。
- 注释定义不是 `article.note`，也没有独立注释表。

## 来源契约

大书库没有以下独立字段：

- 原作系列地址
- 原作地址
- 译作系列地址
- 译作地址
- NGA 楼层地址

这些来源在录入工具内部必须保持结构化；只有 `umalib_adapter` 可以按照最终业务优先级将它们压平成 `article.source`。

当前大书库代码没有 NGA 楼层 URL 标准化、`&to=1` 清理或四类来源优先级逻辑。

## Writer 强制条件

- 只接受用户明确选择的目标 DB。
- 正式大书库应用必须关闭。
- 写前检查 Schema 指纹和文件状态。
- 默认从目标生成新的候选副本。
- 在单个事务内写 `article`、必要的新 `tag` 和 `tagged`。
- 发现同名多标签、Schema 漂移或未知标签类型时阻断人工处理。
- 写后执行 `integrity_check`、`foreign_key_check`、重复关联检查和精确回读。

## 尚未确认

- 正式生产库是否与空白种子完全同构。
- 真实标签名称、联动和重名情况。
- “系列”字段应映射 `type=2` 还是 `type=3`。
- 来源最终优先级。
- NGA 楼层 URL 正式规范化算法。
- 历史作品中颜色、图片和注释的实际变体。
- 多楼层、重复和来源冲突的人工判断标准。

