# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = []
hiddenimports += collect_submodules(
    'usb_pd_decoder',
    filter=lambda name: name not in {
        'usb_pd_decoder.gui',
        'usb_pd_decoder.windows_driver',
    },
)
datas = collect_data_files(
    'usb_pd_decoder',
    excludes=['drivers/*.inf', 'drivers/*.cat', 'drivers/*.txt'],
)


a = Analysis(
    ['usbpd_txt_decoder.py'],
    pathex=[],
    binaries=[],
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
    name='usbpd_txt_decoder',
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
)
