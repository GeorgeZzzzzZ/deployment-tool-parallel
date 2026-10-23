import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import main
from test_package_trace import MemorySource, PACKAGE


class EnvironmentTraceInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        with patch.object(main.DeployTool, "_auto_login_jenkins"):
            self.tool = main.DeployTool(store=store, embedded=True)
        self.pending = []
        self.tool.run_thread = lambda function, on_finished=None: self.pending.append(function)
        self.tool.jenkins = SimpleNamespace(url="https://jenkins.example")
        self.tool.cloud_username.setText("fixture-user")
        self.tool.cloud_password.setText("fixture-password")
        self.regions = ["Huawei / 测试", "Huawei / 正式", "Huawei / 预正式"]
        self.seed_regions()

    def seed_regions(self):
        settings = self.tool._cloud_settings_from_ui()
        projects = ["pqm", "etm", "etmmobile"]
        for project in [""] + projects:
            types = [project + " 后端", project + " 前端"] if project else []
            self.tool._save_selection_cache("cloud_targets", self.tool._cloud_options_cache_key(
                {**settings, "project": project, "cloud": "Huawei"}), {
                    "projects": projects, "project": project, "types": types,
                    "regions_by_type": {service: list(self.regions) for service in types},
                }, save=False)

    def tearDown(self):
        self.tool.close()
        self.tool.deleteLater()
        self.app.processEvents()

    def select(self, service="3-SERVICE", product="PQM"):
        self.tool._sync_trace_job_browser()
        folder = self.tool._create_job_tree_item(None, [], {
            "name": "22-" + product + "-Module", "_class": "Folder",
        }, tree=self.tool.trace_job_browser.tree)
        item = self.tool._create_job_tree_item(folder, [folder.text(0)], {
            "name": service, "_class": "FreeStyleProject",
        }, tree=self.tool.trace_job_browser.tree)
        self.tool._select_trace_job(item, 0)

    def environment_mode(self):
        self.assertTrue(hasattr(self.tool, "trace_mode"), "Environment trace mode must be available")
        self.tool.trace_mode.setCurrentText("环境溯源")

    def test_environment_mode_uses_cached_nodes_and_keeps_tab1_independent(self):
        self.environment_mode()
        self.select()
        self.assertTrue(self.tool.trace_input.isHidden())
        self.assertEqual([self.tool.trace_region.itemText(i) for i in range(self.tool.trace_region.count())],
                         ["Huawei / 测试", "Huawei / 正式", "Huawei / 预正式"])
        self.assertEqual(self.tool._trace_environment_settings()["project"], "pqm")
        self.assertEqual(self.tool._trace_environment_settings()["deploy_type"], "pqm 后端")
        self.assertEqual(self.pending, [])
        self.assertEqual(self.tool.current_job_path, [])

    def test_production_region_can_be_selected_before_job_and_is_preserved(self):
        self.environment_mode()
        self.assertTrue(self.tool.trace_region.isEnabled())
        self.assertEqual(self.tool.trace_region.count(), 3)
        self.assertEqual(self.tool.trace_region.currentText(), "")
        self.tool.trace_region.setCurrentText("Huawei / 正式")
        self.select()
        self.assertEqual(self.tool.trace_region.currentText(), "Huawei / 正式")
        self.assertEqual(self.pending, [])

    def test_query_card_has_no_extra_environment_text_box(self):
        self.environment_mode()
        card = self.tool.trace_input.parentWidget()
        self.assertEqual(card.findChildren(main.QPlainTextEdit), [self.tool.trace_input])
        self.assertTrue(self.tool.trace_input.isHidden())

    def test_unsupported_production_region_does_not_fall_back_to_test_region(self):
        self.environment_mode()
        self.tool.trace_region.setCurrentText("Huawei / 正式")
        settings = {**self.tool._cloud_settings_from_ui(), "project": "pqm"}
        key = self.tool._cloud_options_cache_key(settings)
        value = self.tool._cached_selection("cloud_targets", key)
        value["regions_by_type"]["pqm 后端"] = ["Huawei / 测试"]
        self.tool._save_selection_cache("cloud_targets", key, value, save=False)
        self.select()
        self.assertEqual(self.tool.trace_region.currentText(), "")
        self.assertIn("重新选择", self.tool.trace_status.text())
        self.tool._ensure_cloud_login = Mock(return_value=SimpleNamespace(
            load_target_options=Mock(return_value={"targets": [
                {"typeId": "backend", "project": "pqm", "region_label": "Huawei / 测试"},
            ]}),
        ))
        self.tool._load_trace_regions(force=True)
        self.pending.pop()()
        self.assertTrue(self.tool.trace_region.isEnabled())
        self.assertEqual(self.tool.trace_region.currentText(), "")
        self.tool._start_package_trace()
        self.assertIsNone(self.tool._trace_request)
        self.assertEqual(self.pending, [])

    def test_uncached_regions_load_without_job_and_only_for_current_account(self):
        self.tool.cloud_username.setText("another-fixture-user")
        self.environment_mode()
        self.assertEqual(self.tool.trace_region.count(), 0)
        self.assertEqual(len(self.pending), 1)
        api = SimpleNamespace(
            list_projects=Mock(return_value=["etm"]),
            load_target_options=Mock(return_value={"types": ["etm 后端"], "targets": [
                {"deploy_type_label": "etm 后端", "region_label": "Huawei / 正式"},
            ]}),
        )
        self.tool._ensure_cloud_login = Mock(return_value=api)
        self.pending.pop()()
        self.assertTrue(self.tool.trace_region.isEnabled())
        self.assertEqual(self.tool.trace_region.currentText(), "")
        self.tool.trace_region.setCurrentText("Huawei / 正式")
        self.assertEqual(self.tool.trace_region.currentText(), "Huawei / 正式")
        api.list_projects.assert_called_once()
        self.assertEqual(self.tool._trace_job_path, [])

    def test_stale_unscoped_regions_are_not_saved_after_account_change(self):
        self.tool.cloud_username.setText("another-fixture-user")
        self.environment_mode()
        request = self.tool._trace_region_request
        self.tool.cloud_username.setText("third-fixture-user")
        with patch.object(self.tool, "_save_selection_cache") as save:
            self.tool._on_trace_regions({"request": request, "regions": ["Huawei / 正式"],
                                         "cache_entries": {"stale-key": {}}})
        save.assert_not_called()
        self.assertEqual(self.tool.trace_region.count(), 0)

    def test_app_maps_to_mobile_project_and_frontend(self):
        self.environment_mode()
        self.select("2-APP", "ETM")
        settings = self.tool._trace_environment_settings()
        self.assertEqual((settings["project"], settings["deploy_type"]), ("etmmobile", "etmmobile 前端"))

    def test_missing_job_or_region_does_not_query(self):
        self.environment_mode()
        self.tool._start_package_trace()
        self.assertEqual(self.pending, [])
        self.select()
        self.tool.trace_region.setCurrentIndex(-1)
        self.tool._start_package_trace()
        self.assertEqual(self.pending, [])
        self.assertIn("云区", self.tool.trace_status.text())

    def run_environment_query(self, *, no_match=False):
        self.environment_mode()
        self.select()
        self.tool.trace_region.setCurrentText("Huawei / 正式")
        source = MemorySource()
        source.pages = {} if no_match else {0: [source.build(3)]}
        api = SimpleNamespace(load_current_packages=Mock(return_value={
            "packages": ["runtime.example/industry/" + PACKAGE], "target": {}, "source": "instance",
        }))
        self.tool._ensure_cloud_login = Mock(return_value=api)
        def factory(_client, cancel):
            source.cancel = cancel
            return source
        with patch.object(main, "JenkinsHistorySource", side_effect=factory):
            self.tool._start_package_trace()
            self.pending.pop()()
        return api

    def test_runtime_package_is_shown_and_traced_across_registry_relocation(self):
        api = self.run_environment_query()
        self.assertEqual(self.tool.trace_results.topLevelItemCount(), 1)
        self.assertEqual(self.tool.trace_results.topLevelItem(0).text(0), PACKAGE)
        self.assertIn("runtime.example/industry/" + PACKAGE, self.tool.trace_details.toPlainText())
        self.assertIn("Huawei / 正式", self.tool.trace_details.toPlainText())
        self.assertIn("打包人", self.tool.trace_details.toPlainText())
        self.assertEqual(api.load_current_packages.call_args.args[0]["region"], "正式")
        self.assertTrue(self.tool.trace_search_button.isEnabled())

    def test_current_package_remains_visible_when_jenkins_has_no_match(self):
        self.run_environment_query(no_match=True)
        self.assertEqual(self.tool.trace_results.topLevelItemCount(), 0)
        self.assertIn(PACKAGE, self.tool.trace_details.toPlainText())
        self.tool._copy_package_trace()
        self.assertIn("runtime.example/industry/" + PACKAGE, self.app.clipboard().text())

    def test_cancel_before_worker_starts_skips_ops_and_keeps_deployment_untouched(self):
        self.environment_mode()
        self.select()
        self.tool.trace_region.setCurrentText("Huawei / 正式")
        self.tool._ensure_cloud_login = Mock()
        self.tool._start_package_trace()
        self.tool._cancel_package_trace()
        self.pending.pop()()
        self.tool._ensure_cloud_login.assert_not_called()
        self.assertFalse(self.tool.cancel_event.is_set())
        self.assertIsNone(self.tool._trace_request)
        self.assertIn("取消", self.tool.trace_status.text())

    def test_account_change_rejects_stale_environment_results(self):
        self.environment_mode()
        self.select()
        self.tool.trace_region.setCurrentText("Huawei / 正式")
        self.tool._start_package_trace()
        request = self.tool._trace_request
        self.tool.cloud_password.setText("new-fixture")
        self.assertTrue(request["cancel"].is_set())
        self.tool._on_package_trace({"request": request, "kind": "environment", "data": {
            "packages": [PACKAGE], "target": {}, "source": "instance",
        }})
        self.tool._on_package_trace({"request": request, "kind": "finished", "data": {}})
        self.assertEqual(self.tool.trace_details.toPlainText(), "")
        self.assertIsNone(self.tool._trace_request)

    def test_old_node_response_cannot_override_new_selection(self):
        self.environment_mode()
        self.tool.trace_region.setCurrentText("Huawei / 正式")
        self.tool._cached_cloud_options = Mock(return_value=None)
        self.select()
        first = self.tool._trace_region_request
        self.select(product="ETM")
        second = self.tool._trace_region_request
        self.tool._on_trace_regions({"request": second, "regions": ["Huawei / 正式"]})
        self.tool._on_trace_regions({"request": first, "regions": ["Huawei / 测试"]})
        self.assertEqual(self.tool.trace_region.currentText(), "Huawei / 正式")

    def test_switching_mode_clears_previous_environment_package(self):
        self.run_environment_query()
        self.tool.trace_mode.setCurrentText("包号溯源")
        self.assertFalse(self.tool.trace_input.isHidden())
        self.assertEqual(self.tool.trace_details.toPlainText(), "")
        self.assertEqual(self.tool.trace_results.topLevelItemCount(), 0)


if __name__ == "__main__":
    unittest.main()
