# -*- mode: python ; coding: utf-8 -*-
import sys

from PyInstaller.utils.hooks import collect_all

executable_name = (
    'DeployConsoleBatchSafe' if sys.platform == 'win32' else 'DeployConsoleParallel'
)
executable_icon = None
if sys.platform == 'win32':
    executable_icon = 'assets/deploy-console-parallel.ico'
elif sys.platform == 'darwin':
    executable_icon = 'assets/deploy-console-parallel.icns'

datas = []
binaries = []
hiddenimports = []
datas += [('assets/deploy-console-parallel.png', 'assets')]
tmp_ret = collect_all('selenium')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name=executable_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=executable_icon,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='DeployConsoleParallel.app',
        icon='assets/deploy-console-parallel.icns',
        bundle_identifier='com.digiwin.deployconsole.parallel',
        info_plist={
            'CFBundleDisplayName': 'Deploy Console Task Center',
            'NSHighResolutionCapable': True,
        },
    )
