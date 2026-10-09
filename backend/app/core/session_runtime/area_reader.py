"""分区**纯能力**读取：把本实例的分区缓存读成"分区名 → area_id"索引。

它不构造旧 ``LiveController``、不联网、不起线程、不持业务状态：生产默认入口
只把它当作一个只读查表能力，交给 ``TaskDataPort`` 的 ``area_lookup``。

缓存位置与可读性判据沿用 ``app.core.area_data``（每实例数据目录一份）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


def build_area_index(areas) -> Dict[str, int]:
    """把分区树（含 children）摊平成 名称 → area_id；非法项一律跳过。"""
    index: Dict[str, int] = {}

    def walk(nodes):
        for node in nodes or ():
            if not isinstance(node, dict):
                continue
            name = str(node.get('name') or '').strip()
            area_id = node.get('area_id', node.get('id'))
            if name and isinstance(area_id, int) and not isinstance(area_id, bool) \
                    and area_id > 0 and name not in index:
                index[name] = area_id
            children = node.get('children')
            if isinstance(children, list):
                walk(children)

    walk(areas)
    return index


def load_area_index(data_dir) -> Dict[str, int]:
    """读取本实例分区缓存；读不出/无缓存时返回空索引（能力缺失，不抛错）。"""
    try:
        from ..area_data import area_cache_path, read_cache
        path = area_cache_path(Path(data_dir))
        areas, reason = read_cache(path)
        if not areas:
            logger.info('分区缓存不可用（%s）：%s', reason, path)
            return {}
        return build_area_index(areas)
    except Exception as exc:                              # noqa: BLE001
        logger.warning('读取分区缓存失败（不影响启动）：%s', exc)
        return {}
