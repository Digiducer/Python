"""Shared channel palette and per-user color overrides."""

import json
import os
import re
import sys
import tkinter as tk
from pathlib import Path

from matplotlib.colors import TABLEAU_COLORS


DEFAULT_CHANNEL_COLORS = ("#005eb8", "#58585b", "#c47a14", "#008c83")
_FALLBACK_COLORS = tuple(TABLEAU_COLORS.values())


def color_settings_path():
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "digital-sensor-app" / "channel_colors.json"


def channel_color(channel, overrides):
    if channel in overrides:
        return overrides[channel]
    if 1 <= channel <= len(DEFAULT_CHANNEL_COLORS):
        return DEFAULT_CHANNEL_COLORS[channel - 1]
    return _FALLBACK_COLORS[(channel - 1) % len(_FALLBACK_COLORS)]


def channel_number(label, index):
    match = re.match(r"Ch\s+(\d+)\b", label, re.IGNORECASE)
    return int(match.group(1)) if match else index + 1


def load_channel_colors(root):
    path = color_settings_path()
    if not path.is_file():
        path = path.with_name("window_state.json")
        try:
            with path.open(encoding="utf-8") as state_file:
                saved = json.load(state_file).get("channel_colors", {})
        except (OSError, ValueError, AttributeError):
            saved = {}
    else:
        try:
            with path.open(encoding="utf-8") as settings_file:
                saved = json.load(settings_file)
        except (OSError, ValueError):
            saved = {}

    colors = {}
    if isinstance(saved, dict):
        for number, color in saved.items():
            try:
                channel = int(number)
                if channel < 1 or not isinstance(color, str):
                    continue
                root.winfo_rgb(color)
            except (TypeError, ValueError, tk.TclError):
                continue
            colors[channel] = color
    return colors


def save_channel_colors(colors):
    path = color_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as settings_file:
        json.dump({str(channel): color for channel, color in colors.items()}, settings_file)