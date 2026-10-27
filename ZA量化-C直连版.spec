# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)

# Windows 文件属性与启动界面使用同一个 VERSION，避免复制/安装后难以判断
# 当前运行的是哪一轮合约查询修复。构建失败应直接报错，不回落到旧版本号。
product_version = (Path(SPECPATH) / 'VERSION').read_text(encoding='utf-8').strip()
version_tuple = tuple(int(part) for part in product_version.split('.')) + (0,)
version_resource = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple,
                      mask=0x3f, flags=0, OS=0x40004, fileType=0x1,
                      subtype=0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable('080404b0', [
            StringStruct('FileDescription', 'ZA量化-C直连版'),
            StringStruct('FileVersion', product_version),
            StringStruct('ProductName', 'ZA量化'),
            StringStruct('ProductVersion', product_version),
            StringStruct('OriginalFilename', 'ZA量化-C直连版.exe'),
        ])]),
        VarFileInfo([VarStruct('Translation', [2052, 1200])]),
    ],
)

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static'), ('config.json.example', '.'), ('VERSION', '.')],
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
    name='ZA量化-C直连版',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icon.ico'],
    version=version_resource,
)
