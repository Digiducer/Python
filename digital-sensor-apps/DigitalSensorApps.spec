from PyInstaller.utils.hooks import collect_data_files


strip_chart = Analysis(
    ["stripChartDisplay.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
viewer = Analysis(
    ["streamedDataViewer.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")] + collect_data_files("tkinterdnd2"),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)

strip_chart_exe = EXE(
    PYZ(strip_chart.pure),
    strip_chart.scripts,
    [],
    exclude_binaries=True,
    name="DataRecorder",
    console=False,
    icon="assets/modal-shop-favicon.ico",
)
viewer_exe = EXE(
    PYZ(viewer.pure),
    viewer.scripts,
    [],
    exclude_binaries=True,
    name="DataViewer",
    console=False,
    icon="assets/modal-shop-favicon.ico",
)

bundle = COLLECT(
    strip_chart_exe,
    viewer_exe,
    strip_chart.binaries,
    strip_chart.datas,
    viewer.binaries,
    viewer.datas,
    name="DigitalSensorApps",
)