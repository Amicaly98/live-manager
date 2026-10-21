# 桌面移植第三阶段交付说明（打包与精简收尾）

- 日期：2026-10-10（含当日两轮独立复核后的返修；第二轮返修的窄跑跨到 2026-10-11 凌晨，
  第三轮 A 包返修在 2026-10-11）
- 分支：`port-server-core-20261009`（入场 HEAD `4880123c474db9081de6ffc057fa43599c41b155`）
- 提交：正文 `26354aaf07b5d5dee915776b41627fb11a2ad3b7`，文档补记 `734fa91`、`efb9f4b`；
  **三轮独立复核后的返修见第零节**（`7681aeb`，父 `efb9f4b`；`f65b9ba`，父 `e0f23c7`；
  **`e42c086` + `8b8e9e9`，父 `72b26b1`**）
- 状态：**第三阶段候选完成，待独立复核**。未推送、未公开发布、未改服务端、未触碰日常数据目录。

## 零、独立复核后的两包返修（本轮新增）

第一轮独立复核
（`F:/livestream/deliveries/desktop-port-phase3-review-efb9f4b-20261010/REVIEW.md`）
结论为「暂不通过」，两条：**A 覆盖升级会删除旧资料**（原计划的"升级资料不丢"未满足）、
**B 媒体采证工具存在假绿**（结束命令失败也照样判通过；进程查询失败被当成 0）。
第二轮独立复核
（`F:/livestream/deliveries/desktop-port-phase3-rework-review-e0f23c7-20261010/REVIEW.md`）
确认正常路径已修好，但 A 包还有两处 P1、B 包还有两处采证遗漏（见本节末的"第二轮返修"）。
每轮只修被指出的那几处，不重刷整仓、不重跑历史阶段。

- **A 包改产品** → 仓库提交 `7681aeb`（第一轮）与 `f65b9ba`（第二轮）
  （`build/installer.nsh`、`electron/main.ts`、`backend/app/core/data_migration.py`）。
- **B 包只改采证工具** → 不进仓库；改动与证据全在
  `F:/livestream/deliveries/desktop-port-phase3-20261010/`（`packaged-smoke.mjs` 等），
  因此仓库里没有对应提交。

### A：覆盖升级的旧资料保全（改产品）

原来的事实：1.0.x 的 Electron 用 spawn 拉起后端且未设 cwd，运行时数据落在**安装根目录**；
同 appId 覆盖安装时，安装器先静默跑**旧版**卸载器，而旧卸载器会 `RMDir /r $INSTDIR`，
资料在"升级成功"的同时消失。旧版驱动把这一点登记成"记录存活情况"（恒为 true），
因此 85/85 覆盖不了这条完成条件。

修法（不重写迁移体系，只补两个接缝）：

- **新增 `build/installer.nsh`**（electron-builder 的 `nsis.include` 约定路径，自动拾取）。
  在 `.onInit` 的 `customInit` 里——**早于 `installSection.nsh` 调用的 `uninstallOldVersion`**
  ——把已知的旧数据文件从 `$INSTDIR`（以及注册表 `InstallLocation` 登记的位置）复制到
  `%APPDATA%\<包名>\legacy-rescue`。复制清单与后端 `data_migration._MIGRATED_FILES`
  对齐，另带 `live_tasks.db` 的 `-wal`/`-shm`（后端用 SQLite backup API 做一致性快照，
  只拿主库会丢掉还在 WAL 里的已提交内容）。只在检测到旧数据文件时才建目录。
- **`electron/main.ts`**：`getLegacyDataDirs()` 增加
  `path.join(app.getPath('appData'), app.getName(), 'legacy-rescue')` 作为候选，
  仍走既有的 `--legacy-data-dir` 入口。写成 `appData + 应用名` 而不是 `userData`，
  是因为安装器只会写到这个固定位置（正常运行时两者本就是同一个目录）。
- **`backend/app/core/data_migration.py`**：`_MIGRATED_FILES` 补 `stop_intent.json`。
  它是计划 §阶段 1.4 明确要求接管的旧字段，而 `desktop.read_stop_intent` 读的是
  数据目录里的同名文件——不列进来，"升级前人工停止过"这件事永远走不到接管链上。

**没有**新建迁移入口、**没有**改归属判定、**没有**改 appId 或 userData 路径。

### B：媒体采证工具（只改夹具/驱动，不改产品）

