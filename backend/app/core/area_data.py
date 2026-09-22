"""
area_data.py - 分区数据（运行时数据）的落位与读写（服务器版）

背景：B 站分区列表属于频繁变化的外部业务数据，既不进 Git，也不作为代码
契约的一部分。旧的写法把运行时缓存放在**仓库根/bili_areas_full.json**（与
代码同目录、且曾经是一个受跟踪文件），会导致：多个实例共用/互相覆盖同一份
文件、分区数据变化污染工作区、升级或回退时可能被代码里的旧快照盖掉。

本模块确立的规则：

1. 运行时缓存 = ``--data-dir`` 数据目录下的 ``bili_areas_full.json``，
   每个实例各一份，互不干扰（对应 DATA/实例隔离要求）。
2. 旧位置（仓库根、backend 目录）的同名文件**只作为一次性兼容导入来源**：
   - 仅在目标缓存缺失且旧文件通过结构校验时复制；
   - 复制走"临时文件 + 原子替换"，中断不会留下吃不掉的半截 JSON；
   - 目标已存在时**绝不覆盖**（升级、回退都保持该实例最新的运行数据）；
   - 迁移失败不影响启动，旧文件保留原位，下次启动可再试。
3. 写缓存：先校验 → 同目录临时文件 → ``os.replace`` 原子替换 → 读回校验；
   任何一步失败都返回明确的失败原因，不返回"假成功"。
4. 并发保护只围绕本地读写（``_IO_LOCK``）。网络请求在锁外完成，不把网络
   往返放进长期持有的业务锁。

注意（发布/切换流程必须遵守）：取消跟踪某Docker/部署环境里已被跟踪的分区
表后，切代码的其他检出环境可能**移除**该受跟踪文件。因此从旧发布切换到
新代码前，必须先从"当前正在使用的旧发布目录"保留/迁移分区缓存，不能指望
新代码目录里还留着那份表；回退时同样要保留最新运行数据。
"""

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

AREA_CACHE_FILENAME = "bili_areas_full.json"

# 只保护本地缓存读写；网络请求不持锁。
_IO_LOCK = threading.RLock()


def area_cache_path(data_dir=None) -> Path:
    """运行时分区缓存路径（每实例数据目录内）。

    延迟导入 config，避免"config.init 之前就固化路径"。
    """
    from app.core.config import get_data_path
    if data_dir is not None:
        return Path(data_dir) / AREA_CACHE_FILENAME
    return get_data_path(AREA_CACHE_FILENAME)


