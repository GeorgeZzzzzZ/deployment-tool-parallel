"""Local deployment regression tests; Linux PTY cases also run through WSL.

Only the selected production methods are loaded on Linux, so PTY checks need
neither Qt nor SSH packages. Commands, waiting and process-group cancellation
are real; the SSH/SFTP boundary is redirected into an isolated local directory.
"""
import ast
import codecs
import io
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from contextlib import contextmanager
from shlex import quote as shell_quote
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import update_yaml_image, yaml_image_reference
from task_diagnostics import trace_event, trace_stage, trace_operation, RemoteTransferTrace


def load_deployment_code():
    path = Path(__file__).with_name("main.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {
        "PipelineCancelled", "RemoteCommandInterrupted", "BatchReassembled",
        "RemoteCommandUncertain", "DeploymentCancellation",
        "ssh_request_deadline",
        "BatchDeploymentCoordinator", "exception_message", "ANSI_ESCAPE_RE",
        "TRANSFER_PROGRESS_RE", "clean_remote_output_line", "_transfer_bytes",
        "parse_transfer_progress", "format_transfer_bytes", "DockerImageProgress",
        "_yaml_delete_command", "_deployment_delete_command",
    }
    methods = {
        "_execute_deploy_batch", "_remote_exec", "_quiet_remote_status",
        "_terminate_remote_process_group", "_remote_command_succeeded",
        "_probe_remote_image", "_wait_for_remote_image", "_deployment_target_key",
        "_create_isolated_image_workspace", "_remove_isolated_image_workspace",
        "_apply_deployment_yaml",
        "_start_ssh_replaced_packages", "_freeze_replaced_packages",
        "_managed_transfer_command", "_block_deployment_target",
    }
    selected = []
    for node in tree.body:
        if getattr(node, "name", None) in names:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "DeployTool":
            node.bases = []
            node.body = [item for item in node.body if getattr(item, "name", None) in methods]
            selected.append(node)
    namespace = dict(globals(), paramiko=SimpleNamespace(), connect_ssh_client=lambda *_: None)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


production = load_deployment_code()


def command_result(status, error=b""):
    stdout = io.BytesIO()
    stdout.channel = SimpleNamespace(recv_exit_status=lambda: status)
    return None, stdout, io.BytesIO(error)