- `countFfmpegFor` 现在**观察失败即抛出**（spawn 失败、非零退出、超时、结果解析不出、
  部分进程的命令行读不出），只有"命令成功且确实列出空结果"才返回 0。新增
  `observeFfmpegFor` 包装，未知一律记 `{count: null, error}`，绝不折算成 0。
- 媒体中断段改为：**先留结束命令回执、再按本场景持有的媒体身份确证原对象退出，
  才谈中断后的行为**。前提没确证时，2.14–2.17 四项按"未取得"（失败）计，不再空过。
- 夹具前提修正：合成素材从 8 秒改成 900 秒。8 秒素材会让后端起的 ffmpeg 在场景跑到
  "媒体中断"之前就播完退出（随后被重新拉起、PID 也换掉），于是那一步打的其实是一个
  早已不存在的 PID——旧写法下中断根本没发生却照样全绿。本轮实测复现了这一点
  （`alive_before=false`、`taskkill` 返回 128 找不到进程），修正后为
  `alive_before=true / status=0 / alive_after=false`。

`packaged-smoke.mjs` 里被复核探针抽取的那两段保持"只依赖 `check` / `spawnSync` /
`pidAlive` / `sleep` / `countFfmpegFor` / `api` / `platform`"，因此复核方原有的
`probe_media_evidence.cjs` 可以**原样**再跑（见第五节）。

### 第二轮返修（独立复核 `e0f23c7` → 本轮）

复核结论：正常升级路径已经修好，但 A 包还有两处 P1、B 包还有两处采证遗漏。
四条都成立，处置如下。

**A1：保全失败仍会继续安装。** 原实现的 `CreateDirectory` 与各次 `CopyFiles` 都不消费
失败，宏照常返回，安装继续走到清空 `$INSTDIR` 的旧卸载。现在**每次复制后都核对目标与
源的大小一致**，任何一份没落地或不完整，就把原因写进 `$legacyRescueError`；
`legacyRescueAbortIfFailed` 在旧卸载**之前** `SetErrorLevel 3` + `Quit` 中止这次升级
（此刻原件还在）。中止时若保全目录是**这一次**新建的，把它整个删掉——半份副本比没有
副本更危险，下次启动会被当成一份完整的旧资料接管。

**A2：保全发生在停写确证之前。** 原实现挂在 `.onInit` 的 `customInit`，而模板的
`CHECK_APP_RUNNING` 到后面的安装 Section 才执行，于是可能对着**仍在写**的旧库逐个复制
主库/WAL/SHM，拼出一个从未存在过的时刻（复核的受控交错里源库只出现过
`(0,0)→(1,0)→(1,1)`，副本却是 `(0,1)`，而 `integrity_check` 仍然是 ok；
之后再调 backup API 也救不回来）。现在入口改挂在模板的 `customCheckAppRunning` 上：
它在 `installSection.nsh` 里**恰好**位于"按既有流程关掉正在运行的应用"之后、
`uninstallOldVersion` 之前——复用产品既有的关闭流程，不新造停写机制。宏体先
`!insertmacro _CHECK_APP_RUNNING`（行为不变），再做保全；复制前用 `CreateFileW`
（共享模式 0）打开旧主库**确证这一刻没有别的进程持有它**，打不开就重试若干次后中止。
**确证不了就不复制**，而不是"先复制、回头再对齐"。

> 附带一处必须补的：定义了 `customCheckAppRunning` 就等于接管了模板的进程信息供给
> （模板的 `!ifmacrondef` 分支因此跳过），`getProcessInfo.nsh` 与 `Var pid` 由本文件
> 补上，并用 `!ifdef APP_GUID` 兜住——那是由构建器 `-D` 传入的，所以这份文件仍然可以
> 被"直接 include 的独立小探针"使用。

**B1：存活观察失败被当成已退出。** `pidAlive` 原来忽略 `error` / 非零退出 / 超时，
空输出即返回 false，外层据此声称"原对象确证退出"。现在改成**三态**
（`true` 在跑 / `false` 确证不在 / `null` 读不出），"确证不在"一律走 `pidGone()`；
进程清单读取也收进共同的观察入口 `queryProcessCommandLines`（`countFfmpegFor` 与
按数据目录找 run.exe 都走它），失败即抛出，只有"命令成功且结果为空"才允许是空清单。
（覆盖范围就是本段依赖的那两样：**PID 观察**与**进程清单观察**。）

