import threading
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

from main import DeployTool


class DashboardTargetTests(unittest.TestCase):
    def test_browser_import_failure_is_reported_as_dashboard_failure(self):
        tool = SimpleNamespace(emit=Mock())
        item = {"name": "测试", "url": "https://dashboard.example", "token": "test-token"}
        with patch("main.unprotect_secret", return_value="test-token"), patch(
            "main.importlib.import_module", side_effect=zlib.error("Error -3 while decompressing data: incorrect header check")
        ):
            DeployTool._open_dashboard_item(tool, item)
        events = tool.emit.call_args_list
        self.assertTrue(any(call.args[0] == "dashboard_fallback" for call in events))
        self.assertFalse(any(call.args[0] == "error" for call in events))
        self.assertEqual(sum(call.args[0] == "loading_start" for call in events), 1)
        self.assertEqual(sum(call.args[0] == "loading_stop" for call in events), 1)

    def test_pipeline_opens_the_final_package_product_without_changing_saved_config(self):
        dashboard = {"id": "shared", "name": "环境2", "url": "https://dashboard.example"}
        environment = {
            "name": "环境2", "host": "example.invalid", "port": "22",
            "yaml_dir": "/data/yaml/app/backend", "yaml_file": "etm.yaml",
            "apply_command": "kubectl apply -f etm.yaml",
        }
        cases = (
            ("pqmbackend-hbxx-5.2.0:5.2.0.1001.10002", "pqm"),
            ("escbackend-hbxx-5.2.0:5.2.0.1001.10004", "esc"),
            ("etmbackend-hbxx-5.2.0:5.2.0.1001.10022", "etm"),
            ("etmmobilefrontend-2.1.0:4.0.0.1000.12136", "etm"),
        )
        for package, expected in cases:
            with self.subTest(package=package):
                jenkins = Mock()
                jenkins.build.return_value = "https://jenkins.example/queue/item/1"
                jenkins.wait_for_build.return_value = (
                    {"result": "SUCCESS", "number": 1},
                    "naming to registry.example/industry/" + package + " done",
                )
                tool = SimpleNamespace(
                    jenkins=jenkins, cancel_event=threading.Event(), emit=Mock(),
                    _handle_build_output=Mock(), _handle_build_progress=Mock(),
                    _raise_if_deploy_cancelled=Mock(), _execute_deploy=Mock(),
                )

                DeployTool._run_pipeline(
                    tool, ["old-etm-job"], {}, "https://jenkins.example/job/old-etm-job/",
                    "SSH 环境部署", environment, dashboard, None,
                )

                opened = [call.args[1] for call in tool.emit.call_args_list
                          if call.args[0] == "open_dashboard_item"]
                self.assertEqual(opened, [{**dashboard, "namespace": expected}])
                self.assertNotIn("namespace", dashboard)
                self.assertEqual(environment["yaml_file"], "etm.yaml")

    def test_batch_opens_each_product_and_deduplicates_frontend_backend(self):
        dashboard = {"id": "shared", "name": "环境2", "url": "https://dashboard.example"}
        packages = (
            "etmbackend-hbxx-5.2.0:5.2.0.1001.10022",
            "pqmbackend-hbxx-5.2.0:5.2.0.1001.10002",
            "escbackend-hbxx-5.2.0:5.2.0.1001.10004",
            "pqmfrontend-5.2.0:4.0.2.3430",
        )
        owners = [SimpleNamespace(emit=Mock()) for _ in packages]
        entries = [
            {"owner": owner, "dashboard": dashboard, "package": package}
            for owner, package in zip(owners, packages)
        ]

        opened = DeployTool._open_batch_dashboards(entries)

        self.assertEqual(opened, 3)
        for owner, namespace in zip(owners, ("etm", "pqm", "esc")):
            owner.emit.assert_called_once_with(
                "open_dashboard_item", {**dashboard, "namespace": namespace}
            )
        owners[-1].emit.assert_not_called()
        self.assertNotIn("namespace", dashboard)

    def test_default_browser_and_login_fallback_keep_the_task_namespace(self):
        for namespace in ("pqm", "esc", "etm"):
            with self.subTest(namespace=namespace):
                item = {
                    "name": "环境2", "url": "https://dashboard.example/#/login",
                    "namespace": namespace,
                }
                tool = SimpleNamespace(_copy_dashboard_token=Mock())
                expected = "https://dashboard.example/#/pod?namespace=" + namespace
                with (
                    patch("main.sys.platform", "win32"),
                    patch("main.webbrowser.open") as browser,
                    patch("main.QMessageBox.warning"),
                ):
                    DeployTool._open_dashboard_url(tool, item)
                    self.assertEqual(browser.call_args.args, (expected,))
                    browser.reset_mock()
                    DeployTool._on_dashboard_fallback(tool, item, "自动登录失败")
                    browser.assert_called_once_with(expected)
                tool._copy_dashboard_token.assert_called_once_with(item)


if __name__ == "__main__":
    unittest.main()
