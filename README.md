直播控制系统 - 一站式自动化直播管理工具
 
面向主播的桌面直播管理工具，集成账号授权、直播间配置、推流信息获取、配置直播日程、切换直播分区、邮件推送等功能。

## 项目架构


live-manager/
├── assets/                   # 图标资源
├── scripts/generate-icons.mjs
├── backend/                  # Python FastAPI 后端
│   ├── app/
│   │   ├── main.py           # 入口 + 生命周期 + CORS
│   │   ├── dependencies.py   # 全局单例
│   │   ├── api/              # auth / tasks / live / areas / settings / email
│   │   ├── core/             # task_manager / live_controller / db / email_sender / config
│   │   └── models/schemas.py # Pydantic 模型（含 actual_days 公式）
│   ├── requirements.txt
│   └── run.py
├── frontend/                 # Vue 3 + Vite + Element Plus
│   ├── src/
│   │   ├── api/request.ts    # Axios 泛型封装
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
├── package.json              # 构建链 
└── .gitignore

## 快速开始

npm run dev:electron    # 一键启动桌面应用


## 构建

npm run build           # 完整构建 → release/


## 核心功能

有空再详细说明

