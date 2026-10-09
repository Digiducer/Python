import sounddevice as sd
import numpy as np
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
def FindInputDevices():
    # The Modal Shop model number substrings
    models=["485B", "333D", "633A", "SDC0"]
    hapis = sd.query_hostapis()

    # Return all available audio inputs
    devices = sd.query_devices()
    dev_info = []   # Array to store info about each compa
    # Iterate through available devices and find ones named with a TMS model.
    # Note this returns multiple instances of the same device, because there
    # are different audio API's available.
    for dev_num, device in enumerate(devices):
        if device['max_input_channels'] <= 0:
            continue

        name = device['name']
        match = next((model for model in models if model in name), None)
        device_info = {
            "device": dev_num,
            "hostapi": hapis[device['hostapi']]['name'],
            "name": name,
            "max_input_channels": device['max_input_channels'],
        }

        if match:
            loc = name.find(match)
            model = name[loc:loc + 6]
            fmt = name[loc + 7:loc + 8]
            serialnum = name[loc + 8:loc + 14]
            if len(model) == 6 and len(serialnum) == 6:
                device_info.update({"model": model, "serial_number": serialnum})

            try:
                if fmt in ("2", "3"):
                    form = 1
                    sens = [int(name[loc + 14:loc + 21]), int(name[loc + 21:loc + 28])]
                    if fmt == "3":
                        sens = [value * 20 for value in sens]
                    scale = np.array([8388608.0 / sens[0], 8388608.0 / sens[1]], dtype='float32')
                    date = datetime.datetime.strptime(name[loc + 28:loc + 34], '%y%m%d')
                elif fmt == "1":
                    form = 0
                    sens = [int(name[loc + 14:loc + 19]), int(name[loc + 19:loc + 24])]
                    scale = np.array([855400.0 / sens[0], 855400.0 / sens[1]], dtype='float32')
                    date = datetime.datetime.strptime(name[loc + 24:loc + 30], '%y%m%d')
                else:
                    raise ValueError("Unsupported device format")
            except (ValueError, ZeroDivisionError):
                pass
            else:
                device_info.update({
                    "date": date,
                    "format": form,
                    "sensitivity_int": sens,
                    "scale": scale,
                })

        dev_info.append(device_info)

    calibration_fields = ("date", "format", "sensitivity_int", "scale")
    calibration_by_device = {
        (device["model"], device["serial_number"]): {
            field: device[field] for field in calibration_fields
        }
        for device in dev_info
        if all(field in device for field in ("model", "serial_number", *calibration_fields))
    }
    for device in dev_info:
        identity = (device.get("model"), device.get("serial_number"))
        calibration = calibration_by_device.get(identity)
        if calibration:
            for field, value in calibration.items():
                device.setdefault(field, value)

    if len(dev_info) == 0:
        print("No compatible devices found")
    return dev_info
