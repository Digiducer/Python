"""Single-instance activation and inter-app launch helpers."""

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path


_APP_PORTS = {
    "strip-chart": 49371,
    "streamed-data-viewer": 49372,
}


def notify_instance(app_name, message=None, timeout=0.5):
    port = _APP_PORTS[app_name]
    payload = (json.dumps(message or {}) + "\n").encode("utf-8")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(payload)
            return connection.makefile("rb").readline().strip() == b"OK"
    except OSError:
        return False


class InstanceBroker:
    def __init__(self, root, app_name, on_activate):
        self.root = root
        self.app_name = app_name
        self.on_activate = on_activate
        self.stop_event = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        self.is_primary = False

        try:
            self.listener.bind(("127.0.0.1", _APP_PORTS[app_name]))
        except OSError:
            self.listener.close()
            self.is_primary = False
            self.notified_existing = notify_instance(app_name)
            return

        self.listener.listen(5)
        self.listener.settimeout(0.25)
        self.is_primary = True
        self.notified_existing = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop_event.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            with connection:
                try:
                    connection.settimeout(1.0)
                    message = bytearray()
                    while len(message) <= 65536 and not message.endswith(b"\n"):
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        message.extend(chunk)
                    payload = json.loads(message.decode("utf-8")) if message else {}
                    connection.sendall(b"OK\n")
                    if isinstance(payload, dict):
                        self.root.after(0, lambda payload=payload: self.on_activate(payload))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue

    def close(self):
        if not self.is_primary:
            return
        self.stop_event.set()
        self.listener.close()
        self.thread.join(timeout=1.0)


def launch_or_activate(app_name, script_path, message=None):
    script_path = Path(script_path).resolve()
    payload = message or {}
    if notify_instance(app_name, payload):
        return None

    if getattr(sys, "frozen", False):
        executable_names = {
            "strip-chart": "DataRecorder.exe",
            "streamed-data-viewer": "DataViewer.exe",
        }
        target = Path(sys.executable).with_name(executable_names[app_name])
        command = [str(target)]
    else:
        target = script_path
        command = [sys.executable, str(target)]
    if app_name == "streamed-data-viewer" and payload.get("path"):
        command.append(str(payload["path"]))
    return subprocess.Popen(command, cwd=str(target.parent))
