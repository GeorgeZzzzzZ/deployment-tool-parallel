"""Deployment instrumentation exercises local fakes only, without server access."""
import json
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from main import DeployTool
from task_diagnostics import DiagnosticLog, TaskTrace


class DeploymentDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sink = DiagnosticLog(self.temp.name)
        self.addCleanup(self.sink.close)
        self.trace = TaskTrace(self.sink, 1)
        self.owner = SimpleNamespace(_diagnostics=self.trace, emit=Mock())

    def records(self):
        self.sink.close()
        return [json.loads(line) for line in self.sink.path.read_text(encoding="utf-8").splitlines()]

    def test_remote_transfer_keeps_layer_timing_but_never_raw_output_or_command(self):
        image = "registry.example/etmbackend:1"
        chunks = [line.encode() for line in (
            "abc: Downloading 50MB/100MB\r",
            "abc: Download complete\rabc: Extracting 25MB/100MB\r",
            "abc: Pull complete\npassword=FAKE_OUTPUT_SECRET\n",
            "def: Downloading 1iB/2iB\r",
        )]
        channel = SimpleNamespace(
            exit_status_ready=lambda: not chunks, recv_ready=lambda: bool(chunks),
            recv=lambda size: chunks.pop(0), recv_stderr_ready=lambda: False,
            recv_exit_status=lambda: 0,
        )
        client = Mock()
        client.exec_command.return_value = (None, SimpleNamespace(channel=channel), None)
        with patch("main.time.sleep"):
            DeployTool._remote_exec(self.owner, client, "password=FAKE_COMMAND_SECRET",
                                    progress_range=(15, 78), transfer_images=[image])
        records = self.records()
        states = [r["state"] for r in records if r["event"] == "layer_state"]
        self.assertIn("Downloading", states)
        self.assertIn("Extracting", states)
        self.assertTrue(any(r["event"] == "transfer_end" and r["exit_code"] == 0 for r in records))
        self.assertNotIn("FAKE_", json.dumps(records))
        client.exec_command.assert_called_once()

    def test_image_probe_attempts_and_outcome_are_logged(self):
        self.owner._probe_remote_image = Mock(side_effect=[(False, "not visible"), (True, "ok")])
        with patch("main.time.sleep"):
            DeployTool._wait_for_remote_image(self.owner, Mock(), "registry.example/etm:1")
        records = self.records()
        probes = [r for r in records if r["event"] == "image_probe_result"]
        self.assertEqual([(r["attempt"], r["visible"]) for r in probes], [(1, False), (2, True)])
        self.assertTrue(any(r.get("operation") == "image_visibility_wait" and
                            r["event"] == "operation_end" for r in records))

    def test_remote_read_failure_flushes_last_transfer_phase_and_preserves_error(self):
        calls = [0]
        failure = OSError("password=FAKE_READ_ERROR")

        def receive(_size):
            calls[0] += 1
            if calls[0] == 1:
                return b"abc: Extracting 50MB/100MB\r"
            raise failure

        channel = SimpleNamespace(exit_status_ready=lambda: False, recv_ready=lambda: True,
                                  recv=receive, recv_stderr_ready=lambda: False)
        client = Mock()
        client.exec_command.return_value = (None, SimpleNamespace(channel=channel), None)
        with patch("main.time.sleep"), self.assertRaises(OSError) as caught:
            DeployTool._remote_exec(self.owner, client, "fake-command", progress_range=(15, 78),
                                    transfer_images=["registry.example/etm:1"])
        self.assertIs(caught.exception, failure)
        records = self.records()
        self.assertTrue(any(r["event"] == "transfer_context_end" for r in records))
        end = next(r for r in records if r["event"] == "transfer_end")
        self.assertIsNone(end["exit_code"])
        self.assertNotIn("FAKE_READ_ERROR", json.dumps(records))

    def test_target_queue_wait_is_separate_from_network_operations(self):
        coordinator = Mock()
        coordinator.acquire.side_effect = lambda *args, **kw: kw["on_wait"]()
        self.owner.window_manager = SimpleNamespace(target_coordinator=coordinator)
        DeployTool._wait_for_deployment_target(self.owner, ("secret-key",), threading.Event())
        records = self.records()
        self.assertTrue(any(r.get("operation") == "target_queue_wait" for r in records))
        self.assertTrue(any(r.get("wait_reason") == "same_deployment_target" for r in records))
        self.assertNotIn("secret-key", json.dumps(records))

    def test_pipeline_records_submit_wait_and_server_duration_without_parameters(self):
        client = Mock()
        client.build.return_value = "https://user:FAKE_SECRET@jenkins/queue/1"
        client.wait_for_build.return_value = ({"result": "SUCCESS", "number": 7, "duration": 12345}, "")
        self.owner.jenkins = client
        self.owner.cancel_event = threading.Event()
        DeployTool._run_pipeline(self.owner, ["folder", "job"], {"password": "FAKE_PARAMETER"},
                                  "", "SSH 环境部署", {}, {}, {}, deploy_after_build=False)
        records = self.records()
        operations = {r.get("operation") for r in records}
        self.assertTrue({"jenkins_submit", "jenkins_wait"} <= operations)
        self.assertTrue(any(r.get("jenkins_duration_ms") == 12345 for r in records))
        self.assertNotIn("FAKE_", json.dumps(records))

    def test_batch_links_owners_and_times_each_yaml_after_all_image_transfers(self):
        clock = [0.0]
        self.owner._diagnostics = TaskTrace(self.sink, 1, clock=lambda: clock[0])
        follower = SimpleNamespace(_diagnostics=TaskTrace(self.sink, 2, clock=lambda: clock[0]),
                                   cancel_event=threading.Event(), emit=Mock())
        self.owner.cancel_event = threading.Event()
        self.owner.window_manager = SimpleNamespace(batch_coordinator=None)
        self.owner._deployment_target_key = DeployTool._deployment_target_key
        self.owner._wait_for_deployment_target = Mock()
        self.owner._release_deployment_target = Mock()
        self.owner._remote_command_succeeded = Mock(return_value=False)
        self.owner._create_isolated_image_workspace = Mock(return_value="/images/test")
        self.owner._remove_isolated_image_workspace = Mock()
        self.owner._wait_for_remote_image = Mock()
        self.owner._sftp_update = Mock()
        entries = []
        for owner, product in ((self.owner, "etm"), (follower, "pqm")):
            entries.append(dict(owner=owner, package=f"{product}backend:new", environment=dict(
                host="local-test-only", port="22", username="test", password_plain="FAKE_PASSWORD",
                image_dir="/images", image_file="list.txt", pull_command="sh transfer.sh",
                yaml_dir="/yaml", yaml_file=f"{product}.yaml", apply_command="kubectl apply -f safe.yaml")))
        client = Mock()
        sftp = client.open_sftp.return_value
        sftp.open.side_effect = lambda path, mode: io.BytesIO(
            f"image: registry.example/{Path(path).stem}backend:old\n".encode())

        def remote(_client, _command, **kwargs):
            if "on_image_progress" in kwargs:
                clock[0] = 5.0
                kwargs["on_image_progress"]("registry.example/etmbackend:new", 78, "pushed")
                clock[0] = 12.0
                kwargs["on_image_progress"]("registry.example/pqmbackend:new", 78, "pushed")
            else:
                clock[0] += 2.0

        self.owner._remote_exec = remote
        with patch("main.paramiko.SSHClient", return_value=client), patch("main.connect_ssh_client"):
            DeployTool._execute_deploy_batch(self.owner, entries)
        self.owner._diagnostics.finish("succeeded")
        follower._diagnostics.finish("succeeded")
        records = self.records()
        links = [r for r in records if r["event"] == "batch_link"]
        self.assertEqual({r["task_number"] for r in links}, {1, 2})
        self.assertEqual({r["leader"] for r in links}, {self.owner._diagnostics.run_id})
        pushes = [r for r in records if r["event"] == "image_push_complete"]
        self.assertEqual([r["task_number"] for r in pushes], [1, 2])
        applies = [r for r in records if r["event"] == "operation_end" and r.get("operation") == "yaml_apply"]
        self.assertEqual([(r["task_number"], r["duration_s"]) for r in applies], [(1, 2.0), (2, 2.0)])
        transfer_end = next(i for i, r in enumerate(records) if r["event"] == "operation_end" and r.get("operation") == "image_transfer")
        apply_start = next(i for i, r in enumerate(records) if r["event"] == "operation_start" and r.get("operation") == "yaml_apply")
        self.assertLess(transfer_end, apply_start)
        self.assertNotIn("FAKE_PASSWORD", json.dumps(records))
        client.close.assert_called_once()

    def test_tracked_task_finishes_once_and_includes_total_duration(self):
        self.owner.task_number = 3
        self.owner._notify_task_summary = Mock()
        with patch("main.new_task_trace", return_value=self.trace) as factory:
            DeployTool._begin_tracked_task(self.owner, "SSH 部署", "环境3", package="etm:1")
        factory.assert_called_once_with(3, kind="SSH 部署", target="环境3", package="etm:1", title="")
        self.owner._task_phase = "部署完成"
        DeployTool._finish_tracked_task(self.owner)
        DeployTool._finish_tracked_task(self.owner)
        ends = [r for r in self.records() if r["event"] == "task_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["result"], "succeeded")
        self.assertGreaterEqual(ends[0]["elapsed_s"], 0)


if __name__ == "__main__":
    unittest.main()
