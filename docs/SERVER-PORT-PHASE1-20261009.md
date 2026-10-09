# 桌面移植阶段 1 交付说明（后端核心与旧数据接管）

日期：2026-10-09。分支：`port/server-core-20261009`（父 `aba4ea7`，本地三个未推送提交原样保留）。

- 本次提交：`ca51a98900d8fb0a373ddecf7c467f4d91f2a976`
- 移植来源：`bilibili-live-server` @ `8bd2fb67ee2a868df70935e7b3498a0a7e6e1e7d`（按该提交读取）
- 未推送、未发布、未改服务端；未进入第二阶段（页面/退出交互）与第三阶段（打包）
- 独立复核（R1–R4）的集中返修见 §6，本节以下正文保留首次交付时的现场描述

## 1. 默认链路接线

```
Electron main.ts（单实例锁 / BackendLifecycle / 端口归属）
  → backend/run.py  --data-dir <userData>/data  --account-root <userData>/account-data
      → config.init(data_dir, account_root) → app.main lifespan
          → session_runtime.desktop.build_desktop_owner
              → ApplicationOwner → ControlService → RuntimeInstance → Core
```

旧 `LiveController` **不再被默认构造**（健康面 `live_controller` 恒为 `null`，`control_service` 为 `true`）。
它仍留在仓库里，作为 `BilibiliApi` / `AreaLoader` / `VideoPathFinder` 等纯能力的宿主，
以及旧用例的对照材料；桌面不再有旧监控线程、旧业务时钟或第二套日切。

不迁入：`api/instances.py`、`api/panel.py`、`core/instance_console.py`、`core/panel_auth.py`、
`models/instance_dto.py`。后端**不注册** `/api/panel`、`/api/instance*`，也不启动控制台连接服务
（`config.console_enabled()` 恒为 `False`）。

## 2. 桌面适配点

| 文件 | 适配内容 |
| --- | --- |
| `app/core/session_runtime/desktop.py`（新） | 三条桌面产品差异：重启只提示恢复不自动开播、首次领取账号目录前先接管旧资料、设置页视频目录是权威 |
| `app/core/session_runtime/production.py` | 三处带标记接缝：`manual_resume_only`（Bootstrap 派发任何 Start **之前**落实）、`account_preparer`（在 `open_account_binding` **之前**调用）、`_apply_saved_video_base` |
| `backend/run.py` | 账号根由启动入口显式传入，缺省 `<数据目录>/../account-data`；数据目录与账号根走同一份解析；`API_HOST/API_PORT` 在导入 config 前入环境；保留 `--mode` / `--legacy-data-dir` / 打包种子 |
| `backend/app/main.py` | 桌面 owner；不注册面板/实例路由与控制台中间件；CORS 保留 `file://` 的 `null`；`/api/shutdown?stop_live=` 接新 owner |
| `app/core/config.py` | 服务端路径模型 + 桌面既有路径访问口合一（`init`/`init_data_dir`/`get_data_path`/`state_file_path`… 指向同一数据目录） |
| `app/dependencies.py` | 去掉实例控制台连接服务 |
| `app/core/area_data.py` | 追加桌面打包种子 `copy_seed_if_missing` |
| `electron/main.ts` | 新增 `getAccountRoot()` 并随启动参数显式传入（`ACCOUNT_DATA_ROOT` 优先） |
| `backend/tests/conftest.py` | 旧控制器夹具改用数据目录内的绝对路径；退役用例集中登记 |

`/api/shutdown` 语义：

- `stop_live=true`（停止并退出）：经**同一** `ControlService` 发停止意图，在有界窗口内等唯一 Core
  收尾（关房核对 → 必要时下播 → 收尾），再用**同一份**剩余预算走统一关闭门；受理 ≠ 平台已关房，
  回执与 `settled` 分开回报。
- `stop_live=false`（不停止并退出）：**不**请求平台下播，只回收自有本地媒体与后端，保留恢复资料。
- 本地资源未确认收齐 → `503 SHUTDOWN_NOT_CONFIRMED` 且**不**放行进程退出；不伪报成功、不误杀外部进程、不提前释放账号锁。

## 3. 迁移与接管