**B2：媒体退出遮住了应用退出。** 中断段里新声明的 `const exited`（媒体）在同一块里
遮住了外层由 Electron `exit` 事件更新的 `exited`（应用），于是"媒体确已退出、应用还
活着"时 2.18 照样通过。现在媒体退出叫 `mediaExited`，2.18 回到应用自己的退出事实，
并保留原来的有界等待——超时也如实落一条判据，不再把结论吞掉。

夹具侧同轮修掉两处（都不是产品缺陷）：

- **合成旧资料的日期口径**：原来用 `toISOString()`（UTC），而产品判"同日半场"用
  **本地业务日**。东八区本地 00:00–08:00 两边差一天，迁移就会走"停止目标下的旧日半场：
  丢弃无意义秒数"这一支，**Run 根本不建**，`2.06` / `4.13` 的恢复入口不出现。
  本轮跨过本地午夜后真的撞上（同一份夹具、同一份产品代码由绿转红），已改为本地日。
- **本机残留的同 appId 卸载登记**：留着它，**旧**安装包一启动就会在
  `uninstallOldVersion` 处 `SetErrorLevel 2` 退出、什么都不装，而驱动的症状只是
  "旧版安装产物超时"。已确认清理后修复。

### 第三轮返修（独立复核 `72b26b1` → 本轮）

复核结论：**B 包通过**（两组媒体探针原件 4/4、3/3，退出码 0）；**A 包还有三条 P1**，
阶段暂不通过。三条都成立，全部落在同一个责任上——"旧卸载前取得安全、完整的保全结果"。
本轮只动 `build/installer.nsh`（外加文档），B 包不重开。

**R1：已有同大小的旧副本会掩盖真实的复制失败。** 原实现复制后只比源/目标大小，
没有消费 `CopyFiles` 自己的错误标志；目标已存在且长度恰好相同时，"大小相同"
这件事可能只是上一轮那份旧副本，本次复制其实失败了也照样 `exit=0` 继续。
现在复制一律**先落进本次新建的干净候选目录**（`legacy-rescue-new`），那里不可能
存在上一轮的旧副本，同时**显式读 `CopyFiles` 的错误标志**（`${If} ${Errors}`），
再逐份核对大小。

**R2：中止升级后，已有的保全目录可能变成混合资料。** 原实现往已有目录里逐文件覆盖，
失败时只删"本次新建的"目录；已有目录里的四份新版 + 一份旧库会留在那里，而应用启动时
还会把它当成一份完整来源。现在改成**独立候选 → 全部成功才整体切换**：
候选完整之后才把已有保全 `Rename` 成 `legacy-rescue-old`、把候选换上去、再删掉旧的。
任何一步不成就整体回退并**中止升级**：候选整个删掉，**已有保全一个字节都不动**
（既不留混合目录，也不把已有完整保全删掉）。移不开（被占用）时同样什么都不动。

**R3：停写保护没有覆盖复制窗口。** 原实现拿共享模式 0 打开旧主库确证停写后**立刻
关掉**，再回头逐文件复制——只证明了打开那一瞬间没人持有，挡不住复制期间重新开库的
写者（复核的受控交错里源库只出现过 `(0,0)→(1,0)→(1,1)`，副本却拼出了 `(0,1)`，
`integrity_check` 还是 ok）。现在确证之后**立刻改握一个"只读 + `FILE_SHARE_READ`"的
句柄并一直握到整族复制与核验结束**（成功/失败出口都释放）：其他读者还能读（`CopyFiles`
读得到源），任何想拿写权限的进程都被拒。

> 确证（共享模式 0）与握持（只读 + 共享只读）之间还有一个极短的空档，这段**是安全的、
> 也是故意的**：若真有写者在这段挤进来并且还开着，第二个 `CreateFileW` 会直接失败 →
> 按"取不到排写保护"中止；若它已经写完并退出，复制到的是它留下的那个**真实**状态，
> 仍然是自洽的。挡住"复制窗口里重新开库"的是第二个句柄，它在复制与核验全程都在。

### 返修后的实测

> 下表是**第二轮**返修后的实测（`smoke-r6` / `smoke-r7`）。第三轮的实测在同节末尾。

