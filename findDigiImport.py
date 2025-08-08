# A Simple Digiducer and DigiDAQ App
# # Find the Digi Device and scale the data importing the
# FindDigiDevices() function found in FindDigiDevices.py
# Record two seconds of data and plot

import sounddevice as sd
import matplotlib.pyplot as plt
import numpy as np
# import the FindDigiDevices() function from FindDigiDevices.py
from FindDigiDevices import FindDigiDevices

fs = 48000 # Sampling rate
duration = 2 # Recording duration in seconds
buffersize = int(duration * fs)+1 # buffer size
devices=FindDigiDevices()
if not devices:
    print("No Devices Found")
    exit()

data = sd.rec(buffersize, samplerate=fs, channels=2, device=devices[0]['device']) # Start recording
sd.wait() # Wait for recording to finish
scaledData = data * devices[0]['scale'] # Scale the data
t = np.linspace(0, duration, int(duration * fs)+1)
plt.plot(t,scaledData) # Plot the recorded signal of Channel 1 & 2
plt.xlim(min(t),max(t))
plt.title(devices[0]['model'] + ' S/N: ' + devices[0]['serial_number'])
plt.xlabel('Time(seconds)')
if devices[0]['model'] == '485B39':
    plt.ylabel("Amplitude (Volts)")
else:
    plt.ylabel("Amplitude (g\'s)")
plt.show()
