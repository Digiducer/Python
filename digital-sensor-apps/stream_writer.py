"""Asynchronous CSV writer for live selected-channel samples."""

import csv
import os
from pathlib import Path
import queue
import tempfile
import threading

import numpy as np


def _mat_variables(samples, sample_rate, channel_numbers, channel_labels, channel_units):
    return {
        "data": samples,
        "sample_rate": np.array([[sample_rate]], dtype=np.float64),
        "sample_interval": np.array([[1.0 / sample_rate]], dtype=np.float64),
        "num_samples": np.array([[len(samples)]], dtype=np.uint64),
        "channel_numbers": np.asarray(channel_numbers, dtype=np.int32).reshape(1, -1),
        "channel_labels": np.asarray(channel_labels, dtype=object).reshape(1, -1),
        "engineering_units": np.asarray(channel_units, dtype=object).reshape(1, -1),
        "data_description": "data contains engineering-unit float32 samples",
    }


def save_samples(paths, sample_rate, channel_numbers, channel_labels, channel_units, data):
    """Write a complete samples-by-channels block to each .csv/.mat path."""
    data = np.asarray(data, dtype=np.float32)
    sample_rate = float(sample_rate)
    for path in paths:
        path = Path(path)
        if path.suffix.casefold() == ".mat":
            from scipy.io import savemat

            savemat(
                str(path),
                _mat_variables(data, sample_rate, channel_numbers, channel_labels, channel_units),
                format="5",
                do_compression=False,
                oned_as="row",
            )
        else:
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["Time (s)", *channel_labels])
                for index, row in enumerate(data.tolist()):
                    writer.writerow([index / sample_rate, *row])


class CsvStreamWriter:
    def __init__(self, path, sample_rate, channel_labels):
        self.path = Path(path)
        self.sample_rate = float(sample_rate)
        self.queue = queue.Queue()
        self.error = None
        self.samples_written = 0
        self.file = self.path.open("x", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        self.writer.writerow(["Time (s)", *channel_labels])
        self.file.flush()
        self.thread = threading.Thread(target=self._write_loop, daemon=True)
        self.thread.start()

    def write(self, data):
        if self.error is not None:
            return
        self.queue.put_nowait(np.array(data, dtype=np.float32, copy=True))

    def close(self):
        self.queue.put_nowait(None)
        self.thread.join()
        if self.error is not None:
            raise OSError(f"Could not finish CSV recording: {self.error}") from self.error

    def _write_loop(self):
        try:
            while True:
                block = self.queue.get()
                if block is None:
                    break
                for row in block:
                    time_seconds = self.samples_written / self.sample_rate
                    self.writer.writerow([time_seconds, *row.tolist()])
                    self.samples_written += 1
                self.file.flush()
        except Exception as error:
            self.error = error
        finally:
            self.file.close()


class MatStreamWriter:
    def __init__(self, path, sample_rate, channel_numbers, channel_labels, channel_units):
        from scipy.io import savemat

        self.path = Path(path)
        self.savemat = savemat
        self.sample_rate = float(sample_rate)
        self.channel_numbers = list(channel_numbers)
        self.channel_labels = list(channel_labels)
        self.channel_units = list(channel_units)
        self.queue = queue.Queue()
        self.error = None
        self.samples_written = 0
        self.final_path_created = False
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{self.path.stem}.", suffix=".streamtmp", dir=self.path.parent
        )
        os.close(descriptor)
        self.temporary_path = Path(temporary_path)
        self.file = self.temporary_path.open("wb")
        self.thread = threading.Thread(target=self._write_loop, daemon=True)
        self.thread.start()

    def write(self, data):
        if self.error is None:
            self.queue.put_nowait(np.array(data, dtype=np.float32, copy=True))

    def close(self):
        self.queue.put_nowait(None)
        self.thread.join()
        if self.error is not None:
            self.temporary_path.unlink(missing_ok=True)
            raise OSError(f"Could not buffer MAT recording: {self.error}") from self.error
        try:
            self._save_mat_file()
        except Exception as error:
            self.error = error
            if self.final_path_created:
                self.path.unlink(missing_ok=True)
            raise OSError(f"Could not create MAT recording: {error}") from error
        finally:
            self.temporary_path.unlink(missing_ok=True)

    def _write_loop(self):
        try:
            while True:
                block = self.queue.get()
                if block is None:
                    break
                block.tofile(self.file)
                self.samples_written += len(block)
                self.file.flush()
        except Exception as error:
            self.error = error
        finally:
            self.file.close()

    def _save_mat_file(self):
        channel_count = len(self.channel_numbers)
        samples = None
        if self.samples_written:
            samples = np.memmap(
                self.temporary_path,
                dtype=np.float32,
                mode="r",
                shape=(self.samples_written, channel_count),
            )
        else:
            samples = np.empty((0, channel_count), dtype=np.float32)

        mat_data = _mat_variables(
            samples, self.sample_rate, self.channel_numbers, self.channel_labels, self.channel_units
        )
        try:
            with self.path.open("xb") as mat_file:
                self.final_path_created = True
                self.savemat(
                    mat_file,
                    mat_data,
                    format="5",
                    do_compression=False,
                    oned_as="row",
                )
        finally:
            if isinstance(samples, np.memmap):
                samples._mmap.close()
                del samples
            else:
                del samples


class CompositeStreamWriter:
    def __init__(self, writers):
        self.writers = list(writers)

    @property
    def error(self):
        return next((writer.error for writer in self.writers if writer.error is not None), None)

    @property
    def paths(self):
        return [writer.path for writer in self.writers]

    @property
    def samples_written(self):
        return max((writer.samples_written for writer in self.writers), default=0)

    def write(self, data):
        for writer in self.writers:
            writer.write(data)

    def close(self):
        errors = []
        for writer in self.writers:
            try:
                writer.close()
            except OSError as error:
                errors.append(str(error))
        if errors:
            raise OSError("; ".join(errors))