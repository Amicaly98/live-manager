# 桌面版 1.1.0 稳定性移植执行报告（第二轮 / rc2）

日期：2026-09-14。对象：`F:\livestream\bilibili-live-manager`，分支 `stability-desktop-port-20260914`。
基线：第一轮复核对象 `b5dbbc0`；本轮修复提交：`0900698`（D1-D6 定向修复）+ 本报告提交。
原始第一轮报告已归档：`F:\livestream\deliveries\desktop-supervision-round1\ARCHIVE-EXECUTION-REPORT-R1.md`。

## 结论

**监督第一轮 7 项 Python 反例 + 3 项 Node 反例全部转绿；未改写既有 Git 历史；本轮重新构建了候选产物（rc2）。** 正式发布仍待监督复核与真实桌面 GUI 验收（本环境无可用 GUI 窗口，见"未验证边界"）。

| 项 | 状态 | 说明 |
|----|------|------|
| D1 控制与响应性 | ✅ 已修复 | 提交复核入控制锁；重连捕获代际；慢换区/刷新离开事件循环；停止登记与执行分离（意图绑定） |
| D2 Electron 身份与生命周期 | ✅ 已修复 | PID 文件带命令行+实时身份验证；spawn/health/close/error 全部绑定代际；"不停止并退出"不发平台 stop |
| D3 OBS 任务入口 | ✅ 已修复 | start/resume/run-next 无本地视频放行（仅 FFmpeg 推流要求视频） |
| D4 迁移数据安全 | ✅ 已修复 | SQLite backup API 一致性快照（含已提交 WAL）；备份/目标内容验证；临时文件+原子替换；冻结版旧目录由 Electron 显式传入 |
| D5 耐久证据 | ✅ 已重做 | 真实产品控制器耐久（判定器闭环：先检出注入停顿→产品自恢复→正常耐久）；句柄口径修正为 PowerShell HandleCount |
| D6 日志存活期容量 | ✅ 已修复+实测 | 每 10s 检查、原地截断（共享文件指针复位），Windows 真实写句柄跨阈值实测通过；截断失败不影响推流 |
| D7 证据与报告 | ✅ 已补齐 | 真实前端请求链测试 4/4；产物源码一致性核对通过；manifest+SHA256；旧报告表述逐项修正 |

## 测试命令与结果（全部在本轮复跑）

```powershell
# Python（仓库 43 项 + 监督反例 7 项）
$env:PYTHONIOENCODING='utf-8'; $env:BILIBILI_SKIP_MIGRATION='1'
.\.venv-test\Scripts\python.exe -m pytest backend/tests ../deliveries/desktop-supervision-round1/test_desktop_supervision.py -q --ignore=backend/tests/electron -p no:cacheprovider
# → 50 passed（修复前：监督 7 项全红，均断言失败；复现与转绿在同一环境）

# Node 生命周期（自有 9 项，含 PID 复用/退出分流/旧回调身份）
node --test backend/tests/electron/backendManager.test.mjs        # → 9 pass / 0 fail

# Node 监督反例（3 项）
node ../deliveries/desktop-supervision-round1/electron-supervision.cjs   # → 3 pass / 0 fail

# 真实前端请求链（store→request→axios→真实 HTTP，端口 8000）
node backend/tests/frontend/frontendChain.test.cjs                # → 4/4 passed

# 生命周期 10 轮（对象=打包 run.exe，命令与证据一致）
.venv-test/Scripts/python.exe backend/tests/lifecycle_endurance.py --cycles 10 --port 18330 --backend-exe backend/dist-r2/run.exe
# → cycles_ok=10/10：health=True、优雅退出 exit=0、零残留；
#   RSS ~11MB 稳定、句柄(HandleCount)=128 恒定
# 原始 JSON：backend/tests/_endurance_results/lifecycle-20260914-193*.json

# 产品耐久（20 分钟，真实 LiveController→_ffmpeg_loop→FFmpeg→本地 sink）
.venv-test/Scripts/python.exe backend/tests/lifecycle_endurance.py --cycles 10 --endurance-minutes 20 --port 18200 --backend-exe backend/dist-r2/run.exe
# （同命令一次运行：10 轮因脚本参数 bug 无效后已用修复脚本单独重跑；
#   耐久部分有效，原始 JSON：endurance-20260914-185120.json）
# → pass=true：注入停顿被同一判定器检出 ✓；kill 后产品 _ffmpeg_loop 自动重启（新 PID）✓；
#   steady 217 窗口零缺口 ✓；末尾新鲜 ✓；推送 82MB（16 次 concat 重连）✓；
#   RSS 106→106MB、句柄 235→234（无增长）✓；产品 stop 后推流进程零残留 ✓
```

监督反例红→绿对应（场景与断言语义未删改）：

| 监督反例 | 修复点 |
|---|---|
| test_stop_after_platform_response_before_commit_does_not_revive | `_start_streaming_sync` 最终提交（is_streaming/state/推流启动）移入 `_start_lock` 并复核 cancel+代际 |
| test_reconnect_response_after_stop_cannot_restart_local_pusher | `_retry_start_live` 捕获代际+意图，平台返回后复核并撤销迟到房间（新意图存在时不补发下播，房间归新意图清理） |
| test_slow_switch_does_not_block_event_loop | switch-area 移入线程池执行，结果提交前复核代际（areas/refresh 同原则） |
| test_obs_task_entry_does_not_require_local_video[start/resume/run_next] | 三入口按推流设置放行：仅 stream_mode=ffmpeg 要求视频 |
| test_migration_keeps_committed_wal_data | 迁移改 `sqlite3.Connection.backup`（读到含 WAL 的统一提交快照），内容验证+原子替换 |

