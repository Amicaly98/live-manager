直播控制系统 - 一站式自动化直播管理工具
 
面向主播的桌面直播管理工具，集成账号授权、直播间配置、推流信息获取、配置直播日程、切换直播分区、邮件推送等功能。

## 运行链（当前默认，2026-10-10 起）

```
Electron 主进程（单实例锁 → 端口 → 后端 → 窗口）
  └─ 打包版：resources/backend/run.exe        （PyInstaller 冻结程序，不需要系统 Python）
     源码版：python backend/run.py            （仅 npm run dev:electron / 开发调试）
        └─ app.main 的 lifespan
            └─ session_runtime.desktop.build_desktop_owner
                ＝ 共享 production.build_application_owner ＋ 三条桌面策略
                  └─ ApplicationOwner → ControlService → RuntimeInstance → Core
```

- 桌面三条策略（`backend/app/core/session_runtime/desktop.py`）：**重启只提示恢复不自动开播**、
  **首次领取账号目录前先接管旧资料**、**通知不探测云元数据**。共享的 `session/`、
  `session_runtime/` 其余部分与服务端同源，桌面只做装配。
- **旧 `LiveController` 已不是默认控制实例**，也不并跑旧监控线程/旧日切线程。
  它的下列能力仍被新链复用（`BilibiliApi` 平台出口、`AreaLoader` 分区能力、
  `credential_commit_lock`、选片/格式检查）；`/api/health` 的 `live_controller`
  字段保留但恒为 `null`。
- 登录、创建、覆盖、历史、退出都走客户端生成的 `X-Operation-Token` 与冻结的
  `X-Account-UID`；任务/进度/历史/账本按 UID 存在 `<账号根>/accounts/<uid>/`。

## 桌面行为要点

- **手动恢复**：应用启动本身**零自动开播**。有可恢复的场次时只显示恢复入口，
  由用户点「继续场次」；恢复沿用原 Run 身份、原固定目标与原检查点，不补造离线时长。
- **推流方式**：默认 `ffmpeg`，由设置页的 `stream_mode` 决定；已有用户设置不会被覆盖。
- **账号根**：默认 `userData/account-data`（可用 `--account-root` / `ACCOUNT_DATA_ROOT` 指定）。
- **旧资料接管**：升级前的 `live_state.json` / `live_tasks.db` 在**第一次领取账号目录之前**
  迁移进 `accounts/<uid>/`；归属判据取库登记 `tasks_meta.account_owner_uid`、
  场次快照的运行账号、或旧档房间号——**归属不明不自动认领、也不建空库**，原件保留。
  用安装包**原地升级**时，安装器（`build/installer.nsh`）会在旧版卸载器清空安装根目录
  **之前**把已知的旧数据文件复制到 `<appData>/<应用名>/legacy-rescue`，应用再把这个
  目录交给同一条接管链——否则 1.0.x 落在安装根目录里的资料会在"升级成功"的同时被删掉。
  这一步挂在安装器既有的"关闭正在运行的应用"流程**之后**、卸载旧版本**之前**，并且
  先用共享模式 0 打开旧主库确证没有进程还在写它，**随后一直握住一个"只读 + 允许其他
  读者 + 拒绝任何写者"的句柄，直到整族文件复制并逐份核验结束**——只确证一次又立刻
  松手，挡不住复制窗口里重新开库的写者（那会拼出一个源库从未出现过的时刻）。
  复制**先落进一个本次新建的干净候选目录**并核验 CopyFiles 自己的错误标志与大小，
  全部成功之后才整体切换上去；任何一步不成立就**中止这次升级**（此刻原件还在），
  半份副本一律删掉。**已经有保全目录时，只有候选完整成功才会被整体替换；失败时
  已有保全一个字节都不动**（既不留"四份新版 + 一份旧库"的混合目录，也不删掉它）。
- **两种退出**（外加显式放弃等待）：`停止并退出` 先经同一 `ControlService` 停播并核对
  关房，再收本地资源；`不停止并退出` 不发平台 Stop，只回收自有媒体与后端、保留恢复依据。
  未确认收齐时**不**静默强杀：保留后端与账号占用，页面显示失败/待核对并允许重试。