顺序固定：**核身份与来源 → 一致性候选副本 → 转换与核对 → 原子发布 → 账号激活**，全程在
`open_account_binding` **之前**（`desktop.prepare_account_target`），因此不会出现"先登录生成空目标、
再尝试覆盖迁移"。

- 只读预检 + 一致性副本 + 转换 + 归属核对 + `os.rename` 原子发布，全部复用既有
  `session_runtime/account_migrate.py`；同一份来源重跑返回 `already_done`。
- 桌面 `stop_intent.json` → 共享链认识的 `.no_auto_resume`，随既有辅助文件一起进入候选并**随账号目录发布**；
  读不出时按"存在停止意图"处理（不把读失败当成用户没停过）。
- 归属**确证属于别人**（`room_mismatch` / `owner_mismatch` / `state_owner_mismatch` / `run_mismatch`）：
  原件保留、如实报告，本次登录的账号照常拿自己的空库，旧资料等原账号再登录时再迁移。
- **读不出 / 转换不过**（如 `source_db_unreadable`）：**拒绝激活**，不留下会被当成接管成功的空目标。
- 活 SQLite 用一致性副本（整族字节复制 → `wal_checkpoint(TRUNCATE)` → `VACUUM INTO`），源目录一个字节都不动。

真实启动器实测（隔离临时目录，`--legacy-data-dir` 指向合成旧安装目录）：

```
HEALTH        logged_in=true  account={uid:92003, room_id:55003}  live_controller=null  control_service=true
/api/live/status  resumable=true  run_id=legacy-92003  target_seconds=5000  confirmed_seconds=1234
                  goal={wanted:stopped, stop_reason:user, start_kind:new}
accounts/92003/live_tasks.db  tasks=[('旧机分区',2,4,1,'2027-01-31')]  表= tasks/tasks_meta/task_history/task_completion_ledger
accounts/92003/live_state.json schema_version=core-reset-1a.1
启动就绪 1.55s；/api/shutdown?stop_live=false → 200；进程退出码 0
```

即：**旧场的身份、固定目标（5000s）与检查点（1234s）全部保留，启动本身零平台动作**。

## 4. 本轮实际跑过的检查

| 命令 | 结果 |
| --- | --- |
| `python -m pytest tests -q --ignore=tests/electron`（backend） | **63 passed**（含本轮新增 7 条） |
| `node --test backend/tests/electron/*.test.mjs` | **21 passed**（单实例锁 / BackendLifecycle / 端口归属 / 两种退出） |
| `npm run build:electron-ts` | 通过 |
| 真实 `run.py` 隔离目录端到端（无旧资料 / 有旧资料两条） | 见上，均 200 且退出码 0 |

新增 `backend/tests/test_desktop_port_phase1.py` 覆盖的直接事实：

1. 真实桌面默认 lifespan 装配新 owner，旧 `LiveController` 构造次数 `0`；设置页 `video_path` 成为选片根。
2. 持有旧可恢复场次启动：**平台 start/stop/query 全为 0**；goal 落 `stopped`，Run 身份/固定目标/检查点保持。
3. 显式恢复：`platform_start == 1`，身份/目标/检查点仍保持；同令牌重放只回原回执、不产生第二份效果。
4. 「停止并退出」经同一 `ControlService` 受理并真的请求平台下播（`platform.count('stop') >= 1`）。
5. 旧资料确证属于另一房间 → `room_mismatch`，不接管、不建空目标、原件字节不变。
6. 旧库读取失败 → 拒绝激活，不建空目标，原字节不变。
7. 原生账号锁：同 UID 第二次领取被拒；收齐后报告与注册表一致。

平台侧用**本地 loopback HTTP 服务**（`tests/port_support/loopback.py`，逐字节取自服务端
`tests/session_runtime/loopback.py`）真实收请求、真实计次，不接真实 B 站 / RTMP / SMTP / 账号。

### 已退役用例登记

旧控制器私有方法、旧票据协议与旧通知措辞的用例在 `backend/tests/conftest.py:RETIRED_MODULES`
集中登记并停止收集（**文件保留**，未删除）；每条登记了对应业务守卫现在由谁承担
（`ControlService` 令牌与回执、Core 的 Run/Goal 身份与观察、MediaPort 归属、共享 `EmailSender` 等）。
被替换的是被测对象，不是业务要求。`stability-contracts/scenarios/adapter_desktop.py` 仍接到旧控制器，
其共同契约套件在适配器按共享链重接前不适用。