Electron 三反例：spawn 期间停止→迟到子进程回收且 started=false；waitReady 期间停止→不进 ready；旧 close（带 pid+generation 身份）不影响新 owner。生产 `main.ts` close/error 已接入真实身份（child.pid + spawn 时刻代际）。

## 本轮新增测试（监督要求补充的 4 类）

- **PID 复用**：`classifyPortConflict` 需 `identityMatches` 通过（实时查询进程命令行 vs PID 文件登记命令行）；PID 数字相同但命令行不符→foreign（只提示，不 taskkill）。Node 用例 1 项 + 生产实现 PowerShell CIM/wmic。
- **不停止退出**：`lifecycle.stop(graceful, timeout, stopPlatform)` 传递到 `requestShutdown(stopPlatform)`→`/api/shutdown?stop_live=`；backend-restart 保留完整停止语义。Node 用例 1 项。
- **停止重复排队**：登记（快照目标意图）与执行（核对当前意图）分离；`_stop_live_sync` 执行时目标已变→不执行下播。Python 用例 1 项。
- **真实前端请求取消链**：esbuild 打包真实 `stores/live.ts`+`request.ts`+`operationToken.ts`，对接真实 HTTP（产品端口 8000）：取票/同票重试/取消不重试/409 不换票重发，4 项。

## 产物（rc2，本轮重建；旧 exe 仅作旧候选保留）

- `release-1.1.0-rc2/直播控制系统 Setup 1.1.0.exe` — SHA256 `73b28ed0edbf6070f69f2ac1a11c785212dd39ccdf5683d0efc5d38cc9131ca0`
- `release-1.1.0-rc2/直播控制系统 1.1.0.exe`（portable）— SHA256 `83914c28df0cccc64b5a09a8a82c4cbb351cf4d1495bdab3ab84a264efc08185`
- 产物核对（`deliveries/artifact-source-check-rc2.json`）：asar 内 main/preload/backendManager.js == 当前源码构建；内置 run.exe == `backend/dist`（4c91b72c…，含 D1-D6 修复）；SHA256SUMS 同步 `deliveries/SHA256SUMS-rc2.txt`
- rc2 打包应用烟测：内置 run.exe 拉起、health OK、数据目录（userData/data）正确初始化；GUI 进程退出后 8000 端口无残留后端（D2 收尾生效）

## 旧报告表述修正（D7）

1. ~~"旧版本重开可能自动恢复"~~ → **桌面旧版与新版均不自动恢复直播（保留旧行为）**；本轮修复的是在途开播的取消与迟到回调（平台响应后的代际复核），不是"自动恢复"。
2. ~~"打包后端十轮"~~ → 第一轮的 10 轮实际命令是 `python backend/run.py`；本轮另以 `--backend-exe backend/dist-r2/run.exe` 实跑 10 轮（上文命令即证据）。
3. ~~句柄口径~~ → 第一轮把 tasklist CSV 的"会话编号"列误当句柄计数（字段选错，非"解析不可靠"）；本轮统一 PowerShell `Get-Process` 的 `HandleCount`/`WorkingSet64`。寿命前后句柄 235→234、128 恒定，仅作为本轮观察记录，不宣称解决 Windows 句柄问题。
4. ~~耐久对象~~ → 第一轮耐久是独立 FFmpeg→TCP（主要测 FFmpeg 本身）；本轮为**真实产品控制器**（LiveController、控制代际、_ffmpeg_loop、真实 settings/媒体发现、产品 stop 收尾）。
5. ~~"API 前端 40 项通过"~~ → 补充真实 store→request→axios→HTTP 链路测试（4 项），Python 侧 50 项不再被表述为覆盖前端请求链。

## 未验证边界（如实声明）

- **Setup/portable 安装升级、中文空格路径安装、卸载保留数据**：未验证（无可用 GUI 桌面环境）。
- **打包应用 GUI 交互**（开播/停止/继续、OBS 模式、导入导出、托盘退出、file:// 页面取票渲染）：本环境 GPU 进程不可用，Electron 窗口无法存活；已验证的仅是后端链路（health、数据目录、退出回收）。**不以 HTTP 200 或静态构建替代 GUI 验收。**
- 真实平台行为（B 站开播/下播/换区）、真实邮件、真实账号：未接触（全部替身）。
- macOS：未测试，不宣称。
- 耐久 RSS/句柄为单进程观察样本，不构成内存/句柄泄漏问题的完整结论。

## 提交与回滚

- 分支 `stability-desktop-port-20260914`：`48e52f1 → … → b5dbbc0（第一轮候选）→ 0900698（本轮 D1-D6）→ c1b830f（报告+前端链测试+产物核对）→ 6a8927e（D1 补充：迟到重连回滚不误关新意图房间）`，未改写历史。
- 旧候选 exe（release-1.1.0/）未删除；rc2 为独立目录。回滚即检出 `b5dbbc0`。
