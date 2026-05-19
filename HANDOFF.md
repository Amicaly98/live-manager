# 项目交接文档 — 直播控制系统

## 项目概览

一站式桌面直播管理工具。前端 Vue3+ElementPlus，后端 Python FastAPI，桌面壳 Electron28。

**启动**: `npm run dev:electron` (项目根目录)

## 目录要点

| 路径 | 说明 |
|------|------|
| `backend/app/core/task_manager.py` | 核心：SQLite 存储、公式计算、排序、统计、每日简报 |
| `backend/app/core/db.py` | SQLite 数据库层：CRUD、自动迁移、导入导出校验 |
| `backend/app/core/live_controller.py` | 核心：B站 API(含 ts/build/version 签名)、分区加载、直播控制、FFmpeg 推流、后端事件推送 |
| `backend/app/core/email_sender.py` | 推送通知：SMTP 邮件 + Server酱、人脸验证远程确认 HTTP 服务 |
| `backend/app/api/live.py` | 直播 API：start/stop/resume/status/state |
| `backend/app/api/tasks.py` | 任务 CRUD + 统计 + 导入导出(分区名校验) |
| `backend/app/api/settings.py` | 设置持久化(含 stream_mode/ffmpeg_reencode/通知渠道) |
| `backend/app/api/email.py` | 邮件测试发送 + 人脸验证远程确认回调 |
| `backend/app/dependencies.py` | 全局单例容器（解耦循环导入）—— 所有 API 模块从这取实例 |
| `frontend/src/views/Dashboard.vue` | 双模式、进度条(store计时)、统计面板、操作日志(localStorage)、恢复提示 |
| `frontend/src/views/TaskManager.vue` | 任务列表、新建/编辑(含分区校验)、导入导出 |
| `frontend/src/views/Settings.vue` | 推流方式、FFmpeg重编码开关、推送通知(邮箱/Server酱) |
| `frontend/src/stores/live.ts` | 直播状态 + 本地计时器 + 跨页面后端事件轮询 |
| `frontend/src/stores/settings.ts` | 设置 store（updateField 泛型、保存门控防死循环） |
| `electron/main.ts` | 窗口/托盘/后端管理/关闭确认弹窗/stopBackend(graceful) |

## 关键公式

```
# 字段语义
C (category): 0=已完成, >0=每日基准小时数(如2=每天2h)
actual_days = total_days（不额外增加）

# 计算列
A (a_val) = IF(C>0, (J+1)*100 - I, 10000)     # 优先度(越小越优先)
I (i_val) = IF(C=0, 0, total_days - days_done)  # 距离完成天数
J (j_val) = IF(C>0且deadline有效, (deadline-今天)+1-I, -1)  # 松弛度

# 直播时长 = 基础×下限 + 基础×(上限-下限)×随机因子(0~1)
duration = base×lo + base×(hi-lo)×factor(0~1)
base = category × 3600s
lo/hi 从设置读取，默认 1.05/1.25

# 统计（修正后）
remaining_time = Σ(I × C)（C=每日小时数）
avg_remaining = AVG(J|J>-1) + AVG(I|I>0)
urgency = remaining_time / max(1, avg_remaining × 20)
```

## 存储架构

- **主存储**：`live_tasks.db` (SQLite3, WAL 模式, 线程安全)
- **导入导出**：`live_tasks.xlsx` (仅用于导入导出, 不再作为主存储)
- **首次启动**：DB 为空时自动从 Excel 迁移；旧 schema(含 extra_hours/priority) 自动检测降级
- **状态文件**：`live_state.json` 保存直播进度(含 duration_seconds)，`task_manager_last_run.json` 保存上次运行日期
- **设置文件**：`settings.json`（含 stream_mode, ffmpeg_reencode, notification_channel, email_*, serverchan_sendkey 等），`bili_cookies.json`

### tasks 表结构
```
id, zone_name(UNIQUE), category, total_days, days_done,
deadline_raw, today_done, remaining_days,
a_val, i_val, j_val, created_at, updated_at
```
(无 extra_hours, priority 已合并到 a_val)

## 导入导出格式

### 导入 Excel 列
A=优先度 B=分区名 C=类别 D=需要完成天数 E=已完成天数 F=截止时间 G=(跳过) H=今日是否完成

校验：D 列必填>0，F 列必须可解析为有效日期，C 列空时默认2。分区名与 bili_areas_full.json **完全匹配**。

### 导出 Excel 列（宋体16pt）
A=优先度 B=分区名 C=类别(默认2) D=需要完成天数 E=已完成天数 F=截止时间(如 2026/5/21) G=今日是否完成 H=距离完成天数 I=松弛度
J/K 列 1-3 行放统计（剩余时间/平均剩余/紧迫率），列宽=汉字数×3.3(A=10,B=30,C=7)

## B站 API 调用要点

| 方法 | 端点 | 关键参数 |
|------|------|---------|
| startLive | room/v1/Room/startLive | platform:pc_link, ts/build/version 从 click/now + liveVersionInfo 获取, 全部 _appsign 签名 |
| stopLive | room/v1/Room/stopLive | platform:pc_link |
| updateArea | room/v1/Room/update | platform:pc_link |
| 状态检查 | room/v1/Room/room_init?id= | (非 get_status_info_by_roomId) |
| 登录轮询 | passport-login/web/qrcode/poll | 从响应 cookies 提取 SESSDATA/bili_jct/DedeUserID |
| 获取 room_id | getRoomInfoOld / space/acc/info / room_id_by_uid | 三接口容错 |

## 直播状态生命周期