## 5. 未完成 / 未验证（不得报为可发布版本）

- **旧前端页面尚未适配新协议**：`/api/live` 的取票、状态编辑、清空/重载、直播中换区等入口已按设计
  返回 410，`/api/live/start|stop|resume|abandon|restart-after-abandon` 改走操作令牌 + 冻结上下文。
  当前 `frontend/` 的登录、开播、停止、恢复、覆盖、历史页面**均不可用**。→ 第二阶段。
- 退出交互（托盘 / 更新安装 / 系统退出的完整弹窗与 IPC 交错）与防多开联合验证 → 第二阶段。
- 未接真实 B 站 / RTMP / SMTP / 账号；未跑媒体 20 轮、未打 PyInstaller 安装包。
- 未验证：通知（真实 `EmailSender` 模板与参数转换）（§七.7）、日切/账本唯一结算（§七.6 相关）、
  媒体中断时同 Run 进度不回退与媒体恢复单份自有对象（§七.6）、M1 任务覆盖与持久创建回执
  （§七.5 后半）——这些由共享链承担，应在第二阶段连同页面接上后按真实链路取证。
- `README.md` / `HANDOFF.md` 仍描述旧控制器、取票与计时语义，第三阶段随文档一并更正。

## 6. 独立复核 R1–R4 的集中返修

复核报告：`F:/livestream/deliveries/desktop-port-phase1-review-f918cf-20261009/REVIEW.md`
（结论"不通过、停在第一阶段返修"）。四条反例与测试退役范围的处理如下，**未改动
服务端仓库**，未进入第二阶段。

### R1（P1）新账号首次登录被当成"状态不可信"

- **位置**：`app/core/session_runtime/production.py` 的 `_pre_bootstrap`。
- **原因**：桌面 `manual_resume_only=True` 让该函数每次启动都会走到读状态那一步，
  而它把 `SessionStore.load()` 的 `first_start`（主档与副本都不存在）与"状态存在却
  读不出"合并处理，于是**每个新账号的第一次扫码登录**都抛
  "状态不可信，外部停止要求无法落实"、实例未安装。
- **修法**：三条结论分三条路。`first_start` → 没有可续播的原场，也就没有东西要停，
  消费标记后按首次启动继续（Bootstrap 里 `Goal()` 默认 `stopped`，因此仍然零自动
  Start）；**存在**可信快照 → 按原停止语义落实；状态**存在却不可信** → 仍然抛错、
  保留标记。不是"一律放过读失败"。
- **同时覆盖**：只有旧任务库、没有场次档的账号（账号目录里同样没有状态档）。

### R2（P1）同一份旧资料被自动发给两个账号

- **位置**：`app/core/session_runtime/desktop.py` 的 `prepare_account_target`。
- **原因**：迁移入口把**本次扫码 UID** 当成显式授权，旧库未标注归属、又没有场次档时
  两次调用都会 `published`。
- **修法**：自动接管前先按**既有能力**建立归属依据（`_ownership_verdict`）——
  库归属登记（`tasks_meta.account_owner_uid`）→ 有效状态快照冻结的运行 UID →
  旧格式 `LiveState` 记下的房间号（与本次登录房间一致才算）。四类结论：
  - `bound`（线索与本账号一致）/ `unknown`（既没有登记也没有场次身份）→ 可以接管；
  - `foreign`（确证属于别人）→ 原件保留、如实报告，本次账号照常新开；
  - `unreadable`（登记在却读不出）→ **拒绝激活**，不建空目标（读不出 ≠ 没有登记）。
  另外，`unknown` 时这份资料**只能被领取一次**：用既有迁移回执
  （`tasks_meta` 里 `account_migration:<来源指纹>`）扫已经存在的账号目录，已被别的
  UID 领走就保留原件、本次不接管（`reason=claimed_by_other_account`）。扫描在**整族
  字节副本**上做，不去直接打开别人账号目录里的 SQLite。不新增账号系统、不加第二份
  权威文件。

