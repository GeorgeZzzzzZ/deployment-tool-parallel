import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests
from shiboken6 import isValid

import main
from ops_api import OpsApiClient
from test_ops_session import OfflineOpsSession


class CloudSessionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        store = SimpleNamespace(data={
            "jenkins": {"url": "", "username": "", "password": "", "values": {}},
            "environments": [], "dashboards": [],
        }, save=Mock())
        self.tool = main.DeployTool(store=store)
        self.clock = patch("ops_api.time.monotonic", return_value=1000).start()
        self.transport = OfflineOpsSession()
        self.api = OpsApiClient(session=self.transport)
        self.settings = {
            "url": "https://offline.example", "username": "offline-user",
            "password_plain": "synthetic-password",
        }
        self.tool.cloud_url.setText(self.settings["url"])
        self.tool.cloud_username.setText(self.settings["username"])
        self.tool.cloud_password.setText(self.settings["password_plain"])
        with patch.object(main, "OpsApiClient", return_value=self.api):
            self.tool._ensure_cloud_login(self.settings)
        self.app.processEvents()
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        thread = getattr(self.tool, "_ops_session_thread", None)
        if thread:
            thread.join(3)
        if isValid(self.tool):
            self.tool._task_runtime_active = False
            self.tool.shutdown_resources()
            self.tool.close()
        self.app.processEvents()
        patch.stopall()

    def wait_until(self, predicate):
        deadline = time.perf_counter() + 3
        while not predicate() and time.perf_counter() < deadline:
            self.app.processEvents()
            threading.Event().wait(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), "Background session operation did not finish")

    def start_maintenance(self):
        self.assertTrue(hasattr(self.tool, "_maintain_cloud_session"),
                        "Idle Ops session maintenance is missing")
        self.clock.return_value = 1301
        self.tool._maintain_cloud_session()

    def block_analysis(self):
        entered = threading.Event()
        request = self.transport.request

        def blocked(method, url, **kwargs):
            if url.endswith("/token/analyze"):
                entered.set()
                if not self.release.wait(3):
                    raise AssertionError("Test did not release token analysis")
            return request(method, url, **kwargs)

        self.transport.request = blocked
        return entered

    def test_hidden_workspace_timer_recovers_an_expired_idle_session(self):
        self.assertTrue(hasattr(self.tool, "_ops_session_timer"),
                        "No timer maintains hidden workspace sessions")
        self.transport.responses["/api/iam/v2/identity/token/analyze"] = [(401, {})]
        self.clock.return_value = 1301
        self.tool.hide()
        self.tool._ops_session_timer.setInterval(10)
        self.wait_until(lambda: self.transport.login_count == 2)
        self.assertTrue(self.api.authenticated)
        self.assertFalse(self.tool.has_active_work())
        self.assertIs(self.tool.ops_api, self.api)

    def test_same_identity_recovers_invalid_session_without_replacing_client(self):
        self.api.authenticated = False
        with patch.object(main, "OpsApiClient", side_effect=AssertionError("Replaced client")):
            result = self.tool._ensure_cloud_login(self.settings)
        self.assertIs(result, self.api)
        self.assertTrue(self.api.authenticated)
        self.assertEqual(self.transport.login_count, 2)

    def test_repeated_timer_ticks_do_not_block_ui_or_spawn_duplicate_workers(self):
        entered = self.block_analysis()
        self.start_maintenance()
        self.wait_until(entered.is_set)
        thread = self.tool._ops_session_thread
        for _ in range(5):
            self.tool._maintain_cloud_session()
        self.assertIs(self.tool._ops_session_thread, thread)
        self.assertTrue(thread.daemon)
        self.assertFalse(self.tool.has_active_work())
        self.assertFalse(self.tool._workers)
        self.release.set()
        self.wait_until(lambda: not thread.is_alive())
        self.assertEqual(len(self.transport.calls_to("/api/iam/v2/identity/token/analyze")), 1)

    def test_background_failure_does_not_mark_deployment_failed_or_replace_phase(self):
        self.tool._task_runtime_active = True
        self.tool._task_phase = "正在上传部署包"
        self.tool._task_state = "running"
        self.tool.cloud_status_label.setText("正在执行云端部署...")
        self.transport.responses["/api/iam/v2/identity/token/analyze"] = [requests.Timeout()]
        self.start_maintenance()
        self.wait_until(lambda: not self.tool._ops_session_thread.is_alive())
        self.assertFalse(self.tool._task_failed)
        self.assertEqual(self.tool._task_state, "running")
        self.assertEqual(self.tool._task_phase, "正在上传部署包")
        self.assertEqual(self.tool.cloud_status_label.text(), "正在执行云端部署...")
        self.assertIn("会话", self.tool.cloud_log.toPlainText())

    def test_account_edit_ignores_old_background_result_and_stops_old_maintenance(self):
        entered = self.block_analysis()
        self.transport.responses["/api/iam/v2/identity/token/analyze"] = [requests.Timeout()]
        self.start_maintenance()
        self.wait_until(entered.is_set)
        thread = self.tool._ops_session_thread
        self.tool.cloud_username.setText("another-user")
        self.tool._invalidate_cloud_account()
        before = self.tool.cloud_log.toPlainText()
        self.release.set()
        self.wait_until(lambda: not thread.is_alive())
        self.assertIsNone(self.tool.ops_api)
        self.assertEqual(self.tool.cloud_log.toPlainText(), before)
        self.tool._maintain_cloud_session()
        self.assertEqual(self.transport.login_count, 1)

    def test_account_edit_during_login_cannot_restore_previous_account(self):
        self.tool.ops_api = None
        self.tool._ops_api_identity = None
        self.tool.cloud_status_label.setText("正在登录...")
        entered = threading.Event()
        client = OpsApiClient(session=OfflineOpsSession())
        login = client.login

        def blocked_login(*args):
            entered.set()
            self.release.wait(3)
            return login(*args)

        client.login = blocked_login
        errors = []

        def login_old_account():
            try:
                self.tool._ensure_cloud_login(self.settings)
            except Exception as exc:
                errors.append(exc)

        with patch.object(main, "OpsApiClient", return_value=client):
            thread = threading.Thread(target=login_old_account, daemon=True)
            thread.start()
            self.wait_until(entered.is_set)
            self.tool.cloud_username.setText("another-user")
            self.tool._invalidate_cloud_account()
            self.release.set()
            thread.join(3)
        self.app.processEvents()
        self.assertIsNone(self.tool.ops_api)
        self.assertTrue(errors, "Stale login must stop its caller")
        self.assertNotEqual(self.tool.cloud_status_label.text(), "已登录")

    def test_shutdown_stops_timer_without_waiting_for_background_network(self):
        entered = self.block_analysis()
        self.start_maintenance()
        self.wait_until(entered.is_set)
        thread = self.tool._ops_session_thread
        self.tool.shutdown_resources()
        self.assertFalse(self.tool._ops_session_timer.isActive())
        self.assertFalse(self.tool._workers)
        before = self.tool.cloud_log.toPlainText()
        self.release.set()
        self.wait_until(lambda: not thread.is_alive())
        self.assertEqual(self.tool.cloud_log.toPlainText(), before)

    def test_deleting_workspace_during_maintenance_leaves_no_running_qthread(self):
        entered = self.block_analysis()
        self.start_maintenance()
        self.wait_until(entered.is_set)
        thread = self.tool._ops_session_thread
        self.assertFalse(self.tool._workers)
        self.tool.close()
        self.app.sendPostedEvents(None, main.QEvent.DeferredDelete)
        self.assertFalse(isValid(self.tool))
        self.release.set()
        self.wait_until(lambda: not thread.is_alive())

    def test_same_identity_validation_error_propagates_without_erasing_client(self):
        self.clock.return_value = 1301
        self.transport.responses["/api/iam/v2/identity/token/analyze"] = [(403, {})]
        with self.assertRaisesRegex(main.OpsApiError, "403"):
            self.tool._ensure_cloud_login(self.settings)
        self.assertIs(self.tool.ops_api, self.api)
        self.assertTrue(self.api.authenticated)

    def test_queued_request_for_previous_account_is_rejected_after_edit(self):
        self.tool.cloud_username.setText("another-user")
        self.tool._invalidate_cloud_account()
        with self.assertRaisesRegex(main.OpsApiError, "登录信息已变更"):
            self.tool._ensure_cloud_login(self.settings)
        self.assertIsNone(self.tool.ops_api)
        self.assertEqual(self.transport.login_count, 1)

    def test_cancellation_during_cloud_validation_prevents_deployment_post(self):
        cancel = threading.Event()
        package = "etmbackend-5.3.0:5.3.0.1001.10400"
        settings = {"project": "etm", "deploy_type": "etm 后端", "cloud": "Huawei",
                    "region": "测试", "package": package}
        target = {"id": 1, "cloud": "Huawei", "api_area": "test", "appId": "etm",
                  "typeId": "backend", "deploymentName": "etm"}
        tool = SimpleNamespace(_ensure_cloud_login=lambda _: self.api, emit=Mock())
        submissions = []

        def submit(method, path, **kwargs):
            kwargs["before_request"]()
            submissions.append((method, path))
            return {}

        with (
            patch.object(self.api, "resolve_target", return_value=target),
            patch.object(self.api, "verify_package", side_effect=lambda *_: cancel.set()),
            patch.object(self.api, "_deployment_records", side_effect=[[], [{"imageName": package}]]),
            patch.object(self.api, "_bff", side_effect=submit),
        ):
            with self.assertRaises(main.PipelineCancelled):
                main.DeployTool._run_cloud_deploy(tool, settings, cancel)
        self.assertEqual(submissions, [])


if __name__ == "__main__":
    unittest.main()
