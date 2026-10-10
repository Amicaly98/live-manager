# 桌面移植第三阶段交付说明（打包与精简收尾）

- 日期：2026-10-10
- 分支：`port-server-core-20261009`（入场 HEAD `4880123c474db9081de6ffc057fa43599c41b155`）
- 状态：**第三阶段候选完成，待独立复核**。未推送、未公开发布、未改服务端、未触碰日常数据目录。

## 一、这一阶段解决的真实缺口

入场时的构建链有一个不会报错的错配：

- `backend/run.spec` 的导入清单还停留在旧模块（`app.core.live_controller` 等），
  新控制核心 `app/core/session/` 与 `app/core/session_runtime/`（共 15.8k 行、29 个模块）
  完全不在清单里；
- 根 `npm run build` 只做「前端 → Electron」，**不执行** `build:backend`；
- `electron-builder` 从 `backend/dist` 取 `run.exe`——而那是一个 2026-09-14 的旧产物。

于是只跑一次根构建，就会得到「新页面 + 旧 `run.exe`」的安装包，而且全程没有任何提示。
本阶段把这三处接上，并用**打包产物本身**跑了冒烟验证。

## 二、提交范围（本轮实际修改）

### 构建（`backend/run.spec`、`package.json`、`scripts/build-release.mjs`）

- `run.spec`：`hiddenimports` 补齐到当前真实导入面——`session/` 10 个模块、
  `session_runtime/` 19 个模块、`app.dependencies`、`app.core.area_data /
  account_history / diagnostics / ffmpeg_log_progress / media_format /
  task_manager_recovered`、`app.models.control_dto`；旧控制器条目保留（它仍是平台出口
  与选片的依赖）。`excludes` 与分区种子（构建输入，非代码资产）规则未改。
- 新增 `scripts/build-release.mjs`，并让 `npm run build` 指向它。正式入口按**固定顺序**
  执行，任一步失败立即停止：
  1. `pyinstaller run.spec` → `backend/dist/run.exe`（先删上一轮产物）
  2. `vite build` → `frontend/dist/`
  3. `tsc` + 图标 + `electron-builder --publish never` → `release/`
     结束时核对 `release/win-unpacked/resources/backend/run.exe` 与本次构建的 `run.exe`
     **逐字节一致**，不一致即失败；并把提交、Node/Python 版本、种子来源、
     各产物 SHA256 写进 `release/build-manifest.json` 与 `release/build-release.log`。
- `npm run build:backend` 也走同一份脚本（`--only=backend`），不再直接调 PATH 上的
  `pyinstaller`——那个解释器缺后端依赖，会产出与环境不符的 `run.exe`。
- `--publish never` 写进 `build:electron`：生成安装包不等于上传发布。
- 版本号沿用 `package.json` 的 `1.1.1`，未新造发布版本。

### 文档

- `README.md`：改写为当前运行链 + 桌面行为要点 + 准确构建命令与产物位置 + 文档导航；
  旧控制器职责写成"默认入口不再构造、仍复用哪些能力"。
- `HANDOFF.md`：按现实现重写，旧章节移入「历史内容（已退役）」并逐条标注更正
  （旧取票链、浏览器自主计时、直接改恢复进度、旧控制器作为默认实例、"永不放弃"）。
- 本文件。

### 未改动

`electron/`、`frontend/`、`backend/app/` 的**产品源码一行未改**。本阶段只改构建配置、
构建入口与文档。这一点对下面的"退出衔接"证据很重要：退出链相关代码与阶段 2 逐字节相同。

## 三、构建产物与绑定事实

| 产物 | 路径 | 字节 | SHA256 |
|---|---|---|---|
| 安装版（NSIS） | `release/直播控制系统 Setup 1.1.1.exe` | 128,427,593 | `9e0a8fe853129f1a9f2841ca152858becccaf511aecc74a201e58ec252f82778` |
| 便携版 | `release/直播控制系统 1.1.1.exe` | 128,211,507 | `b66921653f198efd8e4ef0c064300fd8d0de6908d973ebdf8bb8cd3c76ef2263` |
| 包内后端 | `release/win-unpacked/resources/backend/run.exe`（与两个包内同一文件） | 46,053,813 | `26cee87a409182c3e498fa49d191e6e798bc20a53919877aa7f671bf27abdaa3` |
| 构建输入：分区种子 | `backend/build-inputs/bili_areas_full.json`（未跟踪） | 72,654 | `648ac56d76938bde5601e6c3e1b912affc7aed5862ac586de4229d79be94c768` |

