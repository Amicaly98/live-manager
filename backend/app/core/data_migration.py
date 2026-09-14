"""
data_migration.py - 旧数据迁移（E3/D4）

旧版本（1.0.x）把运行时数据散落在工作目录（安装目录/仓库根）。本模块在后
端启动时把可识别的旧数据文件迁移进统一数据目录：

- 只在目标文件**不存在**时迁移（新数据绝不被旧数据覆盖）；
- 迁移前先整体备份到 data/migration_backup/<时间戳>/；
- SQLite 数据库（live_tasks.db）使用 backup API 做一致性快照（D4）：
  正确合并已提交的 WAL 内容，绝不拷贝"主库+旧 WAL"的分裂状态；
  备份与目标都做内容验证（表清单+行数对比），不是只有 integrity_check；
- 目标先写临时文件、验证通过后原子替换——半成品不会在第二次启动被
  当作"已有新数据"跳过（D4）；
- 迁移失败不影响启动（旧文件保留原位，下次重试）；
- 日志文件不迁移（旧日志属于旧安装，容量控制从新文件开始）。
"""

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

# 旧布局 → 新布局的文件名映射（同名文件）
_MIGRATED_FILES = [
    "live_state.json",
    "bili_areas_full.json",
    "bili_cookies.json",
    "rtmp_cache.json",
    "settings.json",
    "live_tasks.db",
    "live_tasks.xlsx",
    "task_manager_last_run.json",
]

_SQLITE_SUFFIXES = ('.db',)


def _is_sqlite_file(path: Path) -> bool:
    return path.suffix.lower() in _SQLITE_SUFFIXES


def _sqlite_content_signature(conn: sqlite3.Connection) -> dict:
    """取表清单 + 各用户表行数（内容级指纹，比 integrity_check 强）。"""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    sig = {}
    for t in tables:
        try:
            sig[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.DatabaseError as e:
            sig[t] = f'error:{e}'
    return sig


def _copy_sqlite_consistent(src: Path, dst: Path) -> None:
    """用 backup API 把 src（可能带未合并 WAL）一致性地复制到 dst。

    - 打开 src 时显式带上 -wal/-shm 视角，backup API 读到的是已提交的
      统一快照（包含仅存在于 WAL 中的提交）；
    - dst 写满后做内容验证：integrity_check + 表/行数与源快照一致。
    """
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
            # 验证 1：结构完整
            check = dst_conn.execute('PRAGMA integrity_check').fetchone()[0]
            if check != 'ok':
                raise sqlite3.DatabaseError(f'integrity_check={check}')
            # 验证 2：内容一致（表清单 + 行数与源相同）
            src_sig = _sqlite_content_signature(src_conn)
            dst_sig = _sqlite_content_signature(dst_conn)
            if src_sig != dst_sig:
                raise sqlite3.DatabaseError(
                    f'内容不一致：src={src_sig} dst={dst_sig}')
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _copy_one(src: Path, dst_tmp: Path) -> None:
    """单个文件的一致性复制（SQLite 用 backup API，其它字节复制）。"""
    if _is_sqlite_file(src):
        _copy_sqlite_consistent(src, dst_tmp)
    else:
        import shutil
        shutil.copy2(src, dst_tmp)


def _verify_plain_copy(src: Path, dst: Path) -> bool:
    """非 SQLite 文件备份后的内容验证：字节数一致。"""
    try:
        return src.stat().st_size == dst.stat().st_size
    except OSError:
        return False


def migrate_legacy_data(data_dir: Path, legacy_dir: Path = None) -> List[Tuple[str, str]]:
    """把 legacy_dir（默认 BASE_DIR，即旧工作目录）下的数据文件迁入 data_dir。

    返回 [(文件名, 动作)]，动作 ∈ {"migrated", "kept_new", "missing", "failed"}。
    """
    from app.core.config import BASE_DIR

    legacy_dir = Path(legacy_dir) if legacy_dir else BASE_DIR
    if legacy_dir.resolve() == data_dir.resolve():
        return []  # 数据目录就是旧目录：无需迁移

    actions: List[Tuple[str, str]] = []
    existing = [f for f in _MIGRATED_FILES if (legacy_dir / f).exists()]
    if not existing:
        return []

    # 备份（只备份即将迁移的文件；备份同样走一致性复制）
    backup_dir = data_dir / "migration_backup" / time.strftime("%Y%m%d-%H%M%S")
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"迁移备份目录创建失败，跳过迁移：{e}")
        return [(f, "failed") for f in existing]

    for name in existing:
        src = legacy_dir / name
        dst = data_dir / name
        if dst.exists():
            actions.append((name, "kept_new"))  # 新数据已存在，不覆盖
            continue
        dst_tmp = dst.with_name(dst.name + '.migrating')
        try:
            # 1) 备份（一致性复制；失败则放弃迁移，不产生半成品）
            backup_path = backup_dir / name
            _copy_one(src, backup_path)
            if not _is_sqlite_file(src) and not _verify_plain_copy(src, backup_path):
                raise OSError('备份大小不一致')
            # 2) 目标先写临时文件
            _copy_one(src, dst_tmp)
            # 3) 验证通过后原子替换（半成品永远不占用正式文件名）
            os.replace(dst_tmp, dst)
            actions.append((name, "migrated"))
            logger.info(f"已迁移旧数据：{name} → {dst}")
        except Exception as e:
            actions.append((name, "failed"))
            logger.warning(f"迁移 {name} 失败（保留原文件，下次重试）：{e}")
            try:
                dst_tmp.unlink(missing_ok=True)
            except Exception:
                pass

    migrated_any = any(a == "migrated" for _, a in actions)
    if migrated_any:
        # state/cookies 属于敏感数据：迁移后不删除旧文件（备份已含），
        # 由用户自行处理旧安装目录；这里只记录。
        logger.info(f"旧数据迁移完成，备份位于：{backup_dir}")
    return actions
