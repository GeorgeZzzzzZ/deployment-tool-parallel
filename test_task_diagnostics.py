import inspect
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import task_diagnostics as diagnostics
except ModuleNotFoundError:
    diagnostics = None


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TaskDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(diagnostics, "The local diagnostic module must be implemented")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(diagnostics.shutdown_diagnostics)

    def sink(self, **options):
        sink = diagnostics.DiagnosticLog(self.directory.name, **options)
        self.addCleanup(sink.close)
        return sink

    def records(self, sink):
        sink.close()
        return [json.loads(line) for line in sink.path.read_text(encoding="utf-8").splitlines()]

    def test_flushes_utf8_records_and_unique_filenames(self):
        first, second = self.sink(), self.sink()
        self.assertNotEqual(first.path, second.path)
        first.write({"event": "check", "target": "正式区", "attempt": 1})
        self.assertEqual(self.records(first), [{"event": "check", "target": "正式区", "attempt": 1}])
        self.assertRegex(first.path.name, r"^deployment-\d{8}-\d{6}-\d+.*\.log$")
        first.close()
        self.assertFalse(first.error)

    def test_rotates_and_bounds_retained_files(self):
        sink = self.sink(max_bytes=400, backup_count=2)
        for attempt in range(80):
            sink.write({"event": "rotation", "attempt": attempt, "target": "x" * 100})
        sink.close()
        paths = list(Path(self.directory.name).glob("*.log*"))
        self.assertLessEqual(len(paths), 3)
        self.assertGreater(len(paths), 1)
        self.assertTrue(all(path.stat().st_size <= 400 for path in paths))
        self.assertEqual(json.loads(sink.path.read_text(encoding="utf-8").splitlines()[-1])["attempt"], 79)

    def test_stage_timing_exception_and_task_summary(self):
        sink, clock = self.sink(), Clock()
        trace = diagnostics.TaskTrace(sink, 7, kind="deploy", clock=clock)
        with trace.stage("remote_pull", image="registry/app:1"):
            clock.advance(2.5)
        failure = ValueError("password=never-log-this")
        with self.assertRaises(ValueError) as caught:
            with trace.stage("remote_pull"):
                clock.advance(1.25)
                raise failure
        self.assertIs(caught.exception, failure)
        trace.finish("failed")
        trace.finish("ignored")
        records = self.records(sink)
        ends = [record for record in records if record["event"] == "operation_end"]
        self.assertEqual([record["duration_s"] for record in ends], [2.5, 1.25])
        self.assertEqual([record["outcome"] for record in ends], ["success", "error"])
        self.assertEqual(ends[-1]["exception_type"], "ValueError")
        self.assertNotEqual(ends[0]["op_id"], ends[1]["op_id"])
        self.assertEqual(records[-1]["elapsed_s"], 3.75)
        self.assertEqual(records[-1]["operation_summary"]["remote_pull"],
                         {"count": 2, "total_s": 3.75, "max_s": 2.5})
        self.assertEqual(sum(record["event"] == "task_end" for record in records), 1)
        self.assertTrue(records[0]["timestamp"].endswith("+08:00"))
        self.assertNotIn("never-log-this", sink.path.read_text(encoding="utf-8"))

    def test_concurrent_tasks_keep_unique_identity_and_all_records(self):
        sink = self.sink()
        traces = [diagnostics.TaskTrace(sink, number) for number in range(8)]

        def run(trace):
            for attempt in range(20):
                with trace.stage("poll", attempt=attempt):
                    pass
            trace.finish("success")

        workers = [threading.Thread(target=run, args=(trace,)) for trace in traces]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        records = self.records(sink)
        self.assertEqual(len(records), 8 * 42)
        self.assertEqual(len({record["run_id"] for record in records}), 8)
        for number in range(8):
            self.assertEqual(len({record["run_id"] for record in records
                                  if record["task_number"] == number}), 1)

    def test_package_identification_updates_later_events_without_changing_task_identity(self):
        sink = self.sink()
        trace = diagnostics.TaskTrace(sink, 7, package="")
        trace.event("package_identified", package="repo/app:42", run_id="different", task_number=99)
        trace.event("later_stage")
        trace.finish("success")
        records = self.records(sink)
        self.assertEqual([record["package"] for record in records],
                         ["", "repo/app:42", "repo/app:42", "repo/app:42"])
        self.assertTrue(all(record["run_id"] == trace.run_id and record["task_number"] == 7
                            for record in records))

    def test_updated_package_metadata_is_sanitized(self):
        sink = self.sink()
        trace = diagnostics.TaskTrace(sink, 1)
        trace.event("package_identified", package="https://host/app?token=FAKE_PACKAGE_SECRET")
        trace.finish("success")
        records = self.records(sink)
        self.assertEqual(records[-1]["package"], "[redacted]")
        self.assertNotIn("FAKE_PACKAGE_SECRET", sink.path.read_text(encoding="utf-8"))

    def test_allowlist_redacts_embedded_credentials_and_bounds_values(self):
        sink = self.sink()
        secrets = ["FAKE_PASS", "FAKE_TOKEN", "FAKE_COOKIE", "FAKE_AUTH", "FAKE_QUERY", "FAKE_HEADER"]
        values = ["https://user:FAKE_PASS@host/path", "package token=FAKE_TOKEN",
                  "Cookie: session=FAKE_COOKIE", "Authorization: Bearer FAKE_AUTH",
                  "https://host/path?credential=FAKE_QUERY", "x-api-key: FAKE_HEADER"]
        for value in values:
            sink.write({"event": "safe", "title": value, "command": "raw-private-command",
                        "config": {"password": "FAKE_PASS"}, "parameters": "FAKE_TOKEN"})
        sink.write({"event": "bound", "target": "a" * 10000, "participants": ["id"] * 10000})
        records = self.records(sink)
        text = sink.path.read_text(encoding="utf-8")
        self.assertTrue(all(secret not in text for secret in secrets))
        self.assertNotIn("raw-private-command", text)
        self.assertLessEqual(len(records[-1]["target"]), 256)
        self.assertLessEqual(len(records[-1]["participants"]), 64)

    def test_quoted_and_shell_credentials_are_never_persisted(self):
        sink = self.sink()
        for value in ['{"token": "FAKE_JSON_TOKEN"}', "--password FAKE_SHELL_PASS",
                      "request X-Auth-Token: FAKE_AUTH_TOKEN", "user:FAKE_USERINFO@host/path",
                      "curl -H 'Authorization: FAKE_HEADER'", "secret FAKE_SECRET"]:
            sink.write({"event": "safety", "title": value})
        sink.write({"event": "safety", "script": "docker login -u user -p FAKE_COMMAND_PASS"})
        trace = diagnostics.TaskTrace(sink, 1)
        with trace.stage("token:FAKE_OPERATION_SECRET"):
            pass
        trace.finish("done")
        self.records(sink)
        self.assertNotIn("FAKE_", sink.path.read_text(encoding="utf-8"))

    def test_disabled_helpers_keep_result_errors_and_signature(self):
        diagnostics.shutdown_diagnostics()
        self.assertIsNone(diagnostics.new_task_trace(1))

        class Worker:
            @diagnostics.trace_operation("work")
            def work(self, value, *, fail=False):
                if fail:
                    raise LookupError("original")
                return value * 2

        owner = Worker()
        self.assertEqual(str(inspect.signature(Worker.work)), "(self, value, *, fail=False)")
        self.assertEqual(owner.work(3), 6)
        diagnostics.trace_event(owner, "unused", password="secret")
        with diagnostics.trace_stage(owner, "unused"):
            pass
        with self.assertRaisesRegex(LookupError, "original"):
            owner.work(3, fail=True)

    def test_configure_failure_returns_safe_status_and_does_not_create_default_on_import(self):
        blocker = Path(self.directory.name) / "file"
        blocker.write_text("exists", encoding="utf-8")
        self.assertIsNone(diagnostics.configure_diagnostics(blocker / "password=FAKE_PASS"))
        path, error = diagnostics.diagnostics_status()
        self.assertFalse(path)
        self.assertTrue(error)
        self.assertNotIn("FAKE_PASS", error)
        sink = diagnostics.configure_diagnostics(self.directory.name)
        self.assertIsNotNone(sink)
        self.assertEqual(diagnostics.diagnostics_status(), (str(sink.path), ""))
        self.assertIsNotNone(diagnostics.new_task_trace(8))
        diagnostics.shutdown_diagnostics()
        result = subprocess.run([sys.executable, "-c",
                                 "from unittest.mock import patch; "
                                 "guard = patch('pathlib.Path.mkdir', side_effect=AssertionError('import wrote files')); "
                                 "guard.start(); import task_diagnostics"],
                                cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sink_failure_never_changes_operation_result_or_exception(self):
        class BrokenSink:
            def write(self, record):
                raise OSError("password=FAKE_PASS")

        trace = diagnostics.TaskTrace(BrokenSink(), 1)
        owner = SimpleNamespace(_diagnostics=trace)
        diagnostics.trace_event(owner, "test")
        with diagnostics.trace_stage(owner, "test"):
            value = 12
        self.assertEqual(value, 12)
        with self.assertRaisesRegex(RuntimeError, "actual failure"):
            with diagnostics.trace_stage(owner, "test"):
                raise RuntimeError("actual failure")
        trace.finish("failed")

    def test_full_queue_drops_records_without_waiting_for_disk(self):
        sink = self.sink()
        entered, release = threading.Event(), threading.Event()
        original_emit = sink._handler.emit

        def delayed_emit(record):
            entered.set()
            release.wait(3)
            original_emit(record)

        with patch.object(sink._handler, "emit", side_effect=delayed_emit):
            sink.write({"event": "initial"})
            self.assertTrue(entered.wait(1))
            started = time.perf_counter()
            try:
                for attempt in range(2000):
                    sink.write({"event": "overflow", "attempt": attempt})
                self.assertLess(time.perf_counter() - started, 1)
                self.assertGreater(sink.dropped_count, 0)
            finally:
                release.set()
                sink.close()
        self.assertTrue(any(record["event"] == "diagnostic_dropped" for record in self.records(sink)))

    def test_disk_error_is_reported_without_secret_exception_text(self):
        sink = self.sink()
        with patch.object(sink._handler, "emit", side_effect=OSError("password=FAKE_PASS")):
            sink.write({"event": "disk_failure"})
            sink.close()
        self.assertTrue(sink.error)
        self.assertNotIn("FAKE_PASS", sink.error)

    def test_passive_transfer_tracks_slow_stages_and_rate_limits_heartbeat(self):
        sink, clock = self.sink(), Clock()
        owner = SimpleNamespace(_diagnostics=diagnostics.TaskTrace(sink, 1, clock=clock))
        transfer = diagnostics.RemoteTransferTrace(owner, clock=clock)
        transfer.feed("a1: Downloading [==>] 10MB/100MB", image="repo/app:1", phase="pull")
        clock.advance(10)
        for _ in range(500):
            transfer.tick()
        clock.advance(20)
        transfer.feed("a1: Extracting 50MiB/100MiB", image="repo/app:1", phase="pull")
        clock.advance(15)
        transfer.feed("a1: Pull complete", image="repo/app:1", phase="pull")
        transfer.feed("a1: Pushing 5MB/100MB", image="repo/app:1", phase="push")
        clock.advance(12)
        transfer.feed("a1: Pushed", image="repo/app:1", phase="push")
        transfer.finish(0)
        records = self.records(sink)
        layers = [record for record in records if record["event"] == "layer_state"]
        extracting = next(record for record in layers if record["state"] == "Extracting")
        self.assertEqual(extracting["previous_state"], "Downloading")
        self.assertEqual(extracting["duration_s"], 30)
        self.assertEqual(extracting["current_bytes"], 50 * 1024 * 1024)
        snapshots = [record for record in records if record["event"] == "transfer_snapshot"]
        self.assertLessEqual(len(snapshots), 6)
        heartbeat = next(record for record in snapshots if record["last_output_age_s"] == 10)
        self.assertEqual(heartbeat["layers"][0]["current_bytes"], 10000000)
        self.assertEqual(heartbeat["layers"][0]["state_elapsed_s"], 10)
        self.assertEqual(records[-1]["event"], "transfer_end")
        self.assertEqual(records[-1]["elapsed_s"], 57)
        self.assertEqual(records[-1]["exit_code"], 0)
        self.assertNotIn("speed", sink.path.read_text(encoding="utf-8"))

    def test_unknown_image_clears_ownership_after_final_snapshot_of_previous_context(self):
        sink, clock = self.sink(), Clock()
        transfer = diagnostics.RemoteTransferTrace(
            SimpleNamespace(_diagnostics=diagnostics.TaskTrace(sink, 1, clock=clock)), clock=clock)
        transfer.feed("known: Downloading 10MB/20MB", image="repo/app:1", phase="pull")
        clock.advance(7)
        transfer.feed("unrelated: Downloading 2MB/10MB", image="", phase="pull")
        clock.advance(3)
        transfer.finish(0)
        records = self.records(sink)
        end = next(record for record in records if record["event"] == "transfer_context_end")
        self.assertEqual((end["image"], end["phase"], end["duration_s"]), ("repo/app:1", "pull", 7))
        previous = records[records.index(end) - 1]
        self.assertEqual(previous["event"], "transfer_snapshot")
        self.assertEqual(previous["last_output_age_s"], 7)
        self.assertEqual([layer["layer_id"] for layer in previous["layers"]], ["known"])
        unknown = next(record for record in records if record.get("layer_id") == "unrelated")
        self.assertEqual(unknown["image"], "")
        final_snapshot = [record for record in records if record["event"] == "transfer_snapshot"][-1]
        self.assertEqual([layer["layer_id"] for layer in final_snapshot["layers"]], ["unrelated"])

    def test_phase_and_image_changes_finish_context_and_reset_layer_durations(self):
        sink, clock = self.sink(), Clock()
        transfer = diagnostics.RemoteTransferTrace(
            SimpleNamespace(_diagnostics=diagnostics.TaskTrace(sink, 1, clock=clock)), clock=clock)
        transfer.feed("shared: Extracting 10MB/20MB", image="repo/app:1", phase="pull")
        clock.advance(23)
        transfer.feed("shared: Pushing 1MB/20MB", image="repo/app:1", phase="push")
        clock.advance(19)
        transfer.feed("different: Waiting", image="repo/other:2", phase="pull")
        clock.advance(4)
        transfer.finish(0)
        transfer.finish(0)
        records = self.records(sink)
        ends = [record for record in records if record["event"] == "transfer_context_end"]
        self.assertEqual([(record["image"], record["phase"], record["duration_s"]) for record in ends],
                         [("repo/app:1", "pull", 23), ("repo/app:1", "push", 19),
                          ("repo/other:2", "pull", 4)])
        pushing = next(record for record in records if record.get("state") == "Pushing")
        self.assertEqual((pushing["previous_state"], pushing["duration_s"]), ("", 0))
        for end in ends:
            snapshot = records[records.index(end) - 1]
            self.assertEqual(snapshot["event"], "transfer_snapshot")
            self.assertEqual((snapshot["image"], snapshot["phase"]), (end["image"], end["phase"]))
        push_snapshot = next(record for record in records
                             if record["event"] == "transfer_snapshot" and record["phase"] == "push")
        self.assertEqual([layer["state"] for layer in push_snapshot["layers"]], ["Pushing"])

    def test_transfer_discards_raw_output_and_recognizes_terminal_states(self):
        sink, clock = self.sink(), Clock()
        transfer = diagnostics.RemoteTransferTrace(
            SimpleNamespace(_diagnostics=diagnostics.TaskTrace(sink, 1, clock=clock)), clock=clock)
        states = ["Waiting", "Download complete", "Verifying Checksum", "Already exists",
                  "Preparing", "Layer already exists", "Retrying"]
        for state in states:
            transfer.feed("abc123: " + state + " password=FAKE_SECRET")
            clock.advance(1)
        transfer.feed("Authorization: Bearer FAKE_TOKEN")
        transfer.finish(1)
        records = self.records(sink)
        self.assertEqual([record["state"] for record in records if record["event"] == "layer_state"], states)
        text = sink.path.read_text(encoding="utf-8")
        self.assertNotIn("FAKE_SECRET", text)
        self.assertNotIn("FAKE_TOKEN", text)

    def test_malformed_transfer_numbers_cannot_interrupt_deployment(self):
        sink = self.sink()
        transfer = diagnostics.RemoteTransferTrace(SimpleNamespace(_diagnostics=diagnostics.TaskTrace(sink, 1)))
        transfer.feed("abc: Downloading " + "9" * 1000 + "GB/100GB")
        transfer.feed("abc: Downloading 10MB/0MB")
        transfer.finish(0)
        records = self.records(sink)
        self.assertEqual(records[-1]["event"], "transfer_end")
        for record in records:
            self.assertLessEqual(record.get("current_bytes", 0), 2 ** 63 - 1)

    def test_byte_units_reject_invalid_ib_and_accept_decimal_and_binary_units(self):
        sink = self.sink()
        transfer = diagnostics.RemoteTransferTrace(SimpleNamespace(_diagnostics=diagnostics.TaskTrace(sink, 1)))
        for number, text in enumerate(["1iB/2iB", "1MB/2iB", "1MB/2MBps", "1B/2B",
                                       "1kB/2kB", "1KiB/2KiB", "1MiB/2MiB"]):
            transfer.feed(f"layer{number}: Downloading {text}")
        transfer.finish(0)
        layers = [record for record in self.records(sink) if record["event"] == "layer_state"]
        self.assertEqual([record["current_bytes"] for record in layers], [0, 0, 0, 1, 1000, 1024, 1048576])

    def test_feed_parser_failure_cannot_change_business_return(self):
        sink = self.sink()
        transfer = diagnostics.RemoteTransferTrace(SimpleNamespace(_diagnostics=diagnostics.TaskTrace(sink, 1)))

        def business_operation():
            transfer.feed("layer: Downloading 1MB/2MB")
            return 0

        with patch.object(diagnostics, "_byte_count", side_effect=ValueError("password=FAKE_PARSER_SECRET")):
            self.assertEqual(business_operation(), 0)
        transfer.feed("layer: Pushing 1MB/2MB", phase="push")
        transfer.finish(0)
        records = self.records(sink)
        self.assertEqual(records[-1]["exit_code"], 0)
        self.assertNotIn("FAKE_PARSER_SECRET", sink.path.read_text(encoding="utf-8"))

    def test_feed_tick_and_finish_clock_failures_do_not_escape(self):
        for method, args in [("feed", ("layer: Waiting",)), ("tick", ()), ("finish", (None,))]:
            with self.subTest(method=method):
                transfer = diagnostics.RemoteTransferTrace(SimpleNamespace(), clock=Clock())
                with patch.object(transfer, "clock", side_effect=OSError("broken diagnostic clock")):
                    self.assertIsNone(getattr(transfer, method)(*args))

    def test_finish_failure_preserves_original_business_exception(self):
        transfer = diagnostics.RemoteTransferTrace(SimpleNamespace(), clock=Clock())
        failure = RuntimeError("original remote failure")
        with patch.object(transfer, "_snapshot", side_effect=ValueError("diagnostic snapshot failed")):
            with self.assertRaises(RuntimeError) as caught:
                try:
                    raise failure
                finally:
                    transfer.finish(None)
        self.assertIs(caught.exception, failure)

    def test_interrupted_transfer_records_unknown_exit_code_as_null(self):
        sink, clock = self.sink(), Clock()
        transfer = diagnostics.RemoteTransferTrace(
            SimpleNamespace(_diagnostics=diagnostics.TaskTrace(sink, 1, clock=clock)), clock=clock)
        transfer.feed("layer: Downloading 1MB/2MB", image="repo/app:1", phase="pull")
        clock.advance(4)
        transfer.finish(None)
        records = self.records(sink)
        self.assertEqual(records[-1]["event"], "transfer_end")
        self.assertIsNone(records[-1]["exit_code"])
        self.assertEqual(records[-1]["elapsed_s"], 4)


if __name__ == "__main__":
    unittest.main()
