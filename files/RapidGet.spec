# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\TANJIR\\Desktop\\files\\rapidget.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\TANJIR\\Desktop\\files\\rapidget.ico', '.'), ('C:\\Users\\TANJIR\\Desktop\\files\\icon16.png', '.'), ('C:\\Users\\TANJIR\\Desktop\\files\\icon32.png', '.'), ('C:\\Users\\TANJIR\\Desktop\\files\\icon48.png', '.'), ('C:\\Users\\TANJIR\\Desktop\\files\\icon128.png', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='RapidGet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\TANJIR\\Desktop\\files\\rapidget.ico'],
)
