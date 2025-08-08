# A Simple Digiducer and DigiDAQ App
# Record two seconds of data and plot

import sounddevice as sd
import matplotlib.pyplot as plt
import numpy as np

fs = 48000 # Sampling rate
duration = 2 # Recording duration in seconds
buffersize = int(duration * fs)+1 # buffer size
data = sd.rec(buffersize, samplerate=fs, channels=2) # Start recording
sd.wait() # Wait for recording to finish
t = np.linspace(0, duration, int(duration * fs)+1)
plt.plot(t,data) # Plot the recorded signal of Channel 1 & 2
plt.xlim(min(t),max(t))
plt.title('Digi Data')
plt.xlabel('Time(seconds)')
plt.ylabel('Unscaled Amplitude')
plt.show()