| 事项 | 结果 |
|---|---|
| 真实 1.0.0 → **本轮新包** 1.1.1 覆盖升级（`smoke-r6`，`--only=legacy,upgrade`） | **61/61**，`errors: []` |
| 宏级三态窄证（`rescue-macro-run.mjs`：真实 makensis 编译本文件里的保全宏） | 3/3 —— 正常保全逐字节一致且继续；**复制失败 → 中止**；**停写未确证 → 中止** |
| 复核方自己的 `probe_media_evidence.cjs`（原件）对返修后驱动再跑 | 4 run / 0 failure / 0 ERROR |
| 复核方自己的 `probe_media_remaining.cjs`（原件）对返修后驱动再跑 | 3 run / 0 failure / 0 ERROR |

A2 的两条判据（`smoke-r6/aggregate.json`）：

- **停写确证不起作用时必须拒绝升级**：4.02c 在旧库被另一进程真实持有的情况下再装一次，
  安装器 **exit=3**、五份旧资料**一份未动**；紧接着释放句柄后的 4.03/4.05 就正常升级并
  保全——同一轮里"持有→拒绝 / 释放→继续"是一对受控对照。
- **拒绝时不留下半份副本**：4.02d 确认此刻没有 `legacy-rescue` 目录（半份副本会被下次
  启动当成一份完整旧资料接管）。

正常路径的关键判据（同一份 `aggregate.json`）：保全目录
`C:\Users\chenz\AppData\Roaming\bilibili-live-manager\legacy-rescue` 里五份文件与安装前
原件**逐字节相同**；同一时刻安装根目录里的原件**已被清掉**（`survived=[]`）——两条合起来
才是"删除之前先复制"；应用认定的落点与安装器实际写入的目录**是同一个**；五份文件经既有
入口进入数据目录；接管后任务参数与完成天数原样（`3 / 12 / 7 / 2026-12-01`）；保全目录里的
原件在接管后仍在且未变（来源可恢复）；升级后启动零自动开播、呈现恢复入口；受控退出后端
收回、8000 释放。

B 包的实测（`smoke-r6` 内）：2.13c 本次是**真中断**
（`pid=… alive_before=true taskkill /F /T status=0 alive_after=false liveness=1读/0存活`），
2.14–2.17 四项由前提门放行后真实取得；2.18 报 `app_exited=true`，读的是**应用自己**的退出
事实（不再是媒体的那个布尔）。

### 第三轮返修后的实测（A 包）

宏级窄证现在有 9 条，全部用**真实 makensis 编译本文件里的保全宏**（只在交付目录里读写，
不安装、不卸载、不写注册表、不碰 `%APPDATA%`）：

| 事项（`rescue-macro-run.mjs`） | 结果 |
|---|---|
| A-n1 干净来源 + 不存在的目标 | exit 0、继续、五份文件逐字节一致、无候选/暂存残留 |
| A-r1 真实复制失败（源文件被共享模式 0 占住 ⇒ `CopyFiles` 报错） | **中止**、不写继续标记、目标目录不存在 |
| A-r2 停写未确证（源主库被别的进程持有） | **中止**、不产出拼接副本 |
| A-r3 候选目录建不出来 | **中止** |
| A-r4 已有保全被占用（同大小旧副本） | **exit 3 中止**、已有保全**逐字节未变**、无候补目录（复核 R1/R2 的受控情形） |
| A-r5 已有保全被占用（不同大小） | **exit 3 中止**、已有保全**逐字节未变** |
| A-n2 已有保全没被占用 | exit 0、整体替换、新保全完整、暂存目录已清掉 |

排写保护覆盖复制窗口（`rescue-r3-probe.mjs`：真实 SQLite（WAL）+ 真实 NSIS 复制 +
真实独立写者进程；两处观察闸门是**从当前文件机械生成**的副本，全文差异见
`OBSERVATION-DIFF-r3.txt`，保护与复制语句一字未改）：

| 事项 | 结果 |
|---|---|
| R3-n0 正对照：同一个写者、同样的调用，无保护时 | `opened=true`，真的写进去了 |
| R3-r1 保护挂上之后 | `opened=false`（`attempt to write a readonly database`） |
| R3-r2 主库复制完成、保护释放之前 | `opened=false` |
| R3-r3 保全整体成功 | exit 0、继续、五份文件逐字节一致 |
| R3-r4 保全副本是源库真实状态 | 副本 `(0,0)`（写者全程没进得去） |

复核方自己的两个探针**原件**（逐字节复制到 `review-probes-rerun/`，用当前
`build/installer.nsh` 重新编译它们的 harness）再跑：

