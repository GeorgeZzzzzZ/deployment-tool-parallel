import copy
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from ops_api import OpsApiClient, OpsApiError


def target_options(project, cloud="Huawei"):
    return {
        "types": [f"{project.lower()} 前端", f"{project.lower()} 后端"],
        "regions": [f"{cloud} / 测试", f"{cloud} / 正式"],
        "targets": [
            {"id": f"{project}-{type_id}-{region}", "typeId": type_id,
             "region": region, "region_label": f"{cloud} / {region}",
             "deploy_type_label": f"{project.lower()} {label}"}
            for type_id, label in (("frontend", "前端"), ("backend", "后端"))
            for region in ("测试", "正式")
        ],
    }


class OpsOptionsCacheTests(unittest.TestCase):
    def setUp(self):
        self.api = OpsApiClient()
        self.api._fetch_projects = Mock(return_value=["esc", "etm"])
        self.api._fetch_target_options = Mock(side_effect=target_options)

    def test_projects_and_targets_query_once_then_refresh_explicitly(self):
        self.api.list_projects("Huawei").append("modified")
        self.assertEqual(self.api.list_projects("Huawei"), ["esc", "etm"])
        self.api._fetch_projects.assert_called_once()
        first = self.api.load_target_options("etm", "Huawei")
        first["targets"][0]["id"] = "modified"
        again = self.api.load_target_options("ETM", "Huawei")
        self.assertNotEqual(again["targets"][0]["id"], "modified")
        self.api._fetch_target_options.assert_called_once()
        self.api.list_projects("Huawei", refresh=True)
        self.api.load_target_options("etm", "Huawei", refresh=True)
        self.assertEqual(self.api._fetch_projects.call_count, 2)
        self.assertEqual(self.api._fetch_target_options.call_count, 2)

    def test_cloud_project_and_client_caches_are_isolated(self):
        for project, cloud in (("etm", "Huawei"), ("esc", "Huawei"), ("etm", "Ali")):
            self.api.load_target_options(project, cloud)
        self.assertEqual(self.api._fetch_target_options.call_count, 3)
        other = OpsApiClient()
        other._fetch_target_options = Mock(side_effect=target_options)
        other.load_target_options("etm", "Huawei")
        other._fetch_target_options.assert_called_once()

    def test_parallel_requests_share_one_initial_query(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda _: self.api.load_target_options("etm", "Huawei"), range(8)
            ))
        self.assertEqual(len(results), 8)
        self.api._fetch_target_options.assert_called_once()

    def test_refresh_failure_keeps_cache_and_success_removes_deleted_targets(self):
        original = self.api.load_target_options("etm", "Huawei")
        self.api._fetch_target_options.side_effect = OpsApiError("offline")
        with self.assertRaisesRegex(OpsApiError, "offline"):
            self.api.load_target_options("etm", "Huawei", refresh=True)
        self.assertEqual(self.api.load_target_options("etm", "Huawei"), original)
        updated = copy.deepcopy(original)
        updated["targets"] = [target for target in updated["targets"] if target["region"] == "测试"]
        self.api._fetch_target_options.side_effect = None
        self.api._fetch_target_options.return_value = updated
        self.api.load_target_options("etm", "Huawei", refresh=True)
        with self.assertRaisesRegex(OpsApiError, "未找到精确部署目标"):
            self.api.resolve_target({
                "project": "etm", "deploy_type": "etm 后端", "cloud": "Huawei", "region": "正式",
            })

    def test_failed_initial_query_can_be_retried(self):
        self.api._fetch_target_options.side_effect = [OpsApiError("offline"), target_options("etm")]
        with self.assertRaises(OpsApiError):
            self.api.load_target_options("etm", "Huawei")
        self.assertTrue(self.api.load_target_options("etm", "Huawei")["targets"])
        self.assertEqual(self.api._fetch_target_options.call_count, 2)


class CloudTargetUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        store = SimpleNamespace(data={
            "jenkins": {"url": "", "username": "", "password": "", "values": {}},
            "environments": [], "dashboards": [], "last_package": "escbackend-5.3.0:1",
        }, save=Mock())
        self.tool = main.DeployTool(store=store)
        self.pending = []
        self.tool.run_thread = lambda function, on_finished=None: self.pending.append((function, on_finished))
        self.app.processEvents()
        self.tool.cloud_url.setText("https://example.invalid")
        self.tool.cloud_username.setText("test-user")
        self.tool.cloud_password.setText("test-password")
        self.api = OpsApiClient()
        self.api._fetch_projects = Mock(return_value=["esc", "etm"])
        self.api._fetch_target_options = Mock(side_effect=target_options)
        self.tool._ensure_cloud_login = Mock(return_value=self.api)
        self.tool.jenkins = main.JenkinsClient("https://jenkins.example", "test-user", "test-token")
        self.tool._load_parameters = Mock()
        self.tool.build_cloud_region.set_values(["Huawei / 测试"])
        self.tool.cloud_region.set_values(["Huawei / 测试"])

    def tearDown(self):
        self.tool._closing = True
        self.tool.close()
        self.tool.deleteLater()
        self.app.processEvents()

    def drain(self, reverse=False):
        jobs, self.pending = self.pending, []
        for function, finished in reversed(jobs) if reverse else jobs:
            function()
            if finished:
                finished()
        self.app.processEvents()

    def select(self, project, leaf=None):
        root = main.QTreeWidgetItem(self.tool.job_tree, ["云应用打包"])
        module = main.QTreeWidgetItem(root, [f"16-{project.upper()}-模块"])
        path = ["cloud", project]
        item = module
        if leaf:
            item = main.QTreeWidgetItem(module, [leaf])
            path.append(leaf)
        item.setData(0, main.Qt.UserRole, {"path": path, "is_folder": not leaf, "url": "https://example.invalid/job"})
        self.tool._tree_select(item, 0)

    def test_module_and_leaf_update_both_panels_and_preserve_region(self):
        self.tool.build_cloud_type.set_values(["esc 后端"])
        self.select("etm")
        self.drain()
        self.assertEqual(self.tool.current_job_path, [])
        self.tool._load_parameters.assert_not_called()
        for widget in (self.tool.build_cloud_project, self.tool.cloud_project):
            self.assertEqual(widget.currentText(), "etm")
        self.assertEqual(self.tool.build_cloud_type.currentText(), "etm 后端")
        self.assertEqual(self.tool.build_cloud_region.currentText(), "Huawei / 测试")
        self.select("etm", "1-WEB")
        self.drain()
        self.assertEqual(self.tool.build_cloud_type.currentText(), "etm 前端")
        self.assertEqual(self.tool.cloud_type.currentText(), "etm 前端")
        self.tool._load_parameters.assert_called_once()
        self.api._fetch_projects.assert_called_once()
        self.assertEqual(self.api._fetch_target_options.call_count, 2)

    def test_latest_selection_wins_even_when_old_response_arrives_last(self):
        self.select("etm", "4-SERVICE-HUAWEI")
        self.select("esc", "1-WEB")
        self.drain(reverse=True)
        self.assertEqual(self.tool.build_cloud_project.currentText(), "esc")
        self.assertEqual(self.tool.cloud_project.currentText(), "esc")
        self.assertEqual(self.tool.build_cloud_type.currentText(), "esc 前端")
        self.assertFalse(self.tool.build_cloud_loading)

    def test_manual_changes_use_cache_and_refresh_queries_again(self):
        self.select("etm", "4-SERVICE-HUAWEI")
        self.drain()
        self.tool.build_cloud_type.setCurrentText("etm 前端")
        self.tool.build_cloud_type.activated.emit(0)
        self.drain()
        self.assertEqual(self.tool.build_cloud_type.currentText(), "etm 前端")
        self.tool.cloud_project.setCurrentText("esc")
        self.tool.cloud_project.activated.emit(0)
        self.drain()
        self.assertEqual(self.tool.cloud_project.currentText(), "esc")
        self.tool.cloud_project.setCurrentText("etm")
        self.tool.cloud_project.activated.emit(1)
        self.drain()
        self.assertEqual(self.api._fetch_target_options.call_count, 2)
        self.tool.cloud_refresh_button.click()
        self.drain()
        self.assertEqual(self.api._fetch_target_options.call_count, 4)
        self.assertEqual(self.api._fetch_projects.call_count, 2)

    def test_stale_error_does_not_interrupt_latest_selection(self):
        self.select("etm")
        old_id = self.tool._cloud_option_requests["build"]
        self.select("esc")
        with patch.object(self.tool.message_overlay, "show_message") as message:
            self.tool._on_cloud_options({"source": "build", "request_id": old_id, "error": "old failure"})
            message.assert_not_called()
            self.assertTrue(self.tool.build_cloud_loading)
        self.drain()

    def test_region_not_available_in_new_service_requires_manual_selection(self):
        self.tool.build_cloud_region.set_values(["Huawei / 已删除区域"])
        self.tool.cloud_region.set_values(["Huawei / 已删除区域"])
        self.select("etm")
        self.drain()
        self.assertEqual(self.tool.build_cloud_region.currentText(), "")
        self.assertEqual(self.tool.cloud_region.currentText(), "")
        self.assertEqual(self.tool.cloud_region.values(), ["Huawei / 测试", "Huawei / 正式"])

    def test_explicit_login_can_recover_an_expired_session(self):
        self.tool._ops_api_identity = ("old session",)
        with patch.object(self.tool, "_save_cloud_settings"):
            self.tool.login_cloud_platform()
        self.assertIsNone(self.tool._ops_api_identity)
        self.assertEqual(len(self.pending), 1)

    def test_package_defaults_to_jenkins_and_manual_input_is_submitted(self):
        self.assertEqual(self.tool.cloud_package.text(), "escbackend-5.3.0:1")
        self.tool._on_package("etmbackend-5.3.0:2")
        self.assertEqual(self.tool.cloud_package.text(), "etmbackend-5.3.0:2")
        package = "etmbackend-5.3.0:99"
        self.tool.cloud_package.setText(package)
        self.tool.apply_cloud_target_from_package()
        self.drain()
        self.assertEqual(self.tool.cloud_project.currentText(), "etm")
        self.assertEqual(self.tool.cloud_type.currentText(), "etm 后端")
        self.assertEqual(self.tool.package_input.text(), "etmbackend-5.3.0:2")
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes) as question, patch.object(
            self.tool, "_run_cloud_deploy_with_target_lock"
        ) as deploy, patch.object(self.tool, "_save_cloud_settings"):
            self.tool.start_cloud_deploy()
            self.drain()
        self.assertIn(package, question.call_args.args[2])
        self.assertEqual(deploy.call_args.args[0]["package"], package)

    def test_blank_manual_package_does_not_fall_back_to_jenkins(self):
        self.tool.cloud_package.clear()
        with patch.object(main.QMessageBox, "warning") as warning:
            self.tool.start_cloud_deploy()
        warning.assert_called_once()
        self.assertEqual(self.pending, [])

    def test_missing_project_fails_without_replacing_existing_options(self):
        self.select("etm")
        self.drain()
        values = self.tool.cloud_type.values()
        self.tool.cloud_project.setCurrentText("missing")
        with patch.object(self.tool.message_overlay, "show_message") as message:
            self.tool.refresh_cloud_options()
            self.drain()
        message.assert_called_once()
        self.assertEqual(self.tool.cloud_type.values(), values)
        self.assertFalse(self.tool.cloud_options_loading)

    def test_login_identity_change_uses_a_new_api_client(self):
        # Exercise the real session boundary without contacting Ops.
        settings = self.tool._cloud_settings_from_ui()
        with patch.object(main, "OpsApiClient") as factory:
            factory.side_effect = [Mock(authenticated=True), Mock(authenticated=True)]
            first = main.DeployTool._ensure_cloud_login(self.tool, settings)
            self.assertIs(first, main.DeployTool._ensure_cloud_login(self.tool, settings))
            other = main.DeployTool._ensure_cloud_login(self.tool, {**settings, "username": "another-user"})
        self.assertIsNot(first, other)
        self.assertEqual(factory.call_count, 2)


if __name__ == "__main__":
    unittest.main()
