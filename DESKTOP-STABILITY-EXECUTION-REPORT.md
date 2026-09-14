# DESKTOP-STABILITY-EXECUTION-REPORT.md
# 桌面版稳定性移植与 1.1.0 候选交付 — 执行报告

执行日期：2026-09-14。交接文档：`deliveries/WORKBUDDY-DESKTOP-STABILITY-HANDOFF.md`，
配套清单：`deliveries/DESKTOP-STABILITY-PORT-CHECKLIST.md`。
参考仓库：`bilibili-live-server`（只读，固定 26c170c）；未改动参考仓库，
未连接真实直播平台，未发送任何真实邮件/推送。

## 1. 起始/最终提交、变更与清单映射

- 起始基线：`48e52f12047d1ec1e808e30a918f15f43134385b`（main，与远端一致）
- 候选分支：`stability-desktop-port-20260914`（**扁平名**：本环境创建
  含 `/` 的分支引用会静默失败，见第 7 节环境问题）
- 提交链：
  - `e50cdca` backend：A1–A8 稳定性移植
  - `4a44e3b` electron：E1/E2/E3 生命周期与数据目录
  - `7518713` frontend：A3 票据协议/同实例重试/AbortSignal、A7 托盘退出
  - `691aa91` test+build：测试套件、耐久脚本、E4/E5 发布配置
  - `3a0dd6a` gitignore；`b2b3727` 探测文件修复 + CHANGELOG
- 变更规模：后端 core/api/run ~1100 行改动 + 2 个新模块
  （data_migration.py、backendManager.ts、operationToken.ts），测试 ~1900 行。

| 清单项 | 状态 | 位置/证据 |
| --- | --- | --- |
| A1 请求有界/可取消 | 完成 | `BilibiliApi._req` 面板 3 次有界、后台可取消无限重试；构造器网络工作移入 `_bootstrap` 后台线程；`test_a1_request_bounds.py` 4 项 |
| A2 控制代际 | 完成 | `_control_epoch`/`issue_operation`/`begin_control_operation`/`claim_stop_operation`；开播后台线程 + 平台返回后复核 + 撤销房间；`test_a2_control_epoch.py` |
| A3 前端票据/重试 | 完成 | `operationToken.ts` + `request.ts` 同实例递归重试、AbortSignal、取消不重试、409 stale 不换票重发；Electron localhost baseURL 保留 |
| A4 监控语义 | 完成 | 查询失败只记未知+60s 节流提示，不再重开；`_retry_start_live` 按实际推流方式恢复（manual+FFmpeg 可恢复，OBS 不碰）；`test_a4_monitor_semantics.py` |
| A5 进程所有权 | 完成 | 删除全局 taskkill /IM；list argv 直持 PID；`_claim/_release_pusher` 代际所有权；回收失败保留引用并阻止重复创建；`test_a5_ownership.py`（真实子进程+进程树回收） |
| A6 FFmpeg 参数 | 完成 | `build_ffmpeg_command` 纯函数，flvflags 在输出 URL 前（copy/reencode）；无 rtmp_buffer/genpts；编码参数未动；`test_a6_ffmpeg_options.py` |
| A7 停止意图/迟到回调 | 完成 | `stop_intent.json` 持久化、开播清除；邮件/远程人脸确认绑定代际（`retry_after_face_verify_guarded`）；托盘退出移至 App.vue；桌面"重开提示继续不自动开播"语义保持 |
| A8 日志/数据路径 | 完成 | backend.log RotatingFileHandler(5MB×3)；ffmpeg.log 超限轮转；统一数据目录+迁移 |
| E1 freePort | 完成 | `parseNetstatListeners`+`classifyPortConflict`：仅回收 PID 文件匹配的自有旧后端；未知占用弹窗报错阻断启动 |
| E2 后端生命周期 | 完成 | `BackendLifecycle` 状态机；restart 先回收后拉起；close 代际核对；quitting 不复活；`/api/shutdown` 改为真正优雅退出（uvicorn should_exit）；`backendManager.test.mjs` 7 项 |
| E3 数据目录 | 完成 | userData/data 经 `--data-dir` 传入 + cwd 设为数据目录；旧数据带备份迁移（不覆盖新数据）；打包种子分区表首启复制 |
| E4 版本/publish | 完成 | 根+前端版本 1.1.0；publish → `Amicaly98/live-manager` |
| E5 后端分发 | 完成 | `backend/run.spec` PyInstaller 单文件 run.exe（37MB，内置分区表种子）；electron-builder extraResources 装载；打包链 `npm run build:backend` |
| E6 安装包验证 | 部分完成 | NSIS+portable 构建成功；win-unpacked 实机启动验证（见第 4 节）；Setup.exe 安装流程与中文空格路径安装未实测（第 6 节） |
| 诊断 | **延期** | 按清单第 3 步选项 B 整体延期：未接进度 PIPE、无新增诊断参数/线程/文件；无半套管道遗留。后续版本按"消费者生命周期成套"要求单独实现 |
| macOS | 不适用 | 未构建未测试，不宣称支持 |

