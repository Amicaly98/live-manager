# -*- mode: python ; coding: utf-8 -*-
# run.spec - PyInstaller 打包配置：后端单文件 run.exe（E5）
#
# 构建（在 backend/ 目录）：
#   pyinstaller run.spec --noconfirm --clean --distpath dist --workpath build
# 产物：backend/dist/run.exe
#
# 产物说明：
# - 单文件可执行，内置 Python 运行时与全部依赖（fastapi/uvicorn/…），
#   用户机器无需安装 Python；
# - bili_areas_full.json 作为只读种子分区表打包进 exe（首次启动时
#   复制到数据目录，若数据目录已有则不覆盖）；
# - 运行时可写文件全部落在 --data-dir 指定的数据目录。

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)          # backend/
REPO = ROOT.parent             # 仓库根（分区表所在）

a = Analysis(
    ['run.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(REPO / 'bili_areas_full.json'), '.')],
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
