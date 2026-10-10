"""独立回环平台服务（桌面移植阶段 2 端到端专用）。

把 :class:`loopback.LoopbackPlatform` 跑成**独立进程**：端到端驱动（Electron +
真实后端）通过 ``BILIBILI_ENDPOINT_BASE`` 把后端平台出口指到这里；驱动器再经
同一端口的 ``/control/*`` 编排响应计划、闸门并读取房间状态。

用法::

    python loopback_service.py --ready-file <path>

启动后在标准输出打印一行 JSON ``{"platform_port": N}``，然后一直服务到收到
``POST /control/quit``。``--ready-file`` 给出时把同一行 JSON 写入该文件
（驱动器不必解析 stdout）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loopback import LoopbackPlatform            # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ready-file', default='')
    args = parser.parse_args()

    platform = LoopbackPlatform().start()
    ready = json.dumps({'platform_port': platform.port}, ensure_ascii=False)
    print(ready, flush=True)
    if args.ready_file:
        Path(args.ready_file).write_text(ready + '\n', encoding='utf-8')

    deadline_check = 0.05
    try:
        while not platform._quit_requested:                # noqa: SLF001
            time.sleep(deadline_check)
    except KeyboardInterrupt:
        pass
    finally:
        platform.stop()
        print('loopback-service stopped', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