class ImageProbeTests(unittest.TestCase):
    def setUp(self):
        self.tool = production.DeployTool()
        self.events = []
        self.tool.emit = lambda *event: self.events.append(event)

    def wait_with_error(self, error):
        client = Mock()
        client.exec_command.return_value = command_result(1, error)
        with patch.object(time, "monotonic", side_effect=[0, 0, 0, 2]), patch.object(time, "sleep"):
            with self.assertRaises(RuntimeError) as caught:
                self.tool._wait_for_remote_image(client, "registry.example/app:1", timeout_seconds=1)
        return "\n".join(str(event[1]) for event in self.events) + str(caught.exception)

    def test_auth_failure_is_not_reported_as_missing_and_does_not_leak_secrets(self):
        text = self.wait_with_error(
            b'Get "https://user:FAKE_PASSWORD@registry.example/?token=FAKE_TOKEN": '
            b'unauthorized: authentication required\nAuthorization: Bearer FAKE_BEARER\n'
            b'Cookie: session=FAKE_COOKIE'
        )
        self.assertIn("认证", text)
        self.assertIn("退出码 1", text)
        self.assertNotIn("等待仓库同步", text)
        for secret in ("FAKE_PASSWORD", "FAKE_TOKEN", "FAKE_BEARER", "FAKE_COOKIE"):
            self.assertNotIn(secret, text)

    def test_probe_distinguishes_registry_missing_from_network_tls_and_cli_errors(self):
        cases = [
            (b"manifest unknown: manifest unknown", "未找到"),
            (b"no such manifest: registry.example/app:1", "未找到"),
            (b"dial tcp: lookup registry.example: no such host", "DNS"),
            (b"dial tcp: connection refused", "网络"),
            (b"Client.Timeout exceeded while awaiting headers", "网络"),
            (b"x509: certificate signed by unknown authority", "TLS"),
            (b"sh: docker: command not found", "Docker"),
            (b"unknown flag: --insecure", "Docker"),
            (b"unknown diagnostic FAKE_UNCLASSIFIED_SECRET", "无法确认"),
            (b"", "无法确认"),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.events.clear()
                text = self.wait_with_error(error)
                self.assertIn(expected, text)
                self.assertNotIn("FAKE_UNCLASSIFIED_SECRET", text)
                if expected != "未找到":
                    self.assertNotIn("等待仓库同步", text)

    def test_missing_image_can_become_visible_without_false_failure(self):
        client = Mock()
        client.exec_command.side_effect = [command_result(1, b"manifest unknown"), command_result(0)]
        with patch.object(time, "sleep"):
            self.tool._wait_for_remote_image(client, "registry.example/app:1")
        self.assertEqual(client.exec_command.call_count, 2)
        self.assertIn("未找到", str(self.events))

    def test_cancelled_probe_does_not_run_a_command(self):
        cancelled = threading.Event()
        cancelled.set()
        client = Mock()
        with self.assertRaises(production.PipelineCancelled):
            self.tool._wait_for_remote_image(client, "registry.example/app:1", cancel_event=cancelled)
        client.exec_command.assert_not_called()


class LocalPtyChannel:
    def __init__(self, command):
        import select
        self.select = select.select
        self.status = None
        self.buffer = b""
        self.eof = False
        self.deadline = time.monotonic() + 12
        self.pid, self.fd = os.forkpty()
        if self.pid == 0:
            # exec preserves the PTY session/group leader, forcing setsid to fork.
            os.execl("/bin/bash", "bash", "-c", ("exec " if command.startswith(("setsid ", "sh -c ")) else "") + command)

    def exit_status_ready(self):
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
        if self.status is None and time.monotonic() > self.deadline:
            raise TimeoutError("local PTY command did not finish")
        return self.status is not None

    def recv_ready(self):
        if not self.buffer and not self.eof and self.select([self.fd], [], [], 0)[0]:
            try:
                self.buffer = os.read(self.fd, 65536)
                self.eof = not self.buffer
            except OSError as exc:
                if exc.errno != 5:  # Linux PTY EOF is EIO.
                    raise
                self.eof = True
        return bool(self.buffer)

    def recv(self, size):
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result

    def recv_stderr_ready(self):
        return False

    def recv_exit_status(self):
        return self.status

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.status is None:
            if not self.exit_status_ready():
                os.kill(self.pid, signal.SIGKILL)
            _, status = os.waitpid(self.pid, 0) if self.status is None else (self.pid, 0)
            if self.status is None:
                self.status = os.waitstatus_to_exitcode(status)


class LocalSftp:
    def get_channel(self):
        return SimpleNamespace(settimeout=lambda _seconds: None)

    def open(self, path, mode):
        return open(path, mode + "b")

    mkdir = staticmethod(os.mkdir)
    listdir = staticmethod(os.listdir)
    symlink = staticmethod(os.symlink)
    remove = staticmethod(os.remove)
    rmdir = staticmethod(os.rmdir)


class LocalClient:
    def __init__(self, root):
        self.root = root
        self.channels = []
        self.transfer_count = 0
        self.probed_before_completion = False

    def exec_command(self, command, get_pty=False, timeout=None):
        if get_pty:
            if ".deploy-console-command.pgid" in command:
                self.transfer_count += 1
            channel = LocalPtyChannel(command)
            self.channels.append(channel)
            return None, SimpleNamespace(channel=channel), None
        if command.startswith("docker manifest inspect "):
            # No Docker/registry access: only report visible after the script finishes.
            completed = (self.root / "completed").exists()
            if self.transfer_count and not completed:
                self.probed_before_completion = True
                raise AssertionError("image probe ran before the transfer child finished")
            return command_result(0 if completed else 1, b"manifest unknown")
        result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, timeout=3)
        return command_result(result.returncode, result.stderr)

    def set_missing_host_key_policy(self, _policy):
        pass

    def open_sftp(self):
        return LocalSftp()

    def close(self):
        for channel in self.channels:
            channel.close()


class DeploymentHarness(production.DeployTool):
    def __init__(self):
        self.cancel_event = threading.Event()
        self.window_manager = SimpleNamespace(batch_coordinator=None)
        self.events = []
        self.locks = set()
        self.updated = []
        self.on_event = lambda *_: None

    def emit(self, *event):
        self.events.append(event)
        self.on_event(*event)

    def _wait_for_deployment_target(self, key, **_kwargs):
        self.locks.add(key)

    def _release_deployment_target(self, key):
        self.locks.remove(key)

    def _sftp_update(self, _sftp, path, transform, **_kwargs):
        target = Path(path)
        target.write_text(transform(target.read_text()))
        self.updated.append(path)


