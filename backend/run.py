"""
run.py - 后端启动脚本

支持两种模式：
1. 直接运行：python run.py
2. 从 Electron 启动：python run.py --mode service (隐藏控制台)

E3：Electron 通过 --data-dir 传入统一数据目录（userData/data）；
    该参数必须在导入 app 业务模块之前解析并初始化。
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
    parser = argparse.ArgumentParser(description="直播控制系统后端")
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
    args = parser.parse_args()
    if args.legacy_data_dir:
        os.environ["BILIBILI_LEGACY_DATA_DIR"] = (
            args.legacy_data_dir + os.pathsep + os.environ.get("BILIBILI_LEGACY_DATA_DIR", ""))

    # 数据目录必须先于业务模块导入初始化（config.init_data_dir 幂等）
    from app.core.config import init_data_dir, area_file_path
    from app.core.data_migration import migrate_legacy_data
    try:
        data_dir = init_data_dir(args.data_dir)
        # 旧版本数据散落在旧安装目录/仓库根；带备份迁移进数据目录（E3/D4）。
        # BILIBILI_SKIP_MIGRATION=1 可跳过（测试隔离用，避免复制真实账号数据）。
        if os.environ.get("BILIBILI_SKIP_MIGRATION") != "1":
            for legacy in _legacy_data_candidates():
                try:
                    migrate_legacy_data(data_dir, legacy)
                except Exception as e:
                    print(f"[data-migration] 迁移检查失败（忽略）：{e}")
        # E5：打包种子分区表（PyInstaller _MEIPASS 内）→ 数据目录首启复制，
        # 不覆盖已有分区数据
        try:
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                seed = Path(meipass) / "bili_areas_full.json"
                target = area_file_path()
                if seed.exists() and not target.exists():
                    import shutil
                    shutil.copy2(seed, target)
                    print(f"[seed] 分区表已初始化：{target}")
        except Exception as e:
            print(f"[seed] 分区表初始化失败（运行时会尝试在线获取）：{e}")
    except Exception as e:
        print(f"[fatal] 数据目录初始化失败：{e}", file=sys.stderr)
        sys.exit(2)

    import uvicorn

    print("=" * 70)
    print("直播控制系统 - 后端服务")
    print("=" * 70)
    print(f"启动时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"监听地址：http://{args.host}:{args.port}")
    print(f"数据目录：{data_dir}")
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
                                log_level="info")
        server = uvicorn.Server(config)
        fastapi_app.state.uvicorn_server = server
        server.run()


if __name__ == "__main__":
    main()
