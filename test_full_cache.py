import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from ops_api import OpsApiClient, OpsApiError
from test_cloud_targets import target_options


class FullCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.windows = []
        self.pending = []
        self.store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        self.settings = {
            "url": "https://ops.example", "username": "builder", "password_plain": "test-password",
            "cloud": "Huawei", "project": "etm", "deploy_type": "etm 后端", "region": "测试",
        }
        self.client = main.JenkinsClient("https://jenkins.example", "builder", "test-token")
        self.client.session.get = Mock(return_value=Mock(
            ok=True, json=Mock(return_value={
                "authenticated": True, "anonymous": False, "name": "builder",
            }),
        ))

        def job(name, folder=False, display=None):
            return {
                "name": name, "displayName": display or name,
                "_class": "folder" if folder else "project",
                "url": f"https://jenkins.example/custom/{name}/",
            }

        self.jobs = {
            (): [job("cloud", True, "云应用打包"), job("empty", True), job("utility")],
            ("empty",): [],
            ("cloud",): [job("etm", True, "16-ETM-模块"), job("esc", True, "15-ESC-模块")],
            ("cloud", "etm"): [job("service", display="6-SERVICE"), job("web", display="1-WEB")],
            ("cloud", "esc"): [job("service", display="5-SERVICE")],
        }
        self.revision = "old"
        self.client.list_jobs = Mock(side_effect=lambda path: copy.deepcopy(self.jobs[tuple(path)]))
        self.client.get_parameters = Mock(side_effect=self.parameters)
        self.api = OpsApiClient()
        self.api._fetch_projects = Mock(return_value=["etm", "esc"])
        self.api._fetch_target_options = Mock(side_effect=target_options)
        self.tool = self.make_tool(self.store)
        self.store.save.reset_mock()

    def parameters(self, path, **kwargs):
        if path == ["utility"]:
            return []
        return [
            {"name": "GIT_BRANCH", "type": "GitParameterDefinition",
             "choices": [f"origin/{self.revision}"], "defaultParameterValue": {"value": f"origin/{self.revision}"}},
        ]

    def make_tool(self, store):
        with patch.object(main.DeployTool, "_auto_login_jenkins"):
            tool = main.DeployTool(store=store, embedded=True)
        tool.run_thread = lambda fn, on_finished=None: self.pending.append((fn, on_finished))
        tool.message_overlay.show_message = Mock()
        tool._select_build_summary = Mock()
        tool._cloud_settings_from_ui = Mock(side_effect=lambda: {
            **self.settings, "project": tool.cloud_project.currentText() or self.settings["project"],
        })
        tool._build_cloud_settings_from_ui = Mock(side_effect=lambda: {
            **self.settings, "project": tool.build_cloud_project.currentText() or self.settings["project"],
        })
        tool._ensure_cloud_login = Mock(return_value=self.api)
        tool.jenkins = self.client
        tool.j_url.setText(self.client.url)
        tool.j_user.setText("builder")
        tool.j_password.setText("test-token")
        self.windows.append(tool)
        self.app.processEvents()
        return tool

    def tearDown(self):
        for window in self.windows:
            window._closing = True
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def drain(self):
        pending, self.pending = self.pending, []
        for fn, finished in pending:
            fn()
            if finished:
                finished()
        self.app.processEvents()

    def login(self, tool=None):
        with patch.object(main, "JenkinsClient", return_value=self.client), patch.object(main, "protect_secret", return_value=""):
            (tool or self.tool).login_jenkins()

    def item(self, path, tool=None):
        tree = (tool or self.tool).job_tree
        parent = tree.invisibleRootItem()
        for name in path:
            parent = next(
                parent.child(index) for index in range(parent.childCount())
                if parent.child(index).data(0, main.Qt.UserRole)["path"][-1] == name
            )
        return parent

    def test_first_login_loads_only_tree_then_selected_task_is_cached(self):
        self.login()
        self.assertTrue(self.tool._job_cache_loading)
        self.assertFalse(self.tool.job_tree.isEnabled())
        self.drain()
        self.client.get_parameters.assert_not_called()
        self.tool._ensure_cloud_login.assert_not_called()
        self.assertNotIn("selection_cache", self.store.data)
        self.assertEqual(self.store.save.call_count, 2)
        self.assertTrue(self.tool.job_tree.isEnabled())
        self.assertEqual(self.tool.loading_count, 0)
        self.assertIn("点击任务后加载", self.tool.path_label.text())
        self.tool._tree_select(self.item(["cloud", "esc", "service"]), 0)
        self.drain()
        self.client.get_parameters.assert_called_once_with(
            ["cloud", "esc", "service"], job_url="https://jenkins.example/custom/service/"
        )
        self.assertEqual(len(self.store.data["selection_cache"]["parameters"]), 1)
        self.assertEqual(self.tool.param_vars["GIT_BRANCH"][1].values(), ["origin/old"])
        self.tool._tree_select(self.item(["cloud", "esc", "service"]), 0)
        self.assertEqual(self.pending, [])
        self.assertEqual(self.client.get_parameters.call_count, 1)

    def test_refresh_only_reloads_tree_and_preserves_existing_parameter_cache(self):
        self.login()
        self.drain()
        self.tool._tree_select(self.item(["cloud", "esc", "service"]), 0)
        self.drain()
        previous = copy.deepcopy(self.store.data["selection_cache"])
        self.client.get_parameters.reset_mock()
        self.api._fetch_target_options.reset_mock()
        self.tool.job_filter_input.setText("etm")
        self.tool._start_job_search()
        self.revision = "new"
        self.tool.refresh_job_cache()
        self.drain()
        self.client.get_parameters.assert_not_called()
        self.api._fetch_target_options.assert_not_called()
        self.assertEqual(self.client.list_jobs.call_count, 10)
        self.assertEqual(previous, self.store.data["selection_cache"])
        self.tool.job_filter_input.clear()
        self.tool._tree_select(self.item(["cloud", "esc", "service"]), 0)
        self.assertEqual(self.pending, [])
        self.assertEqual(self.tool.param_vars["GIT_BRANCH"][1].values(), ["origin/old"])

    def test_restart_reuses_partial_cache_without_background_prefetch(self):
        self.login()
        self.drain()
        self.tool._tree_select(self.item(["utility"]), 0)
        self.drain()
        restored = SimpleNamespace(data=json.loads(json.dumps(self.store.data)), save=Mock())
        other = self.make_tool(restored)
        self.client.list_jobs.reset_mock()
        self.client.get_parameters.reset_mock()
        self.api._fetch_target_options.reset_mock()
        self.login(other)
        self.assertEqual(len(self.pending), 1)
        self.drain()
        self.assertEqual(self.pending, [])
        self.client.list_jobs.assert_not_called()
        self.client.get_parameters.assert_not_called()
        self.api._fetch_target_options.assert_not_called()
        other._tree_select(self.item(["utility"], other), 0)
        self.assertEqual(self.pending, [])
        self.client.get_parameters.assert_not_called()
        other._tree_select(self.item(["cloud", "etm", "web"], other), 0)
        self.drain()
        self.client.get_parameters.assert_called_once_with(
            ["cloud", "etm", "web"], job_url="https://jenkins.example/custom/web/"
        )

    def test_existing_tree_without_parameter_cache_does_not_start_warmup(self):
        tree = self.tool._fetch_job_cache()
        self.tool._save_job_cache(tree)
        self.client.list_jobs.reset_mock()
        self.login()
        self.assertEqual(len(self.pending), 1)
        self.drain()
        self.assertEqual(self.pending, [])
        self.client.list_jobs.assert_not_called()
        self.client.get_parameters.assert_not_called()
        self.tool._ensure_cloud_login.assert_not_called()

    def test_failed_task_can_retry_on_click_without_blocking_other_tasks(self):
        self.login()
        self.drain()
        self.client.get_parameters.side_effect = [RuntimeError("offline"), self.parameters(["utility"])]
        item = self.item(["utility"])
        self.tool._tree_select(item, 0)
        self.drain()
        self.tool.message_overlay.show_message.assert_called_once()
        self.assertEqual(self.tool.loading_count, 0)
        self.assertIsNone(self.tool._cached_parameters(self.tool._active_parameter_key))
        self.tool._tree_select(item, 0)
        self.drain()
        self.assertEqual(self.client.get_parameters.call_count, 2)
        self.assertEqual(self.tool._cached_parameters(self.tool._active_parameter_key), [])

    def test_tree_refresh_failure_preserves_tree_and_parameter_snapshot(self):
        self.login()
        self.drain()
        self.tool._tree_select(self.item(["utility"]), 0)
        self.drain()
        previous = copy.deepcopy(self.store.data)
        self.client.list_jobs.side_effect = RuntimeError("tree offline")
        self.tool.refresh_job_cache()
        self.drain()
        self.assertEqual(previous, self.store.data)
        self.assertTrue(self.tool.job_tree.isEnabled())
        self.assertEqual(self.tool.loading_count, 0)

    def test_cloud_account_does_not_cause_jenkins_startup_to_prefetch(self):
        for password in ("", "test-password"):
            self.settings["password_plain"] = password
            self.login()
            self.drain()
        self.client.get_parameters.assert_not_called()
        self.tool._ensure_cloud_login.assert_not_called()

    def test_old_generation_cannot_publish_tree(self):
        self.login()
        old_fn, _ = self.pending.pop()
        self.tool._job_cache_generation += 1
        old_fn()
        self.assertIsNone(self.store.data["jenkins"].get("job_cache"))
        self.client.list_jobs.assert_not_called()
        self.client.get_parameters.assert_not_called()
        self.tool._on_job_cache_loaded(self.tool._job_cache_generation - 1, {})

    def test_selection_during_tree_load_does_not_fetch_parameters(self):
        self.login()
        item = main.QTreeWidgetItem(["service"])
        item.setData(0, main.Qt.UserRole, {"path": ["service"], "is_folder": False})
        self.tool._tree_select(item, 0)
        self.assertEqual(len(self.pending), 1)
        self.drain()
        self.client.get_parameters.assert_not_called()

    def test_cloud_login_and_refresh_warm_unselected_projects(self):
        self.tool._queue_cloud_options(self.settings, "build")
        self.drain()
        self.assertEqual(self.api._fetch_target_options.call_count, 2)
        self.tool._queue_cloud_options({**self.settings, "project": "esc", "deploy_type": "esc 前端"}, "tab")
        self.assertEqual(self.pending, [])
        self.assertEqual(self.tool.cloud_type.currentText(), "esc 前端")
        self.tool._queue_cloud_options(self.settings, "build", force=True)
        self.drain()
        self.assertEqual(self.api._fetch_target_options.call_count, 4)

    def test_one_cloud_project_failure_does_not_discard_other_projects(self):
        def fail_esc(project, cloud):
            if project == "esc":
                raise OpsApiError("not available")
            return target_options(project, cloud)

        self.api._fetch_target_options.side_effect = fail_esc
        self.tool._queue_cloud_options(self.settings, "build")
        self.drain()
        self.assertIn("未全部完成", self.tool.cloud_log.toPlainText())
        self.assertIsNotNone(self.tool._cached_cloud_options(self.settings, "build"))
        self.assertIsNone(self.tool._cached_cloud_options({**self.settings, "project": "esc"}, "tab"))
        self.api._fetch_target_options.side_effect = target_options
        self.api._fetch_target_options.reset_mock()
        self.client.get_parameters.reset_mock()
        self.tool._queue_cloud_options(self.settings, "build")
        self.drain()
        self.api._fetch_target_options.assert_called_once_with("esc", "Huawei")
        self.client.get_parameters.assert_not_called()
        self.assertTrue(self.tool._cloud_cache_is_complete(self.settings))

    def test_cloud_failure_outside_selected_project_reports_partial_cache_and_keeps_selection_usable(self):
        def fetch(project, cloud):
            if project != "etm":
                raise OpsApiError("offline")
            return target_options(project, cloud)

        self.api._fetch_target_options.side_effect = fetch
        self.tool._queue_cloud_options(self.settings, "build")
        self.drain()
        self.assertEqual(self.tool.build_cloud_type.currentText(), "etm 后端")
        self.assertFalse(self.tool.build_cloud_loading)
        self.assertIn("未全部完成", self.tool.cloud_log.toPlainText())
        self.tool.message_overlay.show_message.assert_called_once()

    def test_account_switch_rejects_completed_bulk_snapshot_even_with_same_generation(self):
        self.login()
        with patch.object(self.tool, "emit") as emitted:
            self.drain()
        result = next(call.args[1][1] for call in emitted.call_args_list if call.args[0] == "job_cache_loaded")
        self.tool.jenkins = main.JenkinsClient("https://another.example", "another", "test-token")
        self.tool._on_job_cache_loaded(self.tool._job_cache_generation, result)
        self.assertNotIn("selection_cache", self.store.data)

    def test_jenkins_parameter_query_uses_real_job_url_and_fetches_branch_choices(self):
        client = main.JenkinsClient("https://jenkins.example", "builder", "test-token")
        client.get_json = Mock(return_value={"property": [{"parameterDefinitions": [
            {"name": "GIT_BRANCH", "type": "GitParameterDefinition"},
        ]}]})
        client.get_git_parameter_choices = Mock(return_value=["origin/latest"])
        real_url = "https://jenkins.example/special/job/encoded%2Fname/"
        params = client.get_parameters(["name"], job_url=real_url)
        self.assertEqual(client.get_json.call_args.args[0], real_url.rstrip("/") + "/api/json")
        client.get_git_parameter_choices.assert_called_once_with(real_url.rstrip("/"), "GIT_BRANCH")
        self.assertEqual(params[0]["choices"], ["origin/latest"])


if __name__ == "__main__":
    unittest.main()