构建环境：Node `v22.20.0`（`tools/node-v22.20.0-win-x64`）、Python `3.12.2` +
PyInstaller `6.19.0`（`F:/python/python.exe`）。三者都记在 `build-manifest.json` 里。

**包内后端 = 本次源码构建**：构建脚本与冒烟各核对一次（`1.03`），哈希与上表一致。
PyInstaller 的分析图（`backend/build/run/xref-run.html`）里能看到
`app.core.session.*` 9 个模块与 `app.core.session_runtime.*` 18 个模块**全部在列**，
`warn-run.txt` 里没有任何 `app.*` 缺失（只有 `posix`/`fcntl`/`termios` 这类平台条件模块）。

**PyInstaller 单文件不是逐字节可复现的**：同一份源码在本机连续构建三次，
`run.exe` 得到三个不同的哈希与字节数（`4897219c…` / 46,054,704 → `26cee87a…` /
46,053,813 → `3ccd993c…` / 46,053,784）。因此"绑定"的判据只能是**同一次构建内**
包内文件与刚产出的文件逐字节相等，而不是跨构建比对哈希。

**包内不含开发机数据**：`win-unpacked` 全树按名字扫描 `bili_cookies.json` /
`live_tasks.db*` / `live_state.json` / `settings.json` / `*.log` / `backend.pid` /
`account-data` / `*.xlsx` / `.env` — 零命中；`app.asar` 只含 `dist-electron/`、
`frontend/dist/`、`assets/`、`package.json` 与 `electron-updater` 的运行时依赖。

### 构建入口的负向对照（受控失败）

把 `BUILD_PYTHON` 指向一个**存在但不是解释器**的路径，跑整体入口：

```
[clean] 已移除上一轮后端产物 run.exe：…\backend\dist\run.exe
[clean] 已移除上一轮包内后端 run.exe：…\release\win-unpacked\resources\backend\run.exe
$ pyinstaller run.spec
[FAIL] pyinstaller run.spec 无法启动：spawnSync … ENOENT
NEGATIVE_CONTROL_EXIT=1
```

结论（对照构建日志与文件系统）：进程以 1 退出；**前端与 Electron 步骤一次都没跑**
（日志里没有它们）；`backend/dist/run.exe` 与 `release/win-unpacked/resources/backend/run.exe`
都已被删除且**没有**重新出现——即"上一轮的 `run.exe` 不可能被顺手打进包"。
对照结束后已重建 `backend/dist/run.exe`，并把 `release/win-unpacked/…/run.exe`
从交付目录还原为与 `build-manifest.json` 一致的那一份（哈希已复核）。

## 四、实际包验证（不是源码兜底）

驱动：`F:/livestream/deliveries/desktop-port-phase3-20261010/packaged-smoke.mjs`
（复用阶段 1/2 的回环平台桩与合成资料夹具）。证据目录：同目录 `smoke-final/`。

启动的是**静默安装后的 exe**，后端断言必须是
`<安装目录>/resources/backend/run.exe`——不接受指向源码的 junction、系统 Python 或
开发服务器。驱动里同时断言全树没有 `resources/backend/run.py`。
每次场景独立 userData / 账号根；平台出口全部经 `BILIBILI_ENDPOINT_BASE` 指向回环桩；
外网出口用死代理封闭；不连真实 B 站、不发真实 SMTP/Server酱、不触发真实更新下载。

### 场景与结论（最终一轮 85/85 通过、`errors: []`；详见 `smoke-final/aggregate.json`）

1. **首次安装与真实后端（`install`）**：静默安装到隔离目录 → 包内 `run.exe` 与本次构建
   逐字节一致 → 启动后 `file://…/app.asar/frontend/dist/index.html` 加载、`preload`
   的 `electronAPI` 生效、页面→主进程→后端 HTTP 连通、静态资源零加载错误；
   `backend.pid` 的映像就是包内 `run.exe`（不是 `run.py`、不是 `python`）；8000 只有
   一个监听者且它在本次记录的后端进程树里；扫码登录四出口全部经隔离端点；创建任务 →
   覆盖确认框（展示进度/截止/去向）→ 旧任务入历史 → 新任务新身份从 0 开始；
   受控退出后后端收回、8000 释放、未发平台 Stop。