### R3（P1）平台未下播仍允许"停止并退出"

- **位置**：`app/main.py` 的 `/api/shutdown`。
- **原因**：只按本地关闭门的 `ok` 决定成功，`settled=false`（平台持续拒绝关房）时
  仍然返回 200、设 `server.should_exit` 并销毁 owner，调用方失去继续关房的后端。
- **修法**：新增 `_platform_stop_confirmed` 作为退出前置判据——`skipped`（没有控制
  服务/没有已登录账号）判据不适用、不阻断正常退出；回执未受理 → 未确证；已受理 →
  以 `settled`（唯一 Core 是否真的走到 `Goal=stopped` 且 `step=idle`）为准。未确证
  时返回 503 `SHUTDOWN_NOT_CONFIRMED`，**不**销毁运行实例、**不**释放账号占用、
  **不**设 `should_exit`。停止等待与收尾仍共享同一份绝对预算。Core 的停止流程未改。

### R4（P2）拒绝迁移仍改写源文件

- **位置**：`app/core/session_runtime/desktop.py`（`materialize_stop_marker` 已移除）。
- **原因**：桌面停止政策在调用迁移之前就被写进**来源目录**的 `.no_auto_resume`，
  于是 `room_mismatch` 被拒时源目录的摘要已经变了。
- **修法**：停止政策改成**候选目录里的附加文件**：
  `MigrationRequest.extra_auxiliary`（`account_migrate.py` 新增的可选入参，默认空、
  服务端调用方语义逐字不变），在候选组装里写入、随账号目录一起发布。来源目录原有
  的 `.no_auto_resume` 一律**原样保留、不被覆盖**（同名时跳过）。这样"拒绝迁移不动
  来源一个字节"成立，而发布出来的快照本身就是 `stopped`（重开只提示恢复，同时避开
  旧场没有分区缓存时的分区绑定失败）。已登记为共享链的第 4 处桌面适配点：以后同步
  服务端改动时按 `extra_auxiliary` 对账。

### 测试退役范围收窄

- 按模块名整份停收集的 16 个里，`test_a7_e3_data_intent.py` 的
  `test_data_dir_init_and_write_probe` / `test_migration_copies_with_backup_and_never_overwrites`
  / `test_migration_noop_when_same_dir` 与旧控制器无关，且后两条对应的正是 `run.py`
  仍在用的旧安装目录迁移与防覆盖能力 —— 已**拆出**到
  `backend/tests/test_desktop_data_dir_legacy.py`（按当前 `config` 接口重写，语义不变）。
- 旧 `test_a6_ffmpeg_options.py` / `test_d6_log_rotation.py` 断言的函数
  （`build_ffmpeg_command` / `_truncate_ffmpeg_log_inplace`）已不在本仓库；在新链上
  补了**可运行**的替代守卫：`LocalCommandBuilder` 的 argv 形状与 flvflags 位置、
  `OwnedMediaWatcher` 的日志轮转（活着的写入者 + 轮转失败不让观察线程死掉）。
- 仍然**没有**可运行替代用例、如实登记为未覆盖的：桌面通知文案（需要一次真实
  `EmailSender` 模板演练，只换投递出口、不换文案层）与 ffmpeg 所有权/回收门
  （该套件在服务端仓库的媒体端口测试里）。原始文件均保留、未删除。

### 本轮实际跑过的检查

| 入口 | 结果 |
| --- | --- |
| 复核四条冻结反例（`fresh` / `duplicate` / `source` / `account-v2` / `shutdown-v2`） | 全部 rc=0（修前为 4 项业务断言失败），输出留在 `deliveries/desktop-port-phase1-rework-20261009/` |
| `python -m pytest tests -q --ignore=tests/electron`（backend） | **78 passed**（原 63；新增返修 7 条 + 拆出/替代守卫 8 条） |
| 关联守卫 | 正常首次登录、旧任务库无场次档、显式恢复身份/目标/检查点、A→B 换号不串库、`stop_live=false` 正常出口、迁移来源逐字节不变 |

未跑：整仓双树全量、前端构建、electron 测试（本轮未改 `electron/` 与前端）、媒体耐久。