@unittest.skipUnless(sys.platform.startswith("linux"), "real PTY checks run on Linux/WSL")
class RemotePtyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="deploy 'pty ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tool = DeploymentHarness()
        self.client = LocalClient(self.root)
        self.addCleanup(self.client.close)
        self.entries = []
        for name in ("backend", "frontend"):
            (self.root / f"{name}.yaml").write_text(f"image: registry.example/industry/etm{name}:old\n")
            self.entries.append({
                "owner": self.tool,
                "package": f"etm{name}:new",
                "environment": {
                    "host": "local-test-only", "image_dir": str(self.root),
                    "image_file": "images.txt", "pull_command": "sh transfer.sh",
                    "yaml_dir": str(self.root), "yaml_file": f"{name}.yaml",
                    "apply_command": "printf applied >> applied",
                },
            })
        (self.root / "images.txt").write_text("untouched-public-list\n")

    def script(self, exit_code=0, delay="0.6"):
        (self.root / "transfer.sh").write_text(
            "cat images.txt > ../received\n"
            "echo $$ > ../child-pid\n"
            "echo 'Pulling from local-test'\n"
            f"sleep {delay}\n"
            "touch ../completed\n"
            "echo transfer-finished\n"
            f"exit {exit_code}\n"
        )

    def run_batch(self, token=None):
        with patch.object(production.paramiko, "SSHClient", return_value=self.client, create=True), \
                patch.object(production.paramiko, "AutoAddPolicy", return_value=None, create=True):
            self.tool._execute_deploy_batch(self.entries, batch_token=token)

    def assert_clean(self):
        self.assertFalse(self.tool.locks)
        self.assertFalse(list(self.root.glob(".deploy-console-*")))
        self.assertEqual((self.root / "images.txt").read_text(), "untouched-public-list\n")

    def test_success_waits_for_child_and_keeps_batch_manifest_isolated(self):
        self.script()
        started = time.monotonic()
        self.run_batch()
        self.assertGreaterEqual(time.monotonic() - started, 0.55)
        self.assertEqual(self.client.transfer_count, 1)
        self.assertFalse(self.client.probed_before_completion)
        self.assertIn(("deploy_log", "transfer-finished"), self.tool.events)
        self.assertEqual((self.root / "received").read_text(), "etmbackend:new\netmfrontend:new\n")
        self.assertEqual((self.root / "applied").read_text(), "appliedapplied")
        self.assertEqual(len(self.tool.updated), 2)
        self.assert_clean()

    def test_transfer_progress_only_advances_the_current_image_owner(self):
        digest = "new: digest: sha256:" + "a" * 64 + " size: 1234"
        lines = []
        for service, amount in (("backend", 50), ("frontend", 25)):
            lines.extend([
                f"处理镜像: etm{service}:new",
                f"new: Pulling from industry/etm{service}",
                f"a: Downloading {amount}MB/100MB",
                f"The push refers to repository [registry.example/industry/etm{service}]",
                "a: Pushing 50MB/100MB", digest,
            ])
        (self.root / "transfer.sh").write_text(
            "\n".join("printf '%s\\n' " + shell_quote(line) for line in lines)
            + "\ntouch ../completed\n"
        )
        follower = DeploymentHarness()
        self.entries[1]["owner"] = follower
        snapshots = []

        def capture(target, payload):
            if target == "deploy_progress" and "拉取镜像：约" in payload[1]:
                snapshots.append(tuple(
                    [value for name, value in owner.events if name == "deploy_progress"][-1]
                    for owner in (self.tool, follower)
                ))

        self.tool.on_event = follower.on_event = capture
        self.run_batch()
        self.assertTrue(any(first[0] == 30 and second == (15, "等待拉取镜像...")
                            for first, second in snapshots))
        self.assertTrue(any(first[0] == 78 and second[0] == 22 for first, second in snapshots))
        for owner in (self.tool, follower):
            updates = [payload for target, payload in owner.events if target == "deploy_progress"]
            values = [percent for percent, _text in updates]
            self.assertEqual(values, sorted(values))
            self.assertTrue(any(percent == 78 and "已确认" in text for percent, text in updates))
            self.assertEqual(updates[-1], (100, "合并部署完成"))
        self.assert_clean()

    def test_cached_image_keeps_its_progress_while_other_image_transfers(self):
        self.script()
        follower = DeploymentHarness()
        self.entries[1]["owner"] = follower
        with patch.object(self.tool, "_remote_command_succeeded",
                          side_effect=lambda _client, command: "etmbackend:new" in command):
            self.run_batch()
        updates = [payload for target, payload in self.tool.events if target == "deploy_progress"]
        self.assertIn((78, "目标镜像已存在，等待部署..."), updates)
        self.assertEqual([value for value, _text in updates], sorted(value for value, _text in updates))
        self.assertFalse(any("拉取" in text for _percent, text in updates))
        self.assertEqual((self.root / "received").read_text(), "etmfrontend:new\n")
        follower_updates = [payload for target, payload in follower.events if target == "deploy_progress"]
        self.assertIn((15, "等待拉取镜像..."), follower_updates)
        self.assertIn((78, "目标镜像已确认，等待部署..."), follower_updates)
        self.assert_clean()

    def test_serial_script_finishes_pull_tag_push_before_the_next_image(self):
        (self.root / "transfer.sh").write_text(
            "docker() {\n"
            "  printf '%s %s start\\n' \"$1\" \"$2\" >> ../order\n"
            "  sleep 0.03\n"
            "  printf '%s %s end\\n' \"$1\" \"$2\" >> ../order\n"
            "}\n"
            "while IFS= read -r image; do\n"
            "  docker pull \"source/$image\"\n"
            "  docker tag \"source/$image\" \"target/$image\"\n"
            "  docker push \"target/$image\"\n"
            "done < images.txt\n"
            "touch ../completed\n"
        )
        self.run_batch()
        expected = [f"{action} {repository}/etm{service}:new {state}"
                    for service in ("backend", "frontend")
                    for action, repository in (("pull", "source"), ("tag", "source"), ("push", "target"))
                    for state in ("start", "end")]
        self.assertEqual((self.root / "order").read_text().splitlines(), expected)
        self.assertEqual(self.client.transfer_count, 1)
        self.assert_clean()

    def test_nonzero_child_exit_stops_before_probe_or_yaml_apply(self):
        self.script(exit_code=7)
        with self.assertRaisesRegex(RuntimeError, "命令退出码 7"):
            self.run_batch()
        self.assertIn(("deploy_log", "transfer-finished"), self.tool.events)
        self.assertFalse(self.client.probed_before_completion)
        self.assertFalse(self.tool.updated)
        self.assertFalse((self.root / "applied").exists())
        self.assert_clean()

    def test_rebatch_cancels_only_managed_process_group(self):
        # The shell reaps its child after TERM, avoiding zombie-dependent assertions.
        self.script(delay="10 & child=$!; trap 'wait $child; exit 143' TERM; wait $child")
        for entry in self.entries:
            entry["environment"]["pull_command"] = "exec sh transfer.sh"
        coordinator = production.BatchDeploymentCoordinator()
        token = coordinator.register(self.tool, "local-batch")
        coordinator.wait_until_ready(self.tool, token, self.entries[0])
        self.tool.window_manager.batch_coordinator = coordinator
        def request_rebatch(target, message):
            if target == "deploy_log" and message == "Pulling from local-test":
                coordinator.register(object(), "local-batch")
                self.assertTrue(coordinator.request_restart(token))

        self.tool.on_event = request_rebatch
        unrelated = subprocess.Popen(["sleep", "15"], start_new_session=True)
        try:
            with self.assertRaises(production.RemoteCommandInterrupted):
                self.run_batch(token)
            self.assertIsNone(unrelated.poll())
            child_pid = int((self.root / "child-pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.killpg(child_pid, 0)
            self.assertFalse((self.root / "completed").exists())
            self.assertFalse(self.tool.updated)
            self.assert_clean()
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_later_package_for_same_yaml_wins(self):
        self.script()
        self.entries.append(dict(self.entries[0], package="etmbackend:latest"))
        self.run_batch()
        self.assertEqual((self.root / "received").read_text(), "etmbackend:latest\netmfrontend:new\n")
        self.assertIn("backend:latest", (self.root / "backend.yaml").read_text())
        self.assertEqual(self.client.transfer_count, 1)
        self.assert_clean()

    def test_pending_interrupt_is_rejected_after_push_starts(self):
        self.script()
        script = self.root / "transfer.sh"
        script.write_text(script.read_text().replace(
            "echo 'Pulling from local-test'",
            "echo 'The push refers to repository local-test'",
        ))
        coordinator = production.BatchDeploymentCoordinator()
        token = coordinator.register(self.tool, "local-batch")
        coordinator.wait_until_ready(self.tool, token, self.entries[0])
        self.tool.window_manager.batch_coordinator = coordinator

        def interrupt_arrives(target, message):
            if target == "deploy_log" and message == "The push refers to repository local-test":
                coordinator.interrupt_event(token).set()

        self.tool.on_event = interrupt_arrives
        self.run_batch(token)
        self.assertFalse(coordinator.interrupt_event(token).is_set())
        self.assertTrue((self.root / "completed").exists())
        self.assertEqual(len(self.tool.updated), 2)
        self.assert_clean()

    def test_cancelled_owner_is_excluded_from_transfer_and_apply(self):
        self.script()
        cancelled_owner = DeploymentHarness()
        cancelled_owner.cancel_event.set()
        self.entries[0]["owner"] = cancelled_owner
        self.run_batch()
        self.assertEqual((self.root / "received").read_text(), "etmfrontend:new\n")
        self.assertIn("etmbackend:old", (self.root / "backend.yaml").read_text())
        self.assertEqual(len(self.tool.updated), 1)
        self.assert_clean()

    def test_user_cancel_during_transfer_stops_process_and_skips_apply(self):
        self.script(delay="0.6 & child=$!; trap 'wait $child; exit 143' TERM; wait $child")
        self.tool.on_event = lambda target, message: (
            self.tool.cancel_event.set() if target == "deploy_log" and message == "Pulling from local-test" else None)
        with self.assertRaises(production.PipelineCancelled):
            self.run_batch()
        self.assertFalse((self.root / "completed").exists())
        self.assertFalse((self.root / "applied").exists())
        self.assert_clean()

    def test_user_cancel_during_push_is_not_rebatch_restriction(self):
        self.script(delay="0.6 & child=$!; trap 'wait $child; exit 143' TERM; wait $child")
        script = self.root / "transfer.sh"
        script.write_text(script.read_text().replace("Pulling from local-test", "The push refers to repository local-test"))
        self.tool.on_event = lambda target, message: (
            self.tool.cancel_event.set() if target == "deploy_log" and message == "The push refers to repository local-test" else None)
        with self.assertRaises(production.PipelineCancelled):
            self.run_batch()
        self.assertFalse((self.root / "completed").exists())
        self.assertFalse((self.root / "applied").exists())
        self.assert_clean()

    def test_cancelling_one_owner_reassembles_remaining_tasks(self):
        self.script(delay="0.6 & child=$!; trap 'wait $child; exit 143' TERM; wait $child")
        other = DeploymentHarness()
        self.entries[1]["owner"] = other
        self.tool.on_event = lambda target, message: (
            self.tool.cancel_event.set() if target == "deploy_log" and message == "Pulling from local-test" else None)
        with self.assertRaises(production.BatchReassembled) as caught:
            self.run_batch()
        self.assertEqual([entry["package"] for entry in caught.exception.entries], ["etmfrontend:new"])
        self.assertFalse(other.cancel_event.is_set())
        self.assertFalse((self.root / "completed").exists())
        self.entries = caught.exception.entries
        self.tool.on_event = lambda *_: None
        self.client = LocalClient(self.root)
        self.addCleanup(self.client.close)
        self.run_batch()
        self.assertIn("etmfrontend:new", (self.root / "frontend.yaml").read_text())
        self.assertIn("etmbackend:old", (self.root / "backend.yaml").read_text())
        self.assert_clean()


class LegacySetsidPtyTests(RemotePtyTests):
    def setUp(self):
        super().setUp()
        # Reproduce the server's old CLI, but retain real setsid/session behavior.
        real_setsid = shutil.which("setsid")
        self.assertIsNotNone(real_setsid)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "setsid"
        executable.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in --wait|-w) echo 'setsid: unrecognized option --wait' >&2; exit 1;; esac\n"
            f"exec {shell_quote(real_setsid)} \"$@\"\n"
        )
        executable.chmod(0o755)
        environment = patch.dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
        environment.start()
        self.addCleanup(environment.stop)


@unittest.skipUnless(sys.platform == "win32", "WSL bridge is only needed on Windows")
class WslPtyTests(unittest.TestCase):
    def test_production_batch_commands_in_real_linux_pty(self):
        if not shutil.which("wsl"):
            self.skipTest("WSL is not installed; run RemotePtyTests on Linux")
        path = Path(__file__).resolve()
        linux_path = "/mnt/" + path.drive[0].lower() + path.as_posix()[2:]
        result = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "--", "python3", linux_path,
             "RemotePtyTests", "LegacySetsidPtyTests", "-v"],
            capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
