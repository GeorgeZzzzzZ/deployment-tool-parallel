import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from test_build_summary_ui import PARAMETERS


class TaskResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])
        cls.previous_style = cls.app.styleSheet()
        cls.app.setStyleSheet(main.GLOBAL_QSS)

    @classmethod
    def tearDownClass(cls):
        cls.app.setStyleSheet(cls.previous_style)

    def setUp(self):
        self.store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        with patch.object(main.DeployTool, "_auto_login_jenkins"), patch.object(
            main.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False
        ):
            self.manager = main.ApplicationWindowManager(self.app, store=self.store)
            self.task = self.manager.create_window()
        self.client = main.JenkinsClient("https://jenkins.example", "test", "test")
        self.client.cancel_build = Mock()
        self.client.get_build_summary = Mock(return_value=None)
        self.task.jenkins = self.client
        self.task.current_job_path = ["cloud", "esc", "service"]
        self.task.current_job_url = "https://jenkins.example/custom/service/"
        self.task.current_display_path = ["云应用打包", "15-ESC-设备中心", "5-SERVICE"]
        self.task._show_parameters(self.task.current_job_path, PARAMETERS)
        self.task.deploy_after_build_checkbox.setChecked(False)
        self.task._run_pipeline = Mock()
        self.task.message_overlay.show_message = Mock()
        self.pending = []
        self.task.run_thread = lambda function, on_finished=None: self.pending.append(function)
        self.button = self.manager.task_center._actions[self.task][0]
        self.app.processEvents()

    def tearDown(self):
        for task in self.manager.windows:
            task.build_running = False
            task._task_runtime_active = False
            task._workers.clear()
        self.manager._closing = True
        self.manager.close()
        self.manager.deleteLater()
        self.app.processEvents()

    def drain(self):
        pending, self.pending = self.pending, []
        for function in pending:
            function()
        self.app.processEvents()

    def start(self):
        self.task.start_build()
        self.drain()
        self.assertTrue(self.task.build_running)
        self.assertEqual(self.button.text(), "停止")

    def finish(self, status="打包已取消"):
        self.task._update_build_status(status)
        self.task._on_build_finished()
        self.drain()

    def test_stop_waits_for_confirmation_and_workers_before_continue_is_enabled(self):
        self.start()
        self.task.pipeline_phase = "jenkins"
        self.task.pipeline_location = "https://jenkins.example/custom/service/6/"
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            self.button.click()
        self.assertEqual(self.button.text(), "停止中")
        self.assertFalse(self.button.isEnabled())
        self.task.resume_build()
        self.assertEqual(self.task._run_pipeline.call_count, 1)
        self.drain()
        self.client.cancel_build.assert_called_once()
        worker = Mock(isRunning=Mock(return_value=True))
        self.task._workers.append(worker)
        self.finish()
        self.assertEqual(self.button.text(), "继续")
        self.assertFalse(self.button.isEnabled())
        self.task.resume_build()
        self.assertEqual(self.pending, [])
        worker.isRunning.return_value = False
        self.assertFalse(self.task.task_summary()["can_resume"], "wait for queued completion callbacks too")
        self.task._cleanup_worker(worker)
        self.assertTrue(self.button.isEnabled())
        self.assertEqual(self.button.objectName(), "SuccessBtn")

    def test_declining_stop_keeps_running_and_never_offers_continue(self):
        self.start()
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.No):
            self.button.click()
        self.assertFalse(self.task.cancel_event.is_set())
        self.assertEqual(self.button.text(), "停止")
        self.assertTrue(self.button.isEnabled())
        self.assertFalse(self.task.task_summary()["can_resume"])

    def test_continue_uses_original_job_parameters_client_and_build_only_mode(self):
        self.task.param_vars["GIT_BRANCH"][1].setCurrentText("origin/dev")
        self.start()
        original_args = self.task._run_pipeline.call_args.args
        self.task.cancel_event.set()
        self.task._task_progress = 67
        self.task._task_package = "old-package"
        self.task._task_started_at = 0
        self.finish()
        snapshot = self.task._last_build_snapshot
        self.task.param_vars["GIT_BRANCH"][1].setCurrentText("origin/changed")
        self.task.param_vars["folderName"][1].setText("changed")
        self.task.current_job_path = ["other"]
        self.task.current_job_url = "https://other.example/job/other/"
        self.task.current_display_path = ["other"]
        self.task.jenkins = Mock()
        self.task.deploy_after_build_checkbox.blockSignals(True)
        self.task.deploy_after_build_checkbox.setChecked(True)
        self.task.deploy_after_build_checkbox.blockSignals(False)
        self.task.build_log.setPlainText("old log")
        self.button.click()
        self.assertEqual(self.button.text(), "停止")
        self.assertTrue(self.task.build_running)
        self.assertFalse(self.task.cancel_event.is_set())
        self.assertFalse(self.task.active_deploy_after_build)
        self.assertEqual(self.task._task_progress, 0)
        self.assertEqual(self.task._task_package, "")
        self.assertGreater(self.task._task_started_at, 0)
        self.assertIsNone(self.task._task_finished_at)
        self.assertEqual(self.task.build_log.toPlainText(), "")
        self.assertEqual(self.task._task_title, "5-SERVICE")
        self.assertIs(self.task._active_build_client, self.client)
        self.assertIs(self.task._last_build_snapshot, snapshot)
        self.drain()
        self.assertEqual(self.task._run_pipeline.call_args.args, original_args)
        self.assertNotIn("_last_build_snapshot", json.dumps(self.store.data))

    def test_repeated_continue_calls_start_only_one_attempt(self):
        self.start()
        self.finish()
        self.task.resume_build()
        self.task.resume_build()
        self.assertEqual(len(self.pending), 1)
        self.drain()
        self.assertEqual(self.task._run_pipeline.call_count, 2)

    def test_success_failure_unknown_and_unstarted_tasks_have_no_continue(self):
        self.assertFalse(self.task.task_summary()["restartable"])
        for status in ("打包完成", "打包执行失败", "部署结果未知"):
            with self.subTest(status=status):
                self.start()
                self.finish(status)
                self.assertFalse(self.task.task_summary()["can_resume"])
                self.assertEqual(self.button.text(), "停止")
                self.assertFalse(self.button.isEnabled())
                self.task.resume_build()
                self.assertEqual(self.pending, [])

    def test_cloud_target_snapshot_survives_form_changes_and_pipeline_mutation(self):
        settings = {"url": "https://ops.example", "username": "test", "password_plain": "test",
                    "project": "esc", "deploy_type": "esc 后端", "cloud": "Huawei", "region": "测试"}
        self.task.build_deploy_mode.blockSignals(True)
        self.task.build_deploy_mode.setCurrentText("云端部署")
        self.task.build_deploy_mode.blockSignals(False)
        self.task.deploy_after_build_checkbox.blockSignals(True)
        self.task.deploy_after_build_checkbox.setChecked(True)
        self.task.deploy_after_build_checkbox.blockSignals(False)
        self.task.build_cloud_loading = False
        self.task.build_cloud_target_dirty = False
        self.task._build_cloud_settings_from_ui = Mock(return_value=settings)
        self.task._save_cloud_settings = Mock()
        self.start()
        original = copy.deepcopy(self.task._run_pipeline.call_args.args[6])
        self.task._run_pipeline.call_args.args[6]["package"] = "old-package"
        self.task._run_pipeline.call_args.args[6]["region"] = "mutated"
        settings["region"] = "正式"
        self.task.cancel_event.set()
        self.finish("一键流程已取消")
        self.button.click()
        self.drain()
        self.assertEqual(self.task._run_pipeline.call_args.args[6], original)
        self.assertTrue(self.task.active_deploy_after_build)
        self.assertEqual(self.task._task_target, "esc / esc 后端 / 测试")

    def test_ssh_retry_preserves_environment_and_dashboard_and_registers_fresh_batch(self):
        env = {"name": "test-env", "host": "test.example", "image_dir": "/images", "image_file": "images.txt"}
        dashboard = {"name": "test-dashboard", "url": "https://dashboard.example"}
        self.task._selected_environment = Mock(return_value=env)
        self.task._dashboard_for_environment = Mock(return_value=dashboard)
        self.task.deploy_after_build_checkbox.blockSignals(True)
        self.task.deploy_after_build_checkbox.setChecked(True)
        self.task.deploy_after_build_checkbox.blockSignals(False)
        self.task._handle_active_batch_registration = Mock()
        self.manager.batch_coordinator = Mock()
        self.manager.batch_coordinator.register_with_state.side_effect = [(("target", "first"), None), (("target", "second"), None)]
        self.start()
        env["host"] = "changed.example"
        dashboard["url"] = "https://changed.example"
        self.finish("一键流程已取消")
        self.button.click()
        self.drain()
        args = self.task._run_pipeline.call_args.args
        self.assertEqual(args[4]["host"], "test.example")
        self.assertEqual(args[5]["url"], "https://dashboard.example")
        self.assertEqual(args[8], ("target", "second"))
        self.assertEqual(self.manager.batch_coordinator.register_with_state.call_count, 2)

    def test_other_operations_clear_previous_cancelled_build_snapshot(self):
        self.start()
        self.finish()
        self.assertTrue(self.task.task_summary()["can_resume"])
        self.task._begin_tracked_task("SSH 部署", "test-env")
        self.task._task_phase = "已取消"
        self.task._finish_tracked_task()
        self.assertFalse(self.task.task_summary()["restartable"])

    def test_continue_targets_clicked_task_even_when_another_task_is_selected(self):
        self.start()
        self.finish()
        with patch.object(main.DeployTool, "_auto_login_jenkins"):
            second = self.manager.create_window()
        self.assertIs(self.manager.current_task, second)
        self.button.click()
        self.assertIs(self.manager.current_task, self.task)
        self.assertFalse(second.build_running)
        self.assertTrue(self.task.build_running)
        self.drain()

    def test_pipeline_and_remote_cancel_use_frozen_client_after_account_switch(self):
        self.start()
        self.task.jenkins = Mock()
        self.client.build = Mock(return_value="https://jenkins.example/queue/6/")
        self.client.wait_for_build = Mock(side_effect=main.PipelineCancelled("stopped"))
        main.DeployTool._run_pipeline(self.task, ["service"], {}, "https://jenkins.example/custom/service/",
                                      "SSH 环境部署", None, None, None, False)
        self.client.build.assert_called_once()
        self.task.jenkins.build.assert_not_called()
        self.task._cancel_jenkins_remote("jenkins", "https://jenkins.example/custom/service/6/")
        self.client.cancel_build.assert_called_once()
        self.task.jenkins.cancel_build.assert_not_called()
        self.drain()


if __name__ == "__main__":
    unittest.main()
