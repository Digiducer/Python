#!/usr/bin/env python3
"""
Data Recorder live strip chart display with:
- device selection
- start/stop acquisition
- pause/resume display
- auto plot layout based on device max_input_channels
- visible y-axis scale
"""

import queue
import json
import os
import re
import sys
import subprocess
import tkinter as tk
import threading
import uuid
import webbrowser
from datetime import date, datetime
from pathlib import Path
from tkinter import ttk, messagebox, colorchooser, filedialog

import numpy as np
import sounddevice as sd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from FindInputDevices import FindInputDevices
from app_instances import InstanceBroker, launch_or_activate
from channel_colors import channel_color as get_channel_color, load_channel_colors, save_channel_colors
from sensor_catalog import fetch_nominal_sensitivity, load_sensor_catalog, save_sensor_catalog
from stream_writer import CompositeStreamWriter, CsvStreamWriter, MatStreamWriter, save_samples
from trigger_capture import TriggerCapture


def _get_monitors():
    if sys.platform != "win32":
        return []

    import ctypes
    from ctypes import wintypes

    class MonitorInfoEx(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    monitors = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.RECT),
        ctypes.c_ssize_t,
    )

    def collect_monitor(handle, device_context, rect, data):
        info = MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            match = re.search(r"DISPLAY(\d+)$", info.szDevice, re.IGNORECASE)
            monitors.append({
                "device": info.szDevice,
                "index": int(match.group(1)) if match else len(monitors) + 1,
                "primary": bool(info.dwFlags & 1),
                "left": info.rcMonitor.left,
                "top": info.rcMonitor.top,
                "right": info.rcMonitor.right,
                "bottom": info.rcMonitor.bottom,
                "work_left": info.rcWork.left,
                "work_top": info.rcWork.top,
                "work_right": info.rcWork.right,
                "work_bottom": info.rcWork.bottom,
            })
        return True

    callback = callback_type(collect_monitor)
    user32.EnumDisplayMonitors(None, None, callback, 0)
    return sorted(monitors, key=lambda monitor: monitor["index"])


