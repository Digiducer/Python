import sounddevice as sd
import numpy as np
from sys import platform
import datetime

# FindDigiDevices
#
# returns an array of TMS compatible devices with associated information
#
# Returns dictionary items per device
#   "device"        - Device number to be used by SoundDevice stream
#   "model"         - Model number
#   "serial_number" - Serial number
#   "date"          - Calibration date
#   "format"        - format of data from device, 0 - acceleration, 1 - voltage
#   "sensitivity_int - Raw sensitivity as integer counts/EU ie Volta or m/s^2
#   "scale"         - sensitiivty scaled to float for use with a
#                     -1.0 to 1.0 scaled data.  Format returned with
#                     'float32' format to SoundDevice stream.
def FindDigiDevices():
    # The Modal Shop model number substrings
    models=["485B", "333D", "633A", "SDC0"]
    
    # Windows has a variety of API's to access audio
    # many of them manipulate the data and do not support setting actual
    # requested sample rates.  Windows Kernal Streaming allows direct control
    # so find devices using that API
    if platform == "win32":         # Windows...
        hapis=sd.query_hostapis()
        api_num=0
        for api in hapis:
            if api['name'] == "Windows WDM-KS":
                break
            api_num += 1
    else:
        # Not Windows - other platforms don't have the issue with the API
        api_num=0
    # Return all available audio inputs
    devices = sd.query_devices()
    dev_info = []   # Array to store info about each compa
    dev_num=0
    # Iterate through available devices and find ones named with a TMS model.
    # Note this returns multiple instances of the same device, because there
    # are different audio API's available.
    for device in devices:
        if (device['hostapi'] == api_num):
            name = device['name']
            match = next((x for x in models if x in name), False)
            if match != False:
                loc = name.find(match)
                model = name[loc:loc+6] # Extract the model
                fmt = name[loc+7:loc+8] # Extract the format of data
                serialnum = name[loc+8:loc+14]  # Extract the serial number
                # parse devices that are voltage
                if fmt == "2" or fmt == '3':
                    form = 1    # Voltage
                    # Extract the sensitivity
                    sens = [int(name[loc+14:loc+21]), int(name[loc+21:loc+28])]
                    if fmt == "3":  # 50mV reference for format 3
                        sens[0] *= 20 # Convert to 1V reference
                        sens[1] *= 20 
                    scale = np.array([8388608.0/sens[0],
                                      8388608.0/sens[1]],
                                     dtype='float32') # scale to volts
                    date = datetime.datetime.strptime(name[loc+28:loc+34], '%y%m%d') # Isolate the calibration date from the fullname string
                elif fmt == "1":
                    # These devices are acceleration
                    form = 0
                    # Extract the sensitivity
                    sens = [int(name[loc+14:loc+19]), int(name[loc+19:loc+24])]
                    scale = np.array([855400.0/sens[0],
                                      855400.0/sens[1]],
                                      dtype='float32') # scale to g's
                    date = datetime.datetime.strptime(name[loc+24:loc+30], '%y%m%d') # Isolate the calibration date from the fullname string
                else:
                      print("Expecting 1, 2, or 3 format")
                 # Add new device to array   
                dev_info.append({"device":dev_num,\
                                 "model":model,\
                                 "serial_number":serialnum,\
                                 "date":date,\
                                 "format":form,\
                                 "sensitivity_int":sens,\
                                 "scale":scale,\
                                 })
        dev_num += 1
    if len(dev_info) == 0:
        print("No compatible devices found")
    return dev_info