1. 开播 → `LiveState.start_streaming_with_duration(zone,room,duration)` → 写入 live_state.json
2. 监控线程 30s/次：更新 elapsed → 调 save()(每次) → 检查跨日 → 检查时长到期
3. 状态检查仅网络错误时重连(live_status=0 对自行推流是正常的)
4. 关闭应用：弹窗确认 → [停止并退出]调 API 停播 / [不停止并退出]直接 kill → live_state 保留
5. 下次启动：live_state 非当天则弃置；当天且残留则 UI 显示恢复提示条(不自动恢复)
6. 恢复：POST /api/live/resume → 使用保存的 duration_seconds，不复随机
7. 注意：`/api/live/state/full` 不再每次 reload()，改用手动调用 `/api/live/state/reload`；`current_zone` 非空即视为有未完成任务

## 最近改动（截至 2026-05-16，本会话）

### FFmpeg 推流稳定性
1. **防双线程并发**：`_ffmpeg_stop_event` + 启动前 join 旧线程，消除 `poll()` 竞态和 concat 文件冲突
2. **编码兼容性检查**：ffprobe 比对 `(codec, width, height, pix_fmt, profile)`——不含 level（level 5.0 vs 5.1 兼容）
3. **重编码回退**：设置 `ffmpeg_reencode` 控制编码不一致时自动重编码 vs 跳过不兼容视频
4. **长会话异常退出**：主动停止(任务完成)不再误报"concat 异常退出"
5. **永不放弃**：连续 5 次快速失败后切换长间隔重试(每5分钟)，不再退出推流循环
6. **WSAECONNABORTED**：退避重试，不无谓刷新 URL（推流码持久绑定）

### 推送通知模块
7. **EmailSender**：SMTP 邮件异步发送，限频(同类型3分钟)，支持 QQ邮箱
8. **Server酱**：微信推送，`notification_channel` 设置选 email/serverchan
9. **触发事件**：开播/完成/重连/人脸验证/异常/每日简报
10. **人脸验证远程确认**：邮件含一次性 token 链接 → HTTP 服务端口(默认19080) → 确认后自动完整开播
11. **每日简报**：跨日重置前捕获前一天数据(统计+Top5)，HTML 格式发送

### 操作日志
12. **localStorage 持久化**：跨页面、跨启动保留，限 50 条，显示日期+时间，默认滚底
13. **事件来源统一**：后端 `_push_backend_event` 为唯一来源，前端轮询不再自生成——消除重复消息
14. **liveStore 后台轮询**：`startEventPolling()` 在 App.vue 挂载，跨页面持续写入 localStorage

### 直播控制
15. **轮询间隔**：前端 + 后端统一读取 `scan_interval_seconds` 设置（默认30s）
16. **手动模式停播预填**：`lastManualZone`/`lastManualDuration` 存 sessionStorage，StreamConfigurator 接收 defaultZone/defaultDurationMinutes props
17. **任务完成后自动标记全部完成**：`mark_task_done` 检测 `days_done >= actual_days` → 自动设 `category=0`
18. **LiveState 恢复提示**：`current_zone` 非空即显示，不依赖 `is_streaming`
19. **开播事件推送**：`start_streaming` 成功后调 `_push_backend_event('开播', ...)` 触发通知

### 设置页
20. **自动保存防死循环**：`_saveGate` 门控 + 仅回写关键字段
21. **倍率精度**：step=0.001，自然小数显示（第三位0显示两位）
22. **视频检查**：匹配 VideoPathFinder 逻辑（子目录+default 文件夹）
23. **推流码显示**：拆分为服务器地址 + 串流密钥，掩码/复制

### 工程修复
24. **循环导入**：`live.py`/`tasks.py` 改从 `dependencies` 导入，非 `main`
25. **TS 类型**：`updateField` 放宽为 `any`，模板箭头函数无类型标注
26. **清理 emoji**：log 和推送内容全部去掉 emoji，用纯文本标签
27. **缩进修复**：`main.py`/`run.py`/`dependencies.py`/`db.py` 被格式化工具破坏后完整重写

## 存储新增

| 文件 | 用途 |
|------|------|
| `rtmp_cache.json` | RTMP 推流码按 room_id 缓存 |
| `ffmpeg.log` | FFmpeg 输出日志（诊断用） |
| `backend.log` | 后端全量日志 |
| `settings.json` | 含 email/notification_channel/serverchan_sendkey/ffmpeg_reencode 等新增字段 |

## 新增 API 端点

| 端点 | 说明 |
|------|------|
| `POST /api/live/state/reload` | 重新加载 live_state.json |
| `POST /api/live/state/update` | 更新 live_state 字段 |
| `GET /api/live/state/full` | 获取完整状态(含 duration_seconds) |
| `POST /api/live/confirm-face-verify` | 确认人脸验证完成 |
| `POST /api/live/switch-area` | 手动模式直播中切换分区 |
| `GET /api/settings/check-videos` | 检查视频文件夹 |
| `GET /api/settings/rtmp-code` | 获取缓存的推流码 |
| `POST /api/email/test` | 发送测试推送 |
| `POST /api/email/confirm-face-verify` | 远程确认人脸验证（邮件链接回调） |

## 已知问题

- `vue-tsc 1.8` 不兼容 Node24
- FFmpeg `-c copy` + concat 要求所有视频编码参数一致（已通过 ffprobe 检查+重编码回退缓解）
- 人脸验证 URL 硬编码 `mid={DedeUserID}`，B站变更需同步
- 手动模式 OBS 被掐无法自动恢复 OBS 推流，仅重开 B站房间
- B站 RTMP 偶发 WSAECONNABORTED，可能与平台反作弊/节点不稳有关
- `live_state.json` 中 `current_zone` 非空即触发前端恢复提示（之前要求 is_streaming=true）
