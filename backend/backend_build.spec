# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# Collect all data files from app/brokers/plugins and other necessary folders
datas = [
    ('../.env.example', '.'),
    ('app/brokers/plugins', 'app/brokers/plugins'),
]

a = Analysis(
    ['run_entry.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'app.brokers.plugins.paper',
        'app.brokers.plugins.fyers',
        'app.brokers.plugins.backtest',
        'sqlalchemy.ext.baked',
        'psycopg2',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SmartTraderBackend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SmartTraderBackend',
)
