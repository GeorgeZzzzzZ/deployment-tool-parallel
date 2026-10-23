"""Delete/apply behavior uses local fakes only; no Kubernetes or SSH access."""
import io
import os
import threading
import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from PySide6.QtTest import QTest
from task_diagnostics import DiagnosticLog, TaskTrace


class DeleteApplyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tool = SimpleNamespace(emit=Mock(), _remote_exec=Mock())
        self.client = Mock()
        self.cancel = threading.Event()
        self.data = dict(yaml_dir="/data/yaml/app/backend", yaml_file="etm.yaml",
                         apply_command="kubectl apply -f etm.yaml", delete_before_apply=True)

    def apply(self):
        main.DeployTool._apply_deployment_yaml(self.tool, self.client, self.data,
                                              cancel_event=self.cancel)

    def test_enabled_deletes_then_applies_exact_same_yaml(self):
        self.apply()
        commands = [call.args[1] for call in self.tool._remote_exec.call_args_list]
        self.assertEqual(commands, [
            "cd /data/yaml/app/backend && kubectl delete -f etm.yaml",
            "cd /data/yaml/app/backend && kubectl apply -f etm.yaml"])

    def test_custom_delete_command_is_executed_exactly(self):
        command = "kubectl delete -f etm.yaml --ignore-not-found=true --timeout=180s"
        self.data.update(delete_command=command,
                         delete_command_target=[self.data["yaml_dir"], self.data["yaml_file"]])
        self.apply()
        self.assertEqual(self.tool._remote_exec.call_args_list[0].args[1],
                         "cd /data/yaml/app/backend && " + command)

    def test_empty_or_mismatched_custom_command_cannot_delete_or_apply(self):
        for command, target in ((" ", [self.data["yaml_dir"], "etm.yaml"]),
                                ("kubectl delete -f pqm.yaml --timeout=180s",
                                 [self.data["yaml_dir"], "pqm.yaml"])):
            self.data.update(delete_command=command, delete_command_target=target)
            with self.assertRaises(ValueError):
                self.apply()
            self.tool._remote_exec.assert_not_called()

    def test_disabled_and_old_snapshots_only_apply(self):
        for value in (False, None, "false"):
            self.data["delete_before_apply"] = value
            self.tool._remote_exec.reset_mock()
            self.apply()
            self.tool._remote_exec.assert_called_once_with(
                self.client, "cd /data/yaml/app/backend && kubectl apply -f etm.yaml")

    def test_delete_failure_stops_apply_and_reports_stage(self):
        self.tool._remote_exec.side_effect = RuntimeError("fixture delete failure")
        with self.assertRaisesRegex(RuntimeError, "删除.*失败"):
            self.apply()
        self.tool._remote_exec.assert_called_once()

    def test_apply_failure_after_delete_reports_missing_resources(self):
        self.tool._remote_exec.side_effect = [None, RuntimeError("fixture apply failure")]
        with self.assertRaisesRegex(RuntimeError, "已删除.*应用失败"):
            self.apply()
        self.assertEqual(self.tool._remote_exec.call_count, 2)

    def test_cancel_before_delete_does_nothing_but_after_delete_still_applies(self):
        self.cancel.set()
        with self.assertRaises(main.PipelineCancelled):
            self.apply()
        self.tool._remote_exec.assert_not_called()
        self.cancel.clear()
        self.tool._remote_exec.side_effect = lambda *_: self.cancel.set()
        self.apply()
        self.assertEqual(self.tool._remote_exec.call_count, 2)

    def test_file_and_directory_are_shell_quoted(self):
        self.data.update(yaml_file="etm file.yaml", yaml_dir="/yaml folder")
        self.apply()
        command = self.tool._remote_exec.call_args_list[0].args[1]
        self.assertEqual(command,
            "cd '/yaml folder' && kubectl delete -f 'etm file.yaml'")

    def test_single_deployment_derives_delete_from_package_instead_of_stale_yaml(self):
        client = Mock()
        client.open_sftp.return_value.open.return_value = io.BytesIO(
            b"image: registry.example/pqmbackend:old\n")
        self.tool._remote_command_succeeded = Mock(return_value=True)
        self.tool._sftp_update = Mock()
        with patch("main.paramiko.SSHClient", return_value=client), patch("main.connect_ssh_client"):
            main.DeployTool._execute_deploy(self.tool, {**self.data, "host": "fixture-host"}, "pqmbackend:new")
        commands = [call.args[1] for call in self.tool._remote_exec.call_args_list]
        self.assertIn("delete -f pqm.yaml", commands[0])
        self.assertIn("apply -f pqm.yaml", commands[1])
        self.assertNotIn("etm.yaml", " ".join(commands))
        client.close.assert_called_once()

    def test_custom_target_mismatch_is_rejected_before_ssh_connection(self):
        data = {**self.data, "host": "fixture-host", "delete_command": "kubectl delete -f etm.yaml --wait=false",
                "delete_command_target": [self.data["yaml_dir"], "etm.yaml"]}
        with patch("main.paramiko.SSHClient") as client_type:
            with self.assertRaisesRegex(ValueError, "不一致"):
                main.DeployTool._execute_deploy(self.tool, data, "pqmbackend:new")
        client_type.assert_not_called()

    def test_batch_respects_each_tasks_flag_and_yaml(self):
        self.tool.window_manager = SimpleNamespace(batch_coordinator=None)
        self.tool.cancel_event = threading.Event()
        self.tool._deployment_target_key = main.DeployTool._deployment_target_key
        self.tool._wait_for_deployment_target = Mock()
        self.tool._release_deployment_target = Mock()
        self.tool._remote_command_succeeded = Mock(return_value=True)
        self.tool._sftp_update = Mock()
        entries = []
        for product, enabled in (("etm", True), ("pqm", False)):
            owner = SimpleNamespace(cancel_event=threading.Event(), emit=Mock())
            environment = {**self.data, "host": "fixture-host", "yaml_file": f"{product}.yaml",
                           "apply_command": f"kubectl apply -f {product}.yaml", "delete_before_apply": enabled}
            if enabled:
                environment.update(delete_command="kubectl delete -f etm.yaml --timeout=180s",
                                   delete_command_target=[environment["yaml_dir"], "etm.yaml"])
            entries.append(dict(owner=owner, package=f"{product}backend:new", environment=environment))
        client = Mock()
        client.open_sftp.return_value.open.side_effect = lambda path, mode: io.BytesIO(
            f"image: registry.example/{Path(path).stem}backend:old\n".encode())
        with patch("main.paramiko.SSHClient", return_value=client), patch("main.connect_ssh_client"):
            main.DeployTool._execute_deploy_batch(self.tool, entries)
        commands = [call.args[1] for call in self.tool._remote_exec.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertIn("delete -f etm.yaml", commands[0])
        self.assertIn("--timeout=180s", commands[0])
        self.assertIn("apply -f etm.yaml", commands[1])
        self.assertIn("apply -f pqm.yaml", commands[2])

    def test_delete_and_apply_have_separate_diagnostic_durations(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = DiagnosticLog(directory)
            try:
                self.tool._diagnostics = TaskTrace(sink, 1)
                self.apply()
            finally:
                sink.close()
            records = [json.loads(line) for line in sink.path.read_text(encoding="utf-8").splitlines()]
        finished = [record for record in records if record["event"] == "operation_end"]
        self.assertEqual([record["operation"] for record in finished], ["yaml_delete", "yaml_apply"])
        self.assertTrue(all("duration_s" in record for record in finished))


class DeleteApplyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        with patch.object(main.DeployTool, "_auto_login_jenkins"):
            self.tool = main.DeployTool(store=self.store, embedded=True)
        self.tool.show()
        self.app.processEvents()
        self.store.save.reset_mock()

    def tearDown(self):
        self.tool.build_running = False
        self.tool._task_runtime_active = False
        self.tool.close()
        self.tool.deleteLater()
        self.app.processEvents()

    def test_switch_is_off_by_default_syncs_both_tabs_and_remembers_choice(self):
        first, second = self.tool.build_delete_option, self.tool.deploy_delete_option
        self.assertFalse(first.checkbox.isChecked())
        self.assertFalse(second.checkbox.isChecked())
        first.checkbox.click()
        self.assertTrue(second.checkbox.isChecked())
        self.assertIs(self.store.data["delete_before_apply"], True)
        second.checkbox.click()
        self.assertFalse(first.checkbox.isChecked())
        self.assertIs(self.store.data["delete_before_apply"], False)
        self.assertFalse(self.tool.env_dirty)
        self.assertEqual(self.store.save.call_count, 2)

    def test_remembered_choice_survives_config_reload_and_new_tasks(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            main, CONFIG_DIR=Path(directory), CONFIG_FILE=Path(directory) / "config.json",
            LEGACY_CONFIG_FILE=Path(directory) / "missing-legacy.json",
        ):
            self.tool.store = main.ConfigStore()
            self.tool.build_delete_option.checkbox.click()
            restored_store = main.ConfigStore()
            self.assertIs(restored_store.data["delete_before_apply"], True)
            with patch.object(main.DeployTool, "_auto_login_jenkins"):
                other = main.DeployTool(store=restored_store, embedded=True)
            try:
                self.assertTrue(other.build_delete_option.checkbox.isChecked())
                self.assertTrue(other.deploy_delete_option.checkbox.isChecked())
                other.deploy_delete_option.checkbox.click()
                self.assertIs(main.ConfigStore().data["delete_before_apply"], False)
                self.assertTrue(self.tool.delete_before_apply)
            finally:
                other.close()
                other.deleteLater()

    def test_switch_reuses_slider_and_command_is_editable_inline_input(self):
        option = self.tool.build_delete_option
        self.tool.build_env_card.set_expanded(True)
        self.app.processEvents()
        self.assertIsInstance(option.checkbox, main.ToggleSwitch)
        self.assertIsInstance(option.command, main.QLineEdit)
        self.assertFalse(option.command.isReadOnly())
        self.assertLess(option.checkbox.geometry().right(), option.command.x())
        self.assertAlmostEqual(option.checkbox.geometry().center().y(), option.command.geometry().center().y(), delta=1)
        self.tool._set_env_var("yaml_file", "custom.yaml")
        self.assertIn("delete -f custom.yaml", option.command.text())
        self.assertEqual(option.command.cursorPosition(), 0)
        option.command.selectAll()
        option.command.copy()
        self.assertEqual(self.app.clipboard().text(), main._yaml_delete_command("custom.yaml"))

    def edit_delete_command(self, option, command):
        option.command.selectAll()
        QTest.keyClicks(option.command, command)
        self.app.processEvents()

    def test_edited_command_syncs_tabs_and_stays_with_its_target(self):
        first, second = self.tool.build_delete_option, self.tool.deploy_delete_option
        self.tool._set_env_var("yaml_file", "etm.yaml")
        custom = "kubectl delete -f etm.yaml --timeout=180s"
        self.edit_delete_command(first, custom)
        self.assertEqual(second.command.text(), custom)
        self.tool._set_env_var("yaml_file", "pqm.yaml")
        self.assertEqual(first.command.text(), main._yaml_delete_command("pqm.yaml"))
        self.tool._set_env_var("yaml_file", "etm.yaml")
        self.assertEqual(first.command.text(), custom)
        self.tool._set_env_var("host", "another-fixture-host")
        self.assertEqual(first.command.text(), main._yaml_delete_command("etm.yaml"))
        self.store.save.assert_not_called()

    def test_command_editing_preserves_cursor_and_supports_second_tab(self):
        self.tool._set_env_var("yaml_file", "etm.yaml")
        option = self.tool.deploy_delete_option
        option.command.setCursorPosition(len(option.command.text()))
        QTest.keyClicks(option.command, " --wait=false")
        self.assertTrue(option.command.text().endswith(" --wait=false"))
        self.assertEqual(option.command.cursorPosition(), len(option.command.text()))
        self.assertEqual(self.tool.build_delete_option.command.text(), option.command.text())

    def test_manual_deploy_freezes_edited_command(self):
        self.tool._set_env_var("host", "fixture-host")
        self.tool._set_env_var("username", "fixture-user")
        self.tool.package_input.setText("etmbackend:1")
        self.tool._preview_ssh_fields_from_package()
        self.tool.build_delete_option.checkbox.click()
        custom = "kubectl delete -f etm.yaml --timeout=180s"
        self.edit_delete_command(self.tool.deploy_delete_option, custom)
        self.tool.run_thread = Mock()
        self.tool._run_deploy_with_target_lock = Mock()
        self.tool.start_deploy()
        self.edit_delete_command(self.tool.build_delete_option, custom + " --wait=false")
        self.tool.run_thread.call_args.args[0]()
        self.tool.run_thread.call_args.kwargs["on_finished"]()
        data = self.tool._run_deploy_with_target_lock.call_args.args[0]
        self.assertEqual(data["delete_command"], custom)

    def test_enabled_empty_command_is_rejected_before_starting(self):
        self.tool._set_env_var("host", "fixture-host")
        self.tool._set_env_var("username", "fixture-user")
        self.tool.package_input.setText("etmbackend:1")
        self.tool._preview_ssh_fields_from_package()
        self.tool.build_delete_option.checkbox.click()
        option = self.tool.deploy_delete_option
        option.command.selectAll()
        QTest.keyClick(option.command, main.Qt.Key_Backspace)
        self.tool.run_thread = Mock()
        with patch.object(main.QMessageBox, "warning") as warning:
            self.tool.start_deploy()
        warning.assert_called_once()
        self.tool.run_thread.assert_not_called()

    def test_old_config_and_invalid_saved_values_default_to_disabled(self):
        for value in (None, "false", "true", 1):
            self.store.data["delete_before_apply"] = value
            with patch.object(main.DeployTool, "_auto_login_jenkins"):
                other = main.DeployTool(store=self.store, embedded=True)
            try:
                self.assertFalse(other.delete_before_apply)
            finally:
                other.close()
                other.deleteLater()

    def test_package_and_job_change_both_delete_previews(self):
        self.tool.build_delete_option.checkbox.click()
        for package, filename in (("pqmbackend:1", "pqm.yaml"),
                                  ("etmfrontend:1", "etm.yaml"),
                                  ("escmobilefrontend:1", "escmobile.yaml")):
            self.tool.package_input.setText(package)
            self.tool._preview_ssh_fields_from_package()
            for option in (self.tool.build_delete_option, self.tool.deploy_delete_option):
                self.assertEqual("kubectl delete -f " + filename, option.command.text())
                self.assertTrue(option.checkbox.isChecked())
        self.tool.current_job_path = ["custom-dev", "16-ETM", "3-SERVICE"]
        self.tool.current_display_path = self.tool.current_job_path
        self.tool._sync_ssh_target_from_job()
        self.assertEqual("kubectl delete -f etm.yaml", self.tool.build_delete_option.command.text())

    def test_collapsing_moves_same_switch_between_header_and_actions(self):
        option = self.tool.build_delete_option
        option.checkbox.click()
        for expanded in (True, False, True, False):
            self.tool.build_env_card.set_expanded(expanded)
            self.app.processEvents()
            self.assertTrue(option.checkbox.isVisible())
            self.assertEqual(option.command.isVisible(), expanded)
            self.assertTrue(option.checkbox.isChecked())
            self.assertIs(option, self.tool.build_delete_option)

    def test_manual_deploy_freezes_option_and_new_task_starts_disabled(self):
        self.tool._set_env_var("host", "fixture-host")
        self.tool._set_env_var("username", "fixture-user")
        self.tool.package_input.setText("etmbackend:1")
        self.tool.build_delete_option.checkbox.click()
        self.tool.run_thread = Mock()
        self.tool._run_deploy_with_target_lock = Mock()
        self.tool.start_deploy()
        self.tool.build_delete_option.checkbox.click()
        self.tool.run_thread.call_args.args[0]()
        self.tool.run_thread.call_args.kwargs["on_finished"]()
        data = self.tool._run_deploy_with_target_lock.call_args.args[0]
        self.assertIs(data["delete_before_apply"], True)
        with patch.object(main.DeployTool, "_auto_login_jenkins"):
            other = main.DeployTool(store=self.store, embedded=True)
        try:
            self.assertFalse(other.delete_before_apply)
        finally:
            other.close()
            other.deleteLater()

    def test_jenkins_pipeline_freezes_option_without_saving_environment(self):
        self.tool.jenkins = Mock()
        self.tool.current_job_path = ["custom-dev", "16-ETM", "3-SERVICE"]
        self.tool.current_display_path = list(self.tool.current_job_path)
        self.tool.current_job_url = "https://fixture.invalid/job/service/"
        self.tool._selected_environment = Mock(return_value={**main.DEFAULT_DEPLOY, "name": "fixture"})
        self.tool._dashboard_for_environment = Mock(return_value={"name": "fixture"})
        self.tool.build_delete_option.checkbox.click()
        self.tool._set_env_var("yaml_file", "etm.yaml")
        custom = "kubectl delete -f etm.yaml --timeout=180s"
        self.edit_delete_command(self.tool.build_delete_option, custom)
        self.tool.env_dirty = False
        with patch.object(main.DeployTool, "_launch_build") as launch:
            self.tool.start_build()
        snapshot = launch.call_args.args[1]["configuration"]
        self.tool.build_delete_option.checkbox.click()
        self.assertIs(snapshot["environment"]["delete_before_apply"], True)
        self.assertEqual(snapshot["environment"]["delete_command"], custom)
        self.assertFalse(self.tool.env_dirty)


if __name__ == "__main__":
    unittest.main()
