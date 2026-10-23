import copy
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from PySide6.QtTest import QTest


class DeploymentPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        with patch.object(main.DeployTool, "_auto_login_jenkins"):
            self.tool = main.DeployTool(store=self.store, embedded=True)
        self.tool.jenkins = main.JenkinsClient("https://jenkins.example", "builder", "fixture")
        self.tool.run_thread = Mock()
        self.tool._cached_parameters = Mock(return_value=[])
        self.tool.show()
        self.app.processEvents()
        self.store.save.reset_mock()
        self.original = copy.deepcopy(self.store.data)

    def tearDown(self):
        self.tool.build_running = False
        self.tool._task_runtime_active = False
        self.tool.close()
        self.tool.deleteLater()
        self.app.processEvents()

    def select(self, project, service):
        folder = self.tool._create_job_tree_item(None, [], {
            "name": "16-" + project + "-Module", "_class": "Folder"})
        item = self.tool._create_job_tree_item(folder, [folder.text(0)], {
            "name": service, "_class": "FreeStyleProject"})
        self.tool._tree_select(item, 0)
        return item

    def test_selection_immediately_previews_each_service_without_saving(self):
        for project, service, directory, filename in (
            ("PQM", "3-SERVICE", "backend", "pqm.yaml"),
            ("ETM", "1-WEB", "frontend", "etm.yaml"),
            ("ESC", "2-APP", "mobile", "escmobile.yaml"),
        ):
            with self.subTest(project=project, service=service):
                self.select(project, service)
                self.assertEqual(self.tool._get_env_var("yaml_file"), filename)
                self.assertEqual(self.tool._get_env_var("yaml_dir"), "/data/yaml/app/" + directory)
                for entries in (self.tool.env_entries, self.tool.build_env_entries):
                    self.assertEqual(entries["yaml_file"].currentText(), filename)
                    self.assertEqual(entries["apply_command"].text(), "kubectl apply -f " + filename)
                self.assertFalse(self.tool.env_dirty)
        self.assertEqual(self.store.data, self.original)
        self.tool.run_thread.assert_not_called()

    def test_preview_preserves_existing_unsaved_connection_changes(self):
        self.tool._set_env_var("host", "fixture-host")
        self.assertTrue(self.tool.env_dirty)
        self.select("PQM", "3-SERVICE")
        self.assertEqual(self.tool._get_env_var("yaml_file"), "pqm.yaml")
        self.assertEqual(self.tool._get_env_var("host"), "fixture-host")
        self.assertTrue(self.tool.env_dirty)

    def test_switching_environment_keeps_selected_jobs_preview(self):
        self.select("PQM", "3-SERVICE")
        self.store.data["environments"] = [{
            **main.DEFAULT_DEPLOY, "name": "sample", "host": "fixture-host",
            "password": "", "yaml_file": "etm.yaml",
        }]
        self.tool.refresh_environment_names()
        self.tool.load_environment("sample")
        self.assertEqual(self.tool._get_env_var("yaml_file"), "pqm.yaml")
        self.assertFalse(self.tool.env_dirty)
        self.assertEqual(self.store.data["environments"][0]["yaml_file"], "etm.yaml")

    def test_pipeline_rejects_package_that_disagrees_with_preview(self):
        client = Mock()
        client.wait_for_build.return_value = (
            {"result": "SUCCESS", "number": 1},
            "naming to registry.example/industry/pqmbackend-5.3.0:1 done",
        )
        tool = SimpleNamespace(
            jenkins=client, cancel_event=threading.Event(), emit=Mock(),
            _handle_build_output=Mock(), _handle_build_progress=Mock(),
            _raise_if_deploy_cancelled=Mock(), _execute_deploy=Mock(),
        )
        environment = {"name": "sample", "host": "fixture-host", "_preview_yaml": {
            "yaml_dir": "/data/yaml/app/backend", "yaml_file": "etm.yaml",
            "apply_command": "kubectl apply -f etm.yaml",
        }}
        main.DeployTool._run_pipeline(tool, ["job"], {}, "", "SSH 环境部署",
                                     environment, {}, None)
        tool._execute_deploy.assert_not_called()
        errors = [call.args[1] for call in tool.emit.call_args_list if call.args[0] == "error"]
        self.assertTrue(any("预览" in error and "不一致" in error for error in errors))

    def test_build_package_does_not_replace_mismatched_or_new_job_preview(self):
        self.select("ETM", "3-SERVICE")
        self.tool._last_build_snapshot = {"configuration": {
            "path": list(self.tool.current_job_path),
            "environment": {"_preview_yaml": dict(self.tool._preview_ssh_target)},
        }}
        self.tool.build_running = True
        self.tool.active_deploy_after_build = True
        self.tool._on_package("pqmbackend-5.3.0:1")
        self.assertEqual(self.tool._get_env_var("yaml_file"), "etm.yaml")
        self.select("ESC", "3-SERVICE")
        self.tool._on_package("etmbackend-5.3.0:1")
        self.assertEqual(self.tool._get_env_var("yaml_file"), "esc.yaml")

    def edit_package(self, package):
        self.tool.show()
        self.tool.tab_widget.setCurrentIndex(1)
        entry = self.tool.package_input
        entry.setFocus()
        self.app.processEvents()
        entry.selectAll()
        QTest.keyClicks(entry, package)
        self.app.processEvents()

    def test_package_blur_updates_both_tabs_without_network_or_saving(self):
        self.select("PQM", "3-SERVICE")
        self.tool._queue_cloud_options = Mock()
        self.tool.message_overlay.show_message = Mock()
        self.edit_package("etmbackend-hbxx-5.2.0:5.2.0.1001.10031")
        self.assertEqual(self.tool._get_env_var("yaml_file"), "pqm.yaml")
        self.tool.env_entries["host"].setFocus()
        self.app.processEvents()
        for entries in (self.tool.env_entries, self.tool.build_env_entries):
            self.assertEqual(entries["yaml_dir"].currentText(), "/data/yaml/app/backend")
            self.assertEqual(entries["yaml_file"].currentText(), "etm.yaml")
            self.assertEqual(entries["apply_command"].text(), "kubectl apply -f etm.yaml")
        self.assertTrue(self.tool.env_dirty)
        self.assertEqual(self.store.data, self.original)
        self.store.save.assert_not_called()
        self.tool._queue_cloud_options.assert_not_called()
        self.tool.run_thread.assert_not_called()
        self.tool.message_overlay.show_message.assert_not_called()

    def test_package_enter_updates_once_and_blur_does_not_repeat_cloud_lookup(self):
        self.tool._queue_cloud_options = Mock()
        self.tool._cloud_settings_from_ui = Mock(return_value={
            "url": "https://fixture.invalid", "username": "fixture", "password_plain": "fixture"})
        self.edit_package("escmobilefrontend-5.3.0:1")
        QTest.keyClick(self.tool.package_input, main.Qt.Key_Return)
        self.app.processEvents()
        self.tool.env_entries["host"].setFocus()
        self.app.processEvents()
        self.assertEqual(self.tool._get_env_var("yaml_file"), "escmobile.yaml")
        self.assertEqual(self.tool._get_env_var("yaml_dir"), "/data/yaml/app/mobile")
        self.tool._queue_cloud_options.assert_called_once()
        self.assertEqual(self.tool.deploy_log.toPlainText().count("已按包号解析 SSH 配置"), 1)

    def test_invalid_package_blur_preserves_fields_and_does_not_show_dialog(self):
        self.select("PQM", "3-SERVICE")
        self.tool.message_overlay.show_message = Mock()
        for package in ("incomplete", "unknown:1"):
            self.edit_package(package)
            self.tool.env_entries["host"].setFocus()
            self.app.processEvents()
            self.assertEqual(self.tool._get_env_var("yaml_file"), "pqm.yaml")
            self.assertFalse(self.tool.env_dirty)
        self.tool.message_overlay.show_message.assert_not_called()
        self.store.save.assert_not_called()

    def test_programmatic_package_change_cannot_override_selected_job_on_blur(self):
        self.select("PQM", "3-SERVICE")
        self.tool.package_input.setText("etmbackend:1")
        self.tool.package_input.editingFinished.emit()
        self.assertEqual(self.tool._get_env_var("yaml_file"), "pqm.yaml")
        self.assertFalse(self.tool.env_dirty)

    def test_click_deploy_after_edit_submits_the_visible_package_target(self):
        self.select("PQM", "3-SERVICE")
        self.tool._set_env_var("host", "fixture-host")
        self.tool._set_env_var("username", "fixture-user")
        self.tool._run_deploy_with_target_lock = Mock()
        self.edit_package("etmbackend-hbxx-5.2.0:5.2.0.1001.10031")
        button = next(button for button in self.tool.findChildren(main.QPushButton)
                      if button.text() == "执行环境部署")
        QTest.mouseClick(button, main.Qt.LeftButton)
        self.app.processEvents()
        self.tool.run_thread.assert_called_once()
        self.tool.run_thread.call_args.args[0]()
        self.tool.run_thread.call_args.kwargs["on_finished"]()
        data, package = self.tool._run_deploy_with_target_lock.call_args.args
        self.assertEqual(package, self.tool.package_input.text())
        self.assertEqual(data["yaml_file"], "etm.yaml")
        self.assertEqual(data["apply_command"], "kubectl apply -f etm.yaml")
        self.assertEqual(data["apply_command"], self.tool.build_env_entries["apply_command"].text())


if __name__ == "__main__":
    unittest.main()
