# 桌面移植第二阶段交付说明（页面接入 + 退出/防多开适配）

- 日期：2026-10-10
- 分支：`port/server-core-20261009`（入场 HEAD `f943b4329df2edc5a309b4064b562e455646e66f`；
  初稿误写为 `port/server-20261009`，随返修勘误）
- 移植来源（只读）：服务端冻结提交 `8bd2fb67ee2a868df70935e7b3498a0a7e6e1e7d`
- 状态：**第二阶段候选完成，待独立复核**。未推送、未发布、未进入第三阶段。

## 一、提交范围（本轮实际修改）

### 前端（页面接入新协议）

- `src/api/authWire.ts`、`src/api/liveControl.ts`、`src/types/session.ts`：从来源提交原样迁入的
  登录/控制协议层（扫码代次 generation、操作令牌、冻结上下文、SessionProjection 类型）。
- `src/api/request.ts`：重写。去掉旧取票链与多实例面板鉴权；写请求携带冻结的
  `X-Account-UID`；控制类请求有界预算 + 同令牌重试 + `_onAttemptLost`（结果未知回调）。
- `src/api/operationToken.ts`：改为客户端生成令牌（去服务端发票 URL）。
- `src/types/api.ts`：补 M1/M3 创建回执/预检/分区历史类型，去掉多实例聚合类型。
- `src/stores/live.ts`、`auth.ts`、`tasks.ts`：按新投影与冻结操作重写（推流来源默认取设置
  `ffmpeg`）；退出语义改为"服务端受理之后才撤销本地身份"。
- `src/stores/settings.ts`：保留桌面读取代际/取消改进，补齐新协议字段。
- `src/views/Dashboard.vue`：迁入服务端版投影消费 + 桌面系统通知接回；移除旧
  `dashboardResume` 组合式（已删除）。
- `src/views/TaskManager.vue`：M1 创建预检→覆盖确认框（进度/截止/去向）→回执/待核对；
  去掉多实例同步创建面板。
- `src/components/ZoneHistoryDialog.vue`：新增（分区历次任务 + 每日完成记录）。
- `src/components/QRCodeLogin.vue`、`FaceVerifyModal.vue`：迁入新版（扫码代次、
  人脸冻结上下文确认，不另发 Start）。
- `src/App.vue`：三种退出语义（停止并退出 / 不停止并退出 / 仍要退出）+ 退出失败横幅。
- `src/utils/startTaskFlow.ts`、`executingTask.ts`：迁入新版。

### Electron（退出与防多开）

- `main.ts`：
  - 退出尝试期间**不再**置 `_forceQuit`，窗口 close 改为"确认收齐后或显式强杀才放行"——
    退出失败时页面保住并显示失败/待核验（实测旧实现会把窗口放走，页面无从交代）。
  - 失败路径**移除原生 showErrorBox**（原生模态阻塞 confirm-quit 的 IPC 回包，横幅永远
    渲染不出来）；失败后主动把窗口带回前台。失败 UI 的唯一权威是页面横幅。
  - `requestManagedQuit` 语义不变（stop_live 分叉、幂等收尾）。
- `preload.ts`：`forceQuit` 改为 invoke（带结果回执）。
- `env.d.ts` / `shims`：同步类型。

### 后端（桌面装配与验收接缝）

- `run.py`：`INSTANCE_LABEL=desktop` 署名；新增 `BILIBILI_ENDPOINT_BASE` 隔离验收接缝
  （缺省缺省 = 真实端点；经 `configure_boundaries` 的既有 loopback 强校验注入，不可能指向
  远程主机）。服务端语义零改动。
- `session_runtime/production.py` + `desktop.py` + `email_sender.py`：桌面通知装配策略——
  桌面不探测云元数据地址（`_host_resolution_error='desktop_product'`），确认链接不拼不可达
  地址；物理投递仍走真实 EmailSender。
- `tests/port_support/loopback.py`：**只增不改**——登录四出口（qrcode generate/poll、
  myinfo、getRoomInfoOld）默认回包 + `/control/*` 验收控制面（计划/闸门/状态/退出）。
