"""Manual multi-package deployment tests; all SSH and scripts are local fakes."""
import io
import os
import re
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core
import main
from PySide6.QtTest import QTest


PQM = "pqmbackend-hbxx-5.2.0:5.2.0.1001.10013"
ETM = "etmbackend-hbxx-5.2.0:5.2.0.1001.10033"


class PackageListTests(unittest.TestCase):
    def test_accepts_lines_and_preserves_order(self):
        self.assertEqual(core.parse_deployment_packages(f"\n {PQM}\r\n[{ETM}]\n"), [PQM, ETM])

    def test_duplicate_application_rejects_different_versions_and_customers(self):
        for other in (ETM, "etmbackend-yisen-5.3.0:2", "etmbackend-hbxx-5.2.0:3"):
            with self.assertRaisesRegex(ValueError, "etmbackend.*重复"):
                core.parse_deployment_packages(ETM + "\n" + other)

    def test_different_services_are_distinct(self):
        packages = [ETM, "etmfrontend:1", "etmmobilefrontend:1", PQM]
        self.assertEqual(core.parse_deployment_packages("\n".join(packages)), packages)

    def test_blank_invalid_and_unknown_product_are_rejected(self):
        for text in ("", "\n ", ETM + "\ninvalid", ETM + "\nunknown:1"):
            with self.assertRaises(ValueError):
                core.parse_deployment_packages(text)


class ManualBatchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.cancel = threading.Event()
        self.tool = SimpleNamespace(
            emit=Mock(), cancel_event=self.cancel, window_manager=None,
            _deployment_target_key=main.DeployTool._deployment_target_key,
            _wait_for_deployment_target=Mock(), _release_deployment_target=Mock(),
            _remote_command_succeeded=Mock(return_value=False),
            _create_isolated_image_workspace=Mock(return_value="/fixture/workspace"),
            _remove_isolated_image_workspace=Mock(), _wait_for_remote_image=Mock(),
        )
        self.tool._remote_exec = Mock(side_effect=self.remote)
        self.tool._sftp_update = Mock(side_effect=lambda sftp, path, *a, **k: self.events.append(("update", Path(path).stem)))
        self.client = Mock()
        self.client.open_sftp.return_value.open.side_effect = lambda path, mode: io.BytesIO(
            f"image: registry.example/{Path(path).stem}backend:old\n".encode())
        self.entries = []
        for product in ("etm", "pqm", "esc"):
            environment = {**main.DEFAULT_DEPLOY, "host": "fixture-host", "yaml_file": product + ".yaml",
                           "apply_command": "kubectl apply -f " + product + ".yaml", "delete_before_apply": True}
            self.entries.append(dict(owner=self.tool, environment=environment, package=product + "backend:new"))

    def remote(self, client, command, **kwargs):
        match = re.search(r"kubectl (delete|apply) -f (\w+)\.yaml", command)
        self.events.append(match.groups() if match else ("transfer", command))

    def execute(self):
        with patch("main.paramiko.SSHClient", return_value=self.client), patch("main.connect_ssh_client"):
            main.DeployTool._execute_deploy_batch(self.tool, self.entries, unified_apply=True)

    def test_one_transfer_all_updates_all_deletes_then_all_applies(self):
        self.execute()
        self.tool._create_isolated_image_workspace.assert_called_once()
        self.assertEqual(self.tool._create_isolated_image_workspace.call_args.args[2],
                         [entry["package"] for entry in self.entries])
        self.assertEqual(sum(event[0] == "transfer" for event in self.events), 1)
        self.assertEqual(self.events[1:], [(action, product) for action in ("update", "delete", "apply")
                                          for product in ("etm", "pqm", "esc")])
        self.assertTrue(all(entry["result"] == "应用成功" for entry in self.entries))
        self.client.close.assert_called_once()

    def test_off_only_applies_and_existing_images_skip_transfer(self):
        self.tool._remote_command_succeeded.return_value = True
        for entry in self.entries:
            entry["environment"]["delete_before_apply"] = False
        self.execute()
        self.assertEqual(self.events, [(action, product) for action in ("update", "apply")
                                       for product in ("etm", "pqm", "esc")])

    def test_transfer_failure_prevents_any_yaml_update_or_delete(self):
        self.tool._remote_exec.side_effect = RuntimeError("transfer failed")
        with self.assertRaisesRegex(RuntimeError, "transfer failed"):
            self.execute()
        self.tool._sftp_update.assert_not_called()
        self.tool._remove_isolated_image_workspace.assert_called_once()

    def test_worker_rejects_duplicate_application_before_connecting(self):
        self.entries[1]["package"] = "etmbackend-other:2"
        with patch("main.paramiko.SSHClient") as client:
            with self.assertRaisesRegex(ValueError, "etmbackend.*重复"):
                main.DeployTool._execute_deploy_batch(self.tool, self.entries, unified_apply=True)
        client.assert_not_called()

    def test_invalid_yaml_is_detected_before_transfer(self):
        self.client.open_sftp.return_value.open.side_effect = [io.BytesIO(b"image: registry/etmbackend:old\n"),
                                                              OSError("missing yaml")]
        with self.assertRaises(OSError):
            self.execute()
        self.tool._create_isolated_image_workspace.assert_not_called()
        self.tool._remote_exec.assert_not_called()

    def test_yaml_update_failure_prevents_all_delete_and_apply(self):
        self.tool._sftp_update.side_effect = [None, RuntimeError("update failed")]
        with self.assertRaises(RuntimeError):
            self.execute()
        self.assertFalse(any(action in ("delete", "apply") for action, _ in self.events))

    def test_delete_failure_recovers_attempted_including_partially_deleted_item(self):
        def remote(client, command, **kwargs):
            self.remote(client, command, **kwargs)
            if "delete -f pqm.yaml" in command:
                raise RuntimeError("partial deletion")
        self.tool._remote_exec.side_effect = remote
        with self.assertRaisesRegex(RuntimeError, "删除"):
            self.execute()
        commands = [event for event in self.events if event[0] in ("delete", "apply")]
        self.assertEqual(commands, [("delete", "etm"), ("delete", "pqm"), ("apply", "etm"), ("apply", "pqm")])
        self.assertIn("恢复", self.entries[0]["result"])
        self.assertIn("删除失败", self.entries[1]["result"])
        self.assertIn("未执行", self.entries[2]["result"])

    def test_apply_failure_continues_other_packages_and_reports_partial_results(self):
        def remote(client, command, **kwargs):
            self.remote(client, command, **kwargs)
            if "apply -f pqm.yaml" in command:
                raise RuntimeError("apply failed")
        self.tool._remote_exec.side_effect = remote
        with self.assertRaisesRegex(RuntimeError, "应用"):
            self.execute()
        self.assertEqual(self.events[-1], ("apply", "esc"))
        self.assertEqual(self.entries[0]["result"], "应用成功")
        self.assertIn("应用失败", self.entries[1]["result"])
        self.assertEqual(self.entries[2]["result"], "应用成功")

    def test_cancellation_after_delete_still_applies_all_deleted_packages(self):
        def remote(client, command, **kwargs):
            self.remote(client, command, **kwargs)
            if "delete -f etm.yaml" in command:
                self.cancel.set()
        self.tool._remote_exec.side_effect = remote
        self.execute()
        self.assertEqual(self.events[-3:], [("apply", p) for p in ("etm", "pqm", "esc")])


class ManualBatchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        with patch.object(main.DeployTool, "_auto_login_jenkins"):
            self.tool = main.DeployTool(store=self.store, embedded=True)
        self.tool._set_env_var("host", "fixture-host")
        self.tool._set_env_var("username", "fixture-user")
        self.tool.run_thread = Mock()
        self.tool.show()
        self.tool.tab_widget.setCurrentIndex(1)
        self.app.processEvents()

    def tearDown(self):
        self.tool._task_runtime_active = False
        self.tool.close()
        self.tool.deleteLater()
        self.app.processEvents()

    def test_multiline_paste_keeps_two_packages_and_previews_each_yaml(self):
        self.app.clipboard().setText(PQM + "\n" + ETM)
        self.tool.package_input.setFocus()
        self.tool.package_input.selectAll()
        QTest.keyClick(self.tool.package_input, main.Qt.Key_V, main.Qt.ControlModifier)
        self.tool._on_deployment_package_editing_finished()
        self.assertEqual(self.tool.package_input.text(), PQM + "\n" + ETM)
        preview = self.tool.deploy_package_preview
        self.assertEqual(preview.topLevelItemCount(), 2)
        self.assertIn("pqm.yaml", preview.topLevelItem(0).text(1))
        preview.setCurrentItem(preview.topLevelItem(1))
        self.assertEqual(self.tool._get_env_var("yaml_file"), "etm.yaml")

    def test_duplicate_application_starts_nothing(self):
        self.tool.package_input.setText(ETM + "\netmbackend-other:2")
        with patch.object(main.QMessageBox, "warning") as warning:
            self.tool.start_deploy()
        warning.assert_called_once()
        self.tool.run_thread.assert_not_called()

    def test_environment_switch_keeps_selected_batch_package_target(self):
        self.tool.package_input.setText(PQM + "\n" + ETM)
        self.tool._preview_ssh_fields_from_package()
        self.tool.deploy_package_preview.setCurrentItem(self.tool.deploy_package_preview.topLevelItem(1))
        self.store.data["environments"] = [{**main.DEFAULT_DEPLOY, "name": "fixture", "host": "new-host",
                                            "username": "user", "yaml_file": "pqm.yaml"}]
        self.tool.load_environment("fixture")
        self.assertEqual(self.tool._get_env_var("host"), "new-host")
        self.assertEqual(self.tool._get_env_var("yaml_file"), "etm.yaml")

    def test_shift_enter_adds_line_and_enter_previews_without_cloud_lookup(self):
        self.tool.package_input.setText(PQM)
        cursor = self.tool.package_input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.tool.package_input.setTextCursor(cursor)
        QTest.keyClick(self.tool.package_input, main.Qt.Key_Return, main.Qt.ShiftModifier)
        QTest.keyClicks(self.tool.package_input, ETM)
        self.tool._queue_cloud_options = Mock()
        QTest.keyClick(self.tool.package_input, main.Qt.Key_Return)
        self.assertEqual(self.tool.deploy_package_preview.topLevelItemCount(), 2)
        self.tool._queue_cloud_options.assert_not_called()

    def test_replacing_batch_with_single_package_does_not_reuse_hidden_preview(self):
        self.tool.package_input.setText(PQM + "\n" + ETM)
        self.tool._preview_ssh_fields_from_package()
        self.tool.package_input.setText("escbackend:1")
        self.assertTrue(self.tool.deploy_package_preview.isHidden())
        self.store.data["environments"] = [{**main.DEFAULT_DEPLOY, "name": "fixture", "host": "new-host"}]
        with patch.object(main.DeployTool, "_sync_ssh_target_from_job") as sync_job:
            self.tool.load_environment("fixture")
        sync_job.assert_called_once_with(self.tool)

    def test_runtime_failure_marks_task_failed_and_reports_each_package(self):
        entries = [{"package": PQM, "result": "应用成功"}, {"package": ETM, "result": "应用失败"}]
        self.tool._execute_deploy_batch = Mock(side_effect=RuntimeError("fixture apply failure"))
        self.tool._begin_tracked_task("SSH 批量部署", "fixture")
        self.tool._run_manual_deployment_batch(entries)
        self.app.processEvents()
        self.tool._finish_standalone_task()
        self.assertEqual(self.tool._task_state, "failed")
        self.assertIn(PQM + "：应用成功", self.tool.deploy_log.toPlainText())
        self.assertIn(ETM + "：应用失败", self.tool.deploy_log.toPlainText())

    def test_batch_freezes_all_packages_own_commands_and_shared_switch(self):
        self.tool.package_input.setText(PQM + "\n" + ETM)
        self.tool._preview_ssh_fields_from_package()
        self.tool.build_delete_option.checkbox.click()
        command = self.tool.deploy_delete_option.command
        command.selectAll()
        QTest.keyClicks(command, "kubectl delete -f pqm.yaml --timeout=180s")
        self.tool._run_manual_deployment_batch = Mock()
        with patch.object(main.QMessageBox, "warning"):
            self.tool.start_deploy()
        self.tool.build_delete_option.checkbox.click()
        self.tool.package_input.setText("escbackend:99")
        self.tool.run_thread.assert_called_once()
        self.tool.run_thread.call_args.args[0]()
        self.tool.run_thread.call_args.kwargs["on_finished"]()
        entries = self.tool._run_manual_deployment_batch.call_args.args[0]
        self.assertEqual([e["package"] for e in entries], [PQM, ETM])
        self.assertTrue(all(e["environment"]["delete_before_apply"] for e in entries))
        self.assertEqual(entries[0]["environment"]["delete_command"], "kubectl delete -f pqm.yaml --timeout=180s")
        self.assertIn("delete -f etm.yaml", entries[1]["environment"]["delete_command"])


if __name__ == "__main__":
    unittest.main()