## 2. 保留的桌面行为 / 未移植的服务器行为 / 新风险

**保留**：桌面默认"重开后提示继续，不自动开播"；任务公式/优先级/随机
时长/跨日结算/数据库 schema 未动（`task_manager.py` 仅加 `wait_for_reset_complete`
的 cancel 参数与 last_run 文件路径）；Hash 路由、electronAPI/托盘/选文件、
`base:'./'` 布局均保留；无面板密码/panel_token。

**有意不移植**：服务器开机自动恢复（auto_resume）与 `.no_auto_resume`
标记语义（桌面用 stop_intent.json 实现同等保护但语义为"停止后仅提示继续"）；
systemd/TLS/面板密码/OSS 探测/主机采样/多实例注册；服务器 5 秒退避重连
风暴策略**原样保留**（未宣称修复）；不搬无生产调用的监督探针。

**本次引入的新风险**：
1. 打包版 run.exe 直击双击启动时数据目录回退到临时解包目录（Electron
   正常路径始终传 --data-dir，不受影响）——低风险，后续可加默认目录探测。
2. 停止撤销逻辑依赖 `stop_live` 的平台响应（失败仅告警，本地进程必然
   停止）——与旧版一致，无回退。
3. 前端 409 stale 提示依赖后端 detail 文案包含"响应已丢失"（与服务器
   版一致的稳定标识）。

**原有风险（非本次引入）**：长会话异常退出的 5 秒退避（清单第 5 节，
服务器也未修复）；服务器 Windows 句柄范围 19>8 的未决断言（见第 5 节
桌面实测归属）。

## 3. 反例测试：旧版行为 vs 新版行为

| 反例场景 | 旧版结果 | 新版结果（测试） |
| --- | --- | --- |
| 面板查询遇断网 | `_req` 无限重试占死调用方（事件循环/线程） | 3 次有界后返回 `retryable`（test_a1） |
| 后台开播遇断网 + 用户停止 | 等待不可打断，停止后仍可能开播 | `cancel.wait` 立即打断，返回 cancelled（test_a1） |
| 慢开播期间停止 | 平台返回后照常开播/占用状态 | 代际复核丢弃结果并 `stop_live` 撤销房间（test_a2） |
| 旧停止重放（响应丢失+重试） | 再次执行下播，停掉新直播 | `claim_stop_operation` 返回 False，仅确认（test_a2/API 测试） |
| 停止后重放旧开播票据 | 被接受执行 | `begin_control_operation` 返回 None → HTTP 409（test_api） |
| 后端重启后旧票据 | 未知/可能被接受 | boot 前缀不匹配 → `foreign_boot_ticket` 拒绝（test_a2） |
| 状态查询失败 | 进入重开路径，掐掉健康本地流 | 只计数+节流提示，零重开（test_a4） |
| 手动分区+FFmpeg 掉线 | `_stream_mode != 'manual'` 阻止恢复 | 按设置恢复推流（test_a4） |
| 全局杀 ffmpeg.exe | 外部 FFmpeg/OBS 被误杀 | 已删除；真实子进程测试证明外部进程存活（test_a5） |
| 推流进程杀不掉 | 引用被清空，可再开一路同房间推流 | 保留引用 + `_ffmpeg_unrecycled` 阻止重复创建（test_a5） |
| 旧循环 finally 清引用 | 可能清掉新代推流引用 | 代际核对，新代引用保留（test_a5） |
| flvflags 位置 | 输出 URL 后（trailing option 告警） | URL 前（test_a6 断言） |
| 用户停止+重开 | 重开可能自动恢复 | stop_intent 持久化，仅提示继续（test_a7） |
| 停止后迟到的邮件验证确认 | `_retry_after_face_verify` 重试开播 | 代际守卫拒绝（test_a7） |
| freePort 杀未知监听者 | taskkill 任何占 8000 的 PID | 仅自有旧后端可回收，未知→报错（Node 测试 classifyPortConflict） |
| restart-backend 未 await | 旧进程未回收即拉新代 | `BackendLifecycle.restart` 等待回收（Node 测试） |
| 未回收时重启 | 产生双后端 | 拒绝拉新代、引用保留（Node 测试） |
| quitting 期间后端崩溃 | 5 秒后自动复活 | 不复活（Node 测试） |

