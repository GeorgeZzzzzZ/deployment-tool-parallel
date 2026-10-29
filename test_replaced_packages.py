import io
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from ops_api import OpsApiClient
from PySide6.QtTest import QTest


OLD = "etmbackend-hbxx-5.2.0:5.2.0.1001.10001"
NEW = "etmbackend-hbxx-5.2.0:5.2.0.1001.10002"


def resource(kind, uid, owner=None, namespace="etm"):
    result = {"kind": kind, "metadata": {"uid": uid, "name": uid, "namespace": namespace}}
    if owner:
        result["metadata"]["ownerReferences"] = [{"uid": owner, "controller": True}]
    return result


def pod(uid, owner, image=OLD, namespace="etm"):
    result = resource("Pod", uid, owner, namespace)
    result["spec"] = {"containers": [{"name": "app", "image": NEW}]}
    result["status"] = {"phase": "Running", "containerStatuses": [
        {"name": "app", "image": "registry.example/industry/" + image,
         "state": {"running": {"startedAt": "2026-10-26T08:00:00Z"}}}
    ]}
    return result


class CaptureTests(unittest.TestCase):
    def capture(self, **kwargs):
        self.assertTrue(hasattr(main, "ReplacedPackageCapture"), "缺少异步旧包记录")
        return main.ReplacedPackageCapture(**kwargs)

    def test_blocked_query_does_not_block_freeze_and_late_result_is_discarded(self):
        capture = self.capture()
        entered, release = threading.Event(), threading.Event()
        def read(cancel):
            entered.set()
            release.wait(2)
            return [OLD]
        thread = capture.start(NEW, read)
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(capture.snapshot()["text"], "获取中…")
            capture.freeze()
            self.assertEqual(capture.snapshot()["text"], "未获取")
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(capture.snapshot()["packages"], [])

    def test_completed_result_survives_freeze_and_copies_only_package_numbers(self):
        capture = self.capture()
        thread = capture.start(NEW, lambda cancel: ["registry.example/" + OLD, OLD])
        thread.join(2)
        capture.freeze()
        self.assertEqual(capture.snapshot()["packages"], [OLD])
        self.assertEqual(capture.snapshot()["text"], OLD)

    def test_timeout_and_error_are_unknown_without_raw_error_details(self):
        capture = self.capture(timeout=0)
        capture.start(NEW, lambda cancel: [OLD]).join(2)
        self.assertEqual(capture.snapshot()["text"], "未获取")
        capture = self.capture()
        def fail(cancel):
            raise RuntimeError("FAKE_SECRET")
        capture.start(NEW, fail).join(2)
        self.assertEqual(capture.snapshot()["text"], "未获取")
        self.assertNotIn("FAKE_SECRET", str(capture.snapshot()))

    def test_batch_keeps_each_target_and_reports_partial_failure(self):
        capture = self.capture()
        capture.start(NEW, lambda cancel: [OLD]).join(2)
        capture.start("escbackend:new", lambda cancel: []).join(2)
        capture.freeze()
        self.assertEqual(capture.snapshot()["packages"], [OLD])
        self.assertIn("部分未获取", capture.snapshot()["text"])

    def test_reassembled_target_replaces_previous_read_and_isolates_old_worker(self):
        capture = self.capture()
        release = threading.Event()
        first = capture.start(NEW, lambda cancel: (release.wait(2), [OLD])[1])
        second = capture.start(NEW, lambda cancel: [NEW])
        second.join(2)
        release.set()
        first.join(2)
        self.assertEqual(capture.snapshot()["packages"], [NEW])


