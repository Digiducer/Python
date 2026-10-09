# Digital Sensor Apps

## Build executables on a Windows x64 computer

Build on a Windows x64 computer with Python installed:

```powershell
py -m pip install -r requirements.txt
py -m pip install pyinstaller
py -m PyInstaller --noconfirm DigitalSensorApps.spec
```

Send  the **entire** `dist\DigitalSensorApps` folder, not just the `.exe` files. For a single download, zip the folder:

```powershell
tar.exe -a -c -f dist\DigitalSensorApps-Windows.zip -C dist DigitalSensorApps
```

After extracting it, run `DataRecorder.exe` for live acquisition or `DataViewer.exe` to inspect saved CSV/MAT recordings. The two apps can open one another from their menus. No Python installation is needed on the recipient machine. Recording files and window settings are saved outside the app folder, under the user's profile.

Builds are specific to the OS and CPU architecture they were built for. The audio interface must still have its Windows driver installed. Windows may show an unrecognized-app warning for unsigned executables; sign builds before wider distribution if your organization requires it.
