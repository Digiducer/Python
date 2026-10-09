#!/usr/bin/env python3
"""Data Viewer: browse CSV and MATLAB files written by the Data Recorder."""

import csv
import argparse
import re
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from scipy.io import loadmat
from tkinterdnd2 import DND_FILES, TkinterDnD
from app_instances import InstanceBroker, launch_or_activate
from channel_colors import channel_color, channel_number, load_channel_colors, save_channel_colors
from stripChartDisplay import WindowPlacement


def _mat_strings(value):
    if value is None:
        return []
    strings = []
    for item in np.asarray(value, dtype=object).reshape(-1):
        while isinstance(item, np.ndarray) and item.size == 1:
            item = item.item()
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        elif isinstance(item, np.ndarray):
            item = "".join(str(character) for character in item.reshape(-1))
        strings.append(str(item))
    return strings


def load_stream_file(path):
    """Return times, sample matrix, channel labels, and units from a stream file."""
    path = Path(path)
    if path.suffix.casefold() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.reader(stream))
        if len(rows) < 2:
            raise ValueError("The CSV file contains no streamed samples")
        header = rows[0]
        try:
            values = np.asarray(
                [[float(value) for value in row] for row in rows[1:] if row],
                dtype=np.float64,
            )
        except ValueError as error:
            raise ValueError("The CSV file contains a non-numeric sample") from error
        if values.ndim != 2 or values.shape[1] < 2:
            raise ValueError("Expected a time column and at least one channel")
        times = values[:, 0]
        data = values[:, 1:]
        labels = header[1:1 + data.shape[1]]
    elif path.suffix.casefold() == ".mat":
        mat = loadmat(path)
        if "data" not in mat or "sample_rate" not in mat:
            raise ValueError("MAT file must contain data and sample_rate variables")
        data = np.asarray(mat["data"])
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if data.ndim != 2:
            raise ValueError("MAT data must be a samples-by-channels matrix")
        sample_rate = float(np.asarray(mat["sample_rate"]).reshape(-1)[0])
        if not np.isfinite(sample_rate) or sample_rate <= 0:
            raise ValueError("MAT sample_rate must be positive")
        times = np.arange(len(data), dtype=np.float64) / sample_rate
        labels = _mat_strings(mat.get("channel_labels"))
        units = _mat_strings(mat.get("engineering_units"))
        channel_numbers = np.asarray(mat.get("channel_numbers", [])).reshape(-1)
        if len(labels) != data.shape[1]:
            labels = [f"Ch {int(channel_numbers[index])}" if index < len(channel_numbers)
                      else f"Ch {index + 1}" for index in range(data.shape[1])]
    else:
        raise ValueError("Choose a streamed CSV or MATLAB .mat file")

    if data.shape[0] == 0:
        raise ValueError("The stream file contains no samples")
    if not np.all(np.isfinite(times)) or (len(times) > 1 and np.any(np.diff(times) <= 0)):
        raise ValueError("Sample times must be finite and strictly increasing")
    if len(labels) != data.shape[1]:
        labels = [f"Ch {index + 1}" for index in range(data.shape[1])]
    if path.suffix.casefold() == ".csv":
        units = []
        for label in labels:
            match = re.search(r"\(([^()]*)\)\s*$", label)
            units.append(match.group(1) if match else "")
    if len(units) != data.shape[1]:
        units = [""] * data.shape[1]
    return times, data, labels, units


