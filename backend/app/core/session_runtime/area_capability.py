"""分区**纯能力**访问口：一个进程一份 ``AreaLoader`` ＋按需的 ``BilibiliApi``。

它不构造旧 ``LiveController``，不持业务状态、不起线程、不参与任何控制决策。
默认入口的 ``/api/areas/*`` 与 ``/api/live/areas/*`` 只经这里读写分区缓存。
"""

from __future__ import annotations

import threading
from typing import Any, Optional

_lock = threading.Lock()
_loader: Optional[Any] = None


def get_area_loader() -> Any:
    """本实例的分区加载器（懒构造；缓存路径来自 ``config`` 的数据目录）。"""
    global _loader
    with _lock:
        if _loader is None:
            from ..live_controller import AreaLoader
            _loader = AreaLoader()
        return _loader


def reset_area_loader() -> None:
    """仅供测试在同一进程内换数据目录后重新读取缓存。"""
    global _loader
    with _lock:
        _loader = None


def new_bilibili_api() -> Any:
    """一个**纯** B 站 API 客户端（只用于刷新分区等只读能力）。"""
    from ..live_controller import BilibiliApi
    return BilibiliApi()