| 事项 | 结果 |
|---|---|
| `probe_existing_rescue.py`（R1/R2 那组） | **3 run / 0 failure / 0 ERROR，exit 0** —— G1 正常保全照常；R1 同大小旧副本下"复制失败却继续"的断言不再成立；R2 中止后已有保全 `previous_rescue_changed_files=[]` |
| `probe_reopen_during_rescue.py`（R3 那组） | **写者被保护挡住**：它的第二阶段写者在"主库已复制、保护未释放"的闸门处开库失败，探针以异常报告这一点（复核方写明"修后如写者被保护挡住…应按该安全出口验证"，不要求保持旧形状） |

`smoke-r8`（`--only=legacy,upgrade`）**62/62、`errors: []`**；`smoke-r9`
（`--only=install,single`）**37/37**。补记：r9 的 `errors` 还含一条跨场景监视器产生的 `BACKEND DIED`；日志显示它发生在下一场后端启动前，该后端随后正常就绪并退出。不能把 r9 描述成 `errors: []`。这属于采证监视器问题，后续冒烟应按场景重建监视器。关键判据（`smoke-r8/aggregate.json`）：
`4.02c` 旧库被另一进程持有时安装器 **exit=3**、五份旧资料一份未动；`4.02d` 此刻
**没有** `legacy-rescue` 也没有 `legacy-rescue-new`/`-old`；`4.03/4.05/4.06` 释放后正常
升级、保全副本逐字节等于安装前原件、安装根目录里的原件确实已被清掉；`4.14b` 升级后再装
一次（此时安装根目录里已经没有旧资料可保全）→ 已有保全**逐字节未变**、不留候选/暂存目录。
`4.14` 受控退出后后端收回、8000 释放。

> **一次作废轮次（如实登记）**：排查期间同一台机器上曾同时有两条冒烟驱动在跑（我自己的
> 后台进程超时后没杀干净），表现为 `4.03` 上安装器 `0xC0000005` 退出或**静默悬挂**
> （`uninstallOldVersion` 重试 5 次后弹一个没有 `/SD` 兜底的 MessageBox）。清干净进程、
> 残留卸载登记与 `legacy-rescue` 之后，同一份产物 62/62。细节见交付目录
> `smoke-r8-incident.txt` / `stray-processes.txt`。**这不是产品缺陷，也不是"没发生过"。**

### 仍然成立的三条既有事实

- **PyInstaller 单文件不逐字节可复现**：本次构建 `run.exe` 又是新的哈希与字节数
  （`ec88145b…` / 46,053,280），与前五次都不同。绑定判据仍然只能是同一次构建内比对。
  （更早那几版是 `c249e31d…` / 46,053,460、`4897219c…` / 46,054,704、`26cee87a…` /
  46,053,813、`3ccd993c…` / 46,053,784。）
- **electron-builder 的安装包也不是逐字节可复现的**：同一份源码、同一份
  `build/installer.nsh`，用 `--only=electron` 重建出来的 `Setup 1.1.1.exe` 与全链构建的
  不是同一串字节（`44ea18ff…` / 128,430,324 vs 交付的 `edea7e34…` / 128,430,464）。
  所以**交付产物只能取自那一次全链构建**（`full-build-r4.log`）；重建之后必须按
  `artifacts/SHA256SUMS.txt` 把三个二进制还原回去再跑冒烟。
- `--only=backend` 也会清掉 `release/win-unpacked/resources/backend/run.exe`（有意保守）。


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

### 构建（`backend/run.spec`、`package.json`、`scripts/build-release.mjs`、`build/installer.nsh`）

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

`frontend/`、`backend/app/` 的**产品源码一行未改**。本阶段只改构建配置、构建入口与文档。
这一点对下面的"退出衔接"证据很重要：退出链相关代码与阶段 2 逐字节相同。

> **勘误（返修后）**：独立复核的 A 包修到了产品行为，上面这段只描述**第一版**：
> 返修新增了 `build/installer.nsh`，改了 `electron/main.ts` 的 `getLegacyDataDirs()`
> 与 `backend/app/core/data_migration.py` 的 `_MIGRATED_FILES`（见第零节）。
> `backendManager.ts` 与退出链仍未改动。

## 三、构建产物与绑定事实