class StreamedDataViewer:
    def __init__(self, root):
        self.root = root
        self.root.title("Data Viewer")
        self.root.minsize(850, 620)
        self.window_placement = WindowPlacement(root, "viewer_window_state.json")
        self.channel_colors = load_channel_colors(root)
        asset_dir = Path(__file__).resolve().parent / "assets"
        self.window_icon = tk.PhotoImage(file=str(asset_dir / "tms-round-mark.png"))
        self.root.iconphoto(True, self.window_icon)
        self.root.iconbitmap(str(asset_dir / "modal-shop-favicon.ico"))
        self.full_logo = tk.PhotoImage(file=str(asset_dir / "tms-logo-blue.png")).subsample(5, 5)

        self.times = None
        self.data = None
        self.channel_labels = []
        self.channel_units = []
        self.view_start = 0.0
        self.view_end = 1.0
        self.overview_axes = []
        self.detail_axes = []
        self.overview_region = []
        self.overview_region_edges = []
        self.overview_drag_mode = None
        self.overview_drag_axis = None
        self.overview_drag_anchor = None
        self.overview_drag_range = None
        self.detail_drag = None

        self.file_var = tk.StringVar(value="No file loaded")
        self.status_var = tk.StringVar(value="Open a streamed CSV or MAT file")
        self.overview_layout_var = tk.StringVar(value="Overlay")
        self.detail_layout_var = tk.StringVar(value="Overlay")
        self.color_channel_var = tk.StringVar(value="1")
        self.range_var = tk.StringVar(value="")

        self._build_ui()
        self._register_drop_targets(self.root)
        self.window_placement.restore()
        self.root.bind("<FocusIn>", self.refresh_channel_colors, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Left>", lambda event: self.scroll_view(-1))
        self.root.bind("<Right>", lambda event: self.scroll_view(1))
        self.root.bind("<plus>", lambda event: self.zoom_view(0.5))
        self.root.bind("<minus>", lambda event: self.zoom_view(2.0))

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        file_bar = ttk.Frame(main)
        file_bar.pack(fill="x", pady=(0, 6))
        ttk.Button(file_bar, text="Open Stream...", command=self.open_file).pack(side="left")
        ttk.Button(file_bar, text="Open Data Recorder", command=self.open_strip_chart).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(file_bar, textvariable=self.file_var).pack(side="left", padx=10)

        self.plot_panes = ttk.PanedWindow(main, orient="vertical")
        self.plot_panes.pack(fill="both", expand=True)
        overview_pane = ttk.Frame(self.plot_panes)
        detail_pane = ttk.Frame(self.plot_panes)
        self.plot_panes.add(overview_pane, weight=1)
        self.plot_panes.add(detail_pane, weight=2)
        self.splitter_moved = False
        self.plot_panes.bind("<Configure>", self._apply_default_split)
        self.plot_panes.bind("<ButtonPress-1>", self._on_splitter_press)

        navigation = ttk.LabelFrame(overview_pane, text="Overview and Navigation", padding=(7, 4))
        navigation.pack(fill="x")
        ttk.Label(navigation, text="Layout:").pack(side="left", padx=(2, 4))
        self.overview_layout = ttk.Combobox(
            navigation, textvariable=self.overview_layout_var,
            values=("Overlay", "Separate"), state="readonly", width=10,
        )
        self.overview_layout.pack(side="left", padx=(0, 8))
        self.overview_layout.bind("<<ComboboxSelected>>", lambda event: self.redraw_overview())
        ttk.Button(navigation, text="Zoom in", command=lambda: self.zoom_view(0.5)).pack(side="left", padx=2)
        ttk.Button(navigation, text="Zoom out", command=lambda: self.zoom_view(2.0)).pack(side="left", padx=2)
        ttk.Button(navigation, text="Scroll left", command=lambda: self.scroll_view(-1)).pack(side="left", padx=2)
        ttk.Button(navigation, text="Scroll right", command=lambda: self.scroll_view(1)).pack(side="left", padx=2)
        ttk.Button(navigation, text="Full record", command=self.reset_view).pack(side="left", padx=2)
        ttk.Label(navigation, textvariable=self.range_var).pack(side="right", padx=6)

        self.overview_figure = Figure(figsize=(10, 3), dpi=100)
        self.overview_canvas = FigureCanvasTkAgg(self.overview_figure, master=overview_pane)
        self.overview_canvas.get_tk_widget().configure(height=1)
        self.overview_canvas.get_tk_widget().pack(fill="both", expand=True, pady=(2, 6))
        self.overview_canvas.mpl_connect("button_press_event", self._on_overview_press)
        self.overview_canvas.mpl_connect("motion_notify_event", self._on_overview_motion)
        self.overview_canvas.mpl_connect("button_release_event", self._on_overview_release)

        detail_bar = ttk.Frame(detail_pane)
        detail_bar.pack(fill="x", pady=(2, 3))
        ttk.Label(detail_bar, text="Detail View").pack(side="left", padx=(2, 8))
        ttk.Label(detail_bar, text="Layout:").pack(side="left", padx=(2, 4))
        self.detail_layout = ttk.Combobox(
            detail_bar, textvariable=self.detail_layout_var,
            values=("Overlay", "Separate"), state="readonly", width=10,
        )
        self.detail_layout.pack(side="left")
        self.detail_layout.bind("<<ComboboxSelected>>", lambda event: self.redraw_detail())
        ttk.Label(detail_bar, text="Channel:").pack(side="left", padx=(12, 4))
        self.color_combo = ttk.Combobox(
            detail_bar, textvariable=self.color_channel_var,
            values=("1", "2", "3", "4"), state="readonly", width=5,
        )
        self.color_combo.pack(side="left")
        self.color_combo.bind("<<ComboboxSelected>>", self.update_color_swatch)
        ttk.Label(detail_bar, text="Color:").pack(side="left", padx=(8, 4))
        self.color_swatch = tk.Button(detail_bar, width=3, command=self.choose_channel_color)
        self.color_swatch.pack(side="left")
        self.update_color_swatch()

        self.detail_figure = Figure(figsize=(10, 4), dpi=100)
        self.detail_canvas = FigureCanvasTkAgg(self.detail_figure, master=detail_pane)
        self.detail_canvas.get_tk_widget().configure(height=1)
        self.detail_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.detail_canvas.mpl_connect("button_press_event", self._on_detail_press)
        self.detail_canvas.mpl_connect("motion_notify_event", self._on_detail_motion)
        self.detail_canvas.mpl_connect("button_release_event", self._on_detail_release)

        ttk.Label(main, textvariable=self.status_var, anchor="w").pack(fill="x", pady=(5, 0))
        footer = ttk.Frame(main)
        footer.pack(fill="x", pady=(2, 0))
        ttk.Label(footer, image=self.full_logo).pack(side="right")
        self._draw_empty_state()

    def _apply_default_split(self, event=None):
        height = self.plot_panes.winfo_height()
        if not self.splitter_moved and height > 1:
            self.plot_panes.sashpos(0, height // 3)

    def _on_splitter_press(self, event):
        if self.plot_panes.identify(event.x, event.y) != "":
            self.splitter_moved = True

    def refresh_color_channels(self):
        numbers = list(dict.fromkeys(
            [1, 2, 3, 4] + [channel_number(label, index)
                             for index, label in enumerate(self.channel_labels)]
        ))
        options = [str(number) for number in numbers]
        selected = self.color_channel_var.get()
        self.color_combo["values"] = options
        self.color_channel_var.set(selected if selected in options else options[0])
        self.update_color_swatch()

    def update_color_swatch(self, event=None):
        color = channel_color(int(self.color_channel_var.get()), self.channel_colors)
        self.color_swatch.config(bg=color, activebackground=color)

    def refresh_channel_colors(self, event=None):
        colors = load_channel_colors(self.root)
        if colors == self.channel_colors:
            return
        self.channel_colors = colors
        self.update_color_swatch()
        self.redraw_overview()
        self.redraw_detail()

    def choose_channel_color(self):
        channel = int(self.color_channel_var.get())
        _, color = colorchooser.askcolor(
            color=channel_color(channel, self.channel_colors), parent=self.root,
        )
        if color is None:
            return
        self.channel_colors[channel] = color
        save_channel_colors(self.channel_colors)
        self.update_color_swatch()
        self.redraw_overview()
        self.redraw_detail()

    def open_strip_chart(self):
        strip_chart_path = Path(__file__).resolve().with_name("stripChartDisplay.py")
        try:
            launch_or_activate("strip-chart", strip_chart_path)
        except OSError as error:
            messagebox.showerror("Data Recorder", str(error), parent=self.root)

    def on_close(self):
        self.window_placement.save()
        self.root.destroy()

    def _register_drop_targets(self, widget):
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_file_drop)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._register_drop_targets(child)

    def _on_file_drop(self, event):
        dropped_paths = event.widget.tk.splitlist(event.data)
        stream_path = next(
            (path for path in dropped_paths if Path(path).suffix.casefold() in (".csv", ".mat")),
            None,
        )
        if stream_path is None:
            self.status_var.set("Drop a streamed CSV or MAT file")
            return "break"
        self.load_file(stream_path)
        return "break"

    def _draw_empty_state(self):
        for figure, canvas in (
            (self.overview_figure, self.overview_canvas),
            (self.detail_figure, self.detail_canvas),
        ):
            figure.clear()
            axis = figure.add_subplot(111)
            axis.text(0.5, 0.5, "Open a streamed CSV or MAT file", ha="center", va="center")
            axis.set_axis_off()
            canvas.draw_idle()

    def open_file(self):
        filename = filedialog.askopenfilename(
            parent=self.root,
            title="Open streamed data",
            filetypes=(("Streamed data", "*.csv *.mat"), ("CSV files", "*.csv"), ("MAT-files", "*.mat")),
        )
        if filename:
            self.load_file(filename)

    def load_file(self, filename):
        try:
            times, data, labels, units = load_stream_file(filename)
        except (OSError, ValueError, KeyError) as error:
            messagebox.showerror("Open Stream", str(error), parent=self.root)
            return

        self.times = times
        self.data = data
        self.channel_labels = labels
        self.channel_units = units
        self.refresh_color_channels()
        sample_interval = float(np.median(np.diff(times))) if len(times) > 1 else 0.0
        full_end = times[-1] + sample_interval if sample_interval else max(times[-1], 1.0)
        self.view_start = float(times[0])
        self.view_end = float(full_end)
        self.file_var.set(Path(filename).name)
        self.status_var.set(f"{len(data):,} samples, {data.shape[1]} channels")
        self._update_range_label()
        self.redraw_overview()
        self.redraw_detail()

    def _plot_pane(self, figure, canvas, data, times, layout, overview=False):
        figure.clear()
        axes = []
        if layout == "Separate":
            for index in range(data.shape[1]):
                axis = figure.add_subplot(
                    data.shape[1], 1, index + 1,
                    sharex=axes[0] if axes else None,
                )
                axis.plot(times, data[:, index], linewidth=0.8,
                          color=channel_color(channel_number(self.channel_labels[index], index), self.channel_colors))
                axis.set_ylabel(self._channel_axis_label(index))
                axis.grid(True, axis="y", alpha=0.35)
                if index < data.shape[1] - 1:
                    axis.tick_params(labelbottom=False)
                axes.append(axis)
            axes[-1].set_xlabel("Time (s)")
        else:
            axis = figure.add_subplot(111)
            for index in range(data.shape[1]):
                axis.plot(times, data[:, index], label=self.channel_labels[index], linewidth=0.8,
                          color=channel_color(channel_number(self.channel_labels[index], index), self.channel_colors))
            axis.set_ylabel(self._amplitude_label())
            axis.set_xlabel("Time (s)")
            axis.grid(True, axis="y", alpha=0.35)
            if data.shape[1] > 1:
                axis.legend(loc="upper right", ncol=min(data.shape[1], 4), fontsize="small")
            axes.append(axis)

        if overview:
            for axis in axes:
                region = axis.axvspan(
                    self.view_start, self.view_end, color="#c23b22", alpha=0.16
                )
                self.overview_region.append(region)
                left_edge = axis.axvline(self.view_start, color="#c23b22", linewidth=1.5)
                right_edge = axis.axvline(self.view_end, color="#c23b22", linewidth=1.5)
                self.overview_region_edges.append((left_edge, right_edge))
        figure.set_layout_engine("tight", pad=0.7)
        canvas.draw_idle()
        return axes

    def _channel_axis_label(self, index):
        unit = self.channel_units[index]
        label = self.channel_labels[index]
        if unit and not label.endswith(f"({unit})"):
            return f"{label} ({unit})"
        return label

    def _amplitude_label(self):
        units = set(unit for unit in self.channel_units if unit)
        if len(units) == 1:
            return f"Amplitude ({next(iter(units))})"
        if len(units) > 1:
            return "Amplitude (mixed units)"
        return "Amplitude"

    def _plot_sample_indices(self, data, times):
        maximum_points = 100_000
        stride = max(1, len(times) // maximum_points)
        return data[::stride], times[::stride]

    def redraw_overview(self):
        if self.data is None:
            return
        self.overview_drag_mode = None
        overview_data, overview_times = self._plot_sample_indices(self.data, self.times)
        self.overview_region = []
        self.overview_region_edges = []
        self.overview_axes = self._plot_pane(
            self.overview_figure,
            self.overview_canvas,
            overview_data,
            overview_times,
            self.overview_layout_var.get(),
            overview=True,
        )
        interval = float(np.median(np.diff(self.times))) if len(self.times) > 1 else 1.0
        full_end = float(self.times[-1] + interval)
        for axis in self.overview_axes:
            axis.set_xlim(self.times[0], full_end)
        self.overview_canvas.draw_idle()

    def redraw_detail(self):
        if self.data is None:
            return
        start_index = int(np.searchsorted(self.times, self.view_start, side="left"))
        end_index = int(np.searchsorted(self.times, self.view_end, side="right"))
        end_index = max(start_index + 1, min(end_index, len(self.times)))
        detail_data = self.data[start_index:end_index]
        detail_times = self.times[start_index:end_index]
        detail_data, detail_times = self._plot_sample_indices(detail_data, detail_times)
        self.detail_axes = self._plot_pane(
            self.detail_figure,
            self.detail_canvas,
            detail_data,
            detail_times,
            self.detail_layout_var.get(),
        )
        if len(detail_times):
            self.detail_axes[0].set_xlim(self.view_start, self.view_end)
        self.detail_canvas.draw_idle()

    def _on_overview_press(self, event):
        if self.data is None or event.button != 1 or event.inaxes not in self.overview_axes:
            return
        if event.xdata is None:
            return

        left_pixel = event.inaxes.transData.transform((self.view_start, 0))[0]
        right_pixel = event.inaxes.transData.transform((self.view_end, 0))[0]
        if abs(event.x - left_pixel) <= 8:
            self.overview_drag_mode = "left"
        elif abs(event.x - right_pixel) <= 8:
            self.overview_drag_mode = "right"
        elif self.view_start <= event.xdata <= self.view_end:
            self.overview_drag_mode = "move"
        else:
            self.overview_drag_mode = "new"
        self.overview_drag_axis = event.inaxes
        self.overview_drag_anchor = float(event.xdata)
        self.overview_drag_range = (self.view_start, self.view_end)

    def _on_overview_motion(self, event):
        if self.overview_drag_mode is None or self.overview_drag_axis is None:
            return
        x_value = self.overview_drag_axis.transData.inverted().transform((event.x, event.y))[0]
        full_start = float(self.times[0])
        interval = float(np.median(np.diff(self.times))) if len(self.times) > 1 else 1.0
        full_end = float(self.times[-1] + interval)
        original_start, original_end = self.overview_drag_range

        if self.overview_drag_mode == "left":
            self.view_start = min(max(x_value, full_start), original_end - interval)
            self.view_end = original_end
        elif self.overview_drag_mode == "right":
            self.view_start = original_start
            self.view_end = max(min(x_value, full_end), original_start + interval)
        elif self.overview_drag_mode == "move":
            span = original_end - original_start
            start = original_start + x_value - self.overview_drag_anchor
            self.view_start = max(full_start, min(start, full_end - span))
            self.view_end = self.view_start + span
        else:
            self.view_start = max(full_start, min(self.overview_drag_anchor, x_value))
            self.view_end = min(full_end, max(self.overview_drag_anchor, x_value))

        if self.view_end - self.view_start >= interval:
            self._update_overview_region()

    def _on_overview_release(self, event):
        if self.overview_drag_mode is None:
            return
        mode = self.overview_drag_mode
        original_range = self.overview_drag_range
        self.overview_drag_mode = None
        self.overview_drag_axis = None
        if mode == "new" and self.view_end - self.view_start < (
            float(np.median(np.diff(self.times))) if len(self.times) > 1 else 1.0
        ):
            self.view_start, self.view_end = original_range
        self._refresh_view()

    def _update_overview_region(self):
        for region, edges in zip(self.overview_region, self.overview_region_edges):
            region.set_x(self.view_start)
            region.set_width(self.view_end - self.view_start)
            edges[0].set_xdata([self.view_start, self.view_start])
            edges[1].set_xdata([self.view_end, self.view_end])
        self._update_range_label()
        self.overview_canvas.draw_idle()
        self.redraw_detail()

    def _on_detail_press(self, event):
        if self.data is None or event.button != 1 or event.inaxes not in self.detail_axes:
            return
        width = event.inaxes.bbox.width
        if width <= 0:
            return
        span = self.view_end - self.view_start
        self.detail_drag = (event.x, self.view_start, span / width)
        self.detail_canvas.get_tk_widget().configure(cursor="fleur")

    def _on_detail_motion(self, event):
        if self.detail_drag is None or event.x is None:
            return
        anchor_x, original_start, seconds_per_pixel = self.detail_drag
        span = self.view_end - self.view_start
        full_start = float(self.times[0])
        interval = float(np.median(np.diff(self.times))) if len(self.times) > 1 else 1.0
        full_end = float(self.times[-1] + interval)
        start = original_start - (event.x - anchor_x) * seconds_per_pixel
        start = max(full_start, min(start, full_end - span))
        if start != self.view_start:
            self.view_start = start
            self.view_end = start + span
            self._update_overview_region()

    def _on_detail_release(self, event):
        if self.detail_drag is None:
            return
        self.detail_drag = None
        self.detail_canvas.get_tk_widget().configure(cursor="")

    def zoom_view(self, factor):
        if self.data is None:
            return
        center = (self.view_start + self.view_end) / 2
        half_width = (self.view_end - self.view_start) * factor / 2
        full_start = float(self.times[0])
        interval = float(np.median(np.diff(self.times))) if len(self.times) > 1 else 1.0
        full_end = float(self.times[-1] + interval)
        span = min(full_end - full_start, half_width * 2)
        start = center - span / 2
        start = max(full_start, min(start, full_end - span))
        self.view_start = start
        self.view_end = start + span
        self._refresh_view()

    def scroll_view(self, direction):
        if self.data is None:
            return
        span = self.view_end - self.view_start
        full_start = float(self.times[0])
        interval = float(np.median(np.diff(self.times))) if len(self.times) > 1 else 1.0
        full_end = float(self.times[-1] + interval)
        start = self.view_start + direction * span * 0.5
        start = max(full_start, min(start, full_end - span))
        self.view_start = start
        self.view_end = start + span
        self._refresh_view()

    def reset_view(self):
        if self.data is None:
            return
        interval = float(np.median(np.diff(self.times))) if len(self.times) > 1 else 1.0
        self.view_start = float(self.times[0])
        self.view_end = float(self.times[-1] + interval)
        self._refresh_view()

    def _refresh_view(self):
        self._update_range_label()
        self.redraw_overview()
        self.redraw_detail()

    def _update_range_label(self):
        self.range_var.set(f"Detail: {self.view_start:.4f} to {self.view_end:.4f} s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stream_file", nargs="?", help="CSV or MAT stream file to open")
    arguments = parser.parse_args()
    root = TkinterDnD.Tk()
    root.withdraw()
    app_reference = {}

    def activate_existing(message):
        if not app_reference:
            return
        file_path = message.get("path")
        if file_path and Path(file_path).is_file():
            app_reference["app"].load_file(file_path)
        if root.state() == "iconic":
            root.state("normal")
        root.deiconify()
        root.lift()
        root.focus_force()

    broker = InstanceBroker(root, "streamed-data-viewer", activate_existing)
    if not broker.is_primary:
        root.destroy()
        return
    try:
        app = StreamedDataViewer(root)
        app_reference["app"] = app
        if arguments.stream_file:
            app.load_file(arguments.stream_file)
        root.deiconify()
        root.update_idletasks()
        app.window_placement.restore()
        root.mainloop()
    finally:
        broker.close()


if __name__ == "__main__":
    main()