- `tests/port_support/loopback_service.py`：新增独立回环平台服务进程。
- `tests/conftest.py`：`test_desktop_email_notifications_111.py` 的退役登记改为"已由真实
  EmailSender 模板演练替代"（不再标注尚无替代）。

### 测试迁移

- 前端：`frontendChain.test.cjs`、`mainStartupExit.test.mjs`、`mainLifecycle.integration.test.mjs`
  重写到新协议；`display-projection.test.mjs`、`cjs-alias.mjs` 新增；旧的
  `effective-time-*`/`optimistic-display` 四个套件由 display-projection 取代（按断言职责迁移）；
  `auth-settings-lifecycle`、`auth-settings-negative`、`overwrite-precondition`、
  `desktop-111-frontend-ux` 更新到新协议/新退出语义。
- 后端：`test_desktop_email_notifications_111.py` 重写为真实 EmailSender 模板演练
  （只换物理投递出口，核对冻结账号/确认信息/文案；桌面策略不探测云元数据）。

## 二、实跑结果（退出码 0 = 通过）

| 验证 | 命令 | 结果 |
| --- | --- | --- |
| 类型检查 | `npx vue-tsc --noEmit` | exit 0 |
| 前端构建 | `npm run build`（frontend） | ✓（eslintrc-auto-import 假 diff 已还原） |
| Electron 编译 | `npm run build:electron-ts` | exit 0 |
| Electron 三模块 | `node --test backend/tests/electron/*.test.mjs` | 23 pass / 0 fail |
| 前端 mjs 套件 | `node --experimental-strip-types --import ./tests/alias-register.mjs --test tests/*.test.mjs` | 12/12 |
| 前端 cjs 套件 | boot-startup / auth-settings-negative / overwrite-precondition / file-url-shell | 全过（4/4、5/5、5/5、PASS） |
| 后端关联用例 | pytest phase1 + rework + email_notifications + data_dir_legacy | 38 passed |
| **端到端真实链路** | `real-chain/real-chain.mjs`（Playwright Electron + 本轮源码后端 + 回环平台桩） | **49/49 通过**（`real-chain/evidence/aggregate.json`） |

替身与真实证据的区别（如实声明）：

- Electron 三测试模块与前端 mjs/cjs 套件使用模块替身（非真实 Electron GUI）。
- 端到端 `real-chain` 是**真实 Electron 页面 → 真实 HTTP → 真实 Core/SQLite/JSON**：
  源码模式（`python backend/run.py`，backend.pid 核验非 run.exe）；隔离 userData/账号根；
  平台出口经 `BILIBILI_ENDPOINT_BASE` 全部指向 127.0.0.1 回环桩（登录四出口经 rewrite 有轨迹
  证明）；外网出口用死代理封闭并记录 `externalAttempts`。
- 推流是**真实 ffmpeg 子进程**写本地文件（rtmp 缓存既有读取路径，目标为本轮 evidence 目录）；
  "媒体中断"= taskkill 该子进程（pid 来自 `media/owned.json` 归属记录），非 OBS 模式冒充。
- 未做：真实 SMTP/Server酱、打包安装冒烟（第三阶段）。

## 三、端到端覆盖链路（run1/run2/run3）

- **登录**：页面扫码 → 真实 HTTP → 回环桩扫码代次 → 核验/安装 → 控制台。
- **创建/覆盖**：预检 → 提交 → 列表；覆盖确认框展示进度/截止/去向；确认后新任务新身份、
  旧任务入历史（分区历史接口核对 groups）。
- **结果未知两段**：① 回包丢失但请求已真实落库 → 同令牌核对持久回执**自动收敛**为已创建
  （不重复创建，新身份从 0 开始）；② 请求未送达 → "没有得到回包"横幅（原 UID/原令牌被持有）
  → 同令牌重试收敛。
- **开播/媒体**：开始下一任务 → 平台开房（回环桩）→ 真实 ffmpeg 推流（输出文件增长）→
  taskkill 子进程后：同 Run 保持、平台房间仍开播、检查点不回退、页面"直播进度 20秒"未清零。