class SshRunningPackageTests(unittest.TestCase):
    def setUp(self):
        self.workload = resource("Deployment", "deploy")
        self.workload["spec"] = {"template": {"spec": {"containers": [
            {"name": "app", "image": NEW}, {"name": "metrics", "image": "metrics:1"}
        ]}}}
        self.items = [resource("ReplicaSet", "rs-old", "deploy"), pod("pod-old", "rs-old")]
        self.commands = []

    def query(self):
        self.assertTrue(hasattr(main, "read_ssh_running_packages"), "缺少运行包号查询")
        def execute(command, **kwargs):
            self.commands.append(command)
            value = self.workload if " -f " in command else {"kind": "List", "items": self.items}
            output = io.BytesIO(json.dumps(value).encode())
            output.channel = SimpleNamespace(recv_exit_status=lambda: 0)
            return io.BytesIO(), output, io.BytesIO()
        client = SimpleNamespace(exec_command=execute)
        return main.read_ssh_running_packages(client, {"yaml_dir": "/data/yaml/app/backend", "yaml_file": "etm.yaml"}, NEW, threading.Event())

    def test_actual_running_image_wins_over_yaml_and_pod_spec(self):
        self.assertEqual(self.query(), ["registry.example/industry/" + OLD])
        self.assertTrue(all("kubectl get " in command for command in self.commands))
        self.assertTrue(all("--request-timeout=" in command for command in self.commands))

    def test_owner_namespace_container_and_running_state_are_all_required(self):
        self.items.extend([
            pod("unrelated", "another-rs", "etmbackend:wrong"),
            pod("other-namespace", "rs-old", "etmbackend:wrong", "other"),
            pod("waiting", "rs-old", "etmbackend:waiting"),
        ])
        self.items[-1]["status"]["containerStatuses"][0]["state"] = {"waiting": {}}
        self.items[1]["status"]["containerStatuses"].append(
            {"name": "metrics", "image": "etmbackend:sidecar", "state": {"running": {}}})
        self.assertEqual(self.query(), ["registry.example/industry/" + OLD])

    def test_rolling_update_returns_all_running_versions(self):
        self.items.extend([resource("ReplicaSet", "rs-new", "deploy"), pod("pod-new", "rs-new", NEW)])
        self.assertEqual(self.query(), ["registry.example/industry/" + OLD, "registry.example/industry/" + NEW])

    def test_no_running_container_does_not_fall_back_to_yaml(self):
        self.items[1]["status"]["containerStatuses"] = []
        self.assertEqual(self.query(), [])

    def test_direct_pod_manifest_reads_its_actual_status(self):
        self.workload = pod("direct", None)
        self.assertEqual(self.query(), ["registry.example/industry/" + OLD])

    def test_cancelled_lookup_runs_no_remote_command(self):
        self.assertTrue(hasattr(main, "read_ssh_running_packages"))
        cancel = threading.Event()
        cancel.set()
        client = Mock()
        with self.assertRaises(InterruptedError):
            main.read_ssh_running_packages(client, {}, NEW, cancel)
        client.exec_command.assert_not_called()