| 产物 | 路径 | 字节 | SHA256 |
|---|---|---|---|
| 安装版（NSIS） | `release/直播控制系统 Setup 1.1.1.exe` | 128,430,464 | `edea7e34b75d8e9b93b6a05d541a20d3848f6aa9c57adbde2067f894c83982ab` |
| 便携版 | `release/直播控制系统 1.1.1.exe` | 128,211,842 | `3e30c59e9a9d70689b8b4a33eccb644513a48133349f25dfcecd5ffacfc88c1e` |
| 包内后端 | `release/win-unpacked/resources/backend/run.exe`（与两个包内同一文件） | 46,053,280 | `ec88145ba43985443fc795df6081b0a0d971c7a52a9a587e06bb42c012b621f8` |
| 构建输入：分区种子 | `backend/build-inputs/bili_areas_full.json`（未跟踪） | 72,654 | `648ac56d76938bde5601e6c3e1b912affc7aed5862ac586de4229d79be94c768` |

`build/installer.nsh` 本次构建时的 sha256 = `4a5f9196b1fc9c18dfee74b5b6d98f9911e266929301f8d9f1b02babe8526c9c`。

> **勘误（第三轮返修后）**：上表是**本轮**（第三轮复核后的 A 包返修）的构建。改的是安装器
> 脚本，所以两个包的字节都变了；`run.exe` 也换了（PyInstaller 单文件不可复现）。
> 第二轮那一版（`09d335df…` / 128,430,255、`6814608a…` / 128,211,651、`c249e31d…` / 46,053,460）、
> 第一轮那一版（`5faf628f…` / 128,426,353、`ce57c7df…` / 128,209,574、`3c746a0e…` /
> 46,051,537）与最初那一版（`9e0a8fe8…` / `b6692165…` / `26cee87a…`）都从未推送或发布。


构建环境：Node `v22.20.0`（`tools/node-v22.20.0-win-x64`）、Python `3.12.2` +
PyInstaller `6.19.0`（`F:/python/python.exe`）。三者都记在 `build-manifest.json` 里。

上表最终第三轮 A 包产物是在 `72b26b1` 上带着第三轮安装器修复构建的：对应
`build-manifest.json` 记录 `working_tree_dirty=true`、`commit=72b26b1`。
此前写出的 `efb9f4b` 只对应更早的构建，不能作为上表最终产物的提交标识；
第三轮修复后来收入 `e42c086`，收尾说明截至 `b4e29ca`。本段记录的是当时 1.1.1 候选，
不代表后续版本的构建来源。

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

对照结束后：重建了 `backend/dist/run.exe`；并把
`release/win-unpacked/resources/backend/run.exe` 从交付目录还原为与
`build-manifest.json` 一致的那一份（哈希已复核）。
还原这一步是必要的，因为清理是**两步都做**的——只重建后端（`--only=backend`）同样会
清掉包内那一份，理由是此时"包内副本相对新后端已经过期"。这属于有意的保守行为，
不是缺陷；但它意味着**只重建后端不会得到一个自洽的 `win-unpacked`**，
要自洽就得跑完整构建。

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
   `0,0,1,0,…,1,0…`，**峰值恰好 1**，从未同时出现两份本场景媒体。
   **返修后的复核又发现这一节还有更强的空过**：媒体中断那一步打的是 8 秒素材播完后
   早已消失的 PID（`taskkill` 返回 128），中断根本没发生，四项判据却照样全绿——
   见第零节 B 包与 `smoke-r3`。）
3. **冻结程序里的账号原生锁（`legacy` 内）**：另一份**本次构建**的 run.exe、独立端口
   8101、同一隔离账号根 → 同 UID 绑定返回 409 且未安装，`instance_errors` 明写
   `账号已被占用：锁已被占用（WinError 32）`（不是别的构造错误）；账号库 mtime 一字节
   未变；原实例不受影响；第二份 run.exe 的**整棵进程树**已收回。
4. **安装版 / 便携版 / 单实例（`single`）**：在**同一隔离 userData**下，便携版在安装版
   运行时自动退出（`exit=0`）、安装版保持运行、后端 PID 未变、8000 单监听、
   未产生第二份自有媒体、已有窗口被唤起；同版重复启动同样如此。