- **单实例范围**：同一系统用户、同一应用身份、同一 userData 下只运行一份；后启动者只
  唤起已有窗口。不做硬件绑定、联网授权或反破解。账号原生锁（同 UID 独占）是第二层，
  另一个后端尝试绑定同 UID 时在开库/开播之前被拒。

## 项目架构（关键路径）

```
live-manager/
├── assets/                       # 图标资源（安装包与托盘）
├── scripts/
│   ├── build-release.mjs         # 正式构建入口（后端冻结 → 前端 → Electron 打包）
│   └── generate-icons.mjs
├── backend/                      # Python FastAPI 后端
│   ├── run.py                    # 启动入口：解析 --data-dir/--account-root → config.init → app.main
│   ├── run.spec                  # PyInstaller 单文件 run.exe 的导入/资源清单
│   ├── app/
│   │   ├── main.py               # 桌面 lifespan：构造桌面 owner、/api/shutdown、静态托管
│   │   ├── dependencies.py       # 全局单例容器（所有 API 从这里取实例）
│   │   ├── api/                  # auth / tasks / live / areas / settings / email
│   │   ├── core/
│   │   │   ├── session/          # 共享控制核心：Core / Run / Goal / Clock / 账本 / 日切 / 迁移
│   │   │   ├── session_runtime/  # 共享装配：ControlService / Runtime / 账号锁 / 媒体端口 / 平台端口
│   │   │   │   └── desktop.py    # ★ 桌面差异集中在这里
│   │   │   ├── live_controller.py# 旧控制器：不再是默认实例，保留平台出口与选片能力
│   │   │   ├── task_manager.py / db.py / area_data.py / data_migration.py
│   │   │   └── email_sender.py
│   │   └── models/               # schemas.py + control_dto.py
│   ├── requirements.txt
│   └── tests/                    # 后端用例；port_support/ 是隔离回环平台夹具
├── frontend/                     # Vue 3 + Vite + Element Plus（Hash 路由，相对资源路径）
│   ├── src/api/                  # request.ts（令牌/重试/结果未知）、authWire / liveControl
│   ├── src/stores/               # auth / live / tasks / settings
│   ├── src/views/                # Login / Dashboard / TaskManager / Settings
│   └── src/components/           # QRCodeLogin / FaceVerifyModal / ZoneHistoryDialog / ...
├── electron/                     # Electron 28 桌面壳
│   ├── main.ts                   # 单实例锁、窗口/托盘、BackendLifecycle、退出收尾、自动更新
│   ├── backendManager.ts         # 纯逻辑：后端 owner 状态机 + 端口占用判定
│   └── preload.ts                # contextBridge IPC
├── dist-electron/                # TS 编译产物（gitignore）
├── build/installer.nsh           # NSIS 自定义片段：覆盖安装前保全旧资料（nsis.include）
└── package.json                  # 构建链
```

## 初次使用

1. 安装Python及其依赖：
    cd backend
    pip install -r requirements.txt

2. 安装nodejs后前端构建：
    npm install

## 快速开始

npm run dev:electron    # 一键启动桌面应用（源码模式，后端走 python backend/run.py）

## 构建

    npm run build           # 正式构建：后端冻结程序 → 前端 → Electron 编译与打包（--publish never）
    npm run build:backend   # 只重建 backend/dist/run.exe
    npm run build:frontend  # 只重建 frontend/dist
    npm run build:electron  # 只做 tsc + 图标 + electron-builder（含 --publish never）

正式入口按**固定顺序**执行，前一步失败立即停止，不会把上一轮留下的 `run.exe` 装进包：

1. `pyinstaller backend/run.spec` → `backend/dist/run.exe`
2. `vite build` → `frontend/dist/`
3. `tsc` + 图标 + `electron-builder` → `release/`

产物：`release/直播控制系统 Setup <version>.exe`（NSIS 安装版）、
`release/直播控制系统 <version>.exe`（便携版）、`release/win-unpacked/`。
版本号取自 `package.json` 的 `version`。构建完成后脚本会核对
`release/win-unpacked/resources/backend/run.exe` 与本次构建的 `run.exe` **逐字节一致**，
并把提交、Node/Python 环境、种子来源与各产物 SHA256 写进 `release/build-manifest.json`
与 `release/build-release.log`。

环境（可用环境变量覆盖）：构建输入/解释器与包内**不包含**账号、任务库、Cookie、
状态档、日志或真实通知配置——只含应用与明确的公共构建资源（分区种子）。

