# PyInstaller spec: builds a single self-contained executable.
#
#   pyinstaller jvc-sr-mv55.spec
#
# Output: dist/JVC-SR-MV55-Control(.exe)

block_cipher = None

analysis = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[("icon.ico", "."), ("icon.png", ".")],
    hiddenimports=["serial.tools.list_ports"],
    hookspath=[],
    runtime_hooks=[],
    # Qt ships a great deal we never touch; dropping it roughly halves the
    # size of the executable.
    excludes=[
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebChannel", "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtBluetooth",
        "PySide6.QtNetworkAuth", "PySide6.QtPositioning", "PySide6.QtSensors",
        "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
        "PySide6.QtHelp", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
        "tkinter", "matplotlib", "numpy", "PIL", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="JVC-SR-MV55-Control",
    icon="icon.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
)