class WindowPlacement:
    def __init__(self, root, state_filename="window_state.json"):
        self.root = root
        self.path = self._state_path().with_name(state_filename)
        self.saved = self._load_state()
        self.monitors = _get_monitors()
        self.monitor = self._choose_monitor()
        self.normal_geometry = self.saved.get("normal_geometry", {})
        self.was_maximized = bool(self.saved.get("maximized", False))
        self.restored = False
        self.root.bind("<Configure>", self._remember_normal_geometry, add="+")

    @staticmethod
    def _state_path():
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / "digital-sensor-app" / "window_state.json"

    def _load_state(self):
        try:
            with self.path.open(encoding="utf-8") as state_file:
                state = json.load(state_file)
            return state if isinstance(state, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _choose_monitor(self):
        if not self.monitors:
            return None

        saved_device = self.saved.get("monitor")
        if saved_device:
            for monitor in self.monitors:
                if monitor["device"] == saved_device:
                    return monitor
            return max(self.monitors, key=lambda monitor: monitor["index"])

        return next(
            (monitor for monitor in self.monitors if monitor["primary"]),
            self.monitors[0],
        )

    def restore(self):
        self.root.update_idletasks()
        geometry = self.normal_geometry
        width = self._positive_int(geometry.get("width"), self.root.winfo_reqwidth())
        height = self._positive_int(geometry.get("height"), self.root.winfo_reqheight())

        if self.monitor:
            left = self.monitor["work_left"]
            top = self.monitor["work_top"]
            right = self.monitor["work_right"]
            bottom = self.monitor["work_bottom"]
        else:
            left = top = 0
            right = self.root.winfo_screenwidth()
            bottom = self.root.winfo_screenheight()

        available_width = max(1, right - left - 16)
        available_height = max(1, bottom - top - 16)
        width = min(width, available_width)
        height = min(height, available_height)
        x = self._int_or_none(geometry.get("x"))
        y = self._int_or_none(geometry.get("y"))
        x = left + 8 if x is None else min(max(x, left + 8), right - width - 8)
        y = top + 8 if y is None else min(max(y, top + 8), bottom - height - 8)

        self.normal_geometry = {"width": width, "height": height, "x": x, "y": y}
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.root.update_idletasks()
        if self.was_maximized and sys.platform == "win32":
            self.root.state("zoomed")
        self.restored = True

    @staticmethod
    def _positive_int(value, fallback):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return max(1, fallback)

    @staticmethod
    def _int_or_none(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _remember_normal_geometry(self, event=None):
        if not self.restored or self.root.state() == "zoomed":
            return
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        if width > 1 and height > 1:
            self.normal_geometry = {
                "width": width,
                "height": height,
                "x": self.root.winfo_x(),
                "y": self.root.winfo_y(),
            }

    def save(self):
        self.root.update_idletasks()
        maximized = self.root.state() == "zoomed"
        if not maximized:
            self._remember_normal_geometry()

        x = self.root.winfo_rootx()
        y = self.root.winfo_rooty()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        overlapping = [
            monitor for monitor in self.monitors
            if min(x + width, monitor["right"]) > max(x, monitor["left"])
            and min(y + height, monitor["bottom"]) > max(y, monitor["top"])
        ]
        if overlapping:
            self.monitor = max(
                overlapping,
                key=lambda monitor: (
                    min(x + width, monitor["right"]) - max(x, monitor["left"])
                ) * (
                    min(y + height, monitor["bottom"]) - max(y, monitor["top"])
                ),
            )

        state = {
            "monitor": self.monitor["device"] if self.monitor else None,
            "maximized": maximized,
            "normal_geometry": self.normal_geometry,
            "channel_colors": self.saved.get("channel_colors", {}),
            "stream_directory": self.saved.get("stream_directory"),
            "stream_name": self.saved.get("stream_name", "Streamed Data"),
            "stream_format": self.saved.get("stream_format", "CSV"),
            "last_streamed_file": self.saved.get("last_streamed_file"),
            "selected_host": self.saved.get("selected_host"),
            "selected_device_key": self.saved.get("selected_device_key"),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", encoding="utf-8") as state_file:
                json.dump(state, state_file)
        except OSError:
            pass


class AcquisitionEngine:
    def __init__(self):
        self.q = queue.Queue()
        self.stream = None
        self.running = False

        self.device_info = None
        self.device_index = None
        self.sample_rate = None
        self.downsample = 10
        self.window_ms = 3000

        self.selected_channels = []   # user-facing: [1,2,3,...]
        self.mapping = []             # zero-based: [0,1,2,...]
        self.channel_scales = np.ones(0, dtype=np.float32)
        self.channel_units = []
        self.units = None
        self.unit_conversion = 1.0
        self.trigger_capture = None
        self.triggered_records = queue.Queue()
        self.stream_writer = None
        self.stream_writer_lock = threading.Lock()

    def available_devices(self):
        return FindInputDevices()

    def configure(
        self, device_info, selected_channels, window_ms=3000, downsample=10,
        acceleration_unit="g's", channel_sensors=None, trigger_settings=None,
    ):
        if not selected_channels:
            raise ValueError("No channels selected")

        self.device_info = device_info
        self.device_index = device_info["device"]
        self.sample_rate = sd.query_devices(self.device_index, "input")["default_samplerate"]
        self.window_ms = float(window_ms)
        self.downsample = int(downsample)

        if self.downsample < 1:
            raise ValueError("Downsample must be >= 1")

        max_channels = int(device_info["max_input_channels"])
        if any(ch < 1 or ch > max_channels for ch in selected_channels):
            raise ValueError(
                f"Selected channels must be between 1 and {max_channels}"
            )

        self.selected_channels = list(selected_channels)
        self.mapping = [ch - 1 for ch in self.selected_channels]
        scales = device_info.get("scale")
        if scales is None:
            self.channel_scales = np.ones(len(self.mapping), dtype=np.float32)
        else:
            scales = np.asarray(scales, dtype=np.float32)
            if scales.ndim != 1 or any(channel > len(scales) for channel in self.selected_channels):
                raise ValueError("Calibration scale is missing for a selected channel")
            self.channel_scales = scales[self.mapping].copy()
        self.units = {0: "g's", 1: "Volts"}.get(device_info.get("format"))
        self.unit_conversion = 9.80665 if self.units == "g's" and acceleration_unit == "m/s²" else 1.0
        if self.units == "g's":
            self.units = acceleration_unit
        self.channel_units = [self.units] * len(self.selected_channels)

        for index, channel in enumerate(self.selected_channels):
            sensor = (channel_sensors or {}).get(channel)
            if sensor is None or device_info.get("format") != 1:
                continue
            sensitivity = sensor.get("calibrated_sensitivity") or sensor.get("nominal_sensitivity")
            try:
                sensitivity = float(sensitivity)
            except (TypeError, ValueError):
                raise ValueError(f"Channel {channel} sensor needs a valid sensitivity") from None
            engineering_unit = str(sensor.get("engineering_unit", "")).strip()
            if not np.isfinite(sensitivity) or sensitivity <= 0 or not engineering_unit:
                raise ValueError(f"Channel {channel} sensor needs a positive sensitivity and engineering unit")
            self.channel_scales[index] *= 1000.0 / sensitivity
            self.channel_units[index] = engineering_unit

        self.trigger_capture = None
        if trigger_settings is not None:
            trigger_channel = int(trigger_settings["channel"])
            if trigger_channel not in self.selected_channels:
                raise ValueError("Trigger channel must be included in selected channels")
            record_seconds = float(trigger_settings["record_seconds"])
            trigger_level = float(trigger_settings["level"])
            offset_percent = float(trigger_settings["offset_percent"])
            if not np.isfinite(record_seconds) or record_seconds <= 0:
                raise ValueError("Trigger record duration must be positive")
            if not np.isfinite(trigger_level) or not np.isfinite(offset_percent):
                raise ValueError("Trigger level and offset must be finite numbers")
            self.trigger_capture = TriggerCapture(
                selected_channels=self.selected_channels,
                trigger_channel=trigger_channel,
                level=trigger_level,
                slope=trigger_settings["slope"],
                record_samples=max(1, round(record_seconds * self.sample_rate)),
                offset_percent=offset_percent,
                mode=trigger_settings["mode"],
            )

    def get_plot_length(self):
        length = int(self.window_ms * self.sample_rate / (1000 * self.downsample))
        if length <= 0:
            raise ValueError("Plot length computed as zero or negative")
        return length

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)

        if not self.running:
            return

        try:
            data = indata[:, self.mapping].copy()
            data *= self.channel_scales
            data *= self.unit_conversion
            with self.stream_writer_lock:
                if self.stream_writer is not None:
                    self.stream_writer.write(data)
            self.q.put(data[::self.downsample].copy())
            if self.trigger_capture is not None:
                for record in self.trigger_capture.feed(data):
                    record["sample_rate"] = self.sample_rate
                    record["channels"] = list(self.selected_channels)
                    record["channel_units"] = list(self.channel_units)
                    self.triggered_records.put(record)
        except Exception as e:
            print(f"Callback error: {e}", file=sys.stderr)

    def start(self):
        if self.device_info is None:
            raise ValueError("Engine is not configured")

        if self.running:
            return

        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        while not self.triggered_records.empty():
            try:
                self.triggered_records.get_nowait()
            except queue.Empty:
                break

        # Need enough device channels to include the highest requested channel
        stream_channels = max(self.selected_channels)

        self.stream = sd.InputStream(
            device=self.device_index,
            channels=stream_channels,
            samplerate=self.sample_rate,
            callback=self._audio_callback,
        )
        self.stream.start()
        self.running = True

    def stop(self):
        self.running = False
        stream = self.stream
        self.stream = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def read_available_blocks(self):
        blocks = []
        while True:
            try:
                blocks.append(self.q.get_nowait())
            except queue.Empty:
                break
        return blocks

    def read_triggered_records(self):
        records = []
        while True:
            try:
                records.append(self.triggered_records.get_nowait())
            except queue.Empty:
                break
        return records

    def set_stream_writer(self, writer):
        with self.stream_writer_lock:
            previous_writer = self.stream_writer
            self.stream_writer = writer
        return previous_writer


class LiveAudioApp:
    def __init__(self, root, window_placement):
        self.root = root
        self.window_placement = window_placement
        self.root.title("Data Recorder")

        asset_dir = Path(__file__).resolve().parent / "assets"
        self.window_icon = tk.PhotoImage(file=str(asset_dir / "tms-round-mark.png"))
        self.root.iconphoto(True, self.window_icon)
        self.root.iconbitmap(str(asset_dir / "modal-shop-favicon.ico"))
        self.full_logo = tk.PhotoImage(file=str(asset_dir / "tms-logo-blue.png")).subsample(5, 5)

        self.engine = AcquisitionEngine()

        self.devices = []
        self.host_devices = []
        self.active_device_key = None
        self._device_snapshot = ()
        self._device_poll_id = None
        self._closing = False
        self.display_paused = False

        self.plotdata = None
        self.axes = []
        self.lines = []
        self.current_ylim = 0.025
        self.right_ylim = 0.025
        self.peak_abs = np.zeros(0, dtype=np.float32)
        self.right_axis = None
        self.right_channels = []
        self.channel_colors = load_channel_colors(self.root)
        self.sensor_catalog_path = window_placement.path.with_name("sensor_catalog.json")
        self.sensor_library, self.sensor_assignments = load_sensor_catalog(self.sensor_catalog_path)
        self.sensor_buttons = []
        default_stream_directory = WindowPlacement._state_path().parent / "streamed_data"
        self.stream_name_var = tk.StringVar(
            value=self.window_placement.saved.get("stream_name", "Streamed Data")
        )
        self.stream_format_var = tk.StringVar(
            value=self.window_placement.saved.get("stream_format", "CSV")
        )
        self.stream_directory_var = tk.StringVar(
            value=self.window_placement.saved.get("stream_directory", str(default_stream_directory))
        )
        self.stream_to_disk_var = tk.BooleanVar(value=False)
        self.stream_file_status_var = tk.StringVar(value="Not recording")
        self.stream_writer = None
        self.last_streamed_file = self.window_placement.saved.get("last_streamed_file")

        self.host_var = tk.StringVar()
        self.device_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle")
        self.channels_var = tk.StringVar(value="")
        self.right_channels_var = tk.StringVar(value="")
        self.order_channel_var = tk.StringVar(value="")
        self.window_var = tk.StringVar(value="3000")
        self.interval_var = tk.StringVar(value="30")
        self.downsample_var = tk.StringVar(value="10")
        self.yrange_var = tk.StringVar(value="0.025")
        self.yrange_display_var = tk.StringVar(value="±0.025")
        self.right_yrange_display_var = tk.StringVar(value="")
        self.layout_var = tk.StringVar(value="overlay")
        self.scale_mode_var = tk.StringVar(value="Continuous")
        self.acceleration_unit_var = tk.StringVar(value="g's")
        self.trigger_enabled_var = tk.BooleanVar(value=False)
        self.trigger_channel_var = tk.StringVar(value="1")
        self.trigger_slope_var = tk.StringVar(value="Positive")
        self.trigger_duration_var = tk.StringVar(value="2")
        self.trigger_mode_var = tk.StringVar(value="One-shot")
        self.trigger_offset_var = tk.StringVar(value="-25")
        self.trigger_level = 0.0
        self.trigger_level_display_var = tk.StringVar(value="Level: 0")
        self.trigger_cursor = None
        self.trigger_cursor_axis = None
        self._dragging_trigger_cursor = False
        self.trigger_record_count = 0
        self.repeat_capture_window = None

        self._build_ui()
        self.root.bind("<FocusIn>", self.refresh_channel_colors, add="+")
        self.refresh_devices()
        self._schedule_device_poll()
        self._schedule_plot_update()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(main, text="Controls", padding=10)
        controls.pack(fill="x")

        ttk.Label(controls, text="Driver:").grid(row=0, column=0, sticky="w", padx=5, pady=5)

        self.host_combo = ttk.Combobox(
            controls, textvariable=self.host_var, state="readonly", width=28
        )
        self.host_combo.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        self.host_combo.bind("<<ComboboxSelected>>", self.on_host_selected)

        ttk.Label(controls, text="Device:").grid(row=0, column=2, sticky="w", padx=5, pady=5)

        self.device_combo = ttk.Combobox(
            controls, textvariable=self.device_var, state="readonly", width=50
        )
        self.device_combo.grid(row=0, column=3, columnspan=4, sticky="ew", padx=5, pady=5)
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_selected)

        ttk.Label(controls, text="Channels (order):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.channels_entry = ttk.Entry(controls, textvariable=self.channels_var, width=20)
        self.channels_entry.grid(row=1, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(controls, text="Window ms:").grid(row=1, column=2, sticky="w", padx=5, pady=5)
        ttk.Entry(controls, textvariable=self.window_var, width=10).grid(
            row=1, column=3, sticky="w", padx=5, pady=5
        )

        ttk.Label(controls, text="Update ms:").grid(row=1, column=4, sticky="w", padx=5, pady=5)
        ttk.Entry(controls, textvariable=self.interval_var, width=10).grid(
            row=1, column=5, sticky="w", padx=5, pady=5
        )

        ttk.Label(controls, text="Downsample:").grid(row=1, column=6, sticky="w", padx=5, pady=5)
        ttk.Entry(controls, textvariable=self.downsample_var, width=10).grid(
            row=1, column=7, sticky="w", padx=5, pady=5
        )

        ttk.Label(controls, text="Initial Y range:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(controls, textvariable=self.yrange_var, width=12).grid(
            row=2, column=1, sticky="w", padx=5, pady=5
        )

        ttk.Label(controls, text="Left Y range:").grid(row=2, column=2, sticky="e", padx=5, pady=5)
        ttk.Label(controls, textvariable=self.yrange_display_var).grid(
            row=2, column=3, sticky="w", padx=5, pady=5
        )

        self.start_btn = ttk.Button(controls, text="Start", command=self.start_stream)
        self.start_btn.grid(row=2, column=4, padx=5, pady=5)

        self.stop_btn = ttk.Button(controls, text="Stop", command=self.stop_stream, state="disabled")
        self.stop_btn.grid(row=2, column=5, padx=5, pady=5)

        self.pause_btn = ttk.Button(
            controls, text="Pause Display", command=self.toggle_pause, state="disabled"
        )
        self.pause_btn.grid(row=2, column=6, padx=5, pady=5)

        ttk.Label(controls, textvariable=self.status_var).grid(
            row=2, column=7, sticky="w", padx=5, pady=5
        )

        ttk.Label(controls, text="Plot layout:").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        ttk.Radiobutton(
            controls, text="Overlay", variable=self.layout_var, value="overlay",
            command=self.on_layout_selected,
        ).grid(row=3, column=1, sticky="w", padx=5, pady=5)
        ttk.Radiobutton(
            controls, text="Separate", variable=self.layout_var, value="separate",
            command=self.on_layout_selected,
        ).grid(row=3, column=2, sticky="w", padx=5, pady=5)

        ttk.Label(controls, text="Right Y range:").grid(row=3, column=4, sticky="e", padx=5, pady=5)
        ttk.Label(controls, textvariable=self.right_yrange_display_var).grid(
            row=3, column=5, sticky="w", padx=5, pady=5
        )

        self.acceleration_controls = ttk.LabelFrame(
            controls, text="Digiducer Acceleration Units", padding=5
        )
        self.acceleration_controls.grid(
            row=5, column=0, columnspan=8, sticky="ew", padx=5, pady=5
        )
        self.acceleration_controls.grid_remove()
        ttk.Label(self.acceleration_controls, text="Units:").pack(side="left", padx=(0, 5))
        self.acceleration_unit_combo = ttk.Combobox(
            self.acceleration_controls, textvariable=self.acceleration_unit_var,
            values=("g's", "m/s²"), state="disabled", width=10,
        )
        self.acceleration_unit_combo.pack(side="left")

        ttk.Label(controls, text="Right axis channels:").grid(
            row=4, column=0, sticky="w", padx=5, pady=5
        )
        self.right_channels_entry = ttk.Entry(
            controls, textvariable=self.right_channels_var, width=20
        )
        self.right_channels_entry.grid(row=4, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(controls, text="Select channel:").grid(row=4, column=2, sticky="w", padx=5, pady=5)
        self.order_combo = ttk.Combobox(
            controls, textvariable=self.order_channel_var, state="readonly", width=8,
            postcommand=self.refresh_order_options,
        )
        self.order_combo.grid(row=4, column=3, sticky="w", padx=5, pady=5)
        self.order_combo.bind("<<ComboboxSelected>>", self.update_color_swatch)
        self.move_up_btn = ttk.Button(controls, text="Up", command=lambda: self.move_channel(-1))
        self.move_up_btn.grid(row=4, column=4, padx=5, pady=5)
        self.move_down_btn = ttk.Button(controls, text="Down", command=lambda: self.move_channel(1))
        self.move_down_btn.grid(row=4, column=5, padx=5, pady=5)
        ttk.Label(controls, text="Color:").grid(row=4, column=6, sticky="e", padx=5, pady=5)
        self.color_swatch = tk.Button(controls, width=3, command=self.choose_channel_color)
        self.color_swatch.grid(row=4, column=7, sticky="w", padx=5, pady=5)

        self.sensor_controls = ttk.LabelFrame(controls, text="DigiDAQ Channel Sensors", padding=5)
        self.sensor_controls.grid(row=5, column=0, columnspan=8, sticky="ew", padx=5, pady=5)
        self.sensor_controls.grid_remove()

        self.trigger_enable = ttk.Checkbutton(
            controls,
            text="Enable Triggering",
            variable=self.trigger_enabled_var,
            command=self.on_trigger_toggle,
        )
        self.trigger_enable.grid(row=6, column=0, sticky="w", padx=5, pady=5)
        ttk.Label(controls, text="Y scale:").grid(row=6, column=2, sticky="e", padx=5, pady=5)
        ttk.Radiobutton(
            controls, text="Continuous", variable=self.scale_mode_var,
            value="Continuous", command=self.on_scale_mode_changed,
        ).grid(row=6, column=3, sticky="w", padx=5, pady=5)
        ttk.Radiobutton(
            controls, text="Peak hold", variable=self.scale_mode_var,
            value="Peak hold", command=self.on_scale_mode_changed,
        ).grid(row=6, column=4, sticky="w", padx=5, pady=5)
        self.stream_to_disk_check = ttk.Checkbutton(
            controls,
            text="Stream to disk",
            variable=self.stream_to_disk_var,
            command=self.on_disk_stream_toggle,
            state="disabled",
        )
        self.stream_to_disk_check.grid(row=6, column=6, columnspan=2, sticky="w", padx=5, pady=5)
        self.trigger_controls = ttk.LabelFrame(controls, text="Trigger Capture", padding=5)
        self.trigger_controls.grid(row=7, column=0, columnspan=8, sticky="ew", padx=5, pady=5)
        self.trigger_controls.grid_remove()

        ttk.Label(self.trigger_controls, text="Channel:").grid(row=0, column=0, padx=4, pady=3)
        self.trigger_channel_combo = ttk.Combobox(
            self.trigger_controls, textvariable=self.trigger_channel_var,
            values=("1", "2"), state="readonly", width=5,
        )
        self.trigger_channel_combo.grid(row=0, column=1, padx=4, pady=3)
        self.trigger_channel_combo.bind("<<ComboboxSelected>>", self.update_trigger_cursor)
        ttk.Label(self.trigger_controls, text="Slope:").grid(row=0, column=2, padx=4, pady=3)
        self.trigger_slope_combo = ttk.Combobox(
            self.trigger_controls, textvariable=self.trigger_slope_var,
            values=("Positive", "Negative"), state="readonly", width=9,
        )
        self.trigger_slope_combo.grid(row=0, column=3, padx=4, pady=3)
        ttk.Label(self.trigger_controls, text="Record (s):").grid(row=0, column=4, padx=4, pady=3)
        self.trigger_duration_entry = ttk.Entry(
            self.trigger_controls, textvariable=self.trigger_duration_var, width=7
        )
        self.trigger_duration_entry.grid(row=0, column=5, padx=4, pady=3)
        ttk.Label(self.trigger_controls, text="Mode:").grid(row=0, column=6, padx=4, pady=3)
        self.trigger_mode_combo = ttk.Combobox(
            self.trigger_controls, textvariable=self.trigger_mode_var,
            values=("One-shot", "Repeat (same window)", "Repeat (new window)"),
            state="readonly", width=21,
        )
        self.trigger_mode_combo.grid(row=0, column=7, padx=4, pady=3)
        ttk.Label(self.trigger_controls, text="Offset (% of record):").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=4, pady=3
        )
        self.trigger_offset_entry = ttk.Entry(
            self.trigger_controls, textvariable=self.trigger_offset_var, width=7
        )
        self.trigger_offset_entry.grid(row=1, column=2, sticky="w", padx=4, pady=3)
        ttk.Label(
            self.trigger_controls,
            text="Negative = pre-trigger; positive = wait after trigger",
        ).grid(row=1, column=3, columnspan=3, sticky="w", padx=4, pady=3)
        ttk.Label(self.trigger_controls, textvariable=self.trigger_level_display_var).grid(
            row=1, column=6, columnspan=2, sticky="e", padx=4, pady=3
        )

        self.disk_stream_controls = ttk.LabelFrame(controls, text="Stream File Settings", padding=5)
        self.disk_stream_controls.grid(row=9, column=0, columnspan=8, sticky="ew", padx=5, pady=5)
        ttk.Label(self.disk_stream_controls, text="File name:").grid(
            row=0, column=0, sticky="w", padx=4, pady=3
        )
        self.stream_name_entry = ttk.Entry(
            self.disk_stream_controls, textvariable=self.stream_name_var, width=22
        )
        self.stream_name_entry.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        ttk.Label(self.disk_stream_controls, text="Directory:").grid(
            row=0, column=2, sticky="e", padx=4, pady=3
        )
        self.stream_directory_entry = ttk.Entry(
            self.disk_stream_controls, textvariable=self.stream_directory_var,
            state="readonly", width=42,
        )
        self.stream_directory_entry.grid(row=0, column=3, columnspan=3, sticky="ew", padx=4, pady=3)
        self.stream_browse_button = ttk.Button(
            self.disk_stream_controls, text="Browse...", command=self.choose_stream_directory
        )
        self.stream_browse_button.grid(row=0, column=6, columnspan=2, sticky="w", padx=4, pady=3)
        stream_formats = ("CSV", "MAT-file (.mat)", "CSV + MAT-file")
        if self.stream_format_var.get() not in stream_formats:
            self.stream_format_var.set("CSV")
        ttk.Label(self.disk_stream_controls, text="Format:").grid(
            row=1, column=0, sticky="w", padx=4, pady=3
        )
        self.stream_format_combo = ttk.Combobox(
            self.disk_stream_controls,
            textvariable=self.stream_format_var,
            values=stream_formats,
            state="readonly",
            width=18,
        )
        self.stream_format_combo.grid(row=1, column=1, sticky="w", padx=4, pady=3)
        ttk.Label(self.disk_stream_controls, textvariable=self.stream_file_status_var).grid(
            row=1, column=2, columnspan=6, sticky="w", padx=4, pady=3
        )
        self.open_viewer_button = ttk.Button(
            self.disk_stream_controls,
            text="Open Data Viewer",
            command=self.open_streamed_data_viewer,
        )
        self.open_viewer_button.grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=3)
        self.disk_stream_controls.columnconfigure(3, weight=1)

        plot_frame = ttk.LabelFrame(main, text="Live Plot", padding=5)
        plot_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.fig = Figure(figsize=(10, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("button_press_event", self._on_trigger_cursor_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_trigger_cursor_motion)
        self.canvas.mpl_connect("button_release_event", self._on_trigger_cursor_release)

        footer = ttk.Frame(main)
        footer.pack(fill="x", pady=(3, 0))
        ttk.Label(footer, image=self.full_logo).pack(side="right")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    @staticmethod
    def _device_key(device):
        if device.get("model") and device.get("serial_number"):
            return device.get("hostapi"), device["model"], device["serial_number"]
        return device.get("hostapi"), device.get("name"), device.get("max_input_channels")

    @classmethod
    def _device_signature(cls, devices):
        return tuple(cls._device_key(device) for device in devices)

    def refresh_devices(self, devices=None):
        try:
            previous_host = self.host_var.get() or self.window_placement.saved.get("selected_host", "")
            previous_device_key = (
                self._device_key(self.get_selected_device())
                if self.host_devices and self.device_combo.current() >= 0 else None
            )
            if previous_device_key is None:
                saved_device_key = self.window_placement.saved.get("selected_device_key")
                if isinstance(saved_device_key, list):
                    previous_device_key = tuple(saved_device_key)
            self.devices = self.engine.available_devices() if devices is None else devices
            self._device_snapshot = self._device_signature(self.devices)
            hosts = list(dict.fromkeys(d["hostapi"] for d in self.devices))
            self.host_combo["values"] = hosts

            if hosts:
                selected_host = previous_host if previous_host in hosts else (
                    "Windows WDM-KS" if "Windows WDM-KS" in hosts else hosts[0]
                )
                self.host_combo.current(hosts.index(selected_host))
                self.on_host_selected()
                for index, device in enumerate(self.host_devices):
                    if self._device_key(device) == previous_device_key:
                        self.device_combo.current(index)
                        self.on_device_selected()
                        break
                self.status_var.set(f"{len(self.devices)} device(s) found")
            else:
                self.host_var.set("")
                self.host_devices = []
                self.device_combo["values"] = []
                self.device_var.set("")
                self.channels_var.set("")
                self.refresh_sensor_controls({})
                self.status_var.set("No input devices found")
        except Exception as e:
            messagebox.showerror("Device Error", str(e))
            self.status_var.set("Error refreshing devices")

    def _schedule_device_poll(self):
        if not self._closing:
            self._device_poll_id = self.root.after(1000, self._poll_devices)

    def _poll_devices(self):
        if self._closing:
            return

        try:
            devices = self.engine.available_devices()
        except Exception:
            self._schedule_device_poll()
            return

        signature = self._device_signature(devices)
        active_device_removed = (
            self.active_device_key is not None
            and self.active_device_key not in signature
        )
        if active_device_removed:
            self.stop_stream("Active device disconnected; acquisition stopped")

        if signature != self._device_snapshot:
            self.refresh_devices(devices)
            if active_device_removed:
                self.status_var.set("Active device disconnected; acquisition stopped")

        self._schedule_device_poll()

    def get_selected_device(self):
        index = self.device_combo.current()
        if index < 0 or index >= len(self.host_devices):
            raise ValueError("No device selected")
        return self.host_devices[index]

    def on_host_selected(self, event=None):
        self.host_devices = [d for d in self.devices if d["hostapi"] == self.host_var.get()]
        self.device_combo["values"] = [
            (
                f'{("DigiDAQ " if d["model"].startswith("485B") else "Digiducer " if d["model"].startswith(("333D", "633A")) else "")}'
                f'{d["model"]} (Serial {d["serial_number"]})'
                if d.get("model") and d.get("serial_number")
                else f'{d["name"]} (device {d["device"]})'
            )
            for d in self.host_devices
        ]
        if self.host_devices:
            self.device_combo.current(0)
            self.on_device_selected()
        else:
            self.device_var.set("")
            self.channels_var.set("")
            self.refresh_sensor_controls({})
            self.status_var.set("No input devices for this driver")

    def on_device_selected(self, event=None):
        try:
            dev = self.get_selected_device()
            max_ch = int(dev["max_input_channels"])
            self.channels_var.set(",".join(str(i) for i in range(1, max_ch + 1)))
            self.right_channels_var.set("")
            self._update_acceleration_unit_control(dev)
            self.refresh_sensor_controls(dev)
            self.refresh_order_options()
            self.status_var.set(
                f"Selected device with {max_ch} input channel(s)"
            )
        except Exception as e:
            self.status_var.set(str(e))

    def _update_acceleration_unit_control(self, device=None):
        if device is None:
            try:
                device = self.get_selected_device()
            except ValueError:
                device = {}
        is_digiducer = str(device.get("model", "")).startswith(("333D", "633A"))
        if is_digiducer:
            self.acceleration_controls.grid()
        else:
            self.acceleration_controls.grid_remove()
        state = "readonly" if is_digiducer and not self.engine.running else "disabled"
        self.acceleration_unit_combo.configure(state=state)

    def on_trigger_toggle(self):
        if self.trigger_enabled_var.get():
            self.trigger_controls.grid()
            self._refresh_trigger_channel_options()
        else:
            self.trigger_controls.grid_remove()
        self._set_trigger_controls_state(not self.engine.running and self.trigger_enabled_var.get())
        self.update_trigger_cursor()

    def _refresh_trigger_channel_options(self):
        try:
            channels = [str(channel) for channel in self.parse_selected_channels()]
        except ValueError:
            channels = []
        self.trigger_channel_combo["values"] = channels
        if channels and self.trigger_channel_var.get() not in channels:
            self.trigger_channel_var.set(channels[0])
        self._update_trigger_level_display()

    def _trigger_settings(self, selected_channels):
        if not self.trigger_enabled_var.get():
            return None
        try:
            channel = int(self.trigger_channel_var.get())
            duration = float(self.trigger_duration_var.get())
            offset = float(self.trigger_offset_var.get())
        except ValueError:
            raise ValueError("Enter a valid trigger channel, duration, and offset") from None
        if channel not in selected_channels:
            raise ValueError("The trigger channel must be included in Channels (order)")
        if not np.isfinite(duration) or duration <= 0:
            raise ValueError("Trigger record duration must be positive")
        if not np.isfinite(offset) or not -100 <= offset <= 100:
            raise ValueError("Trigger offset must be between -100% and 100%")
        return {
            "channel": channel,
            "level": self.trigger_level,
            "slope": self.trigger_slope_var.get(),
            "record_seconds": duration,
            "offset_percent": offset,
            "mode": self.trigger_mode_var.get(),
        }

    def _trigger_axis(self, channel):
        if not self.axes:
            return None
        if self.layout_var.get() == "overlay":
            if channel in self.right_channels and self.right_axis is not None:
                return self.right_axis
            return self.axes[0]
        try:
            return self.axes[self.engine.selected_channels.index(channel)]
        except (ValueError, IndexError):
            return None

    def update_trigger_cursor(self, event=None):
        if self.trigger_cursor is not None:
            try:
                self.trigger_cursor.remove()
            except (NotImplementedError, ValueError):
                pass
            self.trigger_cursor = None
            self.trigger_cursor_axis = None

        if self.trigger_enabled_var.get() and self.engine.selected_channels:
            try:
                channel = int(self.trigger_channel_var.get())
            except ValueError:
                channel = None
            axis = self._trigger_axis(channel) if channel is not None else None
            if axis is not None:
                self.trigger_cursor = axis.axhline(
                    self.trigger_level, color="#c23b22", linestyle="--",
                    linewidth=1.5, picker=7, zorder=10,
                )
                self.trigger_cursor_axis = axis
        self._update_trigger_level_display()
        if self.axes:
            self.canvas.draw_idle()

    def _update_trigger_level_display(self):
        unit = ""
        try:
            channel = int(self.trigger_channel_var.get())
            channel_index = self.engine.selected_channels.index(channel)
            unit = self.engine.channel_units[channel_index]
        except (ValueError, IndexError):
            pass
        level = f"{self.trigger_level:.5g}"
        self.trigger_level_display_var.set(f"Level: {level} {unit}".rstrip())

    def _on_trigger_cursor_press(self, event):
        if (
            not self.trigger_enabled_var.get()
            or self.trigger_cursor is None
            or event.inaxes is not self.trigger_cursor_axis
        ):
            return
        contains, _ = self.trigger_cursor.contains(event)
        self._dragging_trigger_cursor = bool(contains)

    def _on_trigger_cursor_motion(self, event):
        if (
            not self._dragging_trigger_cursor
            or self.trigger_cursor is None
            or event.inaxes is not self.trigger_cursor_axis
            or event.ydata is None
        ):
            return
        self.trigger_level = float(event.ydata)
        self.trigger_cursor.set_ydata([self.trigger_level, self.trigger_level])
        if self.engine.trigger_capture is not None:
            self.engine.trigger_capture.set_level(self.trigger_level)
        self._update_trigger_level_display()
        self.canvas.draw_idle()

    def _on_trigger_cursor_release(self, event):
        self._dragging_trigger_cursor = False

    def _set_trigger_controls_state(self, enabled):
        for widget in (
            self.trigger_channel_combo,
            self.trigger_slope_combo,
            self.trigger_duration_entry,
            self.trigger_mode_combo,
            self.trigger_offset_entry,
        ):
            state = "readonly" if enabled and isinstance(widget, ttk.Combobox) else (
                "normal" if enabled else "disabled"
            )
            widget.configure(state=state)

    @staticmethod
    def _sensor_assignment_key(device, channel):
        if device.get("model") and device.get("serial_number"):
            identity = [device["model"], device["serial_number"]]
        else:
            identity = [device.get("name", "unknown")]
        return json.dumps([*identity, channel], separators=(",", ":"))

    def refresh_sensor_controls(self, device=None):
        if device is None:
            try:
                device = self.get_selected_device()
            except ValueError:
                device = {}
        if not str(device.get("model", "")).startswith("485B"):
            self.sensor_controls.grid_remove()
            return

        for widget in self.sensor_controls.winfo_children():
            widget.destroy()
        self.sensor_controls.grid()
        self.sensor_buttons = []
        for channel in range(1, int(device["max_input_channels"]) + 1):
            sensor_id = self.sensor_assignments.get(self._sensor_assignment_key(device, channel))
            sensor = self.sensor_library.get(sensor_id)
            if sensor:
                sensor_name = f"{sensor['manufacturer']} {sensor['model']}"
                if sensor.get("serial_number"):
                    sensor_name += f" (S/N {sensor['serial_number']})"
                text = f"Ch {channel}: {sensor_name}"
            else:
                text = f"Configure Ch {channel} Sensor"
            button = ttk.Button(
                self.sensor_controls,
                text=text,
                command=lambda selected_channel=channel: self.open_sensor_dialog(
                    device, selected_channel
                ),
            )
            button.pack(side="left", padx=(0, 6))
            self.sensor_buttons.append(button)

    def _persist_sensor_catalog(self, sensors, assignments):
        save_sensor_catalog(self.sensor_catalog_path, sensors, assignments)
        self.sensor_library = sensors
        self.sensor_assignments = assignments

    def open_sensor_dialog(self, device, channel):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Channel {channel} Sensor")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        assignment_key = self._sensor_assignment_key(device, channel)
        labels_by_id = {}
        ids_by_label = {}
        for sensor_id, sensor in sorted(
            self.sensor_library.items(),
            key=lambda item: (item[1].get("manufacturer", ""), item[1].get("model", "")),
        ):
            label = (
                f"{sensor.get('manufacturer', '')} {sensor.get('model', '')}"
                f" (S/N {sensor.get('serial_number') or 'not set'})"
            )
            if label in ids_by_label:
                label += f" [{sensor_id[:6]}]"
            labels_by_id[sensor_id] = label
            ids_by_label[label] = sensor_id

        new_sensor_label = "New sensor"
        choice_var = tk.StringVar(dialog, value=new_sensor_label)
        saved_sensor_combo = ttk.Combobox(
            dialog,
            textvariable=choice_var,
            values=(new_sensor_label, *ids_by_label),
            state="readonly",
            width=48,
        )
        ttk.Label(dialog, text="Saved sensor:").grid(row=0, column=0, sticky="w", padx=8, pady=5)
        saved_sensor_combo.grid(row=0, column=1, columnspan=3, sticky="ew", padx=8, pady=5)

        fields = {
            "manufacturer": tk.StringVar(dialog),
            "model": tk.StringVar(dialog),
            "serial_number": tk.StringVar(dialog),
            "nominal_sensitivity": tk.StringVar(dialog),
            "calibrated_sensitivity": tk.StringVar(dialog),
            "engineering_unit": tk.StringVar(dialog),
            "calibration_date": tk.StringVar(dialog),
        }
        field_labels = (
            ("manufacturer", "Manufacturer:"),
            ("model", "Model number:"),
            ("serial_number", "Serial number:"),
            ("nominal_sensitivity", "Nominal sensitivity (mV/EU):"),
            ("calibrated_sensitivity", "Calibrated sensitivity (mV/EU):"),
            ("engineering_unit", "Engineering unit (e.g. g, lbf, N):"),
            ("calibration_date", "Calibration date (YYYY-MM-DD):"),
        )
        for row, (field_name, label) in enumerate(field_labels, start=1):
            ttk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(dialog, textvariable=fields[field_name], width=36).grid(
                row=row, column=1, columnspan=3, sticky="ew", padx=8, pady=4
            )

        status_var = tk.StringVar(value="")
        source_url_var = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=status_var, wraplength=510).grid(
            row=8, column=0, columnspan=4, sticky="w", padx=8, pady=(5, 0)
        )
        source_button = ttk.Button(
            dialog,
            text="Open source page",
            command=lambda: webbrowser.open(source_url_var.get()),
            state="disabled",
        )
        source_button.grid(row=9, column=0, sticky="w", padx=8, pady=5)

        def selected_sensor_id():
            return ids_by_label.get(choice_var.get())

        def populate_fields(sensor=None):
            for field_name, variable in fields.items():
                variable.set(str(sensor.get(field_name, "")) if sensor else "")
            source_url_var.set(sensor.get("sensitivity_source", "") if sensor else "")
            source_button.configure(state="normal" if source_url_var.get() else "disabled")
            status_var.set("")

        def on_sensor_selected(event=None):
            sensor_id = selected_sensor_id()
            populate_fields(self.sensor_library.get(sensor_id))

        saved_sensor_combo.bind("<<ComboboxSelected>>", on_sensor_selected)
        assigned_id = self.sensor_assignments.get(assignment_key)
        if assigned_id in labels_by_id:
            choice_var.set(labels_by_id[assigned_id])
            populate_fields(self.sensor_library[assigned_id])

        def clear_sensitivity_source(*args):
            source_url_var.set("")
            source_button.configure(state="disabled")

        fields["manufacturer"].trace_add("write", clear_sensitivity_source)
        fields["model"].trace_add("write", clear_sensitivity_source)

        search_button = ttk.Button(dialog, text="Search Web")
        search_button.grid(row=2, column=4, sticky="w", padx=(0, 8), pady=4)

        def search_sensitivity():
            manufacturer = fields["manufacturer"].get().strip()
            model = fields["model"].get().strip()
            if not manufacturer or not model:
                status_var.set("Enter manufacturer and model number first.")
                return
            search_button.configure(state="disabled")
            status_var.set("Searching manufacturer and product data...")

            def search_worker():
                try:
                    result = fetch_nominal_sensitivity(manufacturer, model)
                    error = None
                except Exception as exception:
                    result = None
                    error = str(exception)

                def finish_search():
                    if not dialog.winfo_exists():
                        return
                    search_button.configure(state="normal")
                    if error:
                        status_var.set(f"Sensitivity search failed: {error}")
                        return
                    fields["nominal_sensitivity"].set(
                        f"{result['nominal_sensitivity']:g}"
                    )
                    fields["engineering_unit"].set(result["engineering_unit"])
                    source_url_var.set(result["source_url"])
                    source_button.configure(state="normal")
                    status_var.set(
                        f"Found {result['nominal_sensitivity']:g} mV/{result['engineering_unit']}"
                    )

                try:
                    self.root.after(0, finish_search)
                except (RuntimeError, tk.TclError):
                    pass

            threading.Thread(target=search_worker, daemon=True).start()

        search_button.configure(command=search_sensitivity)

        def assign_existing():
            sensor_id = selected_sensor_id()
            if sensor_id is None:
                messagebox.showerror("Sensor", "Select a saved sensor first.", parent=dialog)
                return
            assignments = dict(self.sensor_assignments)
            assignments[assignment_key] = sensor_id
            try:
                self._persist_sensor_catalog(dict(self.sensor_library), assignments)
            except OSError as error:
                messagebox.showerror("Sensor", f"Could not save sensor assignment: {error}", parent=dialog)
                return
            self.refresh_sensor_controls(device)
            dialog.destroy()

        def save_and_assign():
            manufacturer = fields["manufacturer"].get().strip()
            model = fields["model"].get().strip()
            engineering_unit = fields["engineering_unit"].get().strip()
            if not manufacturer or not model or not engineering_unit:
                messagebox.showerror(
                    "Sensor", "Manufacturer, model, and engineering unit are required.", parent=dialog
                )
                return
            try:
                nominal_sensitivity = float(fields["nominal_sensitivity"].get())
                calibrated_text = fields["calibrated_sensitivity"].get().strip()
                calibrated_sensitivity = float(calibrated_text) if calibrated_text else None
                if not np.isfinite(nominal_sensitivity) or nominal_sensitivity <= 0 or (
                    calibrated_sensitivity is not None
                    and (not np.isfinite(calibrated_sensitivity) or calibrated_sensitivity <= 0)
                ):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Sensor", "Enter positive numeric sensitivities in mV per engineering unit.",
                    parent=dialog,
                )
                return

            calibration_date = fields["calibration_date"].get().strip()
            if calibration_date:
                try:
                    date.fromisoformat(calibration_date)
                except ValueError:
                    messagebox.showerror(
                        "Sensor", "Enter the calibration date as YYYY-MM-DD.", parent=dialog
                    )
                    return

            sensor_id = selected_sensor_id() or uuid.uuid4().hex
            sensor = {
                "id": sensor_id,
                "manufacturer": manufacturer,
                "model": model,
                "serial_number": fields["serial_number"].get().strip(),
                "nominal_sensitivity": nominal_sensitivity,
                "calibrated_sensitivity": calibrated_sensitivity,
                "engineering_unit": engineering_unit,
                "calibration_date": calibration_date,
                "sensitivity_source": source_url_var.get(),
            }
            sensors = dict(self.sensor_library)
            sensors[sensor_id] = sensor
            assignments = dict(self.sensor_assignments)
            assignments[assignment_key] = sensor_id
            try:
                self._persist_sensor_catalog(sensors, assignments)
            except OSError as error:
                messagebox.showerror("Sensor", f"Could not save sensor: {error}", parent=dialog)
                return
            self.refresh_sensor_controls(device)
            dialog.destroy()

        actions = ttk.Frame(dialog)
        actions.grid(row=10, column=0, columnspan=5, sticky="e", padx=8, pady=8)
        ttk.Button(actions, text="Assign Existing", command=assign_existing).pack(side="left", padx=4)
        ttk.Button(actions, text="Save & Assign", command=save_and_assign).pack(side="left", padx=4)
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="left", padx=4)
        dialog.columnconfigure(1, weight=1)
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{x}+{y}")

    def parse_selected_channels(self):
        text = self.channels_var.get().strip()
        if not text:
            raise ValueError("Channels cannot be empty")

        channels = [int(x.strip()) for x in text.split(",") if x.strip()]
        if not channels:
            raise ValueError("No valid channels entered")
        if any(ch < 1 for ch in channels):
            raise ValueError("Channels must be >= 1")
        if len(channels) != len(set(channels)):
            raise ValueError("Channels must be unique")

        return channels

    def parse_right_channels(self, channels):
        text = self.right_channels_var.get().strip()
        right_channels = [int(value.strip()) for value in text.split(",") if value.strip()]
        if len(right_channels) != len(set(right_channels)) or any(
            channel not in channels for channel in right_channels
        ):
            raise ValueError("Right axis channels must be unique selected channels")
        return right_channels

    def refresh_order_options(self):
        try:
            channels = self.parse_selected_channels()
        except ValueError:
            self.order_combo["values"] = []
            self.order_channel_var.set("")
            return
        selected = self.order_channel_var.get()
        self.order_combo["values"] = [str(channel) for channel in channels]
        self.order_channel_var.set(selected if selected in self.order_combo["values"] else str(channels[0]))
        self.update_color_swatch()
        self._refresh_trigger_channel_options()

    def channel_color(self, channel):
        return get_channel_color(channel, self.channel_colors)

    def _save_channel_colors(self):
        self.window_placement.saved["channel_colors"] = {
            str(channel): color for channel, color in self.channel_colors.items()
        }
        save_channel_colors(self.channel_colors)

    def refresh_channel_colors(self, event=None):
        colors = load_channel_colors(self.root)
        if colors == self.channel_colors:
            return
        self.channel_colors = colors
        self.update_color_swatch()
        for channel, line in zip(self.engine.selected_channels, self.lines):
            line.set_color(self.channel_color(channel))
        self.canvas.draw_idle()

    def update_color_swatch(self, event=None):
        try:
            color = self.channel_color(int(self.order_channel_var.get()))
        except ValueError:
            self.color_swatch.config(state="disabled")
            return
        self.color_swatch.config(state="normal", bg=color, activebackground=color)

    def choose_channel_color(self):
        channel = int(self.order_channel_var.get())
        _, color = colorchooser.askcolor(color=self.channel_color(channel), parent=self.root)
        if color is None:
            return
        self.channel_colors[channel] = color
        self._save_channel_colors()
        self.update_color_swatch()
        for selected, line in zip(self.engine.selected_channels, self.lines):
            if selected == channel:
                line.set_color(color)
        if self.layout_var.get() == "overlay" and self.axes:
            self.axes[0].legend(self.lines, [line.get_label() for line in self.lines], loc="upper right")
        self.canvas.draw_idle()

    def move_channel(self, direction):
        try:
            channels = self.parse_selected_channels()
            selected = int(self.order_channel_var.get())
            index = channels.index(selected)
            destination = index + direction
            if 0 <= destination < len(channels):
                channels[index], channels[destination] = channels[destination], channels[index]
                self.channels_var.set(",".join(str(channel) for channel in channels))
                self.refresh_order_options()
        except (ValueError, IndexError) as error:
            self.status_var.set(str(error))

    def build_plot(self, num_channels, plot_length, initial_ylim):
        self.trigger_cursor = None
        self.trigger_cursor_axis = None
        self._dragging_trigger_cursor = False
        self.fig.clear()
        self.axes = []
        self.lines = []
        self.right_axis = None
        self.current_ylim = initial_ylim
        self.right_ylim = initial_ylim
        self.yrange_display_var.set(f"±{self.current_ylim:.4g}")
        self.right_yrange_display_var.set("")

        overlay = self.layout_var.get() == "overlay"
        for i in range(num_channels):
            if overlay and self.axes:
                ax = self.axes[0]
            else:
                sharex = self.axes[0] if self.axes else None
                ax = self.fig.add_subplot(
                    1 if overlay else num_channels, 1, 1 if overlay else i + 1,
                    sharex=sharex,
                )
                ax.set_xlim(0, plot_length)
                ax.set_ylim(-initial_ylim, initial_ylim)
                ax.grid(True, axis="y")
                if not overlay and i < num_channels - 1:
                    ax.tick_params(labelbottom=False)
                else:
                    ax.set_xlabel("Samples")
                if not overlay:
                    ax.set_ylabel(self._channel_axis_label(self.engine.selected_channels[i]))
                self.axes.append(ax)

            channel = self.engine.selected_channels[i]
            if overlay and channel in self.right_channels:
                if self.right_axis is None:
                    self.right_axis = ax.twinx()
                    self.right_axis.set_ylim(-initial_ylim, initial_ylim)
                ax = self.right_axis
            line, = ax.plot(
                self.plotdata[:, i], label=self._channel_axis_label(channel),
                color=self.channel_color(channel)
            )
            self.lines.append(line)

        if overlay:
            self.axes[0].set_ylabel(f"±{self.current_ylim:.3g}")
            if self.right_axis is not None:
                self.right_axis.set_ylabel(f"±{self.right_ylim:.3g}")
            self.axes[0].legend(self.lines, [line.get_label() for line in self.lines], loc="upper right")

        self.update_y_scales()
        self.update_trigger_cursor()
        self.fig.tight_layout(pad=0.8)
        self.canvas.draw_idle()

    def _channel_axis_label(self, channel):
        try:
            channel_index = self.engine.selected_channels.index(channel)
            units = self.engine.channel_units[channel_index]
        except (ValueError, IndexError):
            units = self.engine.units
        if units:
            return f"Ch {channel} ({units})"
        return f"Ch {channel}"

    def _axis_units_label(self, indices):
        units = {
            self.engine.channel_units[index]
            for index in indices
            if index < len(self.engine.channel_units) and self.engine.channel_units[index]
        }
        if len(units) == 1:
            return f"Amplitude ({next(iter(units))})"
        if len(units) > 1:
            return "Amplitude (mixed units)"
        return "Amplitude"

    def on_layout_selected(self):
        if self.plotdata is not None and self.engine.selected_channels:
            self.build_plot(len(self.engine.selected_channels), len(self.plotdata), self.current_ylim)

    def start_stream(self):
        if self.engine.running:
            return

        try:
            device = self.get_selected_device()
            channels = self.parse_selected_channels()
            right_channels = self.parse_right_channels(channels)
            window_ms = float(self.window_var.get())
            interval_ms = int(float(self.interval_var.get()))
            downsample = int(self.downsample_var.get())
            initial_ylim = float(self.yrange_var.get())
            trigger_settings = self._trigger_settings(channels)

            self.engine.configure(
                device_info=device,
                selected_channels=channels,
                window_ms=window_ms,
                downsample=downsample,
                acceleration_unit=self.acceleration_unit_var.get(),
                channel_sensors={
                    channel: self.sensor_library[sensor_id]
                    for channel in channels
                    if (sensor_id := self.sensor_assignments.get(
                        self._sensor_assignment_key(device, channel)
                    )) in self.sensor_library
                },
                trigger_settings=trigger_settings,
            )

            plot_length = self.engine.get_plot_length()
            self.right_channels = right_channels
            self.plotdata = np.zeros((plot_length, len(channels)))
            self.peak_abs = np.zeros(len(channels), dtype=np.float32)

            self.build_plot(
                num_channels=len(channels),
                plot_length=plot_length,
                initial_ylim=initial_ylim,
            )

            self.engine.start()
            self.active_device_key = self._device_key(device)
            self.stream_to_disk_check.configure(state="normal")

            self.display_paused = False
            self.pause_btn.config(text="Pause Display", state="normal")
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.host_combo.config(state="disabled")
            self.device_combo.config(state="disabled")
            self.acceleration_unit_combo.config(state="disabled")
            self.trigger_enable.config(state="disabled")
            self._set_trigger_controls_state(False)
            self.channels_entry.config(state="disabled")
            self.right_channels_entry.config(state="disabled")
            self.move_up_btn.config(state="disabled")
            self.move_down_btn.config(state="disabled")
            self.status_var.set(
                f"Running @ {self.engine.sample_rate:.0f} Hz, {len(channels)} channel(s)"
            )

        except Exception as e:
            messagebox.showerror("Start Error", str(e))
            self.status_var.set("Start failed")
            self.engine.stop()

    def stop_stream(self, status="Stopped"):
        self.engine.stop()
        stream_error = self._stop_disk_stream()
        self.stream_to_disk_var.set(False)
        self.stream_to_disk_check.configure(state="disabled")
        self._set_disk_file_controls_state(True)
        if stream_error:
            messagebox.showerror("Disk Recording", stream_error, parent=self.root)
        for record in self.engine.read_triggered_records():
            self.show_triggered_record(record)
        self.active_device_key = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.pause_btn.config(state="disabled", text="Pause Display")
        self.host_combo.config(state="readonly")
        self.device_combo.config(state="readonly")
        self._update_acceleration_unit_control()
        self.trigger_enable.config(state="normal")
        self._set_trigger_controls_state(self.trigger_enabled_var.get())
        self.update_trigger_cursor()
        self.channels_entry.config(state="normal")
        self.right_channels_entry.config(state="normal")
        self.order_combo.config(state="readonly")
        self.move_up_btn.config(state="normal")
        self.move_down_btn.config(state="normal")
        self.status_var.set(status)

    def toggle_pause(self):
        self.display_paused = not self.display_paused
        self.pause_btn.config(text="Resume Display" if self.display_paused else "Pause Display")
        self.status_var.set("Display paused" if self.display_paused else "Running")

    def on_scale_mode_changed(self):
        if self.plotdata is not None and self.axes:
            if self.scale_mode_var.get() == "Peak hold":
                for axis, indices in self._scale_groups():
                    if indices:
                        current_ylim = max(abs(axis.get_ylim()[0]), abs(axis.get_ylim()[1]))
                        self.peak_abs[indices] = current_ylim / 1.2
            else:
                self.peak_abs.fill(0)
            self.update_y_scales()
            self.canvas.draw_idle()

    def choose_stream_directory(self):
        current_directory = Path(self.stream_directory_var.get()).expanduser()
        if not current_directory.is_dir():
            current_directory = Path.home()
        selected_directory = filedialog.askdirectory(
            parent=self.root,
            title="Choose stream output folder",
            initialdir=str(current_directory),
            mustexist=True,
        )
        if selected_directory:
            self.stream_directory_var.set(selected_directory)

    def open_streamed_data_viewer(self):
        viewer_path = Path(__file__).resolve().with_name("streamedDataViewer.py")
        try:
            path = self.last_streamed_file
            if not path or not Path(path).is_file():
                path = None
            launch_or_activate(
                "streamed-data-viewer",
                viewer_path,
                {"path": path} if path else {},
            )
        except OSError as error:
            messagebox.showerror("Data Viewer", str(error), parent=self.root)

    def _stream_output_paths(self):
        directory = Path(self.stream_directory_var.get()).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        base_name = re.sub(r'[<>:"/\\|?*]', "_", self.stream_name_var.get()).strip(" .")
        for extension in (".csv", ".mat"):
            if base_name.lower().endswith(extension):
                base_name = base_name[:-len(extension)].rstrip(" .")
        if not base_name:
            base_name = "Streamed Data"
        formats = {
            "CSV": (".csv",),
            "MAT-file (.mat)": (".mat",),
            "CSV + MAT-file": (".csv", ".mat"),
        }
        extensions = formats.get(self.stream_format_var.get(), (".csv",))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"{base_name}_{timestamp}"
        suffix = 1
        while any((directory / f"{stem}{extension}").exists() for extension in extensions):
            stem = f"{base_name}_{timestamp}_{suffix:03d}"
            suffix += 1
        return {extension: directory / f"{stem}{extension}" for extension in extensions}

    def _set_disk_file_controls_state(self, enabled):
        state = "normal" if enabled else "disabled"
        self.stream_name_entry.configure(state=state)
        self.stream_browse_button.configure(state=state)
        self.stream_format_combo.configure(state="readonly" if enabled else "disabled")

    def _start_disk_stream(self):
        if self.stream_writer is not None:
            return
        paths = self._stream_output_paths()
        labels = [self._channel_axis_label(channel) for channel in self.engine.selected_channels]
        sample_rate = self.engine.sample_rate
        writers = []
        try:
            if ".csv" in paths:
                writers.append(CsvStreamWriter(paths[".csv"], sample_rate, labels))
            if ".mat" in paths:
                channel_units = list(self.engine.channel_units)
                writers.append(
                    MatStreamWriter(
                        paths[".mat"], sample_rate, self.engine.selected_channels,
                        labels, channel_units,
                    )
                )
        except Exception:
            for writer in writers:
                writer.close()
            raise
        writer = CompositeStreamWriter(writers)
        self.engine.set_stream_writer(writer)
        self.stream_writer = writer
        self.stream_file_status_var.set(
            "Recording to " + " and ".join(str(path) for path in writer.paths)
        )
        self._set_disk_file_controls_state(False)

    def _stop_disk_stream(self):
        writer = self.engine.set_stream_writer(None)
        self.stream_writer = None
        if writer is None:
            self.stream_file_status_var.set("Not recording")
            return None
        try:
            writer.close()
        except OSError as error:
            self.stream_file_status_var.set(f"Recording error: {error}")
            return str(error)
        self.stream_file_status_var.set(
            f"Saved {writer.samples_written} samples to "
            + " and ".join(str(path) for path in writer.paths)
        )
        preferred_path = next(
            (path for path in writer.paths if path.suffix.casefold() == ".csv"),
            writer.paths[0] if writer.paths else None,
        )
        self.last_streamed_file = str(preferred_path) if preferred_path is not None else None
        self.window_placement.saved["last_streamed_file"] = self.last_streamed_file
        return None

    def on_disk_stream_toggle(self):
        if self.stream_to_disk_var.get():
            try:
                self._start_disk_stream()
            except (ImportError, OSError, ValueError) as error:
                self.stream_to_disk_var.set(False)
                self._set_disk_file_controls_state(True)
                messagebox.showerror("Disk Recording", str(error), parent=self.root)
        else:
            error = self._stop_disk_stream()
            self._set_disk_file_controls_state(True)
            if error:
                    messagebox.showerror("Disk Recording", error, parent=self.root)

    def _scale_groups(self):
        right_indices = [
            index for index, channel in enumerate(self.engine.selected_channels)
            if channel in self.right_channels
        ]
        left_indices = [
            index for index, channel in enumerate(self.engine.selected_channels)
            if channel not in self.right_channels
        ]
        if self.layout_var.get() == "overlay":
            groups = [(self.axes[0], left_indices)]
            if self.right_axis is not None:
                groups.append((self.right_axis, right_indices))
            return groups
        return [(axis, [index]) for index, axis in enumerate(self.axes)]

    def update_plot(self):
        if not self.engine.running or self.display_paused or self.plotdata is None:
            return

        if self.stream_writer is not None and self.stream_writer.error is not None:
            error = self._stop_disk_stream()
            self.stream_to_disk_var.set(False)
            self._set_disk_file_controls_state(True)
            messagebox.showerror(
                "Disk Recording",
                error or "The CSV writer stopped unexpectedly.",
                parent=self.root,
            )

        blocks = self.engine.read_available_blocks()
        if not blocks:
            return

        updated = False

        for data in blocks:
            if data.size == 0:
                continue

            self.peak_abs = np.maximum(self.peak_abs, np.max(np.abs(data), axis=0))

            shift = len(data)
            if shift > len(self.plotdata):
                data = data[-len(self.plotdata):, :]
                shift = len(data)

            self.plotdata = np.roll(self.plotdata, -shift, axis=0)
            self.plotdata[-shift:, :] = data
            updated = True

        for record in self.engine.read_triggered_records():
            self.show_triggered_record(record)

        if not updated:
            return

        self.update_y_scales()

        for i, line in enumerate(self.lines):
            line.set_ydata(self.plotdata[:, i])

        self.canvas.draw_idle()

    def show_triggered_record(self, record):
        self.trigger_record_count += 1
        channels = record["channels"]
        units = record["channel_units"]
        data = record["data"]
        sample_rate = record["sample_rate"]
        times = np.arange(len(data)) / sample_rate
        trigger_channel = record["trigger_channel"]
        channel_index = channels.index(trigger_channel)
        unit = units[channel_index] or "units"
        title = (
            f"Trigger {self.trigger_record_count}: Ch {trigger_channel} "
            f"{record['slope'].lower()} edge at {record['trigger_level']:.5g} {unit}"
        )
        if record["offset_percent"] > 0:
            delay_seconds = len(data) * record["offset_percent"] / 100 / sample_rate
            title += f"; record starts {delay_seconds:.4g} s later"

        reuse_window = record["mode"] == "Repeat (same window)"
        window_state = self.repeat_capture_window if reuse_window else None
        if window_state is not None and not window_state["window"].winfo_exists():
            self.repeat_capture_window = None
            window_state = None
        is_new_window = window_state is None
        if window_state is None:
            window = tk.Toplevel(self.root)
            window.transient(self.root)
            toolbar = ttk.Frame(window, padding=4)
            toolbar.pack(fill="x")
            save_format_var = tk.StringVar(value=self.stream_format_var.get())
            ttk.Label(toolbar, text="Format:").pack(side="left", padx=4)
            ttk.Combobox(
                toolbar, textvariable=save_format_var, state="readonly", width=16,
                values=("CSV", "MAT-file (.mat)", "CSV + MAT-file"),
            ).pack(side="left", padx=4)
            figure = Figure(figsize=(9, max(3, 2.5 * len(channels))), dpi=100)
            canvas = FigureCanvasTkAgg(figure, master=window)
            canvas.get_tk_widget().pack(fill="both", expand=True)
            axes = []
            window_state = {
                "window": window,
                "figure": figure,
                "canvas": canvas,
                "axes": axes,
                "save_format_var": save_format_var,
            }
            ttk.Button(
                toolbar, text="Save...",
                command=lambda state=window_state: self.save_triggered_record(state),
            ).pack(side="left", padx=4)
            if reuse_window:
                self.repeat_capture_window = window_state
        else:
            window = window_state["window"]
            figure = window_state["figure"]
            canvas = window_state["canvas"]
            axes = window_state["axes"]
        window_state["record"] = record
        window_state["record_number"] = self.trigger_record_count
        window.title(title)

        for index, channel in enumerate(channels):
            if index < len(axes):
                axis = axes[index]
                axis.clear()
            else:
                axis = figure.add_subplot(
                    len(channels), 1, index + 1, sharex=axes[0] if axes else None
                )
                axes.append(axis)
            axis.plot(times, data[:, index], color=self.channel_color(channel))
            axis.set_ylabel(f"Ch {channel} ({units[index]})" if units[index] else f"Ch {channel}")
            axis.grid(True, axis="y")
            if record["trigger_index"] is not None:
                axis.axvline(times[record["trigger_index"]], color="#c23b22", linestyle="--")
            if index < len(channels) - 1:
                axis.tick_params(labelbottom=False)
        axes[-1].set_xlabel("Time (s)")
        figure.tight_layout(pad=0.8)
        if is_new_window:
            window.update_idletasks()
            x = self.root.winfo_x() + (self.root.winfo_width() - window.winfo_reqwidth()) // 2
            y = self.root.winfo_y() + (self.root.winfo_height() - window.winfo_reqheight()) // 2
            window.geometry(f"+{x}+{y}")
        canvas.draw_idle()

    def save_triggered_record(self, window_state):
        record = window_state["record"]
        window = window_state["window"]
        extensions = {
            "CSV": (".csv",),
            "MAT-file (.mat)": (".mat",),
            "CSV + MAT-file": (".csv", ".mat"),
        }.get(window_state["save_format_var"].get(), (".csv",))
        directory = Path(self.stream_directory_var.get()).expanduser()
        if not directory.is_dir():
            directory = Path.home()
        base_name = re.sub(r'[<>:"/\\|?*]', "_", self.stream_name_var.get()).strip(" .") or "Triggered Data"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filetypes = [("CSV files", "*.csv"), ("MATLAB files", "*.mat")]
        if extensions[0] == ".mat":
            filetypes.reverse()
        selected = filedialog.asksaveasfilename(
            parent=window,
            title="Save triggered record",
            initialdir=str(directory),
            initialfile=f"{base_name}_Trigger{window_state['record_number']}_{timestamp}{extensions[0]}",
            defaultextension=extensions[0],
            filetypes=filetypes,
        )
        if not selected:
            return
        selected = Path(selected)
        stem = selected.with_suffix("") if selected.suffix.casefold() in (".csv", ".mat") else selected
        paths = [stem.with_name(stem.name + extension) for extension in extensions]
        # The dialog only confirms overwriting the file name the user picked.
        unconfirmed = [path for path in paths if path.exists() and path != selected]
        if unconfirmed and not messagebox.askyesno(
            "Save Triggered Record",
            "Overwrite existing file(s)?\n" + "\n".join(str(path) for path in unconfirmed),
            parent=window,
        ):
            return
        channels = record["channels"]
        units = record["channel_units"]
        labels = [f"Ch {channel} ({unit})" if unit else f"Ch {channel}" for channel, unit in zip(channels, units)]
        try:
            save_samples(paths, record["sample_rate"], channels, labels, units, record["data"])
        except (ImportError, OSError, ValueError) as error:
            messagebox.showerror("Save Triggered Record", str(error), parent=window)
            return
        self.last_streamed_file = str(paths[0])
        self.window_placement.saved["last_streamed_file"] = self.last_streamed_file
        self.status_var.set("Saved triggered record to " + " and ".join(str(path) for path in paths))

    def update_y_scales(self):
        initial_ylim = float(self.yrange_var.get())
        overlay = self.layout_var.get() == "overlay"
        right_indices = [
            index for index, channel in enumerate(self.engine.selected_channels)
            if channel in self.right_channels
        ]
        left_indices = [
            index for index, channel in enumerate(self.engine.selected_channels)
            if channel not in self.right_channels
        ]
        groups = self._scale_groups()

        for axis, indices in groups:
            if not indices:
                continue
            current_ylim = max(abs(axis.get_ylim()[0]), abs(axis.get_ylim()[1]))
            if self.scale_mode_var.get() == "Peak hold":
                ymax = float(np.max(self.peak_abs[indices]))
                target_ylim = max(ymax * 1.2, initial_ylim, current_ylim)
            else:
                ymax = float(np.max(np.abs(self.plotdata[:, indices])))
                target_ylim = max(ymax * 1.2, initial_ylim)
            if abs(target_ylim - current_ylim) / max(current_ylim, 1e-12) > 0.1:
                axis.set_ylim(-target_ylim, target_ylim)

        self.current_ylim = max(abs(self.axes[0].get_ylim()[0]), abs(self.axes[0].get_ylim()[1]))
        self.yrange_display_var.set(f"±{self.current_ylim:.4g}")
        if self.right_axis is not None:
            self.right_ylim = max(abs(self.right_axis.get_ylim()[0]), abs(self.right_axis.get_ylim()[1]))
            self.right_yrange_display_var.set(f"±{self.right_ylim:.4g}")

        for i, channel in enumerate(self.engine.selected_channels):
            if not overlay:
                channel_ylim = max(abs(self.axes[i].get_ylim()[0]), abs(self.axes[i].get_ylim()[1]))
                self.axes[i].set_ylabel(
                    f"{self._channel_axis_label(channel)}\n±{channel_ylim:.3g}"
                )

        if overlay:
            left_label = self._axis_units_label(left_indices)
            self.axes[0].set_ylabel(f"{left_label}\n±{self.current_ylim:.3g}")
            if self.right_axis is not None:
                right_label = self._axis_units_label(right_indices)
                self.right_axis.set_ylabel(f"{right_label}\n±{self.right_ylim:.3g}")

    def _schedule_plot_update(self):
        try:
            self.update_plot()
        finally:
            try:
                interval_ms = int(float(self.interval_var.get()))
            except Exception:
                interval_ms = 30
            self.root.after(interval_ms, self._schedule_plot_update)

    def on_close(self):
        self._closing = True
        if self._device_poll_id is not None:
            self.root.after_cancel(self._device_poll_id)
        self.engine.stop()
        self._stop_disk_stream()
        self.window_placement.saved["stream_directory"] = self.stream_directory_var.get()
        self.window_placement.saved["stream_name"] = self.stream_name_var.get()
        self.window_placement.saved["stream_format"] = self.stream_format_var.get()
        self.window_placement.saved["last_streamed_file"] = self.last_streamed_file
        try:
            selected_device = self.get_selected_device()
        except ValueError:
            pass
        else:
            self.window_placement.saved["selected_host"] = selected_device.get("hostapi")
            self.window_placement.saved["selected_device_key"] = list(
                self._device_key(selected_device)
            )
        self.window_placement.save()
        self.root.destroy()


def main():
    root = tk.Tk()
    root.withdraw()
    app_reference = {}

    def activate_existing(message):
        if not app_reference:
            return
        if root.state() == "iconic":
            root.state("normal")
        root.deiconify()
        root.lift()
        root.focus_force()

    broker = InstanceBroker(root, "strip-chart", activate_existing)
    if not broker.is_primary:
        root.destroy()
        return
    try:
        window_placement = WindowPlacement(root)
        app = LiveAudioApp(root, window_placement)
        app_reference["app"] = app
        window_placement.restore()
        root.deiconify()
        root.mainloop()
    finally:
        broker.close()


if __name__ == "__main__":
    main()