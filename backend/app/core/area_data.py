"""
area_data.py - 分区数据（运行时数据）的读写规则（桌面版）

与服务器版同一套行为约定（具体实现因数据目录初始化方式不同而异）：

1. 运行时缓存 = 数据目录（userData/data 或 --data-dir）内的 bili_areas_full.json；
   每个实例（每个用户数据目录）各一份，互不干扰。
2. 打包种子（install bundle 内 / PyInstaller _MEIPASS）**只在缓存缺失时**
   复制一次；已有缓存时绝不覆盖——升级、重装、回退都保持用户当前的最新数据。
   种子是**构建输入**（从仓库外的明确路径注入），不是随代码分发的快照，
   也不入库。
3. 写缓存：先结构校验 → 同目录临时文件 → fsync → 读回校验 → os.replace 原子
   替换。任一步失败都不动目标文件，返回明确失败原因，不返回假成功。
4. 并发保护只围绕本地读写（_IO_LOCK），网络请求在锁外完成。
"""

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

AREA_CACHE_FILENAME = "bili_areas_full.json"

# 只保护本地缓存读写；网络请求不持锁。
_IO_LOCK = threading.RLock()


def area_cache_path() -> Path:
    """运行时分区缓存路径（数据目录内）。"""
    from app.core.config import area_file_path
    return Path(area_file_path())


def validate_areas(data) -> Tuple[bool, str]:
    """结构校验：非空 list，元素为含 id/name 的 dict；children 若是必须是 list。"""
    if not isinstance(data, list):
        return False, "分区数据不是列表"
    if not data:
        return False, "分区数据为空"
    for item in data:
        if not isinstance(item, dict):
            return False, "分区元素不是对象"
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return False, "分区缺少 name"
        try:
            int(item.get("id"))
        except (TypeError, ValueError):
            return False, "分区 id 不是整数"
        children = item.get("children")
        if children is not None and not isinstance(children, list):
            return False, "children 不是列表"
    return True, ""


def read_cache(path: Path) -> Tuple[List[dict], str]:
    """读取并校验本地缓存，返回 (分区列表, 状态)。缺/坏都不抛异常。"""
    p = Path(path)
    if not p.exists():
        return [], "cache_missing"
    try:
        with _IO_LOCK:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
    except json.JSONDecodeError as e:
        return [], f"cache_corrupt: {e}"
    except OSError as e:
        return [], f"cache_unreadable: {e}"
    ok, reason = validate_areas(data)
    if not ok:
        return [], f"cache_invalid: {reason}"
    return data, "loaded"


def write_cache(path: Path, areas: List[dict]) -> Tuple[bool, str]:
    """原子写入缓存，返回 (是否成功, 失败原因)。"""
    ok, reason = validate_areas(areas)
    if not ok:
        return False, f"refuse_write_{reason}"
    p = Path(path)
    try:
        with _IO_LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(areas, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                with open(tmp_name, "r", encoding="utf-8") as f:
                    written = json.load(f)
                ok2, reason2 = validate_areas(written)
                if not ok2:
                    return False, f"verify_failed_{reason2}"
                os.replace(tmp_name, p)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
    except OSError as e:
        return False, f"write_failed: {e}"
    except Exception as e:  # pragma: no cover - 兜底：任何异常都不算成功
        return False, f"write_failed: {e}"
    return True, ""


def copy_seed_if_missing(target: Path, seed: Path) -> str:
    """种子 → 数据目录缓存（仅当缓存缺失）。返回动作标记。

    - seed_missing / seed_rejected_<原因>：没有可用种子，不写入任何目标；
    - kept_new：缓存已存在，不覆盖（用户数据优先）；
    - seeded：成功复制。
    """
    seed = Path(seed)
    target = Path(target)
    if not seed.exists():
        return "seed_missing"
    data, reason = read_cache(seed)
    if not data:
        return f"seed_rejected_{reason}"
    if target.exists():
        return "kept_new"
    ok, err = write_cache(target, data)
    return "seeded" if ok else f"seed_failed_{err}"
