import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main


PARAMETERS = [
    {"name": "GIT_BRANCH", "type": "GitParameterDefinition",
     "choices": ["origin/master", "origin/dev"], "description": "打包分支",
     "defaultParameterValue": {"value": "origin/master"}},
    {"name": "folderName", "type": "StringParameterDefinition",
     "defaultParameterValue": {"value": "app_backend"}},
]


def summary(number=6, building=False):
    return {
        "number": number, "url": f"https://jenkins.example/job/service/{number}/",
        "building": building, "result": None if building else "SUCCESS",
        "timestamp": 1750000000000,
        "actions": [
            {"causes": [{"userName": "测试用户"}]},
            {"lastBuiltRevision": {"SHA1": "a" * 40, "branch": [{"name": "origin/master"}]}},
        ],
        "changeSet": {"items": [
            {"commitId": "a" * 40, "msg": "防止组件销毁后内存泄漏", "author": {"fullName": "开发者"}},
            {"commitId": "b" * 40, "msg": "升级框架 <script>literal</script>"},
        ]},
    }


class BuildSummaryClientTests(unittest.TestCase):
    def setUp(self):
        self.client = main.JenkinsClient("https://jenkins.example", "test", "test")
        self.client.get_json = Mock()

    def test_resolves_last_build_using_real_job_url_then_freezes_build_number(self):
        self.client.get_json.side_effect = [{"lastBuild": {"number": 6, "url": "https://jenkins.example/custom/6/"}}, summary()]
        self.client.get_build_summary(["display-name"], job_url="https://jenkins.example/custom/")
        self.assertEqual(self.client.get_json.call_args_list[0].args[0], "https://jenkins.example/custom/api/json")
        args = self.client.get_json.call_args.args
        self.assertEqual(args[0], "https://jenkins.example/custom/6/api/json")
        self.assertIn("changeSet[items[commitId", args[1]["tree"])
        self.assertIn("changeSets[items[commitId", args[1]["tree"])
        self.assertNotIn("parameters", args[1]["tree"])

    def test_exact_build_does_not_resolve_latest(self):
        self.client.get_json.return_value = summary(7)
        self.client.get_build_summary(["service"], build_url="https://jenkins.example/job/service/7/")
        self.client.get_json.assert_called_once()
        self.assertTrue(self.client.get_json.call_args.args[0].endswith("/7/api/json"))

    def test_job_without_build_does_not_request_last_build_alias(self):
        self.client.get_json.return_value = {"lastBuild": None}
        self.assertIsNone(self.client.get_build_summary(["service"]))
        self.client.get_json.assert_called_once()


class BuildSummaryInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])
        cls.previous_style = cls.app.styleSheet()
        cls.app.setStyleSheet(main.GLOBAL_QSS)

    @classmethod
    def tearDownClass(cls):
        cls.app.setStyleSheet(cls.previous_style)

    def setUp(self):
        store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        with patch.object(main.DeployTool, "_auto_login_jenkins"):
            self.tool = main.DeployTool(store=store, embedded=True)
        self.client = main.JenkinsClient("https://jenkins.example", "test", "test")
        self.client.get_build_summary = Mock(side_effect=lambda *args, **kwargs: summary())
        self.client.get_git_parameter_choices = Mock(return_value=["origin/master", "origin/dev"])
        self.tool.jenkins = self.client
        self.pending = []
        self.tool.run_thread = lambda function, on_finished=None: self.pending.append(function)
        self.tool._sync_cloud_target_from_job = Mock()
        self.tool._update_build_cloud_ready = Mock()
        self.tool.resize(1440, 960)
        self.tool.show()
        self.app.processEvents()

    def tearDown(self):
        self.tool.build_running = False
        self.tool._task_runtime_active = False
        self.tool.close()
        self.tool.deleteLater()
        self.app.processEvents()

    def select(self, name="service"):
        self.tool.current_job_path = [name] if name else []
        self.tool.current_job_url = f"https://jenkins.example/job/{name}/" if name else ""
        self.tool._select_build_summary()

    def drain(self, reverse=False):
        pending, self.pending = self.pending, []
        for function in reversed(pending) if reverse else pending:
            function()
        self.app.processEvents()

    def select_built(self, name="service", number=6):
        selection = (self.client, (name,), f"https://jenkins.example/job/{name}/")
        self.tool._summary_runs[selection] = {
            "build_url": f"https://jenkins.example/job/{name}/{number}/", "finished": True,
        }
        self.select(name)

    def test_defaults_and_card_order(self):
        self.assertFalse(self.tool.params_card._toggle.isChecked())
        self.assertTrue(self.tool.build_cloud_card._toggle.isChecked())
        self.assertTrue(self.tool.build_summary_card._toggle.isChecked())
        layout = self.tool.params_card.parentWidget().layout()
        self.assertEqual(layout.indexOf(self.tool.build_summary_card), layout.indexOf(self.tool.params_card) + 1)
        for card in (self.tool.build_cloud_card, self.tool.build_summary_card):
            card.set_expanded(False)
            self.assertTrue(card._content.isHidden())
            card.set_expanded(True)
            self.assertFalse(card._content.isHidden())

    def test_collapse_moves_same_branch_control_and_keeps_all_edits(self):
        self.tool._show_parameters(["service"], PARAMETERS)
        branch = self.tool.param_vars["GIT_BRANCH"][1]
        folder = self.tool.param_vars["folderName"][1]
        branch.setCurrentText("origin/feature")
        folder.setText("edited")
        for expanded in (True, False, True, False):
            self.tool.params_card.set_expanded(expanded)
            self.app.processEvents()
            self.assertIs(branch, self.tool.param_vars["GIT_BRANCH"][1])
            self.assertEqual(branch.currentText(), "origin/feature")
            self.assertEqual(folder.text(), "edited")
            self.assertEqual(folder.isVisible(), expanded)
            self.assertTrue(branch.isVisible())

    def test_branch_change_and_refresh_do_not_expand_parameters(self):
        self.select()
        self.drain()
        self.tool._show_parameters(["service"], PARAMETERS)
        branch = self.tool.param_vars["GIT_BRANCH"][1]
        branch.setCurrentText("origin/dev")
        self.tool.param_refresh_buttons["GIT_BRANCH"].click()
        self.drain()
        self.assertFalse(self.tool.params_card._toggle.isChecked())
        self.assertEqual(branch.currentText(), "origin/dev")
        self.client.get_git_parameter_choices.assert_called_once()

    def test_start_build_submits_edited_branch_while_parameters_remain_collapsed(self):
        self.select()
        self.drain()
        self.tool._show_parameters(["service"], PARAMETERS)
        self.tool.param_vars["GIT_BRANCH"][1].setCurrentText("origin/dev")
        self.tool.deploy_after_build_checkbox.setChecked(False)
        self.tool._run_pipeline = Mock()
        self.tool.start_build()
        self.assertFalse(self.tool.params_card._toggle.isChecked())
        self.assertIn("排队", self.tool.build_summary_label.text())
        self.drain()
        self.tool._run_pipeline.assert_called_once()
        parameters = self.tool._run_pipeline.call_args.args[1]
        self.assertEqual(parameters["GIT_BRANCH"], "origin/dev")
        self.assertEqual(parameters["folderName"], "app_backend")

    def test_branch_button_top_and_height_match_at_both_sizes_and_states(self):
        self.tool._show_parameters(["service"], PARAMETERS)
        branch = self.tool.param_vars["GIT_BRANCH"][1]
        button = self.tool.param_refresh_buttons["GIT_BRANCH"]
        for width in (1080, 1440):
            for expanded in (False, True):
                self.tool.resize(width, 960)
                self.tool.params_card.set_expanded(expanded)
                self.app.processEvents()
                self.assertEqual(button.height(), branch.height())
                self.assertEqual(button.mapToGlobal(main.QPoint()).y(), branch.mapToGlobal(main.QPoint()).y())
                self.assertGreaterEqual(button.width(), button.sizeHint().width())

    def test_switching_parameter_forms_removes_old_compact_row(self):
        self.tool._show_parameters(["a"], PARAMETERS)
        self.tool._show_parameters(["b"], PARAMETERS[1:])
        self.assertTrue(self.tool.branch_summary.isHidden())
        self.assertEqual(self.tool.branch_summary_layout.count(), 0)
        self.tool._show_parameters(["c"], PARAMETERS)
        self.assertEqual(self.tool.branch_summary_layout.count(), 3)
        self.tool._show_parameters(["empty"], [])
        self.assertEqual(self.tool.branch_summary_layout.count(), 0)

    def test_freestyle_summary_displays_changes_branch_and_revision_as_plain_text(self):
        self.select_built()
        self.drain()
        self.assertIn("#6  成功", self.tool.build_summary_label.text())
        self.assertIn("origin/master", self.tool.build_summary_scm.text())
        self.assertIn("a" * 40, self.tool.build_summary_scm.text())
        self.assertIn("测试用户", self.tool.build_summary_scm.text())
        self.assertIn("防止组件销毁后内存泄漏", self.tool.build_changes.toPlainText())
        self.assertIn("<script>literal</script>", self.tool.build_changes.toPlainText())
        self.assertIn("（2）", self.tool.build_changes_title.text())
        self.assertFalse(self.tool._summary_timer.isActive())

    def test_pipeline_combines_all_change_sets(self):
        data = summary()
        data["changeSets"] = [data.pop("changeSet"), {"items": [{"msg": "另一个仓库的提交"}]}]
        self.client.get_build_summary.side_effect = lambda *args, **kwargs: data
        self.select_built()
        self.drain()
        self.assertIn("（3）", self.tool.build_changes_title.text())
        self.assertIn("3. 另一个仓库的提交", self.tool.build_changes.toPlainText())

    def test_no_build_and_no_changes_have_distinct_empty_states(self):
        self.client.get_build_summary.side_effect = [None, {**summary(), "changeSet": {"items": []}}]
        self.select_built()
        self.drain()
        self.assertEqual(self.tool.build_summary_label.text(), "本次构建详情暂不可用，请刷新重试")
        self.assertFalse(self.tool.build_summary_open_button.isEnabled())
        self.tool._refresh_build_summary()
        self.drain()
        self.assertEqual(self.tool.build_changes.toPlainText(), "本次构建无提交变更")

    def test_out_of_order_responses_do_not_replace_new_job(self):
        self.client.get_build_summary.side_effect = lambda path, **kwargs: summary(6 if path == ["a"] else 7)
        self.select_built("a")
        self.select_built("b", 7)
        self.drain(reverse=True)
        self.assertIn("#7", self.tool.build_summary_label.text())

    def test_response_from_old_account_is_ignored(self):
        self.select_built()
        self.tool.jenkins = main.JenkinsClient("https://jenkins.example", "other", "test")
        self.drain()
        self.assertNotIn("#6", self.tool.build_summary_label.text())

    def test_folder_selection_clears_data_and_invalidates_inflight_response(self):
        self.select_built()
        self.select("")
        self.drain()
        self.assertEqual(self.tool.build_summary_label.text(), "请选择 Jenkins 构建任务")
        self.assertFalse(self.tool.build_summary_refresh_button.isEnabled())

    def test_same_job_reselect_does_not_duplicate_status_request(self):
        self.select_built()
        self.select_built()
        self.assertEqual(len(self.pending), 1)
        self.drain()
        self.select_built()
        self.assertEqual(self.pending, [])

    def test_running_build_polls_specific_number_and_stops_when_completed(self):
        self.client.get_build_summary.side_effect = [summary(building=True), summary()]
        self.select_built()
        self.drain()
        self.assertTrue(self.tool._summary_timer.isActive())
        self.tool._summary_timer.timeout.emit()
        self.drain()
        self.assertEqual(self.client.get_build_summary.call_args.kwargs["build_url"], summary()["url"])
        self.assertFalse(self.tool._summary_timer.isActive())

    def test_manual_refresh_keeps_this_tasks_build_number(self):
        self.select_built()
        self.drain()
        self.tool.build_summary_refresh_button.click()
        self.drain()
        self.assertEqual(self.client.get_build_summary.call_args.kwargs["build_url"], summary()["url"])

    def test_current_build_queue_and_number_replace_previous_build(self):
        self.select_built()
        self.drain()
        self.tool.build_running = True
        self.tool._running_summary_selection = self.tool._build_summary_selection()
        self.tool._summary_runs[self.tool._running_summary_selection] = {"build_url": "", "finished": False}
        self.tool._summary_selection = None
        self.tool._select_build_summary()
        self.assertIn("排队", self.tool.build_summary_label.text())
        self.assertNotIn("防止", self.tool.build_changes.toPlainText())
        self.client.get_build_summary.side_effect = lambda *args, **kwargs: summary(7)
        self.tool._on_pipeline_location("jenkins", "https://jenkins.example/job/service/7/")
        self.drain()
        self.assertTrue(self.client.get_build_summary.call_args.kwargs["build_url"].endswith("/7/"))
        self.assertIn("#7", self.tool.build_summary_label.text())
        self.tool._on_build_finished()
        self.drain()
        self.assertTrue(self.client.get_build_summary.call_args.kwargs["build_url"].endswith("/7/"))

    def test_errors_keep_existing_changes_and_allow_retry_without_expanding(self):
        self.select_built()
        self.drain()
        self.tool.build_summary_card.set_expanded(False)
        self.client.get_build_summary.side_effect = RuntimeError("Jenkins HTTP 403")
        self.tool._refresh_build_summary()
        self.drain()
        self.assertIn("加载失败", self.tool.build_summary_label.text())
        self.assertIn("防止", self.tool.build_changes.toPlainText())
        self.assertTrue(self.tool.build_summary_refresh_button.isEnabled())
        self.assertFalse(self.tool.build_summary_card._toggle.isChecked())
        self.assertFalse(self.tool._summary_timer.isActive())

    def test_tree_selection_uses_parameter_cache_without_loading_history(self):
        item = main.QTreeWidgetItem(self.tool.job_tree, ["service"])
        item.setData(0, main.Qt.UserRole, {"path": ["service"], "url": "https://jenkins.example/job/service/"})
        key = self.tool._parameter_cache_key(self.client, ["service"], "https://jenkins.example/job/service/")
        self.tool._save_parameter_cache(key, copy.deepcopy(PARAMETERS))
        self.tool._tree_select(item, 0)
        self.assertIn("GIT_BRANCH", self.tool.param_vars)
        self.assertEqual(self.tool.loading_count, 0)
        self.assertEqual(self.pending, [])
        self.drain()
        self.client.get_build_summary.assert_not_called()
        self.assertIn("尚未打包", self.tool.build_summary_label.text())
        self.assertTrue(self.tool.build_changes.isHidden())
        self.assertFalse(self.tool.build_summary_refresh_button.isEnabled())

    def test_unbuilt_job_cannot_fetch_history_even_by_direct_refresh(self):
        self.select()
        self.tool._refresh_build_summary()
        self.drain()
        self.client.get_build_summary.assert_not_called()
        self.assertFalse(self.tool.build_summary_open_button.isEnabled())
        self.assertTrue(self.tool.build_changes.isHidden())

    def test_cancel_before_build_number_stays_empty_when_switching_modules(self):
        self.select("etm")
        self.tool.deploy_after_build_checkbox.setChecked(False)
        self.tool._run_pipeline = Mock()
        self.tool.start_build()
        self.drain()
        self.tool.cancel_event.set()
        self.tool._on_build_finished()
        self.assertIn("未生成构建详情", self.tool.build_summary_label.text())
        self.select("esc")
        self.assertIn("尚未打包", self.tool.build_summary_label.text())
        self.select("etm")
        self.tool._refresh_build_summary()
        self.drain()
        self.assertIn("未生成构建详情", self.tool.build_summary_label.text())
        self.assertTrue(self.tool.build_changes.isHidden())
        self.client.get_build_summary.assert_not_called()

    def test_build_finishes_while_other_module_selected_and_restores_exact_result(self):
        self.select("etm")
        self.tool.deploy_after_build_checkbox.setChecked(False)
        self.tool._run_pipeline = Mock()
        self.tool.start_build()
        self.drain()
        self.select("esc")
        self.tool._on_pipeline_location("jenkins", "https://jenkins.example/job/etm/873/")
        self.tool._on_build_finished()
        self.assertIn("尚未打包", self.tool.build_summary_label.text())
        self.client.get_build_summary.assert_not_called()
        self.client.get_build_summary.side_effect = lambda *args, **kwargs: summary(873)
        self.select("etm")
        self.drain()
        self.assertIn("本次构建 #873", self.tool.build_summary_label.text())
        self.assertEqual(self.client.get_build_summary.call_args.kwargs["build_url"],
                         "https://jenkins.example/job/etm/873/")

    def test_new_attempt_invalidates_previous_inflight_summary(self):
        self.select_built()
        self.tool.deploy_after_build_checkbox.setChecked(False)
        self.tool._run_pipeline = Mock()
        self.tool.start_build()
        self.drain()
        self.assertIn("排队", self.tool.build_summary_label.text())
        self.assertNotIn("#6", self.tool.build_summary_label.text())
        self.assertTrue(self.tool.build_changes.isHidden())


if __name__ == "__main__":
    unittest.main()
