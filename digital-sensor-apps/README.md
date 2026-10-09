# Digital Sensor Apps

## Rapid Prototyping of Digital Sensor Applications using AI and Python

This code and associated paper will be presented at the 45th International Modal Analysis Conference (IMAC XLV), Kissimmee, Florida, February 1-4, 2027.

### Abstract
Modern digital sensors and open-source software tools have significantly reduced the barriers to developing custom data acquisition applications for dynamic measurement applications. At the same time, recent advances in artificial intelligence (AI) have introduced new software development workflows that allow engineers with limited programming experience to rapidly create sophisticated measurement applications. This paper describes an engineering case study in which GitHub Copilot, used within Visual Studio Code, assisted the development of a Python Data Recorder and companion Data Viewer. Development began with the python-sounddevice example program: “Plot Microphone Signal(s) in Real-Time” and progressed through a graphical interface, host API and device selection, channel configuration, engineering-unit scaling, threshold triggering, continuous recording, and interactive review of recorded data. All of these capabilities are commonly found in commercial data acquisition software. Recorded prompts, application screenshots, source inspection, and Git history document the progression. The author specified measurement behavior and evaluated the application with physical hardware. The reviewed implementation sends full-rate samples to continuous recording and triggered records. The work presents a reproducible approach to AI-assisted prototyping and identifies the validation needed before using the prototype for quantitative dynamic measurements. Particular attention is given to how AI accelerates software development, assists with debugging, and enables rapid experimentation while allowing the engineer to remain responsible for defining measurement requirements and validating results. Rather than presenting AI as a replacement for engineering expertise, this work demonstrates how AI can serve as a practical development partner, enabling the rapid creation of customized digital sensor applications for laboratory testing, structural dynamics, and field measurements. The lessons learned provide a roadmap for engineers interested in leveraging modern AI tools to develop specialized measurement software without extensive software engineering experience.

Keywords: Digital sensors; AI-assisted programming; Python; Data acquisition; Rapid prototyping

## Data Recorder App

The data recorder app stripChartDisplay.py was inspired by plot_input.py, which was slightly modified and included here. It's original version can be found at https://python-sounddevice.readthedocs.io/en/latest/examples.html. The primary features of this app allow for live display of signals, the application of engineering-unit scaling for Digi devices, triggered capture, and disk recording.

## Data Viewer App

The data recorder app streamedDataViewer.py allows provides the ability to view data recorded by the Data Recorder App.

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