- **停止/恢复**：平台关房、投影如实；恢复保持原 Run 身份（run_id=run-3）、检查点不回退。
- **退出**：停止被闸 → 503 → 页面"退出未完成"横幅 + 应用保留 + 后端未销毁 + 房间未关；
  放闸重试 → 安全退出、平台关房。不停止并退出 → 不发平台 Stop、房间仍开播、本地后端收回。
- **单实例**：第二实例 exit=0、首实例保持、8000 端口单 LISTEN、PID 未变、已有窗口被唤起。
- **dist file:// 窄验证**：junction 让非打包布局拉起源码 run.py；`file://…/dist/index.html` 加载、
  静态资源无错误、受控退出正常。完整安装包冒烟留第三阶段。
  独立复核指出：候选驱动 run3 的退出观察原为 `until(() => (exited = true), …)`，条件函数
  第一次求值即自行置位，"3.04 受控退出正常"是恒真而非退出证明；返修已改为先登记
  `app.process().once('exit')` 再等待真实退出并核对后端进程收回（超时/未确认如实失败），
  修正后只窄跑 file:// 路径复核（见返修记录）。

## 四、本轮发现并修复的产品缺陷（端到端抓到的）

1. **退出失败窗口被放走**：退出尝试一开始就置 `_forceQuit`，失败时窗口已销毁，"页面保留并
   显示失败/待核验"落空。已改为确认收齐后才放行 close，失败后主动 show+focus。
2. **原生错误框阻塞页面横幅**：失败路径弹原生 showErrorBox 阻塞 confirm-quit 的 IPC 回包，
   页面横幅永远渲染不出来。已移除（页面横幅为唯一失败 UI），同步更新
   `mainLifecycle.integration.test.mjs` 的契约断言（dialogs 0 次）。

## 五、已知事项 / 未验证

- **弹框期间版本被改写即拒绝**：端到端曾跑通（create_stale_revision 拒绝 + 旧任务 id 不变），
  但该真实交互在 dist 构建下未稳定取得（确认框打开约 1–2 秒后自行关闭），**根因未定**；
  协议拒绝语义由窄用例证明（`overwrite-precondition` 前端 5/5 与后端
  `create_stale_revision` 用例）。独立复核指出：初稿"属于驱动层、与产品语义无关"的归因
  未被证明，已按其要求收窄为"根因未定"。
- 单实例验证中 WMI 进程清单出现 2 条同 data-dir 的 run.py 条目（瞬时窗口），权威判据
  （8000 端口单 LISTEN、PID 未变、第二实例 exit=0）全部成立，如实记录。
- 真实平台登录、真实 SMTP/Server酱、打包安装冒烟：按边界留第三阶段。
- `docs/SERVER-PORT-PLAN-20261009.md` 保持未跟踪（原有计划文档，不动）。

## 七、返修记录（2026-10-10，独立复核 6b9c10b 暂不通过后）

独立复核（`deliveries/desktop-port-phase2-review-6b9c10b-20261010/REVIEW.md`）发现创建流程
四项缺口（R1/R2/R3 P1，R4 P2），退出/防多开/通知/显示适配的既有成果保留。返修集中在
`frontend/src/views/TaskManager.vue`，复用 task store / request 层的既有接缝
（`onAttemptLost`、`writeScopeIsCurrent`、会话存储命令持有），未新增服务器 attempt 表或
第二套状态机，未改 Core / 退出协议 / 通知装配：

- **R1（换号不清除待核对命令）**：`restorePendingCreate` 不再因 UID 不符删除会话存储；
  `checkPending`/`retryPending` 在账号不符时只停用动作并如实说明（模板同步禁用按钮），
  原命令保留，切回原账号后仍可核对。绝不改用新账号重放。
- **R2（后一次拒绝不覆盖前序未知）**：`submitCreation` 接上 `createNewTask` 已有的
  `onAttemptLost`——本轮有物理尝试在途丢失时，后续收到的拒绝只走"查原令牌回执 →
  查不到继续持有"的未知路径；对已持有命令的重试被拒（含 `create_stale_revision`）
  不再销账。无前序未知的首次明确拒绝仍正常拒绝（G1 语义不变）。
