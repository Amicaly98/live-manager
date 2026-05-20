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
│   └── vite.config.ts        # Element Plus  + API proxy
├── electron/                 # Electron 28 桌面壳
│   ├── main.ts               # 窗口/托盘/后端管理/自动更新/端口释放
│   └── preload.ts            # contextBridge IPC
├── dist-electron/            # TS 编译产物
├── package.json              # 构建链 
└── .gitignore

## 初次使用

1. 安装Python及其依赖：
    cd backend
    pip install -r requirements.txt

2. 安装nodejs后前端构建：
    npm install

## 快速开始

npm run dev:electron    # 一键启动桌面应用

## 构建

npm run build           # 完整构建 → release/

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

