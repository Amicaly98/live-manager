"""分区**纯能力**读取：把本实例的分区缓存读成"分区名 → area_id"索引。

它不构造旧 ``LiveController``、不联网、不起线程、不持业务状态：生产默认入口
只把它当作一个只读查表能力，交给 ``TaskDataPort`` 的 ``area_lookup``。

缓存位置与可读性判据沿用 ``app.core.area_data``（默认每实例数据目录一份；
部署可用 ``AREA_CACHE_PATH`` 让多个实例共用同一份）。文件签名变化时自动重读，
因此任一实例刷新分区后，其他实例下一次查表即可看到新内容，无需重启。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: 路径 → (签名, 索引, 最近一次不可用原因)。签名 = (mtime_ns, size, inode)。
_INDEX_CACHE: Dict[str, Tuple[Optional[tuple], Dict[str, int], str]] = {}
_INDEX_LOCK = threading.Lock()


def build_area_index(areas) -> Dict[str, int]:
    """把分区树（含 children）摊平成 名称 → area_id；非法项一律跳过。"""
    index: Dict[str, int] = {}

    def walk(nodes):
        for node in nodes or ():
            if not isinstance(node, dict):
                continue
            name = str(node.get('name') or '').strip()
            area_id = node.get('area_id', node.get('id'))
            if isinstance(area_id, str):
                # 旧实例缓存沿用既有实际形态：子分区 id 为整数字符串
                # （与 ``area_data._is_valid_area_id`` 同口径）；索引统一存整数。
                try:
                    area_id = int(area_id.strip())
                except ValueError:
                    pass
            if name and isinstance(area_id, int) and not isinstance(area_id, bool) \
                    and area_id > 0 and name not in index:
                index[name] = area_id
            children = node.get('children')
            if isinstance(children, list):
                walk(children)

    walk(areas)
    return index


def _signature(path: Path) -> Optional[tuple]:
    """文件签名；不存在/不可读时为 ``None``（同样视为"变化"）。"""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def load_area_index(data_dir=None, force: bool = False) -> Dict[str, int]:
    """读取分区缓存并返回"名称 → area_id"索引。

    - 部署设置了 ``AREA_CACHE_PATH`` 时，所有实例读同一份文件；
    - 文件签名（mtime/size/inode）未变时复用缓存，变化后自动重读；
    - 读不出/无缓存时返回空索引（能力缺失，不抛错）。
    """
    try:
        from ..area_data import area_cache_path, read_cache
        path = area_cache_path(Path(data_dir) if data_dir is not None else None)
        key = str(path)
        before = _signature(path)
        with _INDEX_LOCK:
            cached = _INDEX_CACHE.get(key)
            if not force and cached is not None and cached[0] == before:
                return dict(cached[1])
        # 读取在锁外进行（read_cache 自带本地 IO 锁）；除首次加载外，
        # 每个进程只在文件签名变化时付出这一次完整解析。
        areas, reason = read_cache(path)
        index = build_area_index(areas) if areas else {}
        after = _signature(path)
        # 如果读取期间文件被替换，无法证明解析结果对应读取前还是读取后
        # 的路径内容。记录读取前签名，使当前签名仍不匹配，下一次调用重读；
        # 不要把旧快照标成读取后的新签名，否则旧索引会被永久复用。
        cache_signature = before if before != after else after
        with _INDEX_LOCK:
            previous = _INDEX_CACHE.get(key)
            _INDEX_CACHE[key] = (cache_signature, index, reason)
            if previous is None or previous[2] != reason:
                if areas:
                    logger.info('分区索引已加载：%s 项（%s）', len(index), path)
                else:
                    logger.info('分区缓存不可用（%s）：%s', reason, path)
        return dict(index)
    except Exception as exc:                              # noqa: BLE001
        logger.warning('读取分区缓存失败（不影响启动）：%s', exc)
        return {}


def reset_area_index_cache() -> None:
    """清空进程内索引缓存（测试与配置切换用；生产不需要调用）。"""
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()