def legacy_cache_candidates() -> List[Path]:
    """旧布局下可能的分区缓存位置（仅在目标缺失时作为导入来源）。

    不含"数据目录"（数据目录本身就是要迁移到的目标）。
    """
    try:
        from app.core.config import get_project_root
        root = get_project_root()
    except Exception:  # pragma: no cover - 配置不可用时退回文件推导
        root = Path(__file__).resolve().parents[3]
    backend_dir = root / "backend"
    seen, out = set(), []
    for p in (root, backend_dir):
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _is_valid_area_id(value) -> bool:
    """分区 id：整数或整数字符串（既有实际使用形态），不接受 bool/浮点/其他。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        s = value.strip()
        return s.isdigit() or (len(s) > 1 and s[0] == '-' and s[1:].isdigit())
    return False


# 嵌套深度上限：真实分区表只有两级；超过视为结构不合法（也防恶意深嵌套
# 把校验/展平变成递归异常逃出去）。
_MAX_AREA_DEPTH = 16


def _validate_nodes(nodes, child_key: str, depth: int = 0) -> str:
    """递归校验每级节点；child_key 为该形状下的子分区键。

    缓存/种子/导入形状用 ``children``；平台原始响应用 ``list``。
    返回空串表示合法，否则返回失败原因（调用方转为明确失败）。
    """
    if depth > _MAX_AREA_DEPTH:
        return "分区嵌套超过最大深度"
    if not isinstance(nodes, list):
        return "分区数据不是列表"
    if not nodes and depth == 0:
        return "分区数据为空"
    for item in nodes:
        if not isinstance(item, dict):
            return "分区元素不是对象"
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return "分区缺少 name"
        if not _is_valid_area_id(item.get("id")):
            return "分区 id 不是合法整数（允许整数字符串）"
        children = item.get(child_key)
        if children is None:
            continue
        if not isinstance(children, list):
            return f"{child_key} 不是列表"
        if children:
            err = _validate_nodes(children, child_key, depth + 1)
            if err:
                return err
    return ""


def validate_areas(data) -> Tuple[bool, str]:
    """结构校验（缓存/种子/导入形状：子分区在 ``children`` 里）。

    **递归**校验每一级节点：元素必须是对象、name 为非空字符串、id 为整数
    或整数字符串（既有实际使用形态）、children 若存在必须是列表且同样合法。
    返回 (是否可用, 原因)；原因在可用时为空串。
    """
    err = _validate_nodes(data, "children")
    return (not err, err)


def validate_api_payload(data) -> Tuple[bool, str]:
    """结构校验（平台原始响应形状：子分区在 ``list`` 里）。

    与 validate_areas 同一约定、同一实现入口，只是子分区键不同；
    在展平（flatten）**之前**调用，保证非法响应不会让转换过程抛异常。
    """
    err = _validate_nodes(data, "list")
    return (not err, err)


def read_cache(path: Path) -> Tuple[List[dict], str]:
    """读取并校验本地缓存。返回 (分区列表, 状态说明)。

    文件不存在 / 内容非法 / 解析失败一律返回空列表 + 原因，**不抛异常**，
    由调用方决定是否在线获取。已有文件损坏时不删除原文件（保留现场）。
    """
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
    """原子写入缓存。返回 (是否成功, 失败原因)。

    顺序：结构校验 → 父目录就绪 → 同目录临时文件 → 刷盘 → 读回校验 →
    原子替换。任一步失败：不生成/不替换目标文件，返回明确原因。
    """
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
                # 写盘后先读回校验，避免半截/畸形内容被替换进去
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
    except Exception as e:  # pragma: no cover - 兜底，任何异常都不算成功
        return False, f"write_failed: {e}"
    return True, ""


def import_legacy_cache(target: Optional[Path] = None,
                        candidates: Optional[List[Path]] = None,
                        enabled: bool = True) -> List[Tuple[str, str]]:
    """把旧位置（仓库根 / backend 目录）的分区表导入到数据目录缓存。

    - 目标已存在 → ``kept_new``（**绝不覆盖**这是关键行为）；
    - 旧文件缺失 → ``missing``；
    - 旧文件非法 → ``rejected``（保留旧文件，不写入半截目标）；
    - 成功 → ``imported``。
    可重复运行（幂等）：第二次调用因目标已存在一律 kept_new。
    """
    target = Path(target) if target is not None else area_cache_path()
    candidates = legacy_cache_candidates() if candidates is None else list(candidates)
    actions: List[Tuple[str, str]] = []
    if not enabled:
        return actions
    target_resolved = str(target)
    for src_dir in candidates:
        src = Path(src_dir) / AREA_CACHE_FILENAME
        try:
            if str(src.resolve()) == target_resolved:
                continue  # 旧位置就是数据目录：无需迁移
        except OSError:
            pass
        if not src.exists():
            actions.append((str(src), "missing"))
            continue
        if target.exists():
            actions.append((str(src), "kept_new"))
            continue
        data, reason = read_cache(src)
        if not data:
            actions.append((str(src), f"rejected_{reason}"))
            logger.warning("旧位置分区表不可用，未导入：%s（%s）", src, reason)
            continue
        ok, err = write_cache(target, data)
        if ok:
            actions.append((str(src), "imported"))
            logger.info("已从旧位置导入分区缓存：%s → %s", src, target)
        else:
            actions.append((str(src), f"failed_{err}"))
            logger.warning("导入旧分区表失败：%s（%s）", src, err)
    return actions



# Desktop build input compatibility: seed data lives in the packaged bundle and
# is copied only when the user cache is absent.
def copy_seed_if_missing(target: Path, seed: Path) -> str:
    """Copy a validated packaged area seed without overwriting user data."""
    target = Path(target)
    seed = Path(seed)
    if target.exists():
        return "kept_new"
    if not seed.exists():
        return "seed_missing"
    data, reason = read_cache(seed)
    if not data:
        return f"seed_rejected_{reason}"
    ok, err = write_cache(target, data)
    return "seeded" if ok else f"seed_failed_{err}"