5. **实际旧安装包 → 本轮新安装包（`upgrade`）**：本机 `release/` 里有 2026-05-20 构建的
   `直播控制系统 Setup 1.0.0.exe`（ProductVersion=1.0.0，sha256 `b85a3fbe…`）。在同一隔离
   目标上先装 1.0.0、把合成旧资料放进**安装根目录**（1.0.x 的真实数据位置）、再覆盖装
   1.1.1。**返修后**这一节验的是第零节 A 包那条链（4.00–4.15）：原件在覆盖安装里被清掉
   的同时，安装器已经把逐字节相同的五份文件保全到 `legacy-rescue`；升级后的安装版跑的是
   包内 `run.exe`，自动找到这个目录并经既有入口接管（`category=3 / total=12 / days_done=7
   / deadline=2026-12-01` 原样），保全目录里的原件接管后仍在，启动零自动开播、
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
升级**的真实场景里通常**没有东西可迁**。要迁移必须让旧资料先到一个能存活的位置。

> **勘误（返修后）**：上面这段是本阶段第一版**当时的事实与结论**。独立复核指出它不满足
> 原计划的"升级资料不丢"完成条件，返修已改产品：安装器在旧卸载清空 `$INSTDIR` **之前**
> 把旧资料复制到 `%APPDATA%\<包名>\legacy-rescue`，应用经既有入口接管（见第零节 A 包）。
> 现在**可以**说"旧安装器升级 + 安装根目录旧数据自动迁移已验证"（`smoke-r2` 4.05/4.06/4.09
> 三条一起构成判据）。"手工把旧资料搬到别处再用 `BILIBILI_LEGACY_DATA_DIR`"这条路径**仍然
> 有效**，是给归属不明或已离开安装目录的资料用的显式入口，本轮未改。

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

上表是**第一轮**（85/85 那一轮）修掉的五处。独立复核之后又修了第二轮的采证问题
（结束命令回执、进程观察未知、8 秒素材造成的中断前提不成立），见第零节 B 包——
它们属于同一类"工具造成的假绿"，不是产品缺陷。

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
  （返修动了 `main.ts` 的 `getLegacyDataDirs()`——旧资料候选表，与退出链无关；
  `tsc` 编译通过，退出相关的模块用例本轮未再重跑，仍是阶段 2 的证据。）

未取得 / 未验证：

- 真实 B 站账号、真实推流到平台、真实 SMTP 与 Server酱发送、生产数据迁移。
- 公网自动更新的下载与安装（只核对退出衔接）。
- macOS / Linux 产物（本轮只做 Windows）。
- 安装到含空格路径的静默安装（沿用 1.0.0/1.1.0 阶段已知限制：`/D=` 在首个空格处截断；
  本轮隔离目标一律不含空格）。
- 安装包代码签名（未签名，SmartScreen 可能提示）。
- **按机器（per-machine / "为所有用户"）安装**下的保全落点：安装器写的是**当前用户**的
  `%APPDATA%`，而提权重启后的内层实例可能是管理员上下文。本产品是按用户安装
  （`RequestExecutionLevel user`），本轮只验了这条路径。

## 六、证据目录（仓库外）

`F:/livestream/deliveries/desktop-port-phase3-20261010/`

- `packaged-smoke.mjs`（返修后的驱动）、`smoke-final.log`、`smoke-final/aggregate.json`
  与截图、各场景的 main/stdout/stderr 日志、`platform.*.log`
- **`smoke-r8.log` / `smoke-r8/`：第三轮（本轮）的窄跑，`--only=legacy,upgrade`，`62/62`、
  `errors: []`**；4.02c/4.02d 是"停写未确证 → 拒绝升级、旧资料一份未动、不留候选目录"的
  实际流程窄证，4.14b 是"再装一次（已无旧资料可保全）→ 已有保全逐字节未变、不留候选/暂存
  目录"
- **`smoke-r9.log` / `smoke-r9/`：第三轮 `--only=install,single`，`37/37`**，覆盖被改过的
  公共观察入口
- **`smoke-r8-incident.txt` / `stray-processes.txt` / `smoke-r10.log` / `smoke-r2bis.log` /
  `smoke-varA.log` / `smoke-trace.log` / `trace-403.txt`**：上面那次作废轮次的完整留档
  （同一台机器上同时两条冒烟驱动造成的假红/悬挂），以及排查时用过的临时变体
  `installer-r3-trace.nsh` / `installer-r3-noguard.nsh` / `installer-r3-nocommit.nsh` /
  `installer-r3-diag.nsh` / `installer-r2-bisect.nsh` 与 `repro-403.mjs`、
  `make-trace-variant.py`、`check-guard-released.py`。**这些都不是产品代码**；
  `installer-r3-final.nsh` 才是仓库里那一份（sha256 `4a5f9196…`）。