2. **旧资料接管 + 两种退出 + 真实媒体（`legacy`）**：合成旧资料（库登记
   `account_owner_uid=7001`、旧 `live_state.json` 600 秒 / 目标 3600）→ 启动即登录并接管
   →任务参数与完成天数原样、原件保留、账号目录按 uid 归属；**启动零自动开播**；
   显式恢复保持原 Run（`run_id=migrated-2026-10-10-学习区`）与固定目标 3600，
   检查点 ≥ 600（不补造离线时长）；打包后端自建**真实 ffmpeg 子进程**（归属记录
   `state=owned`、映像 `ffmpeg.exe`、输出文件写出并持续增长）；媒体中断（taskkill 归属
   记录里的 PID）后同 Run 保持、进度不回退、平台仍 live、不产生重复媒体；
   **不停止并退出** → 不发平台 Stop、房间仍开播、本地后端与自有媒体收齐；
   重开仍只提示恢复 → 二次恢复仍是原 Run、进度不回退；**停止并退出** → 平台关房确证后
   才完成本地收尾、后端与媒体收齐。
   （"二次恢复不产生重复媒体"这一条在 `smoke-final` 里是**弱绿**：采样窗口落在媒体
   生成期之后，全 0，虽然峰值 ≤ 1 成立但没观测到对象。已把采样起点挪到点「继续场次」
   之前，并用 `--only=legacy` 窄跑复核（`smoke-dup-check`，39/39）：采样为
   `0,0,1,0,…,1,0…`，**峰值恰好 1**，从未同时出现两份本场景媒体。）
3. **冻结程序里的账号原生锁（`legacy` 内）**：另一份**本次构建**的 run.exe、独立端口
   8101、同一隔离账号根 → 同 UID 绑定返回 409 且未安装，`instance_errors` 明写
   `账号已被占用：锁已被占用（WinError 32）`（不是别的构造错误）；账号库 mtime 一字节
   未变；原实例不受影响；第二份 run.exe 的**整棵进程树**已收回。
4. **安装版 / 便携版 / 单实例（`single`）**：在**同一隔离 userData**下，便携版在安装版
   运行时自动退出（`exit=0`）、安装版保持运行、后端 PID 未变、8000 单监听、
   未产生第二份自有媒体、已有窗口被唤起；同版重复启动同样如此。
5. **实际旧安装包 → 本轮新安装包（`upgrade`）**：本机 `release/` 里有 2026-05-20 构建的
   `直播控制系统 Setup 1.0.0.exe`（ProductVersion=1.0.0，sha256 `b85a3fbe…`）。在同一隔离
   目标上先装 1.0.0、再覆盖装 1.1.1 → 升级后的包内 `run.exe` 仍是本轮构建产物；
   升级后的安装版跑的是包内 `run.exe`，把（显式旧位置里的）旧资料接管成功
   （`category=3 / total=12 / days_done=7 / deadline=2026-12-01` 原样），启动零自动开播、
   呈现恢复入口、受控退出正常。
6. **收尾清扫**：全部场景结束后没有遗留的自有后端进程，8000/8101 均空闲。

### 本轮发现（不是本阶段引入，但以前没被记录）

**覆盖安装会清空安装目录，1.0.x 留在安装根目录的数据活不到新版本首次启动。**
受控复现（`upgrade-repro.ps1` / `upgrade-repro.log`，无其他安装并存）：
装 1.0.0 到隔离目录 → 放入四个 1.0.x 形状的数据文件并记哈希 → 覆盖装 1.1.1 →
**四个文件全部 MISSING**，目录里只剩新版本自己的文件。
机制是安装器的既有行为：同 appId 覆盖安装时先卸载既有安装、清空 `$INSTDIR`
（旧版 spawn 未设 cwd，数据恰好落在这里）。

后果与边界：`run.py` 的"安装根目录里发现旧数据并迁移"这条路径，在**用户用安装包原地
升级**的真实场景里通常**没有东西可迁**。要迁移必须让旧资料先到一个能存活的位置
（手工复制出来、或用既有的显式旧位置入口 `BILIBILI_LEGACY_DATA_DIR`）。
场景 5 的第二半正是按后者验证的，因此——

> **可以说"旧安装器升级后的安装版能接管仍存在的旧资料"；不能说"旧安装器升级 +
> 安装根目录旧数据自动迁移已验证"。**

这一条属于升级路径的既有风险，本阶段只如实登记，未改产品行为。

### 本轮驱动的返修（采证缺陷，不是产品缺陷）

冒烟驱动本身在跑的过程中被抓出五处会**造假绿/假红**的写法，均已修掉后才出最终一轮：

1. **PyInstaller 的"两个进程条目"被当成双开**：onefile 的形态是"启动器 + 解包出的
   工作进程"，`backend.pid` 记的是启动器，真正 bind 8000 的是子进程。原先断言
   "监听者 PID == 记录 PID" 因此为红；改为"8000 只有一个监听者 + 它在记录的 owner
   进程树里 + 映像名是 run.exe"，并把"全机 run.exe ≤ 2"收窄为"**本应用进程树里** ≤ 2"
   （原写法会被别的测试留下的同类进程误判）。
