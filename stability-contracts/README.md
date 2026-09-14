# stability-contracts —— 双仓共同契约

这一目录是**服务器版与桌面版共享行为契约**的维护位置（放在服务器仓库只表示
维护位置，**不代表服务器实现是标准答案**）。目标是"已知共同缺陷修好、对应行为
有证据、以后不会漏评另一端"，不是承诺软件零风险。

## 目录内容

| 文件 | 作用 | 是否双仓镜像 |
|---|---|---|
| `contracts.json` | 契约台账：稳定编号、要求、适用产品、有意差异、覆盖边界 | 是（逐字节一致） |
| `scenarios/contract_core.py` | 与产品无关的时序与断言（不 import 任何产品 app 包） | 是（逐字节一致） |
| `scenarios/run_contracts.py` | 单产品运行器（CLI） | 是（逐字节一致） |
| `scenarios/__init__.py` | 包标记 | 是（逐字节一致） |
| `scenarios/adapter_server.py` | 服务器端薄适配器：把场景接到本仓真实入口 | 否（端内自持） |
| `scenarios/adapter_desktop.py` | 桌面端薄适配器：把场景接到本仓真实入口 | 否（端内自持） |
| `upstream.json` | 镜像来源仓库、契约提交、镜像文件 SHA256 | 是（各自记录同一来源提交） |

## 怎么跑（两端必须分别起独立进程）

两个仓库的包名都是 `app`，**不能在同一个 Python 进程里混跑两端**。

```powershell
# 服务器端
$env:PYTHONIOENCODING='utf-8'
& <python> <server>/stability-contracts/scenarios/run_contracts.py `
    --product server --json <out>/contract-results-server.json

# 桌面端（需要独立数据目录，避免碰真实账号数据）
$env:PYTHONIOENCODING='utf-8'; $env:BILIBILI_SKIP_MIGRATION='1'
& <python> <desktop>/stability-contracts/scenarios/run_contracts.py `
    --product desktop --data-dir <tmp> --json <out>/contract-results-desktop.json
```

退出码 0 = 全部通过；1 = 存在失败。**没有"环境跳过"档位**：控制器构造失败也按
失败记录，环境问题与断言失败在 JSON 里分开呈现（`error` 字段带异常类型）。

各仓也提供一个薄 pytest 包装：`backend/tests/test_cross_repo_contracts.py`，
使共同契约进入该仓的常规回归。

## 规则（避免这类机制退化成形式）

1. **同一场景、同一断言描述在两端都执行**。产品差异必须写进
   `contracts.json` 的 `intentional_differences` 并由适配器显式处理，不能靠 skip 隐藏失败。
2. **适配器只允许**：构造真实控制器、调用真实入口、观察（替换平台/进程边界、
   安装观察点、把某产品的单体入口收窄到受理阶段）。不得在适配器里重写被测状态机。
3. **镜像只针对上面列明的文件**。对镜像逐文件计算 SHA256 记入 `upstream.json`；
   同步前先干运行列出文件差异，禁止盲删目录，也禁止整文件复制 `live_controller.py`。
4. **每个修复都要回答"影响另一端吗"**。台账状态取 `verified` / `failed` /
   `pending` / `not_applicable` / `deferred` / `accepted_risk`；`accepted_risk` 必须
   写明用户接受的范围。对端无影响要写原因，不能留空当默认。
5. **证据绑定提交与源码快照**。工作区有改动时必须记录差异哈希，不能只贴 HEAD。
6. 本目录**不是 CI、也不是行为正确性证明**。HEAD 一致不等于测试通过；
   `check_alignment.py` 只做台账新鲜度核对。

## 本轮（2026-09-14）落地范围

- 已落地场景组：`CTRL-01`（5 个场景，含"旧重连等待→停止→新意图→旧响应返回"的
  Event 精确交错）、`CTRL-02`、`PUSH-01`、`MODE-01`、`INTENT-01`。
- 未纳入共同场景、只在各自仓库测试里覆盖：`FFMPEG-01`、`LOG-01`。
- 登记为共同后续风险（不改阈值/不加全量探测）：`RETRY-01`、`MEDIA-01`。
- 登记为待对齐：`CTRL-03`；`DATA-01` 桌面已验证、服务器 `not_applicable`。

各项的状态、证据路径与覆盖边界见 `contracts.json`。
