"""Sample-level threshold triggering and record assembly."""

import threading

import numpy as np


class TriggerCapture:
    def __init__(
        self,
        selected_channels,
        trigger_channel,
        level,
        slope,
        record_samples,
        offset_percent,
        mode,
    ):
        if trigger_channel not in selected_channels:
            raise ValueError("Trigger channel must be selected for acquisition")
        if slope not in ("Positive", "Negative"):
            raise ValueError("Trigger slope must be Positive or Negative")
        if mode not in ("One-shot", "Repeat (same window)", "Repeat (new window)"):
            raise ValueError("Invalid trigger mode")
        if record_samples < 1:
            raise ValueError("Trigger record must contain at least one sample")
        if not -100 <= offset_percent <= 100:
            raise ValueError("Trigger offset must be between -100% and 100%")

        self.trigger_channel = trigger_channel
        self.channel_index = selected_channels.index(trigger_channel)
        self.level = float(level)
        self.slope = slope
        self.record_samples = record_samples
        self.offset_percent = float(offset_percent)
        self.mode = mode
        offset_samples = round(record_samples * abs(offset_percent) / 100)
        self.pre_samples = min(record_samples - 1, offset_samples) if offset_percent < 0 else 0
        self.delay_samples = offset_samples if offset_percent > 0 else 0

        self.history = None
        self.samples_seen = 0
        self.previous_value = None
        self.record = None
        self.armed = True
        self.lock = threading.Lock()

    def set_level(self, level):
        with self.lock:
            self.level = float(level)

    def feed(self, data):
        data = np.asarray(data, dtype=np.float32)
        if data.ndim != 2 or not len(data):
            return []
        if self.history is None:
            self.history = np.empty((0, data.shape[1]), dtype=np.float32)

        completed_records = []
        with self.lock:
            position = 0
            while position < len(data):
                if self.record is not None:
                    position = self._fill_record(data, position)
                    if self._record_full():
                        completed_records.append(self._finish_record())
                    continue
                if not self.armed:
                    break
                index = self._find_crossing(data, position)
                if index is None:
                    break
                self._begin_record(data, index)
                position = index + 1
                if self.mode == "One-shot":
                    self.armed = False
                if self._record_full():
                    completed_records.append(self._finish_record())

            self.previous_value = float(data[-1, self.channel_index])
            if self.pre_samples:
                self.history = np.concatenate((self.history, data))[-self.pre_samples:]
            self.samples_seen += len(data)
        return completed_records

    def _find_crossing(self, data, position):
        values = data[position:, self.channel_index]
        if position:
            previous = data[position - 1:-1, self.channel_index]
        elif self.previous_value is None:
            values = values[1:]
            previous = data[:-1, self.channel_index]
            position = 1
        else:
            previous = np.concatenate(([self.previous_value], data[:-1, self.channel_index]))
        if self.slope == "Positive":
            crossed = (previous < self.level) & (self.level <= values)
        else:
            crossed = (previous > self.level) & (self.level >= values)
        # Pre-trigger samples must already be available in the history.
        first_ready = max(0, self.pre_samples - self.samples_seen - position)
        crossed[:first_ready] = False
        hits = np.flatnonzero(crossed)
        return position + int(hits[0]) if len(hits) else None

    def _begin_record(self, data, index):
        buffer = np.empty((self.record_samples, data.shape[1]), dtype=np.float32)
        filled = 0
        if not self.delay_samples:
            if self.pre_samples:
                preceding = np.concatenate((self.history, data[:index]))[-self.pre_samples:]
                buffer[:self.pre_samples] = preceding
            buffer[self.pre_samples] = data[index]
            filled = self.pre_samples + 1
        self.record = {
            "buffer": buffer,
            "filled": filled,
            "skip": max(0, self.delay_samples - 1),
            "trigger_channel": self.trigger_channel,
            "trigger_level": self.level,
            "slope": self.slope,
            "mode": self.mode,
            "offset_percent": self.offset_percent,
            "trigger_index": self.pre_samples if self.offset_percent <= 0 else None,
        }

    def _fill_record(self, data, position):
        record = self.record
        skipped = min(record["skip"], len(data) - position)
        record["skip"] -= skipped
        position += skipped
        count = min(self.record_samples - record["filled"], len(data) - position)
        record["buffer"][record["filled"]:record["filled"] + count] = data[position:position + count]
        record["filled"] += count
        return position + count

    def _record_full(self):
        return self.record is not None and self.record["filled"] >= self.record_samples

    def _finish_record(self):
        completed = dict(self.record)
        completed["data"] = completed.pop("buffer")
        del completed["filled"], completed["skip"]
        self.record = None
        return completed