实际测试命令与日志路径：
- 后端：`.venv-test/Scripts/python.exe -m pytest backend/tests -q --ignore=backend/tests/electron`
  → **40 passed**（最终记录 `/tmp/pytest7.log`，此前多轮 39–40 passed 的
  修复过程见提交链）
- Electron/生命周期：`node --test backend/tests/electron/backendManager.test.mjs`
  → **7 passed**（`/tmp/node_test2.log`）
- 前端：`vue-tsc --noEmit`（升级 vue-tsc@2 后通过，**不是** tsc 冒充）+
  `vite build` 通过（`/tmp/vue_tsc3.log`、`/tmp/fe_build2.log`）
- Electron TS：`tsc -p electron/tsconfig.json` 通过
- 原始日志按时间戳保留：`backend/tests/_endurance_results/*.json`、
  `/tmp/pytest*.log`、`/tmp/node_test*.log`、`/tmp/lc_*.log`

## 4. 数据迁移与退出/恢复证据；打包应用实测

- **迁移**：`test_migration_copies_with_backup_and_never_overwrites`（备份
  目录创建、新数据保留）；实机验证：源码后端在新数据目录启动时把仓库根
  74 任务旧库带备份迁入（日志"数据库已有数据，跳过迁移"，原文件不动）。
  测试隔离用 `BILIBILI_SKIP_MIGRATION=1`，避免真实 cookies 被复制进测试目录。
- **退出/恢复**：打包后端 10 轮真实启停全部 `exit=0`（优雅退出）+ 零残留；
  `/api/shutdown` 修复为保存状态后触发 `uvicorn.should_exit`（旧版只存盘
  不退出，靠强杀兜底——本次发现并修复的真实缺陷）。
- **打包应用实测**（win-unpacked，1.1.0）：
  - 内置 run.exe 被正确拉起：`Starting backend: ...resources\backend\run.exe
    --data-dir C:\Users\chenz\AppData\Roaming\bilibili-live-manager\data`；
  - 后端健康 `{"status":"ok","task_manager":true,"live_controller":true}`；
  - 数据目录实际创建：backend.pid、bili_areas_full.json（种子复制）、
    live_tasks.db、logs/；
  - **file:// 前端加载成功**：日志可见前端经 CORS 持续轮询
    `POST /api/auth/poll/... 200`、`GET /api/health 200`；
  - **未验证**：打包应用的窗口交互（开播/停止点击、托盘退出、导入导出
    对话框）——本环境无可用 GPU 会话，Electron GPU 进程致命退出
    （`GPU process isn't usable`，`--disable-gpu` 亦同），属执行环境限制；
    后端链路以上述日志为证。**不能据此宣称打包 UI 已完成人工验收。**

## 5. 耐久/资源数据与源码哈希

- 脚本：`backend/tests/lifecycle_endurance.py`
  （sha256 `bd94e4de730dd1a362a818205d108675a1ba25f2291a04efe670c1cb0a7ea0df`；
  conftest `bb13f41e…`；产物哈希见 `backend/tests/_endurance_results/TEST-SOURCE-HASHES.txt`）
- **生命周期**：10/10 轮 health=True、exit=0（优雅退出）、零进程残留
  （`lifecycle-20260914-165609.json` 等；隔离数据目录、`BILIBILI_SKIP_MIGRATION=1`）。
