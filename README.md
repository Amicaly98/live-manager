# 直播控制系统 - 一站式自动化直播管理工具

> 面向主播的桌面直播管理工具，集成账号授权、直播间配置、推流信息获取、配置直播日程、自动化选取日程切换直播分区。

## 项目架构

```
bilibili-live-manager/
├── assets/                   # 图标资源（Sharp 自动生成）
├── scripts/generate-icons.mjs
├── backend/                  # Python FastAPI 后端
│   ├── app/
│   │   ├── main.py           # 入口 + 生命周期 + CORS
│   │   ├── dependencies.py   # 全局单例（解耦循环导入）
│   │   ├── api/              # auth / tasks / live / areas / settings / email
│   │   ├── core/             # task_manager / live_controller / db / email_sender / config
│   │   └── models/schemas.py # Pydantic 模型（含 actual_days 公式）
│   ├── requirements.txt
│   └── run.py
├── frontend/                 # Vue 3 + Vite + Element Plus
│   ├── src/
│   │   ├── api/request.ts    # Axios 泛型封装（重连/拦截/类型安全）
│   │   ├── components/       # QRCodeLogin / StreamConfigurator / Sidebar / FaceVerifyModal
│   │   ├── composables/      # usePolling / useCache(TTL) / useNotification
│   │   ├── stores/           # Pinia: auth / live / tasks / settings
│   │   ├── types/api.ts      # 与后端 schemas 对齐
│   │   └── views/            # Login / Dashboard(双模式) / TaskManager / Settings
│   └── vite.config.ts        # Element Plus 按需导入 + API proxy
├── electron/                 # Electron 28 桌面壳
│   ├── main.ts               # 窗口/托盘/后端管理/自动更新/端口释放
│   └── preload.ts            # contextBridge IPC
├── dist-electron/            # TS 编译产物
├── package.json              # 构建链 (concurrently + wait-on + electron-builder)
└── .gitignore
```

## 快速开始

```bash
npm run dev:electron    # 一键启动桌面应用
```

## 构建

```bash
npm run build           # 完整构建 → release/
```

## 核心功能

- 任务管理：SQLite 本地存储 + Excel 导入导出 + 每日重置 + 自动完成检测
- 直播控制：双模式(任务/手动) + 拼音首字母分区搜索 + 自行推流/FFmpeg推流(编码检测+重编码回退)
- 统计面板：剩余时间 / 平均剩余 / 紧迫率 / 今日执行
- 设置：随机时长分布(Beta/正态/均匀) + 断连重试次数 + 推流模式 + FFmpeg重编码开关
- 推送通知：SMTP邮件 + Server酱微信推送，开播/停播/异常/人脸验证/每日简报
- 人脸验证远程确认：邮件链接一键确认并自动重试开播
- 组件：props/emits 解耦 + 泛型 Axios + TTL 缓存 + Excel 导入导出
- 直播进度：实时进度条(每秒刷新) + 异常检测 + 断电恢复提示
- 操作日志：localStorage 持久化，跨启动保留最近50条

## 关键设计决策

- 任务数据主存储从 Excel 迁移至 SQLite (`live_tasks.db`)，Excel 保留为导入导出
- 首次启动自动从旧 Excel 迁移；旧 schema 自动降级
- 截止日期解析统一处理 datetime/=DATE()/序列号
- `category`: 0=已完成，>0=每天基准小时数；直播时长 = category×3600×随机倍率
- B站 API 调用需 ts/build/version + _appsign 签名 (platform=pc_link)
- 进度条计时存入 Pinia store 跨页面持久
- Electron 关闭弹窗支持[停止并退出]/[不停止并退出]/[取消]
- `freePort()` 只杀 LISTENING 且 PID>4 的进程
- `waitForBackend()` 健康检查后才加载窗口

## 发布

1. 修改 `publish.owner/repo`
2. `npm run build`
3. 上传 GitHub Release