- **`rescue-macro-harness.nsi` / `rescue-macro-run.mjs` / `rescue-macro-facts.json` /
  `rescue-macro-work/`**：A 包的宏级窄证，9 条（正常 / 复制失败中止 / 停写未确证中止 /
  候选建不出中止 / 已有保全被占用 ×2 中止且一个字节未变 / 已有保全没被占用时整体替换 /
  选来源命中与未命中）；真实 makensis 编译本文件的保全宏，只在本目录里读写，不安装、
  不卸载、不写注册表、不碰 `%APPDATA%`
- **`rescue-r3-probe.mjs` / `rescue-r3-writer.py` / `rescue-r3-facts.json` /
  `rescue-r3-work/` / `rescue-r3-repo/` / `OBSERVATION-DIFF-r3.txt`**：排写保护覆盖复制窗口的
  窄证（真实 SQLite + 真实 NSIS 复制 + 真实独立写者进程；观察闸门是从当前文件机械生成的
  副本，保护与复制语句一字未改）
- **`review-probes-rerun/`**：复核方第三轮两个探针的**原件**（逐字节复制）用当前
  `build/installer.nsh` 重新编译后再跑 —— `probe_existing_rescue.py` 3/3、exit 0；
  `probe_reopen_during_rescue.py` 的写者被保护挡住（`reopen-rerun.log` 与
  `existing-rescue-rerun.log`），另有 `annotate-r3.mjs` / `OBSERVATION-DIFF.txt` / `observed/`
- `probe_media_evidence.cjs` / `media-evidence-rerun.log` / `media-evidence-summary.json`：
  **复核方原有的探针原件**，`SMOKE_SOURCE` 指向返修后的驱动再跑，4 run / 0 failure / 0 ERROR
- `probe_media_remaining.cjs` / `media-remaining-summary.json`：**复核方第二轮的探针原件**，
  同样指向返修后的驱动再跑，3 run / 0 failure / 0 ERROR
- `smoke-r2` / `smoke-r3` / `smoke-r5` / `smoke-r6` / `smoke-r7`：前两轮返修的窄跑与跨午夜那次
  （4.13 由绿转红，原因见第零节夹具日期口径，不是产品缺陷）——保留对照
- `rescue-probe.mjs`：A 包第一轮的快速独立探针（只验安装器侧保全）
- `probe_upgrade_facts_r2.py`：按复核 A 项的两条判据审计 `smoke-r2` 的字段，5/5 OK。
  复核方原版 `probe_upgrade_facts.py` 读的是返修前那轮的字段名；**旧证据
  `smoke-final/` 原样保留**，用原版审计器跑它仍然复现出原来那条失败
- `smoke-dup-check.log` / `smoke-dup-check/`：第一轮 `--only=legacy` 的窄跑
- `upgrade-repro.ps1` / `upgrade-repro.log`（覆盖安装清空安装目录的受控复现，返修前）
- `artifacts/`（**本轮**的安装版、便携版、包内 `run.exe`、`build-manifest.json`、`SHA256SUMS.txt`）
- `full-build-r4.log`：本轮的整链构建日志（`full-build-r2.log` 是第二轮的、`full-build.log` 是第一轮的）
- 中间轮次的 `smoke-*.log` / `smoke-*/` 一并保留，便于对照（不删除既有 release 内容）

## 七、边界

- 未推送、未打标签、未创建公开 release、未上传任何产物。
- 未修改服务端仓库；未触碰日常使用的数据目录与现有安装。
- 为验证而做的安装（1.1.1 与 1.0.0）已在脚本收尾时静默卸载并核对清空：无遗留进程、
  8000/8101 空闲、无卸载登记、无快捷方式。
- 安装器保留下来的 `%APPDATA%\<包名>\legacy-rescue` 是**产品行为**（升级后仍是一份可
  恢复的旧资料副本，与既有的"迁移后不删旧文件"一致）；验证用的那一份已由驱动在收尾时
  删除，`smoke-r2/aggregate.json` 的 4.15 是这条事实。
- `HANDOFF.md` 已按现实现重写，但它在仓库既有 `.gitignore` 里（第 58 行），
  因此保持为**本地工作文档**、不随本次提交入库；`README.md` 与本文档是入库的那两份。
