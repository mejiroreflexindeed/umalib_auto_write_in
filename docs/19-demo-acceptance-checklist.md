# 19 · Demo 验收清单（TASK-013）

本清单对应任务书 T01-T90 行为矩阵；执行命令见 `tools/demo_acceptance.py`。

## 本地必做（local_demo）

- [x] T01-T06 合成骨架：`--help`/`--check` 无副作用；合成 Schema 与 Writer 兼容；
  6 份虚构样例；缺 Playwright 仍可手工演示（离线演示不依赖浏览器）；路径含空格
  启动定位正确（`%~dp0` + 引号）。
- [x] T07-T14 工作区：manifest 严格校验；路径穿越拒绝；系统文件锁（第二进程无法
  取锁、进程退出后 OS 释放）；原子写失败保留旧内容；CAS 拒绝陈旧修改；choices
  重启保留且改稿标 stale；mode/source alias 服务端映射，浏览器不能注入路径。
- [x] T15-T26 格式引擎：纯文本投影/嵌套/颜色规范化/非法值阻断/栈深/未知注释
  保留/末尾注释精确输出/含格式注释阻断/不支持格式保留/首段格式不吞元数据/
  字段块提取/单次 entity 解码。
- [x] T27-T32 Adapter/Writer：旧调用纯文本行为不变；formatted_body 不一致阻断；
  格式变化改变 plan SHA；真实 Writer 写入并精确回读基本富文本；恶意 HTML 拒绝；
  注释与 note 分离。
- [x] T33-T40 来源/标签：tid+pid 规范化去 to/page；仅 tid 警告；冲突/非法 ID/
  userinfo/端口阻断；外链保留；空白提示不截断；唯一/零/多重标签路径；系列不猜；
  HEAD 变化保留用户输入并标需复核。
- [x] T41-T48 累计写入：A/B/C 累计三篇；复用前篇新标签不重复建；旧 revision 拒绝；
  同 ID 重试只写一次；同 ID 换请求 conflict；preview 无副作用；原库/旧候选不变。
- [x] T49-T56 恢复：6 个真实 killpoint（子进程终止）分别恢复/阻断；无候选可放弃；
  孤立候选不自动接 HEAD；VERIFIED 补 exported/HEAD 不重复 INSERT；exported 补 HEAD
  不重复历史；HEAD 补日志不二次 revision；篡改阻断；pending 无日志阻断新写。
- [x] T57-T64 UI：真实浏览器驱动本地 HTTP+SQLite 走通导入→保存→批准→preview→
  生成；dirty 保护；错误保留输入；1280×720/1920×1080 截图检查；刷新后交付列表
  来自持久状态。
- [x] T65-T70 收集/模拟扫描：字节级假 transport 经真实 fetch→解析→Watcher→Inbox；
  讨论/缺字段正确分类；第二次更新产生 changed_after_import 不覆盖；模拟与真实
  隔离；real_copy 拒绝模拟扫描；取消/登录生命周期保持旧语义。
- [x] T71-T76 检查点：空闲一致备份（SQLite backup）；活跃操作 busy；清单不含
  profile/snapshots/递归检查点；恢复新工作区可继续写入；校验失败保护原区。
- [x] T77-T82 双击与交付包：两个 CMD 入口按 mode 恢复工作区；端口冲突/占用友好
  报错；关闭不遗留线程/浏览器；源码包 allowlist 无真实数据；新目录解包验证。
- [x] T83-T86 自动验收：core（真实服务三篇累计+重启续写第四篇）/ui（真实浏览器）/
  recovery（6 killpoint）四阶段全 PASS；旧 519 项与 night 实验室全过；
  UNAVAILABLE 不记 PASS。

## 实际副本派生（real_source_readback）

- [x] T87-T90：main-copy.db 只读指纹（article 6983/tag 405/tagged 17471，Schema
  指纹 d7b19669…）；三篇虚构验收稿 A/B/C 累计派生，最终候选 +3 article/+1 tag/
  +3 tagged；精确回读三篇标题；原库/旧候选与目录不变；查看副本与权威候选分离。

## 现场检查

- [x] nga_online：**PASS**（2026-09-03）：预览尾页 173/39 楼层；确认"从现在开始"
  基线；真实 scan 页 [173]、20 unchanged、0 新增/触发/导入；导航 4 次 ≤ 6 预算；
  登录态有效无需交互；新隔离 Inbox/state/snapshots；canonical state 未动。
- [ ] umalib_visual：**WAITING_USER** —— 用户在真实大书库打开查看副本核对三篇
  验收稿（标题/作者/标签/来源/正文），核对表见
  `demo-013-acceptance-8a4d62c1/m14/umalib-visual-check.md`。

## 状态字段

- local_demo：PASS
- real_source_readback：PASS
- nga_online：PASS
- umalib_visual：WAITING_USER（用户核对后更新）
- overall：DEMO_OFFLINE_READY（两项现场检查完成且无缺陷后为 DEMO_ACCEPTED）
