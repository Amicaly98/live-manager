"""
data_migration.py - 旧数据迁移（E3）

旧版本（1.0.x）把运行时数据散落在工作目录（安装目录/仓库根）。本模块在
后端启动时把可识别的旧数据文件迁移进统一数据目录：

- 只在目标文件**不存在**时迁移（新数据绝不被旧数据覆盖）；
- 迁移前先整体备份到 data/migration_backup/<时间戳>/；
- 迁移失败不影响启动（旧文件保留原位，下次再试）；
- 日志文件不迁移（旧日志属于旧安装，容量控制从新文件开始）。
"""

import logging
import shutil
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

    # 备份（只备份即将迁移的文件）
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
        try:
            shutil.copy2(src, backup_dir / name)
            shutil.copy2(src, dst)
            actions.append((name, "migrated"))
            logger.info(f"已迁移旧数据：{name} → {dst}")
        except Exception as e:
            actions.append((name, "failed"))
            logger.warning(f"迁移 {name} 失败（保留原文件，下次重试）：{e}")

    migrated_any = any(a == "migrated" for _, a in actions)
    if migrated_any:
        # state/cookies 属于敏感数据：迁移后不删除旧文件（备份已含），
        # 由用户自行处理旧安装目录；这里只记录。
        logger.info(f"旧数据迁移完成，备份位于：{backup_dir}")
    return actions
