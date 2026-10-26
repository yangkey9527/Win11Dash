# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.building.datastruct import TOC

# 本项目只用 QtCore/QtGui/QtWidgets，剔除用不到的标准库模块
EXCLUDES = [
    'ssl', 'tkinter', 'test', 'unittest', 'pydoc', 'pydoc_data', 'doctest',
    'lib2to3', 'asyncio', 'multiprocessing', 'sqlite3', 'venv', 'ensurepip',
    'http', 'xmlrpc',
]

# 运行期不会加载的 Qt 动态库与插件（已核验 Qt6Gui/Qt6Widgets/Qt6Core 不引用它们）
DROP_BINARIES = (
    'opengl32sw',                 # 软件渲染兜底，QPainter 位图绘制用不到
    'qt6quick', 'qtquick', 'qt6qml', 'qtqml', 'qt6pdf', 'qtpdf',
    'qt6opengl', 'qt6network', 'qtnetwork', 'qt6svg', 'qt6virtualkeyboard',
    'libcrypto-3-x64', 'libssl-3-x64',
    'qdirect2d', 'qoffscreen', 'qminimal',
    'qopensslbackend', 'qschannelbackend', 'qmodernwindowsstyle',
    'qtiff', 'qjpeg', 'qwebp', 'assimp',
    'ffmpegmediaplugin', 'windowsmediaplugin',
    'qmldbg', 'qmltooling', 'qgeoservices', 'qnetworklistmanager',
    'qtvirtualkeyboardplugin', 'qtposition', 'qaxwidget', 'qquickwidget',
    'qwebengineview',
)


def _dropped(entry):
    low = ' '.join(str(x) for x in entry[:2]).lower()
    return any(d in low for d in DROP_BINARIES)


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('lib', 'lib'),
           ('assets/PawnIO_setup.bin', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# 剔除冗余的 Qt 动态库与插件
a.binaries = TOC([e for e in a.binaries if not _dropped(e)])
a.datas = TOC([e for e in a.datas if not _dropped(e)])
# 界面文案为自绘中文，Qt 翻译文件只保留简体中文
a.datas = TOC([
    e for e in a.datas
    if 'translations' not in str(e[0]).lower() or 'zh_cn' in str(e[0]).lower()
])

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Win11Dash',
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
    uac_admin=True,
    icon=['app.ico'],
)
