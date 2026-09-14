# stability-contracts —— 双仓共同契约

这一目录是**服务器版与桌面版共享行为契约**的维护位置（放在服务器仓库只表示
维护位置，**不代表服务器实现是标准答案**）。目标是"已知共同缺陷修好、对应行为
有证据、以后不会漏评另一端"，不是承诺软件零风险。

## 目录内容

| 文件 | 作用 | 是否双仓镜像 |
|---|---|---|
| `contracts.json` | 契约台账：稳定编号、要求、有意差异、**覆盖范围与未覆盖项** | 是（逐字节一致） |
| `scenarios/contract_core.py` | 与产品无关的时序与断言（不 import 任何产品 app 包） | 是（逐字节一致） |
| `scenarios/run_contracts.py` | 单产品运行器（CLI，内嵌被测快照） | 是（逐字节一致） |
| `scenarios/tool_selfcheck.py` | 工具负向自测（证明工具不会假 PASS） | 是（逐字节一致） |
| `scenarios/sync_mirror.py` | 镜像清单/哈希/干运行/来源提交核验 | 是（逐字节一致） |
| `scenarios/__init__.py` | 包标记 | 是（逐字节一致） |
| `scenarios/adapter_server.py` | 服务器端薄适配器：把场景接到本仓真实入口 | 否（端内自持） |
| `scenarios/adapter_desktop.py` | 桌面端薄适配器：把场景接到本仓真实入口 | 否（端内自持） |
| `upstream.json` | 镜像来源仓库、契约提交、镜像文件 SHA256 | 是（各自记录同一来源提交） |
| `backend/tests/test_cross_repo_contracts.py` | 本仓常规回归的薄包装 | 是（逐字节一致） |

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

# 工具负向自测（每端各跑一次）
& <python> <repo>/stability-contracts/scenarios/tool_selfcheck.py --product server
```

退出码：**0** 全部通过**且**被测快照有效；**1** 至少一个场景失败；
**2** 工具/选择错误（未知场景 ID、0 场景选择、适配器构造失败）；
**3** 场景通过但被测快照无效，且本次运行要求有效快照（`--require-valid-snapshot`，
**发布验收必须带这个开关**——行为结果不能替代来源证据）。
`--only` 的未知 ID 与空选择一律非零退出——不会把拼错的验收项悄悄丢掉，
也不会把"跑了 0 个场景"当成通过。

**没有"环境跳过"档位**：控制器构造失败也按失败记录，环境问题与断言失败在 JSON
里分开呈现（`error` 字段带异常类型）。结果 JSON **总是**写出（含失败运行与工具
错误），并内嵌被测快照：

```json
"source_snapshot": {
  "repo": "...", "branch": "...",
  "head_commit": "...",            // 仅当格式为 40 位 hex 时才有值
  "head_commit_valid": true,
  "worktree_diff_sha256": "...", "worktree_dirty": true,
  "files": {"stability-contracts/scenarios/adapter_<product>.py": "...", "...": "..."},
  "snapshot_valid": true,
  "snapshot_error": ""             // Git 失败时是唯一的错误落点
}
```

因此**旧结果不会被误配给新 HEAD**；台账里写当前 HEAD 不能替代这份内嵌快照。

**行为结果与来源证据分开标记**：Git 查询失败时 `snapshot_valid=False`、
`head_commit` 与 `worktree_diff_sha256` **留空**，原始错误只进 `snapshot_error`——
错误文本（及其哈希）绝不冒充提交或工作区差异。`safe.directory` 只对本仓使用
`Path.as_posix()` 形式（不使用 `*` 通配，也不修改全局/用户级 git 配置）。

## 规则（避免这类机制退化成形式）

1. **同一场景、同一断言描述在两端都执行**。产品差异必须写进
   `contracts.json` 的 `intentional_differences` 并由适配器显式处理，不能靠 skip 隐藏失败。
2. **适配器只允许**：构造真实控制器、调用真实入口、观察（替换平台/进程边界、
   安装观察点、把某产品的单体入口收窄到受理阶段）。不得在适配器里重写被测状态机，
   **也不得在适配器里补安全保护**（那会把"修复缺失"掩盖成"场景通过"）。
3. **受控交错必须让 worker 异常回传主线程**：`contract_core.interleaved_worker`
   在 finally 放行并 join，worker 内部异常（含断言）会在主线程复现；只凭"线程已
   结束"不构成通过。工具自测对此有专门反例。
4. **镜像只针对上面列明的文件**。哈希口径为**行尾归一化（CRLF→LF）后的文本**，
   避免 autocrlf 检出造成假差异；来源提交是否真的包含这些内容由
   `sync_mirror.py --verify-source-commit` 逐文件比对 git blob 来回答——**做不到时
   只能描述为"工作区镜像一致"**。同步前先干运行列出文件差异，禁止盲删目录，也禁止
   整文件复制 `live_controller.py`。
5. **每个修复都要回答"影响另一端吗"**。台账状态取 `verified` / `failed` /
   `pending` / `deferred` / `not_applicable`；`deferred` 表示**明确延期**，
   不等于风险已获批；若要写风险已接受，必须写明用户接受的范围。
6. **已登记的失败不得消失**：`check_alignment.py` 会汇总台账的
   `discovered_pre_existing`（既有失败）与 `contracts` 里的 failed/pending，
   两类都进"需要关注"与退出状态。
7. **覆盖范围要写清**：`contracts.json` 每个契约都带 `coverage`
   （共享场景覆盖什么、端内有哪几条补充用例 ID、明确未覆盖什么）；
   组级标题不得代替实际覆盖范围。
8. 本目录**不是 CI、也不是行为正确性证明**。HEAD 一致不等于测试通过；
   `check_alignment.py` 只做台账新鲜度、镜像哈希与状态汇总。行为通过也不能替代
   来源证据：发布验收必须带 `--require-valid-snapshot`，快照无效即不可验收。
9. **"函数返回了"不等于"资源已回收"**：停止/清理类判定必须以实际状态
   （owned 进程是否仍存活、`_ffmpeg_unrecycled`）为准；失败必须保留失败状态，
   让后续显式操作能重试，而不是用"已完成"标记把重试短路（CTRL-02e/02f）。

## 当前落地范围（契约版本 2026-09-14.3）

- 已落地共享场景 **20 个**：`CTRL-01a..h`（含"旧重连等待→停止→新意图→旧响应
  返回"的交错，以及直接调用真实 `_start_ffmpeg_stream` 的三个等待边界场景）、
  `CTRL-02a..f`（含"回收失败不得记为清理完成"与"重试成功→后续重复只确认"的
  恢复闭环）、`PUSH-01a/b`、`MODE-01a/b`、`INTENT-01a/b`。
- 未纳入共同场景、只在各仓测试覆盖：`FFMPEG-01`、`LOG-01`。
- **延期（deferred）**：`RETRY-01`（>30 秒异常退出的 5 秒退避）、`MEDIA-01`
  （concat 抽检范围）——两端共有，本轮不改数值/不加入全量探测，附有界方案。
- 待对齐：`CTRL-03`；`DATA-01` 桌面已验证、服务器 `not_applicable`。

各项的状态、证据路径、覆盖范围与未覆盖项见 `contracts.json`。