2. **媒体输出的"增长"采得太早**：只等文件存在（这时是 0 字节）就判增长。改为先等
   写出 > 10000 字节，再判继续增长（阶段 2 的驱动就是这么写的）。修前 `0→0` 是假红。
3. **"不产生重复媒体"判成"必须恰好一个还活着"**：ffmpeg 会随重试起落，瞬时为 0 是正常
   的。改为**采样取峰值 ≤ 1**、且要求确实观察到过；采样起点必须早于点「继续场次」，
   否则整窗可能落在媒体生成期之后（`smoke-final` 里就出现过全 0 的弱绿，
   已由 `smoke-dup-check` 的窄跑补成峰值 1）。
4. **侧车 run.exe 只杀了启动器**：onefile 的启动器被杀后工作进程仍活着、仍占着账号锁，
   下一轮会在别处冒出来（本轮真的出现过一次孤儿）。改为 `taskkill /F /T` 杀整棵树，
   并新增断言"侧车进程树已收回、无残留"。
5. **后端监视器把单次探测失败当成"后端死了"**：一次 2.5 秒 `/api/health` 超时可能只是
   后端在忙（例如正起 ffmpeg），却被记成 `BACKEND DIED`。核对后端日志后确认是假红——
   两个应用会话各自 `Started server process` 一次、`Shutdown complete` 一次，
   **没有任何崩溃或重启**。已改为连续两次失败才下结论，并把每次原始失败
   （时间、步骤、错误）记进 `facts.monitor_failures`，让这条诊断通道本身可被核查；
   同时把解除"预期收尾"标记的时机挪到新后端**已健康之后**（见下一轮）。

另外，`upgrade` 场景必须排在依赖"安装目录还在"的场景**之后**：同 appId 覆盖安装会先
卸载既有安装（见下面的发现），先跑它会把别场景的安装顺手清掉。

## 五、沿用的证据 / 未取得项

沿用的既有证据（本轮未重跑，按阶段 2 的减负规则）：

- 单实例的"启动尚未就绪 / 退出进行中"两种窄时序：由
  `backend/tests/electron/mainStartupExit.test.mjs`、`mainLifecycle.integration.test.mjs`
  的模块用例覆盖；本轮**重跑这三个模块，23/23 通过**，配合本次实际包的重复启动事实。
- **自动更新退出衔接**：`autoInstaller`（`autoInstallOnAppQuit`）触发的退出走的是
  与其它入口同一条 `before-quit` 收尾链，而 `electron/main.ts` / `backendManager.ts`
  本轮**一行未改**；该链的契约由上述模块用例（"每次受控退出都等自有后端收尾"、
  "重复 before-quit 被挡住直到同一份收尾完成"、"退出未确认则保持存活"）覆盖。
  本轮未连接 GitHub 下载或安装真实更新。
  → **"退出衔接与阶段 2 相同、由模块证据覆盖"成立；"公网自动更新已验证"不成立。**

未取得 / 未验证：

- 真实 B 站账号、真实推流到平台、真实 SMTP 与 Server酱发送、生产数据迁移。
- 公网自动更新的下载与安装（只核对了退出衔接）。
- macOS / Linux 产物（本轮只做 Windows）。
- 安装到含空格路径的静默安装（沿用 1.0.0/1.1.0 阶段已知限制：`/D=` 在首个空格处截断；
  本轮隔离目标一律不含空格）。
- 安装包代码签名（未签名，SmartScreen 可能提示）。

## 六、证据目录（仓库外）

`F:/livestream/deliveries/desktop-port-phase3-20261010/`

- `packaged-smoke.mjs`、`smoke-final.log`、`smoke-final/aggregate.json` 与截图、
  各场景的 main/stdout/stderr 日志、`platform.*.log`
- `smoke-dup-check.log` / `smoke-dup-check/`：`--only=legacy` 的窄跑，
  用改好的采样窗口（在点「继续场次」**之前**开始采样）复核"二次恢复不产生重复媒体"
- `upgrade-repro.ps1` / `upgrade-repro.log`（覆盖安装清空安装目录的受控复现）
- `artifacts/`（安装版、便携版、包内 `run.exe`、`build-manifest.json`、`SHA256SUMS.txt`）
- 中间轮次的 `smoke-*.log` / `smoke-*/` 一并保留，便于对照（不删除既有 release 内容）

## 七、边界

- 未推送、未打标签、未创建公开 release、未上传任何产物。
- 未修改服务端仓库；未触碰日常使用的数据目录与现有安装。
- 为验证而做的安装（1.1.1 与 1.0.0 各一次）已在脚本收尾时静默卸载并核对清空。
