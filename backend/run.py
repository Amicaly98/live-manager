"""
run.py - 后端启动脚本（桌面版）

支持两种模式：
1. 直接运行：python run.py
2. 从 Electron 启动：python run.py --data-dir <userData/data>

启动顺序（必须保持）：
    解析参数 → 迁移旧数据目录 → ``config.init(data_dir, account_root)``
    → 导入 ``app.main`` → uvicorn.run

E3：Electron 通过 --data-dir 传入统一数据目录（userData/data）；该参数必须在导入
app 业务模块之前解析并初始化。

账号根（桌面是单实例、单账号客户端，任务/进度/历史仍按 UID 归属）：
- 显式 ``--account-root`` 或环境变量 ``ACCOUNT_DATA_ROOT`` 优先；
- 都没有时按既有推导规则取 ``<data-dir 的父目录>/account-data`` —— 桌面场景即
  ``userData/account-data``。**由本启动入口显式传入** config，不在 config 里偷偷
  推导默认值（隔离夹具与旧测试仍可用 config 的兼容模式）。
"""

import sys
import os
import time
import argparse
from pathlib import Path

# 确保 backend 目录在 sys.path 中
backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)


def _legacy_data_candidates() -> list:
    """旧版本（1.0.x）数据目录候选（D4：冻结版不再猜仓库根）。

    旧版 Electron 用 spawn 拉起后端且未传 cwd → 旧数据落在进程工作目录，
    安装场景即安装根目录（exe 所在目录）。候选优先级：
    1. BILIBILI_LEGACY_DATA_DIR 环境变量 / --legacy-data-dir 参数
       （Electron 明确传入可确认的旧位置，多个用 os.pathsep 分隔）；
    2. 冻结版：run.exe 自身目录（resources/backend）、上级（resources）、
       上上级（安装根，旧版快捷方式启动时的工作目录）；
    3. 源码运行：仓库根（backend 的上级）。
    """
    env = os.environ.get("BILIBILI_LEGACY_DATA_DIR", "")
    candidates = [Path(p) for p in env.split(os.pathsep) if p] if env else []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates += [exe_dir, exe_dir.parent, exe_dir.parent.parent]
    else:
        candidates.append(Path(backend_dir).parent)  # 仓库根
    # 去重保序
    seen, uniq = set(), []
    for c in candidates:
        r = str(c.resolve()) if c.exists() else str(c)
        if r not in seen:
            seen.add(r)
            uniq.append(c)
    return uniq


def main():
    parser = argparse.ArgumentParser(description="直播控制系统后端（桌面版）")
    parser.add_argument(
        "--mode", choices=["console", "service"], default="console",
        help="启动模式：console（默认，显示控制台），service（隐藏，用于 Electron）")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="热重载（仅开发）")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="统一数据目录（Electron 传入 userData/data；A8/E3）")
    parser.add_argument("--legacy-data-dir", type=str, default=None,
                        help="旧版本数据目录（D4：Electron 明确传入，os.pathsep 分隔多个）")
    parser.add_argument("--account-root", type=str, default=None,
                        help="账号数据根目录（显式给出才启用账号级数据路径；"
                             "缺省且没有 ACCOUNT_DATA_ROOT 时取 <数据目录>/../account-data）")
    parser.add_argument("--frontend-dir", type=str, default=None,
                        help="前端构建目录（默认 <项目根>/frontend/dist）")
    args = parser.parse_args()
    if args.legacy_data_dir:
        os.environ["BILIBILI_LEGACY_DATA_DIR"] = (
            args.legacy_data_dir + os.pathsep + os.environ.get("BILIBILI_LEGACY_DATA_DIR", ""))

    # 监听地址/端口在**导入 config 之前**写进环境：config 的 API_HOST/API_PORT 是
    # 模块级常量（实例入口标识等展示口径读它），导入后再设就晚了。
    os.environ["API_PORT"] = str(args.port)
    os.environ["API_HOST"] = str(args.host)

    # 数据目录必须先于业务模块导入初始化。
    from app.core.config import (
        BASE_DIR, init, resolve_account_root, area_file_path,
    )
    from app.core.data_migration import migrate_legacy_data

    data_dir = None
    account_root = None
    try:
        # 账号根与数据目录必须来自**同一份**解析：直接调 resolve_account_root 而不先按
        # config 的口径定出数据目录，会在 --data-dir 缺省时推导出进程 cwd 下的根，
        # 与 init 实际采用的数据目录不是同一个父目录。
        resolved_data = (args.data_dir or os.getenv("BILIBILI_DATA_DIR")
                         or str(BASE_DIR / "data"))
        account_root = resolve_account_root(resolved_data, args.account_root)
        data_dir = init(data_dir=args.data_dir, account_root=str(account_root))
        # 旧版本数据散落在旧安装目录/仓库根；带备份迁移进数据目录（E3/D4）。
        # 这一步只把**文件**搬进数据目录；把它解释成账号资料是启动期的
        # 旧资料接管（session_runtime.desktop.prepare_account_target）。
        # BILIBILI_SKIP_MIGRATION=1 可跳过（测试隔离用，避免复制真实账号数据）。
        if os.environ.get("BILIBILI_SKIP_MIGRATION") != "1":
            for legacy in _legacy_data_candidates():
                try:
                    migrate_legacy_data(data_dir, legacy)
                except Exception as e:
                    print(f"[data-migration] 迁移检查失败（忽略）：{e}")
        # E5：打包种子分区表（PyInstaller _MEIPASS 内）→ 数据目录首启复制，
        # 不覆盖已有分区数据；种子缺失只是没有初始分区，面板仍可启动
        try:
            from app.core.area_data import copy_seed_if_missing
            meipass = getattr(sys, "_MEIPASS", None)
            target = area_file_path()
            if meipass:
                seed = Path(meipass) / "bili_areas_full.json"
                action = copy_seed_if_missing(target, seed)
                print(f"[seed] 分区种子：{action}（目标 {target}）")
            elif target.exists():
                print(f"[seed] 数据目录已有分区缓存，未使用任何种子：{target}")
        except Exception as e:
            print(f"[seed] 分区表初始化失败（运行时会尝试在线获取）：{e}")
    except Exception as e:
        print(f"[fatal] 数据目录初始化失败：{e}", file=sys.stderr)
        sys.exit(2)

    # 将 frontend_dir 存入环境变量，供 main.py 使用。
    if args.frontend_dir:
        os.environ["FRONTEND_DIR"] = os.path.abspath(args.frontend_dir)

    import uvicorn

    print("=" * 70)
    print("直播控制系统 - 后端服务")
    print("=" * 70)
    print(f"启动时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"监听地址：http://{args.host}:{args.port}")
    print(f"数据目录：{data_dir}")
    print(f"账号数据根：{account_root}")
    print(f"API 文档：http://{args.host}:{args.port}/docs")
    if args.mode == "service":
        print("服务模式：控制台输出将重定向")
    print("=" * 70)

    from app.main import app as fastapi_app
    if args.reload:
        # 热重载必须用导入字符串（仅开发）
        uvicorn.run("app.main:app", host=args.host, port=args.port,
                    reload=True, log_level="info")
    else:
        # 显式持有 Server 句柄：/api/shutdown 通过 should_exit 优雅退出（E2）
        config = uvicorn.Config(fastapi_app, host=args.host, port=args.port,
                                log_level="info",
                                timeout_graceful_shutdown=30)
        server = uvicorn.Server(config)
        fastapi_app.state.uvicorn_server = server
        server.run()


if __name__ == "__main__":
    main()