- Node：`BUILD_NODE`，默认 `F:/livestream/tools/node-v22.20.0-win-x64/node.exe`（本机已确认的 Node 22）
- Python：`BUILD_PYTHON`，默认 `F:/python/python.exe`（本机带全量后端依赖 + PyInstaller）
- 分区种子：`BILIBILI_AREA_SEED_FILE`；未设置时用未跟踪的
  `backend/build-inputs/bili_areas_full.json`，两者都没有则**不含种子**照旧成功
  （首启显示"分区待获取"）。种子只在数据目录缓存缺失时安装一次，不覆盖已有缓存。

### 任务各项参数及计算方法

- **类别**：填入每日需要完成小时数，已完成或未开始置0
- **需要完成天数**：根据实际要求填写（个人喜欢增加10%冗余）
- **已完成天数**：一般不用动，按需修改
- **截止时间**：默认为当天的23：59
- **今日是否完成**：1为今日已完成，其余为未完成
- **距离完成**：需要完成天数-已完成天数。辅助计算，excel看起来顺眼点
- **松弛度**：截止日期-今日日期-距离完成+1（余裕天数：同日则截止日期-今日日期=0，若距离完成=1则今日内必须完成否则寄了）若类别==0，则置-1
- **优先度**：（松弛度+1）*100-距离完成。松弛度相同的情况肯定是任务剩余流程越长越紧迫，优先做。若类别==0，则值为10000+今日日期-截止日期（=10000说明任务今日截止，<10000说明还未截止...也没啥别的意思，导出出来已完成的任务也能按截止时间排序，方便强迫症）
- **剩余时间**：当前所有任务总时长数，对于每个任务的类别*距离完成求和。放视图里方便我感觉任务多不多
- **平均剩余时间**：对于每个进行中任务的截止日期-今日日期的平均值+1，表示进行中任务平均还有多少天截止（不足一天算一天）。放视图里方便我感觉任务多不多
- **紧迫率**：剩余时间/平均剩余时间/20，辅助表示当前任务紧迫程度。其实也不是很准，尤其是有很多任务堆在一个时间截止且临近截止的时候。个人感觉120%以下算轻松，150%以下可能还能操作，150%以上不然还是删点吧不然我这贪心能让好多任务白忙活
- **任务调度机制**：选取待执行状态下优先度最低的任务；跨日即中断并将当前任务抛弃，等待重置后选取下一个任务

### 注意事项
- 初次使用建议刷新下分区，你B时常上线下线分区的
- 初次开播后推流码和rtmp地址可在设置中复制
- 需要excel格式直接导出一份即可，用excel填写任务后导入的话仅需填写分区名、类别、需要完成天数、已完成天数、截止时间五列即可，其他导入进去会自己算。哦对了导出的计算数据都是静态值，仅代表导出当天计算结果
- 可选视频文件夹路径（至少一个default子文件夹，其他你看心情）不需要就把自动打开视频关了
- 想用ffmpeg推流自行配置ffmpeg路径
- 关闭窗口默认最小化到托盘，完全退出需右键托盘图标 → 退出
- beta分布期望是0.25，倍率算法是下限+（上限-下限）*随机数（0-1）

## 文档导航

| 文档 | 内容 |
|------|------|
| （本地工作文档）`HANDOFF.md` | 交接草稿：运行链、存储、API 与历史勘误。**它在本仓库 `.gitignore` 内，不随提交分发**——需要交付的现行说明请以本 README 与 `docs/` 下的阶段文档为准 |
| [docs/SERVER-PORT-PHASE1-20261009.md](docs/SERVER-PORT-PHASE1-20261009.md) | 移植阶段 1：后端核心与旧数据接管 |
| [docs/SERVER-PORT-PHASE2-20261010.md](docs/SERVER-PORT-PHASE2-20261010.md) | 移植阶段 2：页面接入、退出与防多开 |
| [docs/SERVER-PORT-PHASE3-20261010.md](docs/SERVER-PORT-PHASE3-20261010.md) | 移植阶段 3：打包与精简收尾（构建链、实际包验证） |
| [docs/SERVER-PORT-PLAN-20261009.md](docs/SERVER-PORT-PLAN-20261009.md) | 三阶段移植计划（未跟踪的他人文档，只读） |
