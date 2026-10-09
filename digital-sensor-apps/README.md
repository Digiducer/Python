# Digital Sensor Apps

## Share with Windows colleagues

Build on a Windows x64 computer with Python installed:

```powershell
py -m pip install -r requirements.txt
py -m pip install pyinstaller
py -m PyInstaller --noconfirm DigitalSensorApps.spec
```

Send colleagues the **entire** `dist\DigitalSensorApps` folder, not just the `.exe` files. For a single download, zip the folder:

```powershell
tar.exe -a -c -f dist\DigitalSensorApps-Windows.zip -C dist DigitalSensorApps
```

After extracting it, run `DataRecorder.exe` for live acquisition or `DataViewer.exe` to inspect saved CSV/MAT recordings. The two apps can open one another from their menus. No Python installation is needed on the recipient machine. Recording files and window settings are saved outside the app folder, under the user's profile.

Both apps use the same channel palette. Channels 1-4 start as Modal Shop blue (`#005eb8`), charcoal (`#58585b`), amber (`#c47a14`), and teal (`#008c83`). Color changes made in either app are saved per user in `%LOCALAPPDATA%\digital-sensor-app\channel_colors.json`; existing Data Recorder color settings are loaded when that file does not yet exist. A recording's channel number, rather than its position in a file, determines its plot color.

Builds are specific to the OS and CPU architecture they were built for. The audio interface must still have its Windows driver installed. Windows may show an unrecognized-app warning for unsigned executables; sign builds before wider distribution if your organization requires it.
