# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件 / 无控制台 / 体积裁剪）。

用法：
    python -m PyInstaller --noconfirm KeyClicker.spec

体积裁剪说明：下面剔除的都是本程序用不到的东西，删掉可以明显减小体积、加快启动。
  1. 软件 OpenGL 渲染器与 ANGLE：opengl32sw.dll(约 20 MB)、libEGL/libGLESv2、d3dcompiler_*.dll。
     界面是纯 QtWidgets 光栅绘制，不使用 OpenGL，运行时不会加载它们。
  2. Qt 翻译文件（*.qm）：程序没有安装 QTranslator，界面文案是代码里的中文，用不到。
  3. 无用的 Qt 插件：只保留 platforms（windows / minimal / offscreen），
     去掉 imageformats、iconengines、styles、generic 等（界面不用图标/图片文件）。
  4. 无用的 Python 模块：tkinter、unittest、numpy、PyQt6 的其它子模块等。
"""

# ── 需要剔除的二进制/DLL（按小写文件名或插件目录名匹配） ──
_DROP_BINARIES = (
    "opengl32sw.dll",
    "libegl.dll",
    "libglesv2.dll",
    "d3dcompiler_",
    "/translations/",          # Qt 翻译文件（约 5~10 MB）
    "/imageformats/",          # 图片格式插件
    "/iconengines/",           # 图标插件
    "/styles/",                # 原生样式插件（程序固定使用 Fusion，内置于 QtWidgets）
    "/generic/",               # 触摸等通用插件
    "/assetimporters/",
    "/sceneparsers/",
    "/geometryloaders/",
    "/renderers/",
    "/multimedia/",
    "/networkinformation/",
    "/position/",
    "/sensors/",
    "/sqldrivers/",
    "/texttospeech/",
    "/tls/",
    "/webview/",
    "/qmltooling/",
)

# ── 需要剔除的 Python 模块 ──
_EXCLUDES = [
    "tkinter", "_tkinter",
    "unittest", "doctest", "pydoc", "pydoc_data", "difflib", "lib2to3",
    "distutils", "setuptools", "pkg_resources", "pip", "wheel",
    "sqlite3", "xml", "xmlrpc", "html", "email", "http", "urllib",
    "asyncio", "concurrent", "curses", "idlelib", "test",
    "ssl", "_ssl",
    "numpy", "PIL", "pytest", "matplotlib", "IPython",
    "PyQt6.QtNetwork", "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtSql",
    "PyQt6.QtSvg", "PyQt6.QtSvgWidgets", "PyQt6.QtPrintSupport", "PyQt6.QtDBus",
    "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets", "PyQt6.QtXml", "PyQt6.QtTest",
]


def _trim(toc, patterns):
    """按关键字过滤 TOC 列表（兼容 (目标名, 源路径, 类型) 三元组与纯字符串两种写法）。"""
    kept = []
    for entry in toc:
        name = entry[0] if isinstance(entry, (tuple, list)) else entry
        low = str(name).replace("\\", "/").lower()
        if any(p in low for p in patterns):
            continue
        kept.append(entry)
    return kept


a = Analysis(
    ["keyclicker.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
    optimize=2,
)

# 在打包前把用不到的二进制与数据文件从清单里摘掉
a.binaries = _trim(a.binaries, _DROP_BINARIES)
a.datas = _trim(a.datas, ("/translations/",))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="KeyClicker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # 本机未安装 UPX；Qt 的 DLL 也不建议压缩（易被杀软误报）
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