- **R3（发送前先落命令）**：原命令（原 UID + 原令牌 + 原冻结载荷）在写请求可能离手
  **之前**写入会话存储；确定终态（成功 / 无前序未知的明确拒绝）才消费这一项。
- **R4（页面生命周期与回包落点）**：`runCreateFlow` 在预检回包与确认框放行后核对
  `pageAlive`；`submitCreation` / `lookupReceipt` 的回包落点共同核对页面存活与
  `writeScopeIsCurrent`。失效页面不继续新投递、不弹旧结果、不改展示；已发出的请求
  不回滚，原命令保持持有。
- **驱动采证修正**：`real-chain.mjs` run3 的退出观察改为登记真实 `exit` 事件后等待
  （原 `until(() => (exited = true))` 恒真），并新增 3.05"退出后后端进程已收回"；
  增加 `--only=run3` 窄跑入口；后端监视器区分受控退出的预期收尾与意外崩溃。
- 交付说明勘误：分支名 `port/server-20261009` → `port/server-core-20261009`；
  "确认框自行关闭"归因收窄为"根因未定"（见第五节）。

返修验证与证据：`deliveries/desktop-port-phase2-create-fix-20261010/`（冻结监督脚本
原样复跑、run3 窄跑、真实浏览器"在途刷新"窄验证、类型检查与前端构建）。

## 八、第二轮返修记录（2026-10-10，复核 92510d3 暂不通过后）

第二轮复核（`deliveries/desktop-port-phase2-rereview-92510d3-20261010/`）确认 R1–R4
已转绿、9 项冻结监督复跑通过，另以 5 项交错探针钉出两处共同职责（S1/S2 P1、S3 P2），
集中在 `frontend/src/views/TaskManager.vue` 的持有/消费/发送接缝；退出、防多开、通知、
显示与 Core 零改动，未新增服务器 attempt 表、后台投递或状态机：

- **S1/S2（持有与销账绑定具体操作）**：`holdPendingCreate` 拒绝覆盖**另一个操作**
  （不同令牌）的待核对命令——新账号的一次成功创建不再间接销掉旧账号的未知命令；
  新增 `releasePendingCreate(token)` 作用域消费，所有成功/明确拒绝出口只销**本次
  操作自己的**命令；回执命中即在本操作的列表刷新等待**之前**消费，迟到的旧核对
  续体不再持有销账权。槽位被占时本次操作的"发送前落账"按占用降级，不新增
  "禁止创建其他任务"规则。
- **S3（发送前资格复核）**：共同发送入口 `submitCreation` 与 `runCreateFlow` 的
  预检回包、确认放行后续体，都在发送前核对页面存活与 `writeScopeIsCurrent`——
  确认期间换号后，尚未发出的旧账号写请求停止投递（POST=0），不再等回包落点才拦。
- **证据表述更正**：第一轮"在途刷新"浏览器窄验证的 route 处理器对 POST 直接
  return（未 continue），请求**从未送达后端**；其证明范围收窄为"发送前落账、刷新
  恢复、（真实回执 GET）found=false 仍保留"，不含"后端已接收"语义。第一轮 REWORK
  已附更正节（`deliveries/desktop-port-phase2-create-fix-20261010/REWORK.md` §4），
  窄验证脚本口径同步更正。按复核要求未重跑完整浏览器矩阵。

第二轮返修验证与证据：`deliveries/desktop-port-phase2-create-fix2-20261010/`（两套
冻结探针原样复跑 9/9 + 5/5、真实浏览器窄用例复跑、类型检查与前端构建）。

## 六、端到端证据目录

`deliveries/desktop-port-phase2-20261010/real-chain/`

- `real-chain.mjs`（驱动脚本）、`evidence/aggregate.json`（49 项结论）
- 截图：登录、控制台、覆盖确认框、回执收敛、待核对横幅、直播中、媒体中断、退出横幅、
  run2 恢复、run3 file:// 等
- `run1.main.stdout/stderr.log`、`run1.renderer.log`、`vite.stdout.log`、`platform.*.log`
- `run1.final-state.json`、`run1.platform-final.json`、`run2.platform-final.json`