class CloudCutoffTests(unittest.TestCase):
    def test_lookup_starts_after_resolution_and_freezes_before_submission(self):
        api = OpsApiClient()
        events = []
        target = {"id": 1, "cloud": "Huawei", "api_area": "test", "appId": "etm",
                  "typeId": "backend", "deploymentName": "etm"}
        api.resolve_target = lambda settings: target
        api.verify_package = lambda *args: events.append("verify")
        api._deployment_records = Mock(side_effect=[[], [{"imageName": NEW}]])
        def submit(*args, **kwargs):
            kwargs["before_request"]()
            events.append("submit")
        api._bff = submit
        self.assertIn("before_submit", __import__("inspect").signature(api.deploy).parameters,
                      "部署提交前没有关闭查询窗口")
        result = api.deploy({"package": NEW},
                            on_target_resolved=lambda value: events.append(("lookup", value["id"])),
                            before_submit=lambda: events.append("freeze"))
        self.assertEqual(events, [("lookup", 1), "verify", "freeze", "submit"])
        self.assertTrue(result["verified"])

    def test_cloud_query_cannot_delay_submission_or_replace_old_record_after_submit(self):
        api = OpsApiClient()
        target = {"id": 1, "cloud": "Huawei", "api_area": "test", "area": "test",
                  "appId": "etm", "typeId": "backend", "deploymentName": "etm", "state": "prod"}
        api.resolve_target = lambda settings: target
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        api.verify_package = lambda *args: self.assertTrue(entered.wait(2))
        api._deployment_records = Mock(side_effect=[[], [{"imageName": NEW}]])
        capture = main.ReplacedPackageCapture()
        events = []
        tool = SimpleNamespace(_ensure_cloud_login=lambda settings: api, _replaced_packages=capture,
                               emit=lambda *args: events.append(args))
        def request(client, method, path, **kwargs):
            if path.endswith("/instance/data"):
                entered.set()
                release.wait(3)
                exited.set()
                return {"data": {"registry": "registry.example/" + NEW}}
            if path == "/api/v3/deployment/record":
                return {"data": [{"imageName": OLD}]}
            self.assertEqual(method, "POST")
            kwargs["before_request"]()
            self.assertEqual(capture.snapshot()["text"], "未获取")
            self.assertFalse(release.is_set(), "部署不应等待运行包号响应")
            return {}
        settings = {"project": "ETM", "deploy_type": "etm 后端", "cloud": "Huawei",
                    "region": "测试", "package": NEW}
        try:
            with patch.object(OpsApiClient, "_bff", request):
                result = main.DeployTool._run_cloud_deploy(tool, settings)
                self.assertTrue(result["verified"])
        finally:
            release.set()
            self.assertTrue(exited.wait(2))
        self.assertEqual(capture.snapshot()["packages"], [])


class DeploymentCutoffTests(unittest.TestCase):
    def test_single_ssh_query_uses_separate_connection_and_never_holds_apply(self):
        capture = main.ReplacedPackageCapture()
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        clients, commands = [], []
        def client_factory():
            client = Mock()
            client.open_sftp.return_value.open.side_effect = lambda *args: io.BytesIO(
                ("image: registry.example/" + OLD).encode())
            clients.append(client)
            return client
        def read(client, environment, package, cancel):
            entered.set()
            release.wait(3)
            exited.set()
            return [NEW]
        def remote(client, command):
            self.assertEqual(capture.snapshot()["packages"], [])
            self.assertEqual(capture.snapshot()["text"], "未获取")
            self.assertFalse(release.is_set())
            commands.append(command)
        tool = SimpleNamespace(_replaced_packages=capture, emit=lambda *args: None,
                               _remote_command_succeeded=lambda *args: entered.wait(2),
                               _sftp_update=lambda *args, **kwargs: None, _remote_exec=remote)
        data = {**main.DEFAULT_DEPLOY, "host": "fixture-only", "delete_before_apply": True}
        try:
            with patch("main.paramiko.SSHClient", client_factory), patch("main.connect_ssh_client"), \
                    patch("main.read_ssh_running_packages", read):
                main.DeployTool._execute_deploy(tool, data, NEW)
                self.assertEqual(len(clients), 2)
                self.assertIn("kubectl delete", commands[0])
                self.assertIn("kubectl apply", commands[1])
        finally:
            release.set()
            self.assertTrue(exited.wait(2))
        self.assertEqual(capture.snapshot()["packages"], [])

    def test_manual_batch_closes_all_lookups_before_first_delete(self):
        capture = main.ReplacedPackageCapture()
        release = threading.Event()
        threads, entries, commands = [], [], []
        tool = SimpleNamespace(_replaced_packages=capture, cancel_event=threading.Event(),
                               emit=lambda *args: None, _sftp_update=lambda *args, **kwargs: None)
        for product in ("etm", "esc"):
            data = {"yaml_dir": "/yaml", "yaml_file": product + ".yaml", "delete_before_apply": True,
                    "apply_command": "kubectl apply -f " + product + ".yaml"}
            entries.append({"owner": tool, "environment": data, "yaml_path": "/yaml/" + product + ".yaml",
                            "package": product + "backend:new"})
            threads.append(capture.start("/yaml/" + product + ".yaml",
                                         lambda cancel: (release.wait(3), [NEW])[1]))
        def remote(client, command):
            self.assertEqual(capture.snapshot()["text"], "未获取")
            self.assertFalse(release.is_set())
            commands.append(command)
        tool._remote_exec = remote
        try:
            main.DeployTool._apply_manual_deployment_batch(tool, None, None, entries)
            self.assertEqual(len(commands), 4)
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(capture.snapshot()["packages"], [])


class ReplacedPackageUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        store = SimpleNamespace(data={"jenkins": {"url": "", "username": "", "password": "", "values": {}},
                                      "environments": [], "dashboards": [], "last_package": ""}, save=Mock())
        with patch.object(main.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.manager = main.ApplicationWindowManager(self.app, store=store)
        self.task = self.manager.create_window()
        self.panel = self.manager.task_center

    def tearDown(self):
        self.manager._closing = True
        self.manager.close()
        self.manager.deleteLater()
        self.app.processEvents()

    def test_new_column_click_and_keyboard_copy_old_package_without_changing_progress(self):
        self.assertEqual(self.panel.tree.headerItem().text(4), "被替换包号")
        self.task._begin_tracked_task("SSH 部署", "环境", package=NEW)
        self.task._task_progress = 39
        self.task._replaced_packages.start(NEW, lambda cancel: [OLD]).join(2)
        self.panel.update_task(self.task)
        item = self.panel._items[self.task]
        self.assertEqual(item.text(4), OLD)
        self.assertEqual(item.text(5), "39%")
        self.panel._activate_item(item, 4)
        self.assertEqual(self.app.clipboard().text(), OLD)
        self.assertEqual(self.panel.copy_feedback.text(), "已复制被替换包号")
        self.panel.tree.setCurrentItem(item, 4)
        self.panel.tree.actions()[0].trigger()
        self.assertEqual(self.app.clipboard().text(), OLD)
        self.panel._activate_item(item, 1)
        self.assertEqual(self.app.clipboard().text(), NEW)

    def test_restart_clears_old_package_and_build_only_is_not_applicable(self):
        self.task._begin_tracked_task("SSH 部署", "环境", package=NEW)
        self.assertIn("replaced_packages", self.task.task_summary())
        previous = self.task._replaced_packages
        previous.start(NEW, lambda cancel: [OLD]).join(2)
        self.task._begin_tracked_task("Jenkins 打包", "环境")
        self.panel.update_task(self.task)
        item = self.panel._items[self.task]
        self.assertEqual(item.text(4), "不涉及")
        self.app.clipboard().setText("keep")
        self.panel._activate_item(item, 4)
        self.assertEqual(self.app.clipboard().text(), "keep")
        self.assertEqual(self.task.task_summary()["replaced_packages"], [])

    def test_real_click_switches_task_and_shortcut_keeps_the_old_package_column(self):
        self.task._begin_tracked_task("SSH 部署", "环境", package=NEW)
        self.task._replaced_packages.start(NEW, lambda cancel: [OLD, "etmbackend:older"]).join(2)
        self.panel.update_task(self.task)
        self.manager.create_window()
        self.manager.resize(1440, 900)
        self.panel.set_expanded(True)
        self.manager.show()
        self.app.processEvents()
        tree, item = self.panel.tree, self.panel._items[self.task]
        point = main.QPoint(tree.header().sectionViewportPosition(4) + 15,
                            tree.visualItemRect(item).center().y())
        QTest.mouseClick(tree.viewport(), main.Qt.LeftButton, pos=point)
        self.assertIs(self.manager.current_task, self.task)
        self.assertEqual(self.app.clipboard().text(), OLD + "\netmbackend:older")
        self.app.clipboard().setText("reset")
        tree.setFocus()
        self.app.processEvents()
        QTest.keyClick(tree, main.Qt.Key_C, main.Qt.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), OLD + "\netmbackend:older")


if __name__ == "__main__":
    unittest.main()
