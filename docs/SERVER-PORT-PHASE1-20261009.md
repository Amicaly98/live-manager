# 桌面移植阶段 1 交付说明（后端核心与旧数据接管）

日期：2026-10-09。分支：`port/server-core-20261009`（父 `aba4ea7`，本地三个未推送提交原样保留）。

- 本次提交：`ca51a98900d8fb0a373ddecf7c467f4d91f2a976`
- 移植来源：`bilibili-live-server` @ `8bd2fb67ee2a868df70935e7b3498a0a7e6e1e7d`（按该提交读取）
- 未推送、未发布、未改服务端；未进入第二阶段（页面/退出交互）与第三阶段（打包）

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
