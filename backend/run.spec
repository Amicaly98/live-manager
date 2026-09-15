# -*- mode: python ; coding: utf-8 -*-
# run.spec - PyInstaller 打包配置：后端单文件 run.exe（E5）
#
# 构建（在 backend/ 目录）：
#   set BILIBILI_AREA_SEED_FILE=<仓库外的分区种子 json>
#   pyinstaller run.spec --noconfirm --clean --distpath dist --workpath build
#
# 产物：backend/dist/run.exe
#
# 分区种子是**构建输入**，不是代码资产：
# - B 站分区列表频繁变化，仓库里不再保存、也不再跟踪任何真实分区表；
# - 需要内置初始分区时，用环境变量指向"仓库外的明确文件"（文件名任意，
#   如 snapshot-20260915.json 或带空格的名字），本脚本会校验结构后把它
#   **复制为固定包内名 bili_areas_full.json**（运行时只认 _MEIPASS/ 下的
#   这个名字；用户源文件不改写，溯源记录原始路径与 SHA256）；
# - 未设置该变量 → 构建不报错但**不含种子**：安装后首次运行没有分区数据，
#   面板照常启动、显示"分区待获取"，用户点刷新即可（也可源码运行，不需要种子）；
# - 设置了变量但文件不存在/结构非法 → **立即失败**，不会静默改用别处的文件，
#   也不会从开发机的任意数据目录里捞数据；
# - 发布构建要求必须带种子时，用 BILIBILI_REQUIRE_AREA_SEED=1 把"缺少种子"
#   变成硬错误。
# - 来源/时间/SHA256 会打到构建日志（[area-seed-provenance] 前缀）；设置
#   BILIBILI_AREA_SEED_PROVENANCE=<路径> 可同时写一份溯源文件。
# - 暂存目录默认每次构建独立（tempfile.mkdtemp，构建结束自动清理）；需要
#   固定位置时用 BILIBILI_AREA_SEED_STAGING_DIR 指定——并发构建/测试不得
#   共享同一暂存目录（会被互相覆盖）。
#
# 运行时行为：种子只在数据目录缓存**缺失**时复制一次，已有缓存绝不覆盖
# （详情见 backend/app/core/area_data.py 与 backend/run.py）。

import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)          # backend/

SEED_ENV = "BILIBILI_AREA_SEED_FILE"
REQUIRE_ENV = "BILIBILI_REQUIRE_AREA_SEED"
PROVENANCE_ENV = "BILIBILI_AREA_SEED_PROVENANCE"
STAGING_ENV = "BILIBILI_AREA_SEED_STAGING_DIR"
PACKAGE_SEED_NAME = "bili_areas_full.json"

# 种子校验与运行时共用同一实现（app/core/area_data.py），
# 不在 spec 里另写一套会逐渐走样的宽松校验。
sys.path.insert(0, str(ROOT))
from app.core.area_data import validate_areas as _validate_seed_areas


def _load_and_validate_seed(path: Path):
    """读取并校验种子分区表。返回 (错误信息, 数据)。

    结构约定与运行时缓存/兼容导入完全一致（递归校验每一级节点：
    非空数组、元素为含合法 id/name 的对象、children 逐级合法），
    避免把破损/空表打进安装包（装上去别人看到的就是"有种子但加载
    失败"这种假象）。
    """
    if not path.exists():
        return f"种子文件不存在：{path}", None
    if path.is_dir():
        return f"种子路径是目录，不是文件：{path}", None
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return f"种子不是合法 JSON：{e}", None
    ok, reason = _validate_seed_areas(data)
    if not ok:
        return f"种子结构不合法：{reason}", None
    return "", data