- **20 分钟真实 FFmpeg 耐久**（`endurance-20260914-170436.json`）：
  - 负向自测先行：kill 推流后字节数停止增长（105092 → 105092），
    **判据能报失败**——夹具不掩盖故障；
  - 正式一路真实 FFmpeg（lavfi 测试源→MPEG-TS→本地 TCP 接收端）连续
    20 分钟：40 个采样点，RSS 稳定 ~134MB（pre/steady 全程无增长趋势），
    累计推送 78,476,276 字节，throughput_ok=True，进程全程存活。
- 判据说明：合成测试源编码复杂度低，实际码率低于 1500kbps 目标，故
  throughput 阈值按"持续真实推进"（≥30KB/s）设定；断推自测已单独证明
  判据能报失败，不存在"夹具补推掩盖故障"。
- **句柄/线程口径**：tasklist CSV 的句柄列在本环境解析不可靠（显示为 1），
  RSS 与进程存活性可信；服务器遗留的"Windows 句柄范围 19>8"断言在此
  **不直接豁免**——桌面口径下以"自建进程零残留 + RSS 平稳 + 10 轮启停
  优雅退出"为通过依据，句柄级波动归因留待有真实 GUI 会话的环境复测。

## 6. 产物、版本、发布与回退

- **安装版（NSIS）**：`release-1.1.0/直播控制系统 Setup 1.1.0.exe`
  SHA256 `cb4af90cb05e3925f4bae8cb8a67514b9a5467459e3dc5c18905e0422e095736`
- **便携版（portable）**：`release-1.1.0/直播控制系统 1.1.0.exe`
  SHA256 `4d106009455a73f3e30caf8d43c87cf4494c57ee81b209aa5ac0df69ba5804ed`
- 内置后端 `backend/dist/run.exe` SHA256 `4edba0a0e671cedb9ad44289e2c18d71d0560fb499c3af05c95ea22c59e5c386`
- 汇总：`release-1.1.0/SHA256SUMS.txt`；构建环境：electron-builder 24.13.3 /
  Electron 28.0.0 / PyInstaller（Python 3.13.12 venv）
- 版本一致性：根 package.json 1.1.0、frontend 1.1.0、app FastAPI version
  1.1.0、安装包文件名 1.1.0、latest.yml 同版
- 产物内容核对：asar+resources 仅含 dist-electron/frontend dist/assets/
  run.exe；**无** cookies/令牌/数据库/日志/测试缓存被打包（源码运行的
  散落数据文件不在 build.files 白名单内）
- 更新元数据：latest.yml 由本次构建生成（同版同源）；publish 目标已改为
  `Amicaly98/live-manager`
- **draft release**：正文已备好 `deliveries/release-notes-1.1.0-draft.md`；
  本环境无发布凭据，未创建远端 draft——发布命令由持有凭据者执行
  （`gh release create v1.1.0 --draft --target stability-desktop-port-20260914`，
  资产=上述两个 exe + blockmap + latest.yml）。
- 升级/回退：见 release notes（迁移不删原文件 → 可无损回退 1.0.0）。

## 7. 阻止发布的问题与未验证项

**全部通过后收口，无开放性重构。** 遗留如下：

1. **未验证（环境受限，需人工/真实环境补测）**：
   - Setup.exe 安装向导流程、中文+空格安装路径安装、卸载保留数据；
   - 打包应用 GUI 全交互（开播/停止/托盘/导入导出）——本环境 GPU 会话
     不可用，Electron 窗口进程无法存活；后端链路已有日志证据；
   - GitHub draft release 创建（无凭据）。
2. **环境事件（影响的是本会话，不是产物）**：会话中 .git 曾被外部进程
   破坏（refs/objects 丢失），已从远端重建并本地备份
   （`deliveries/git-backup-stability-port-20260914/`）；含 `/` 的分支名
   无法创建，改用扁平分支名。
3. **延期项**：诊断套件（进度 PIPE/媒体窗口/脱敏导出）整体延期，无
   半成品遗留；服务器 5 秒退避重连风暴未修（按清单属后续独立方案）。
4. **建议复核顺序**：先人工跑一次 Setup 安装 + UI 冒烟（真实 GPU 环境），
   再创建 draft release，最后监督复核放行正式 Release。
