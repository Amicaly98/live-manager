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
    args = parser.parse_args()

    # 数据目录必须先于业务模块导入初始化（config.init_data_dir 幂等）
    from app.core.config import init_data_dir
    from app.core.data_migration import migrate_legacy_data
    try:
        data_dir = init_data_dir(args.data_dir)
        # 旧版本数据散落在仓库根/backend；带备份迁移进数据目录（E3）
        legacy_dirs = [Path(backend_dir).parent]  # 仓库根
        for legacy in legacy_dirs:
            try:
                migrate_legacy_data(data_dir, legacy)
            except Exception as e:
                print(f"[data-migration] 迁移检查失败（忽略）：{e}")
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

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info"
    )


if __name__ == "__main__":
    main()
