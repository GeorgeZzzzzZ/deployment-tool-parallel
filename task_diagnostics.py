"""Bounded, best-effort local deployment timings; never store raw remote output."""

import functools
import json
import logging
import math
import os
import queue
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
_FIELDS = set("""timestamp event run_id task_number kind target package title operation op_id
job mode batch_id members leader participants host port yaml_path image build_number
jenkins_duration_ms result phase percent visible attempt exit_code script duration_s
wait_reason exception_type source outcome elapsed_s count total_s max_s layer_id state
previous_state current_bytes total_bytes state_elapsed_s last_output_age_s unchanged_s
entries_dropped""".split())
_SECRET = re.compile(
    r"(?i)(?:password|passwd|pwd|token|secret|authorization|proxy-authorization|cookie|"
    r"api[-_ ]?key|credential|access[-_ ]?key|client[-_ ]?secret)[\"']?(?:\s*[=:]|\s+\S)|"
    r"\b(?:bearer|basic)\s+\S+|://[^/\s]*@|\b[^/\s:@]+:[^/\s@]+@|[?&][^\s=&#]+="
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_sink = None
_configuration_error = ""
_global_lock = threading.RLock()


def _text(value):
    if _SECRET.search(value) or _CONTROL.search(value):
        return "[redacted]"
    return value[:256]


def _clean(record):
    cleaned = {}
    for key, value in record.items():
        if key in _FIELDS:
            if isinstance(value, str):
                cleaned[key] = ("[redacted]" if key == "script" and not _IDENTIFIER.fullmatch(value)
                                else _text(value))
            elif value is None or isinstance(value, (bool, int)):
                cleaned[key] = value
            elif isinstance(value, float) and math.isfinite(value):
                cleaned[key] = round(value, 6)
            elif key in {"participants", "members"} and isinstance(value, (tuple, list)):
                cleaned[key] = [_text(item) if isinstance(item, str) else item
                                for item in value[:64] if isinstance(item, (str, int))]
        elif key == "layers" and isinstance(value, list):
            cleaned[key] = [_clean(item) for item in value[:64] if isinstance(item, dict)]
        elif key == "operation_summary" and isinstance(value, dict):
            cleaned[key] = {name: _clean(item) for name, item in list(value.items())[:64]
                            if isinstance(name, str) and _IDENTIFIER.fullmatch(name)
                            and not _SECRET.search(name)
                            and isinstance(item, dict)}
    return cleaned


class _FileHandler(RotatingFileHandler):
    def handleError(self, record):
        # The base handler prints its record and exception to stderr on failure.
        raise OSError("diagnostic_write_failed") from None


class DiagnosticLog:
    def __init__(self, directory, prefix="deployment", max_bytes=10 * 1024 * 1024, backup_count=3):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        prefix = prefix if _IDENTIFIER.fullmatch(prefix) else "deployment"
        stamp = datetime.now(_TZ).strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"{prefix}-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.log"
        self._handler = _FileHandler(self.path, maxBytes=max(256, max_bytes),
                                     backupCount=max(1, backup_count), encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self.error = ""
        self.dropped_count = 0
        self._queue = queue.Queue(maxsize=1024)
        self._lock = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._consume, name="deployment-diagnostics", daemon=True)
        self._thread.start()

    def write(self, record):
        try:
            message = json.dumps(_clean(record), ensure_ascii=False, separators=(",", ":"))
            with self._lock:
                if self._closed:
                    return
                try:
                    self._queue.put_nowait(message)
                except queue.Full:
                    self.dropped_count += 1
                    self.error = "queue_full"
        except Exception as error:
            self.error = type(error).__name__

    def _emit(self, message):
        try:
            self._handler.emit(logging.LogRecord("deployment", logging.INFO, "", 0, message, (), None))
        except Exception as error:
            self.error = type(error).__name__

    def _consume(self):
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    message = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._emit(message)
            if self.dropped_count:
                self._emit(json.dumps({"event": "diagnostic_dropped", "entries_dropped": self.dropped_count}))
        finally:
            try:
                self._handler.close()
            except Exception as error:
                self.error = type(error).__name__

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            self.error = "close_timeout"


def configure_diagnostics(directory=None):
    global _sink, _configuration_error
    with _global_lock:
        shutdown_diagnostics()
        if directory is None:
            if os.name == "nt":
                directory = Path("D:/AI/tool/deployment-tool-parallel/log")
            else:
                root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
                directory = root / "log"
        try:
            _sink = DiagnosticLog(directory)
            _configuration_error = ""
        except Exception as error:
            _configuration_error = type(error).__name__
        return _sink


def shutdown_diagnostics():
    global _sink
    with _global_lock:
        if _sink is not None:
            _sink.close()
            _sink = None


def diagnostics_status():
    with _global_lock:
        return (str(_sink.path), _sink.error) if _sink is not None else ("", _configuration_error)


class TaskTrace:
    def __init__(self, sink, task_number, kind="", target="", package="", title="", clock=None):
        self.sink = sink
        self.run_id = uuid.uuid4().hex
        self.clock = clock or time.perf_counter
        self._started = self.clock()
        self._lock = threading.RLock()
        self._finished = False
        self._summary = {}
        self._identity = dict(run_id=self.run_id, task_number=task_number, kind=kind,
                              target=target, package=package, title=title)
        self.event("task_start")

    def event(self, event, **fields):
        try:
            with self._lock:
                if isinstance(fields.get("package"), str):
                    self._identity["package"] = _text(fields["package"])
                record = dict(fields)
                record.update(self._identity)
                record.update(event=event, timestamp=datetime.now(_TZ).isoformat(timespec="milliseconds"))
                self.sink.write(_clean(record))
        except Exception:
            pass

    @contextmanager
    def stage(self, operation, **fields):
        operation = operation if _IDENTIFIER.fullmatch(operation) else "operation"
        started, op_id = self.clock(), uuid.uuid4().hex[:12]
        fields = dict(fields, operation=operation, op_id=op_id)
        self.event("operation_start", **fields)
        outcome, exception_type = "success", ""
        try:
            yield
        except BaseException as error:
            outcome, exception_type = "error", type(error).__name__
            raise
        finally:
            duration = max(0.0, self.clock() - started)
            with self._lock:
                if operation not in self._summary and len(self._summary) >= 64:
                    operation = "other"
                summary = self._summary.setdefault(operation, {"count": 0, "total_s": 0.0, "max_s": 0.0})
                summary["count"] += 1
                summary["total_s"] += duration
                summary["max_s"] = max(summary["max_s"], duration)
            self.event("operation_end", **dict(fields, duration_s=duration, outcome=outcome,
                                                exception_type=exception_type))

    def finish(self, result):
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self.event("task_end", result=result, elapsed_s=max(0.0, self.clock() - self._started),
                       operation_summary=self._summary)


def new_task_trace(task_number, kind="", target="", package="", title=""):
    with _global_lock:
        return TaskTrace(_sink, task_number, kind, target, package, title) if _sink is not None else None


def trace_event(owner, event, **fields):
    trace = getattr(owner, "_diagnostics", None)
    if isinstance(trace, TaskTrace):
        trace.event(event, **fields)


@contextmanager
def trace_stage(owner, operation, **fields):
    trace = getattr(owner, "_diagnostics", None)
    if isinstance(trace, TaskTrace):
        with trace.stage(operation, **fields):
            yield
    else:
        yield


def trace_operation(operation):
    def decorate(method):
        @functools.wraps(method)
        def wrapped(owner, *args, **kwargs):
            with trace_stage(owner, operation):
                return method(owner, *args, **kwargs)
        return wrapped
    return decorate


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LAYER = re.compile(r"^\s*([A-Za-z0-9_.-]{1,64}):\s*(Waiting|Downloading|Download complete|"
                    r"Verifying Checksum|Extracting|Pull complete|Already exists|Preparing|Pushing|"
                    r"Pushed|Layer already exists|Retrying)\b")
_BYTES = re.compile(r"(\d+(?:\.\d+)?)\s*(B|[KMGT]i?B)\s*/\s*"
                    r"(\d+(?:\.\d+)?)\s*(B|[KMGT]i?B)\b", re.I)


def _byte_count(number, unit):
    unit = unit.upper()
    power = "BKMGT".index(unit[0])
    value = float(number) * (1024 if "I" in unit else 1000) ** power
    return min(int(value), 2 ** 63 - 1) if math.isfinite(value) else 0


def _best_effort(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception:
            # Diagnostic parsing and clocks cannot alter a deployment's result.
            return None
    return wrapped


class RemoteTransferTrace:
    def __init__(self, owner, clock=None):
        self.owner = owner
        self.clock = clock or time.perf_counter
        self._started = self._last_output = self._changed = self.clock()
        self._last_snapshot = self._started
        self._image = self._phase = ""
        self._context_started = None
        self._layers = {}
        self._finished = False
        self._lock = threading.RLock()

    @_best_effort
    def feed(self, line, image="", phase=""):
        with self._lock:
            if self._finished:
                return
            now = self.clock()
            image, phase = _text(image), _text(phase)
            if self._context_started is None or image != self._image or phase != self._phase:
                if self._context_started is not None:
                    self._end_context(now)
                self._layers.clear()
                self._image, self._phase = image, phase
                self._context_started = now
                self._changed = now
                trace_event(self.owner, "transfer_context", image=self._image, phase=self._phase)
            self._last_output = now
            for segment in _ANSI.sub("", line[:8192]).split("\r"):
                match = _LAYER.match(segment)
                if not match:
                    continue
                layer_id, state = match.groups()
                old = self._layers.get(layer_id)
                if old is None and len(self._layers) >= 64:
                    continue
                current = {"layer_id": layer_id, "state": state, "started": now,
                           "current_bytes": 0, "total_bytes": 0}
                byte_match = _BYTES.search(segment, match.end())
                if byte_match:
                    current["current_bytes"] = _byte_count(*byte_match.groups()[:2])
                    current["total_bytes"] = _byte_count(*byte_match.groups()[2:])
                elif old and old["state"] == state:
                    current.update(current_bytes=old["current_bytes"], total_bytes=old["total_bytes"])
                if old is None or old["state"] != state:
                    trace_event(self.owner, "layer_state", image=self._image, phase=self._phase,
                                **{key: value for key, value in current.items() if key != "started"},
                                previous_state=old["state"] if old else "",
                                duration_s=max(0.0, now - old["started"]) if old else 0.0)
                    self._changed = now
                else:
                    current["started"] = old["started"]
                    if current != old:
                        self._changed = now
                self._layers[layer_id] = current
            self._snapshot(now)

    def _end_context(self, now):
        self._snapshot(now, force=True)
        trace_event(self.owner, "transfer_context_end", image=self._image, phase=self._phase,
                    duration_s=max(0.0, now - self._context_started))

    def _snapshot(self, now, force=False):
        if not force and now - self._last_snapshot < 10:
            return
        self._last_snapshot = now
        layers = [dict({key: value for key, value in layer.items() if key != "started"},
                       state_elapsed_s=max(0.0, now - layer["started"])) for layer in self._layers.values()]
        trace_event(self.owner, "transfer_snapshot", image=self._image, phase=self._phase, layers=layers,
                    last_output_age_s=max(0.0, now - self._last_output), unchanged_s=max(0.0, now - self._changed))

    @_best_effort
    def tick(self):
        with self._lock:
            if not self._finished:
                self._snapshot(self.clock())

    @_best_effort
    def finish(self, exit_code):
        with self._lock:
            if self._finished:
                return
            self._finished = True
            now = self.clock()
            if self._context_started is not None:
                self._end_context(now)
            else:
                self._snapshot(now, force=True)
            trace_event(self.owner, "transfer_end", image=self._image, phase=self._phase,
                        elapsed_s=max(0.0, now - self._started), exit_code=exit_code)
