"""Offline cancellation, timeout and target isolation regression checks."""
import os
import time
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import main


class DeploymentCancellationTests(unittest.TestCase):
    def setUp(self):
        self.tool = SimpleNamespace(emit=Mock(), _terminate_remote_process_group=Mock())
        self.channel = Mock()
        self.channel.exit_status_ready.side_effect = [False] * 5 + [True]
        self.channel.recv_ready.return_value = False
        self.channel.recv_stderr_ready.return_value = False
        self.channel.recv_exit_status.return_value = 0
        self.client = Mock()
        self.client.exec_command.return_value = (None, SimpleNamespace(channel=self.channel), None)

    def test_cancel_before_launch_never_runs_remote_command(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(main.PipelineCancelled):
            main.DeployTool._remote_exec(self.tool, self.client, "fixture", cancel_event=cancel)
        self.client.exec_command.assert_not_called()

    def test_cancel_during_command_verifies_group_stopped(self):
        cancel = threading.Event()
        self.channel.recv_ready.side_effect = lambda: (cancel.set() or False)
        with self.assertRaises(main.PipelineCancelled):
            main.DeployTool._remote_exec(self.tool, self.client, "fixture", cancel_event=cancel,
                                        process_group_file="/fixture/command.pgid")
        self.tool._terminate_remote_process_group.assert_called_once_with(self.client, "/fixture/command.pgid")
        self.channel.close.assert_called_once()

    def test_idle_timeout_stops_group_without_waiting_for_remote_exit(self):
        clock = iter(range(100))
        with patch("main.time.monotonic", side_effect=lambda: next(clock)), patch("main.time.sleep"):
            with self.assertRaisesRegex(TimeoutError, "超时"):
                main.DeployTool._remote_exec(self.tool, self.client, "fixture", timeout_seconds=100,
                                            idle_timeout_seconds=2, process_group_file="/fixture/group")
        self.tool._terminate_remote_process_group.assert_called_once()

    def test_total_timeout_applies_even_with_fresh_output(self):
        self.channel.recv_ready.side_effect = [True] * 5 + [False]
        self.channel.recv.return_value = b"still working\n"
        clock = iter(range(100))
        with patch("main.time.monotonic", side_effect=lambda: next(clock)), patch("main.time.sleep"):
            with self.assertRaisesRegex(TimeoutError, "超时"):
                main.DeployTool._remote_exec(self.tool, self.client, "fixture", timeout_seconds=3,
                                            idle_timeout_seconds=100, process_group_file="/fixture/group")
        self.tool._terminate_remote_process_group.assert_called_once()

    def test_stop_failure_is_unknown_instead_of_cancelled(self):
        cancel = threading.Event()
        self.channel.recv_ready.side_effect = lambda: (cancel.set() or False)
        self.tool._terminate_remote_process_group.side_effect = TimeoutError("fixture unavailable")
        with self.assertRaisesRegex(RuntimeError, "无法确认.*停止") as caught:
            main.DeployTool._remote_exec(self.tool, self.client, "fixture", cancel_event=cancel,
                                        process_group_file="/fixture/group")
        self.assertNotIsInstance(caught.exception, main.PipelineCancelled)
        self.channel.close.assert_called_once()

    def test_cancelled_task_does_not_claim_an_unoccupied_target(self):
        coordinator = main.DeploymentTargetCoordinator()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(main.PipelineCancelled):
            coordinator.acquire(object(), "fixture", cancel)
        self.assertFalse(coordinator._owners)

    def test_uncertain_target_stays_blocked_after_owner_releases(self):
        coordinator = main.DeploymentTargetCoordinator()
        first = object()
        coordinator.acquire(first, "fixture")
        coordinator.block(first, "fixture", "无法确认远端停止，请检查服务器")
        coordinator.release(first, "fixture")
        with self.assertRaisesRegex(RuntimeError, "无法确认"):
            coordinator.acquire(object(), "fixture")
        coordinator.acquire(object(), "unrelated-target")

    def test_batch_cancel_while_waiting_never_connects_to_ssh(self):
        coordinator = main.DeploymentTargetCoordinator()
        cancel = threading.Event()
        environment = dict(host="fixture-host", port="22", yaml_dir="/yaml", yaml_file="etm.yaml")
        key = main.DeployTool._deployment_target_key("SSH 环境部署", environment)
        coordinator.acquire(object(), key)
        owner = SimpleNamespace(cancel_event=cancel,
                                window_manager=SimpleNamespace(batch_coordinator=None, target_coordinator=coordinator),
                                _deployment_target_key=main.DeployTool._deployment_target_key)
        owner.emit = lambda target, message: cancel.set() if "安全队列" in str(message) else None
        owner._wait_for_deployment_target = lambda *a, **kw: main.DeployTool._wait_for_deployment_target(owner, *a, **kw)
        owner._release_deployment_target = lambda target: coordinator.release(owner, target)
        with patch("main.paramiko.SSHClient") as factory:
            with self.assertRaises(main.PipelineCancelled):
                main.DeployTool._execute_deploy_batch(owner, [dict(owner=owner, package="etmbackend:new", environment=environment)])
        factory.return_value.connect.assert_not_called()

    def test_missing_exit_status_is_unknown_not_an_ordinary_command_failure(self):
        self.channel.exit_status_ready.side_effect = None
        self.channel.exit_status_ready.return_value = True
        self.channel.recv_exit_status.return_value = -1
        with self.assertRaises(main.RemoteCommandUncertain):
            main.DeployTool._remote_exec(self.tool, self.client, "fixture")

    def test_blocked_target_does_not_contaminate_other_targets_in_batch(self):
        coordinator = main.DeploymentTargetCoordinator()
        owner = SimpleNamespace(cancel_event=threading.Event(), emit=Mock(),
                                window_manager=SimpleNamespace(batch_coordinator=None, target_coordinator=coordinator),
                                _deployment_target_key=main.DeployTool._deployment_target_key)
        owner._wait_for_deployment_target = lambda *a, **kw: main.DeployTool._wait_for_deployment_target(owner, *a, **kw)
        owner._release_deployment_target = lambda key: coordinator.release(owner, key)
        environments = [dict(host="fixture", port="22", yaml_dir="/yaml", yaml_file=name) for name in ("a.yaml", "z.yaml")]
        keys = [main.DeployTool._deployment_target_key("SSH 环境部署", env) for env in environments]
        coordinator.block(object(), keys[1], "fixture unknown remote state")
        entries = [dict(owner=owner, environment=env, package=package)
                   for env, package in zip(environments, ("etmbackend:new", "escbackend:new"))]
        with patch("main.paramiko.SSHClient") as factory:
            with self.assertRaises(main.RemoteCommandUncertain):
                main.DeployTool._execute_deploy_batch(owner, entries)
        factory.return_value.connect.assert_not_called()
        coordinator.acquire(object(), keys[0])
        with self.assertRaises(main.RemoteCommandUncertain):
            coordinator.acquire(object(), keys[1])

    def test_launch_timeout_is_unknown_and_does_not_report_cancelled(self):
        self.client.exec_command.side_effect = TimeoutError("fixture launch timeout")
        with self.assertRaises(main.RemoteCommandUncertain):
            main.DeployTool._remote_exec(self.tool, self.client, "fixture")

    def test_server_without_request_ack_is_bounded_for_exec_pty_and_sftp(self):
        import paramiko
        for request in (lambda channel: channel.exec_command("fixture"),
                        lambda channel: channel.get_pty(),
                        lambda channel: channel.invoke_subsystem("sftp")):
            with self.subTest(request=request):
                channel = paramiko.Channel(1)
                channel.active = True
                channel.remote_chanid = 1
                channel.transport = SimpleNamespace(
                    _send_user_message=lambda _message: None,
                    get_exception=lambda: None,
                )
                client = SimpleNamespace(close=channel.close)
                self.addCleanup(channel.close)
                started = time.perf_counter()
                with self.assertRaises(TimeoutError):
                    with main.ssh_request_deadline(client, timeout=0.02):
                        request(channel)
                self.assertLess(time.perf_counter() - started, 1)
                self.assertTrue(channel.closed)

    def test_stop_confirmation_rejects_disconnected_channel_or_invalid_pid_file(self):
        for status in (-1, 2, 3):
            with self.subTest(status=status):
                owner = SimpleNamespace(_quiet_remote_status=Mock(side_effect=[0, status]))
                with self.assertRaisesRegex(RuntimeError, "确认"):
                    main.DeployTool._terminate_remote_process_group(owner, self.client, "/fixture/group")

    def test_stop_confirmation_accepts_only_absent_process_group(self):
        owner = SimpleNamespace(_quiet_remote_status=Mock(side_effect=[0, 1]))
        main.DeployTool._terminate_remote_process_group(owner, self.client, "/fixture/group")


if __name__ == "__main__":
    unittest.main()
