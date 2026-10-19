import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from ops_api import OpsApiClient, OpsApiError
from test_cloud_targets import target_options


PARAMETERS = [
    {"name": "GIT_BRANCH", "type": "GitParameterDefinition", "choices": ["origin/main"],
     "defaultParameterValue": {"value": "origin/main"}},
    {"name": "folderName", "type": "StringParameterDefinition",
     "defaultParameterValue": {"value": "app_backend"}},
]


class SelectionCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.windows = []
        self.pending = []
        self.store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        self.tool = self.make_tool(self.store)
        self.client = main.JenkinsClient("https://jenkins.example", "builder", "test-token")
        self.client.get_parameters = Mock(side_effect=lambda _, **kwargs: copy.deepcopy(PARAMETERS))
        self.tool.jenkins = self.client
        self.api = OpsApiClient()
        self.api._fetch_projects = Mock(return_value=["etm", "esc"])
        self.api._fetch_target_options = Mock(side_effect=target_options)
        self.tool._ensure_cloud_login = Mock(return_value=self.api)
        self.settings = {
            "url": "https://ops.example", "username": "builder", "password_plain": "test-password",
            "project": "etm", "deploy_type": "etm 后端", "cloud": "Huawei", "region": "测试",
        }

    def make_tool(self, store):
        tool = main.DeployTool(store=store, embedded=True)
        tool.run_thread = lambda function, on_finished=None: self.pending.append((function, on_finished))
        tool._sync_cloud_target_from_job = Mock()
        # Live build status is separate from the parameter/target cache.
        tool._select_build_summary = Mock()
        tool.message_overlay.show_message = Mock()
        self.windows.append(tool)
        self.app.processEvents()
        return tool

    def tearDown(self):
        for tool in self.windows:
            tool._closing = True
            tool.close()
            tool.deleteLater()
        self.app.processEvents()

    def drain(self, reverse=False):
        pending, self.pending = self.pending, []
        for function, finished in reversed(pending) if reverse else pending:
            function()
            if finished:
                finished()
        self.app.processEvents()

    def select(self, name="service", tool=None, folder=False):
        tool = tool or self.tool
        item = main.QTreeWidgetItem(tool.job_tree, [name])
        item.setData(0, main.Qt.UserRole, {
            "path": [name], "url": f"https://jenkins.example/job/{name}/", "is_folder": folder,
        })
        tool._tree_select(item, 0)

    def load_cloud(self, settings=None, source="build", force=False):
        self.tool._queue_cloud_options(settings or self.settings, source, force=force)
        self.drain()

    def test_reselect_keeps_edits_and_uses_no_worker_or_loading_indicator(self):
        self.select()
        self.drain()
        entry = self.tool.param_vars["folderName"][1]
        entry.setText("user-entered")
        with patch.object(self.tool, "_on_loading_start") as loading:
            self.select()
        self.assertEqual(self.pending, [])
        self.assertIs(self.tool.param_vars["folderName"][1], entry)
        self.assertEqual(entry.text(), "user-entered")
        self.assertEqual(self.tool.loading_count, 0)
        self.client.get_parameters.assert_called_once_with(
            ["service"], job_url="https://jenkins.example/job/service/"
        )
        loading.assert_not_called()

    def test_switch_back_and_new_workspace_use_persisted_parameters(self):
        self.select("a")
        self.drain()
        self.select("b")
        self.drain()
        self.select("a")
        self.assertEqual(self.pending, [])
        restored = SimpleNamespace(data=json.loads(json.dumps(self.store.data)), save=Mock())
        other = self.make_tool(restored)
        other.jenkins = main.JenkinsClient("https://jenkins.example/", "builder", "test-token")
        other.jenkins.get_parameters = Mock(side_effect=AssertionError("unexpected request"))
        self.select("a", other)
        self.assertEqual(self.pending, [])
        self.assertEqual(other.param_vars["GIT_BRANCH"][1].values(), ["origin/main"])
        self.assertEqual(self.client.get_parameters.call_count, 2)

    def test_parameters_are_isolated_by_server_account_and_job(self):
        self.select()
        self.drain()
        for url, username in (("https://other.example", "builder"), (self.client.url, "another")):
            client = main.JenkinsClient(url, username, "test-token")
            client.get_parameters = Mock(return_value=[])
            self.tool.jenkins = client
            self.select()
            self.assertEqual(len(self.pending), 1)
            self.drain()
            client.get_parameters.assert_called_once()

    def test_empty_parameters_are_a_cache_hit_and_invalid_data_is_a_miss(self):
        self.client.get_parameters.side_effect = None
        self.client.get_parameters.return_value = []
        self.select()
        self.drain()
        self.select()
        self.assertEqual(self.pending, [])
        key = self.tool._active_parameter_key
        for invalid in ({}, [None], [{"name": "x", "choices": "broken"}]):
            self.store.data["selection_cache"]["parameters"][key] = invalid
            self.select()
            self.assertEqual(len(self.pending), 1)
            self.drain()
        self.store.data["selection_cache"]["version"] = -1
        self.select()
        self.assertEqual(len(self.pending), 1)

    def test_pending_requests_are_deduplicated_and_late_results_cannot_replace_selection(self):
        self.client.get_parameters.side_effect = lambda path, **kwargs: [
            {"name": path[0], "type": "StringParameterDefinition"}
        ]
        self.select("a")
        self.select("a")
        self.select("b")
        self.assertEqual(len(self.pending), 2)
        self.drain(reverse=True)
        self.assertEqual(list(self.tool.param_vars), ["b"])
        self.select("a")
        self.assertEqual(list(self.tool.param_vars), ["a"])
        self.assertEqual(self.pending, [])

    def test_folder_selection_and_relogin_reject_late_parameter_results(self):
        self.select()
        self.select("folder", folder=True)
        self.drain()
        self.assertEqual(self.tool.param_vars, {})
        self.select("other")
        self.tool.jenkins = main.JenkinsClient("https://other.example", "another", "test-token")
        self.tool.jenkins.get_parameters = Mock()
        self.drain()
        self.tool.jenkins.get_parameters.assert_not_called()
        self.assertEqual(self.tool.param_vars, {})

    def test_failed_parameter_fetch_is_retryable(self):
        self.client.get_parameters.side_effect = [RuntimeError("offline"), PARAMETERS]
        self.select()
        self.drain()
        self.assertEqual(self.tool._parameter_requests, {})
        self.assertEqual(self.tool.loading_count, 0)
        self.select()
        self.drain()
        self.assertIn("GIT_BRANCH", self.tool.param_vars)

    def test_uncached_job_disables_build_and_clears_previous_jobs_parameters(self):
        self.select("a")
        self.drain()
        self.assertTrue(self.tool.build_button.isEnabled())
        self.select("b")
        self.assertFalse(self.tool.build_button.isEnabled())
        self.assertEqual(self.tool.param_vars, {})
        self.drain()
        self.assertTrue(self.tool.build_button.isEnabled())
        self.select("folder", folder=True)
        self.select("b")
        self.assertTrue(self.tool.build_button.isEnabled())
        self.assertEqual(self.pending, [])

    def test_branch_refresh_updates_persistent_choices_without_resetting_other_inputs(self):
        self.select()
        self.drain()
        self.tool.param_vars["folderName"][1].setText("keep-me")
        self.client.get_git_parameter_choices = Mock(return_value=["origin/new"])
        self.tool.refresh_branch_choices("GIT_BRANCH")
        self.drain()
        self.assertEqual(self.tool.param_vars["folderName"][1].text(), "keep-me")
        self.select("other")
        self.drain()
        self.select()
        self.assertEqual(self.tool.param_vars["GIT_BRANCH"][1].values(), ["origin/new"])

    def test_password_defaults_are_not_added_to_metadata_cache(self):
        self.client.get_parameters.side_effect = None
        self.client.get_parameters.return_value = [
            {"name": "secret", "type": "PasswordParameterDefinition",
             "defaultParameterValue": {"value": "private-default"}},
        ]
        self.select()
        self.drain()
        self.assertNotIn("private-default", json.dumps(self.store.data["selection_cache"]))

    def test_manual_tree_refresh_failure_keeps_existing_parameters(self):
        self.select()
        self.drain()
        self.tool._job_cache_generation = 3
        self.tool._on_job_cache_error(3, "offline")
        self.select()
        self.assertEqual(self.pending, [])
        self.client.get_parameters.assert_called_once()

    def test_cloud_cache_hit_skips_login_workers_and_loading_for_all_services(self):
        self.load_cloud()
        self.tool._ensure_cloud_login.reset_mock()
        for source in ("build", "tab"):
            with patch.object(self.tool, "_set_build_cloud_loading", wraps=self.tool._set_build_cloud_loading) as build_loading:
                self.tool._queue_cloud_options({**self.settings, "deploy_type": "etm 前端"}, source)
                self.assertFalse(any(call.args[0] for call in build_loading.call_args_list))
        self.assertEqual(self.pending, [])
        self.tool._ensure_cloud_login.assert_not_called()
        self.assertEqual(self.tool.build_cloud_type.currentText(), "etm 前端")
        self.assertEqual(self.tool.cloud_type.currentText(), "etm 前端")
        self.assertEqual(self.api._fetch_target_options.call_count, 2)
        self.assertFalse(self.tool.cloud_deployment_card.isHidden())

    def test_refreshed_project_list_updates_other_cached_projects_and_drops_deleted_ones(self):
        self.load_cloud()
        self.load_cloud({**self.settings, "project": "esc"})
        self.api._fetch_projects.return_value = ["esc", "new"]
        self.load_cloud({**self.settings, "project": "esc"}, force=True)
        self.tool._queue_cloud_options(self.settings, "build")
        self.assertEqual(len(self.pending), 1)
        self.drain()
        self.tool.message_overlay.show_message.assert_called()
        key = self.tool._cloud_options_cache_key(self.settings)
        self.assertNotIn(key, self.store.data["selection_cache"]["cloud_targets"])

    def test_explicit_login_bypasses_saved_cloud_cache(self):
        self.load_cloud()
        with patch.object(self.tool, "_cloud_settings_from_ui", return_value=self.settings), patch.object(self.tool, "_save_cloud_settings"):
            self.tool.login_cloud_platform()
        self.assertEqual(len(self.pending), 1)
        self.drain()
        self.assertEqual(self.api._fetch_projects.call_count, 2)
        self.assertEqual(self.api._fetch_target_options.call_count, 4)

    def test_cloud_cache_identity_includes_platform_account_cloud_and_project(self):
        self.load_cloud()
        for changed in (
            {"url": "https://other.example"}, {"username": "another"},
            {"cloud": "Ali"},
        ):
            self.tool._queue_cloud_options({**self.settings, **changed}, "build")
            self.assertEqual(len(self.pending), 1)
            self.drain()
        self.tool._queue_cloud_options({**self.settings, "project": "esc"}, "build")
        self.assertEqual(self.pending, [])
        self.assertEqual(self.tool.build_cloud_project.currentText(), "esc")

    def test_cloud_refresh_failure_keeps_old_cache_and_success_replaces_deleted_regions(self):
        self.load_cloud()
        previous = copy.deepcopy(self.store.data["selection_cache"])
        self.api._fetch_target_options.side_effect = OpsApiError("offline")
        self.load_cloud(force=True)
        self.assertEqual(self.store.data["selection_cache"], previous)
        self.tool._queue_cloud_options(self.settings, "build")
        self.assertEqual(self.pending, [])
        refreshed = target_options("etm")
        refreshed["targets"] = [row for row in refreshed["targets"] if row["region"] == "正式"]
        self.api._fetch_target_options.side_effect = None
        self.api._fetch_target_options.return_value = refreshed
        self.load_cloud(force=True)
        self.tool._queue_cloud_options(self.settings, "tab")
        self.assertEqual(self.pending, [])
        self.assertEqual(self.tool.cloud_region.values(), ["Huawei / 正式"])

    def test_cached_cloud_selection_rejects_an_older_pending_response(self):
        self.load_cloud()
        self.tool._queue_cloud_options({**self.settings, "project": "esc"}, "build")
        self.tool._queue_cloud_options(self.settings, "build")
        self.drain()
        self.assertEqual(self.tool.build_cloud_project.currentText(), "etm")
        self.assertFalse(self.tool.build_cloud_loading)

    def test_invalid_cloud_cache_falls_back_to_query(self):
        self.load_cloud()
        key = self.tool._cloud_options_cache_key(self.settings)
        entry = copy.deepcopy(self.store.data["selection_cache"]["cloud_targets"][key])
        for field, value in (("projects", None), ("project", "wrong"), ("regions_by_type", [])):
            self.store.data["selection_cache"]["cloud_targets"][key] = {**entry, field: value}
            self.tool._queue_cloud_options(self.settings, "build")
            self.assertEqual(len(self.pending), 1)
            self.drain()

    def test_restart_restores_both_caches_from_disk_and_export_omits_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(main, "CONFIG_DIR", root), patch.object(main, "CONFIG_FILE", root / "config.json"), patch.object(main.sys, "platform", "test"):
                store = main.ConfigStore()
                self.tool.store = store
                self.select()
                self.drain()
                self.load_cloud()
                restored = main.ConfigStore()
                other = self.make_tool(restored)
                other.jenkins = self.client
                self.select(tool=other)
                other._ensure_cloud_login = Mock(side_effect=AssertionError("unexpected login"))
                other._queue_cloud_options(self.settings, "build")
                self.assertEqual(self.pending, [])
                self.assertEqual(other.build_cloud_type.currentText(), "etm 后端")
                self.client.get_parameters.assert_called_once()
                exported = root / "export.deployconfig"
                store.export_to(exported)
                self.assertNotIn("selection_cache", json.loads(exported.read_text(encoding="utf-8"))["data"])

    def test_disk_save_failure_keeps_loaded_data_usable_in_memory(self):
        self.store.save.side_effect = OSError("disk unavailable")
        self.select()
        self.drain()
        self.select()
        self.load_cloud()
        self.tool._queue_cloud_options(self.settings, "build")
        self.assertEqual(self.pending, [])
        self.assertIn("GIT_BRANCH", self.tool.param_vars)
        self.assertEqual(self.tool.build_cloud_type.currentText(), "etm 后端")
        self.tool.message_overlay.show_message.assert_called()


if __name__ == "__main__":
    unittest.main()