def _resolve_seed():
    """解析构建种子，返回 [(打包源文件, 包内目录)]。

    关键约定：**运行时只认 _MEIPASS/bili_areas_full.json**。外部种子可以叫
    任何合法名字（如 snapshot-20260915.json、带空格的名字），这里先复制到
    暂存目录里的固定包内名（不改写用户源文件），再把这个固定名文件作为
    PyInstaller datas 输入，保证打包后路径与 run.py 的查找约定一致。
    """
    seed_env = os.environ.get(SEED_ENV, "").strip()
    require = os.environ.get(REQUIRE_ENV, "") == "1"
    if not seed_env:
        msg = ("未提供分区种子（未设置 %s）：本次构建不含内置分区数据；"
               "安装后首次运行显示“分区待获取”，用户点刷新即可。"
               "需要内置种子请把该变量指向仓库外的明确文件%s"
               % (SEED_ENV, "；发布构建要求种子时必须设置 %s=1" % REQUIRE_ENV if not require else ""))
        print(msg)
        if require:
            raise SystemExit(
                "缺少必需的构建输入：%s（发布要求 %s=1，不提供时不得静默兜底）"
                % (SEED_ENV, REQUIRE_ENV))
        return []
    seed_path = Path(seed_env).expanduser().resolve()
    err, data = _load_and_validate_seed(seed_path)
    if err:
        raise SystemExit(f"分区种子不可用（{err}）。修正 {SEED_ENV} 后重试。")
    # 复制为固定包内名（原文件只读不改写）；复制后逐字节核对，
    # 溯源记录的 SHA256 始终是**原始输入文件**的。
    #
    # 暂存目录默认**每次构建独立**（mkdtemp）：监督发现固定共享名会被并发
    # 进程（例如同时跑的测试夹具）覆盖——字节核对通过了，PKG 组装时读到的
    # 却可能已被换成别的文件，导致打包进错误内容。独立目录从根上消除这个
    # 竞态；需要固定位置时用 BILIBILI_AREA_SEED_STAGING_DIR 显式指定。
    staging_env = os.environ.get(STAGING_ENV, "").strip()
    if staging_env:
        staging_dir = Path(staging_env)
        staging_dir.mkdir(parents=True, exist_ok=True)
    else:
        staging_dir = Path(tempfile.mkdtemp(prefix='bili-area-seed-'))
        atexit.register(shutil.rmtree, staging_dir, ignore_errors=True)
    staged = staging_dir / PACKAGE_SEED_NAME
    shutil.copy2(seed_path, staged)
    if staged.read_bytes() != seed_path.read_bytes():
        raise SystemExit(f"种子暂存复制不一致：{staged}")
    sha = hashlib.sha256(seed_path.read_bytes()).hexdigest()
    # 三个可核验字段：来源路径、种子的本地时间戳（数据上次落盘时间）、构建时间。
    # 不声称"采集自何时"——那是数据来源自己的事，本机构建脚本无从核实。
    line = ("[area-seed-provenance] source=%s sha256=%s source_mtime=%s "
            "built_utc=%s toplevel=%d package_name=%s staged=%s "
            "note=构建输入（非受跟踪文件；包内固定名 %s）"
            % (seed_path.as_posix(), sha,
               datetime.fromtimestamp(seed_path.stat().st_mtime, timezone.utc)
               .isoformat(timespec="seconds"),
               datetime.now(timezone.utc).isoformat(timespec="seconds"),
               len(data), PACKAGE_SEED_NAME, staged.as_posix(), PACKAGE_SEED_NAME))
    print(line)
    pv = os.environ.get(PROVENANCE_ENV, "").strip()
    if pv:
        Path(pv).write_text(line + "\n", encoding="utf-8")
    return [(str(staged), ".")]


a = Analysis(
    ['run.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_resolve_seed(),
    hiddenimports=[
        'app',
        'app.main',
        'app.core.config',
        'app.core.db',
        'app.core.task_manager',
        'app.core.live_controller',
        'app.core.email_sender',
        'app.core.data_migration',
        'app.api.auth',
        'app.api.tasks',
        'app.api.live',
        'app.api.areas',
        'app.api.settings',
        'app.api.email',
        'app.models.schemas',
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'anyio._backends._asyncio',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy.tests'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='run',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
