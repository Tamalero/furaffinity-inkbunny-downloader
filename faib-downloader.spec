# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for FA & Inkbunny Downloader (Type-2 AppImage build)

a = Analysis(
    ['gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'PyQt6.sip',
        'cryptography.fernet',
        'cryptography.hazmat.primitives.ciphers',
        'cryptography.hazmat.primitives.ciphers.algorithms',
        'cryptography.hazmat.primitives.ciphers.modes',
        'cryptography.hazmat.backends',
        'cryptography.hazmat.backends.openssl',
        'bs4',
        'lxml',
        'lxml.etree',
        'lxml._elementpath',
        'lxml.html',
        'playwright',
        'playwright.sync_api',
        'playwright._impl._sync_context_manager',
        'camoufox',
        'camoufox.sync_api',
        'camoufox.pkgman',
        'camoufox.addons',
        'camoufox.fingerprints',
        'camoufox.locale',
        'camoufox.webgl',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy', 'pandas'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='faib-downloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    windowed=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='faib-downloader',
)
