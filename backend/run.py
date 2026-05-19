"""
run.py - 后端启动脚本

支持两种模式：
1. 直接运行：python run.py
2. 从 Electron 启动：python run.py --mode service (隐藏控制台)
"""

import sys
import os
import time
import argparse

# 确保 backend 目录在 sys.path 中
backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.main import app


def main():
    parser = argparse.ArgumentParser(description="直播控制系统后端")
    parser.add_argument(
        "--mode", choices=["console", "service"], default="console",
        help="启动模式：console（默认，显示控制台），service（隐藏，用于 Electron）")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="热重载（仅开发）")
    args = parser.parse_args()

    import uvicorn

    print("=" * 70)
    print("直播控制系统 - 后端服务")
    print("=" * 70)
    print(f"启动时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"监听地址：http://{args.host}:{args.port}")
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
