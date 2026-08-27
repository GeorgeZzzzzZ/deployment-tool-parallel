import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from main import (
    ApplicationWindowManager,
    BatchDeploymentCoordinator,
    ConfigStore,
    DeploymentTargetCoordinator,
    DeployTool,
    PipelineCancelled,
)


class ParallelWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = main.QApplication.instance() or main.QApplication([])

    def test_batch_starts_automatically_after_all_registered_builds_finish(self):
        coordinator = BatchDeploymentCoordinator()
        first_owner = object()
        second_owner = object()
        key = "ssh-batch:environment-1"
        first_token = coordinator.register(first_owner, key)
        second_token = coordinator.register(second_owner, key)
        self.assertEqual(first_token, second_token)
        results = []

        def ready(owner, token, package):
            results.append(
                coordinator.wait_until_ready(
                    owner, token, {"package": package}
                )
            )

        first = threading.Thread(
            target=ready, args=(first_owner, first_token, "first:1")
        )
        second = threading.Thread(
            target=ready, args=(second_owner, second_token, "second:1")
        )
        first.start()
        threading.Event().wait(0.05)
        self.assertTrue(first.is_alive())
        self.assertEqual(results, [])
        second.start()

        for _ in range(20):
            if results:
                break
            threading.Event().wait(0.02)
        self.assertEqual(len(results), 1)
        leader, entries = results[0]
        self.assertTrue(leader)
        self.assertEqual(
            [entry["package"] for entry in entries], ["first:1", "second:1"]
        )
        coordinator.complete(first_token)
        first.join(1)
        second.join(1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

    def test_build_registered_during_active_batch_enters_next_round(self):
        coordinator = BatchDeploymentCoordinator()
        key = "ssh-batch:environment-1"
        first_owner = object()
        first_token = coordinator.register(first_owner, key)
        coordinator.request_start(first_token)
        leader, _ = coordinator.wait_until_ready(
            first_owner, first_token, {"package": "first:1"}
        )
        self.assertTrue(leader)

        later_owner = object()
        later_token = coordinator.register(later_owner, key)
        self.assertNotEqual(first_token, later_token)
        coordinator.complete(first_token)

    def test_different_environments_have_independent_batches(self):
        coordinator = BatchDeploymentCoordinator()
        first = coordinator.register(object(), "ssh-batch:environment-1")
        second = coordinator.register(object(), "ssh-batch:environment-2")
        self.assertNotEqual(first[0], second[0])

    def test_pull_phase_can_request_safe_rebatch(self):
        coordinator = BatchDeploymentCoordinator()
        first_owner = object()
        first_token = coordinator.register(first_owner, "ssh-batch:environment-1")
        leader, _ = coordinator.wait_until_ready(
            first_owner, first_token, {"package": "first:1"}
        )
        self.assertTrue(leader)
        coordinator.set_phase(first_token, "pull")

        second_owner = object()
        second_token, state = coordinator.register_with_state(
            second_owner, "ssh-batch:environment-1"
        )
        self.assertEqual(state["phase"], "pull")
        self.assertTrue(coordinator.request_restart(state["token"]))
        self.assertTrue(coordinator.interrupt_event(first_token).is_set())
        self.assertNotEqual(first_token, second_token)

    def test_push_phase_is_not_interruptible_and_joins_next_round(self):
        coordinator = BatchDeploymentCoordinator()
        first_owner = object()
        first_token = coordinator.register(first_owner, "ssh-batch:environment-1")
        leader, _ = coordinator.wait_until_ready(
            first_owner, first_token, {"package": "first:1"}
        )
        self.assertTrue(leader)
        coordinator.set_phase(first_token, "push")

        second_owner = object()
        _, state = coordinator.register_with_state(
            second_owner, "ssh-batch:environment-1"
        )
        self.assertEqual(state["phase"], "push")
        self.assertFalse(coordinator.request_restart(state["token"]))

    def test_preparing_phase_collects_pending_entries_before_transfer(self):
        coordinator = BatchDeploymentCoordinator()
        first_owner = object()
        first_token = coordinator.register(first_owner, "ssh-batch:environment-1")
        leader, _ = coordinator.wait_until_ready(
            first_owner, first_token, {"package": "first:1"}
        )
        self.assertTrue(leader)
        coordinator.set_phase(first_token, "preparing")
        second_owner = object()
        second_token, state = coordinator.register_with_state(
            second_owner, "ssh-batch:environment-1"
        )
        self.assertEqual(state["phase"], "preparing")
        pending_result = []
        pending_thread = threading.Thread(
            target=lambda: pending_result.append(
                coordinator.wait_until_ready(
                    second_owner, second_token, {"package": "second:1"}
                )
            )
        )
        pending_thread.start()
        threading.Event().wait(0.05)
        self.assertTrue(pending_thread.is_alive())
        entries = coordinator.collect_pending_entries(
            first_token, [{"package": "first:1"}]
        )
        self.assertEqual(
            [entry["package"] for entry in entries], ["first:1", "second:1"]
        )
        coordinator.complete(first_token)
        pending_thread.join(1)
        self.assertEqual(pending_result, [(False, [])])

    def test_same_target_waits_until_first_owner_releases(self):
        coordinator = DeploymentTargetCoordinator()
        first_owner = object()
        second_owner = object()
        key = "ssh:host|22|/data/yaml/app/backend|etm.yaml"
        coordinator.acquire(first_owner, key)
        waiting = threading.Event()
        acquired = threading.Event()

        def acquire_second():
            coordinator.acquire(
                second_owner,
                key,
                on_wait=waiting.set,
            )
            acquired.set()

        thread = threading.Thread(target=acquire_second)
        thread.start()
        self.assertTrue(waiting.wait(1))
        self.assertFalse(acquired.wait(0.1))

        coordinator.release(first_owner, key)

        self.assertTrue(acquired.wait(1))
        coordinator.release(second_owner, key)
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_different_targets_can_run_in_parallel(self):
        coordinator = DeploymentTargetCoordinator()
        coordinator.acquire(object(), "ssh:environment-1")

        second_owner = object()
        coordinator.acquire(second_owner, "ssh:environment-2")

        coordinator.release(second_owner, "ssh:environment-2")

    def test_waiting_target_honors_cancellation(self):
        coordinator = DeploymentTargetCoordinator()
        first_owner = object()
        second_owner = object()
        cancel_event = threading.Event()
        waiting = threading.Event()
        cancelled = threading.Event()
        key = "cloud:etm|backend|huawei|test"
        coordinator.acquire(first_owner, key)

        def acquire_second():
            try:
                coordinator.acquire(
                    second_owner,
                    key,
                    cancel_event=cancel_event,
                    on_wait=waiting.set,
                )
            except PipelineCancelled:
                cancelled.set()

        thread = threading.Thread(target=acquire_second)
        thread.start()
        self.assertTrue(waiting.wait(1))
        cancel_event.set()

        self.assertTrue(cancelled.wait(1))
        coordinator.release(first_owner, key)
        thread.join(1)

    def test_parallel_config_import_does_not_modify_original_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parallel_dir = root / "parallel"
            legacy_dir = root / "original"
            legacy_dir.mkdir()
            legacy_file = legacy_dir / "config.json"
            original = {"last_package": "original-package"}
            legacy_file.write_text(
                json.dumps(original, ensure_ascii=False), encoding="utf-8"
            )

            with patch.multiple(
                main,
                CONFIG_DIR=parallel_dir,
                CONFIG_FILE=parallel_dir / "config.json",
                LEGACY_CONFIG_FILE=legacy_file,
            ):
                store = ConfigStore()
                store.data["last_package"] = "parallel-package"
                store.save()

                parallel_saved = json.loads(
                    (parallel_dir / "config.json").read_text(encoding="utf-8")
                )

            self.assertEqual(store.data["last_package"], "parallel-package")
            self.assertEqual(parallel_saved["last_package"], "parallel-package")
            self.assertEqual(
                json.loads(legacy_file.read_text(encoding="utf-8")), original
            )

    def test_local_config_export_import_keeps_credentials_encrypted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.multiple(
                main,
                CONFIG_DIR=root / "appdata",
                CONFIG_FILE=root / "appdata" / "config.json",
                LEGACY_CONFIG_FILE=root / "legacy" / "config.json",
            ):
                store = ConfigStore()
                store.data["jenkins"]["password"] = main.protect_secret(
                    "jenkins-secret"
                )
                store.data["environments"] = [
                    {
                        "name": "环境1",
                        "password": main.protect_secret("ssh-secret"),
                    }
                ]
                store.data["dashboards"] = [
                    {
                        "name": "环境1",
                        "env_password": main.protect_secret("rancher-secret"),
                        "token": main.protect_secret("dashboard-token"),
                    }
                ]
                export_path = root / "transfer.deployconfig"
                store.export_to(export_path)
                exported_text = export_path.read_text(encoding="utf-8")
                self.assertNotIn("jenkins-secret", exported_text)
                self.assertNotIn("dashboard-token", exported_text)

                store.import_from(export_path)

                self.assertEqual(
                    main.unprotect_secret(store.data["jenkins"]["password"]),
                    "jenkins-secret",
                )
                self.assertEqual(
                    main.unprotect_secret(store.data["environments"][0]["password"]),
                    "ssh-secret",
                )
                self.assertEqual(
                    main.unprotect_secret(store.data["dashboards"][0]["token"]),
                    "dashboard-token",
                )

    def test_configuration_export_does_not_request_password(self):
        store = SimpleNamespace(export_to=Mock(return_value=Path("backup.deployconfig")))
        overlay = SimpleNamespace(show_message=Mock())
        tool = SimpleNamespace(store=store, message_overlay=overlay)

        with (
            patch.object(
                main.QFileDialog,
                "getSaveFileName",
                return_value=("backup.deployconfig", "config"),
            ),
            patch.object(main.QInputDialog, "getText") as get_password,
        ):
            DeployTool.export_configuration(tool)

        get_password.assert_not_called()
        store.export_to.assert_called_once_with("backup.deployconfig")
        overlay.show_message.assert_called_once()

    def test_configuration_import_does_not_request_password(self):
        store = SimpleNamespace(import_from=Mock())
        overlay = SimpleNamespace(show_message=Mock())
        window = SimpleNamespace(_reload_configuration_widgets=Mock())
        tool = SimpleNamespace(
            store=store,
            message_overlay=overlay,
            _configuration_import_is_blocked=Mock(return_value=False),
            _configuration_windows=Mock(return_value=(window,)),
        )

        with (
            patch.object(
                main.QFileDialog,
                "getOpenFileName",
                return_value=("backup.deployconfig", "config"),
            ),
            patch.object(
                main.QMessageBox,
                "question",
                return_value=main.QMessageBox.Yes,
            ),
            patch.object(main.QInputDialog, "getText") as get_password,
        ):
            DeployTool.import_configuration(tool)

        get_password.assert_not_called()
        store.import_from.assert_called_once_with("backup.deployconfig")
        window._reload_configuration_widgets.assert_called_once_with()
        overlay.show_message.assert_called_once()

    def test_window_close_is_blocked_while_pipeline_runs(self):
        event = Mock()
        tool = SimpleNamespace(build_running=True, _workers=[])

        with patch("main.QMessageBox.warning") as warning:
            DeployTool.closeEvent(tool, event)

        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        warning.assert_called_once()

    def test_new_parallel_task_inherits_current_ssh_environment(self):
        class Combo:
            def __init__(self, value):
                self.value = value

            def currentText(self):
                return self.value

            def setCurrentText(self, value):
                self.value = value

        child = SimpleNamespace(
            build_deploy_mode=Combo("云端部署"),
            build_env_combo=Combo(""),
            batch_status_label=Mock(),
        )
        manager = Mock()
        manager.create_window.return_value = child
        tool = SimpleNamespace(
            window_manager=manager,
            build_deploy_mode=Combo("SSH 环境部署"),
            build_env_combo=Combo("环境2（江湖）"),
        )

        DeployTool.request_new_task_window(tool)

        manager.create_window.assert_called_once_with(tool)
        self.assertEqual(child.build_deploy_mode.currentText(), "SSH 环境部署")
        self.assertEqual(child.build_env_combo.currentText(), "环境2（江湖）")
        child.batch_status_label.setText.assert_called_once_with("自动合批待命")

    def test_task_center_embeds_tasks_and_tracks_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.multiple(
                main,
                CONFIG_DIR=root / "parallel",
                CONFIG_FILE=root / "parallel" / "config.json",
                LEGACY_CONFIG_FILE=root / "legacy" / "config.json",
            ):
                manager = ApplicationWindowManager(self.qt_app)
                first = manager.create_window()
                second = manager.create_window(first)
                manager.resize(1440, 960)
                manager.show()
                self.qt_app.processEvents()

                self.assertEqual(manager.task_stack.count(), 2)
                self.assertFalse(first.isWindow())
                self.assertFalse(second.isWindow())
                self.assertTrue(manager.task_center.expanded)
                self.assertEqual(
                    manager.windowTitle(), "Deploy Console 并行任务中心版"
                )
                self.assertFalse(hasattr(manager.task_center, "new_task_button"))
                self.assertEqual(first.title_bar.new_task_button.height(), 34)
                self.assertEqual(first.title_bar.config_button.height(), 34)
                self.assertEqual(
                    first.title_bar.new_task_button.y(),
                    first.title_bar.config_button.y(),
                )
                cancel_button, remove_button = manager.task_center._actions[first]
                self.assertEqual(cancel_button.text(), "停止")
                self.assertEqual(remove_button.text(), "移除")
                self.assertEqual(manager.task_center.tree.columnWidth(6), 184)
                self.assertEqual(cancel_button.width(), 68)
                self.assertEqual(remove_button.width(), 68)
                self.assertGreaterEqual(
                    remove_button.x() - cancel_button.geometry().right() - 1, 8
                )
                self.assertGreaterEqual(
                    cancel_button.parentWidget().height(), cancel_button.height() + 4
                )
                manager.activate_task(first)
                self.assertEqual(
                    manager.windowTitle(), "Deploy Console 并行任务中心版"
                )

                first._begin_tracked_task(
                    "一键流程",
                    "环境1",
                    package="etmbackend-5.3.0:5.3.0.1001.10581",
                    title="etmbackend",
                )
                self.assertEqual(first.task_summary()["state"], "running")
                first._task_phase = "一键流程执行完成"
                first._finish_tracked_task()
                self.assertEqual(first.task_summary()["state"], "succeeded")
                self.assertEqual(first.task_summary()["progress"], 100)

                manager.task_center.set_expanded(False)
                second._begin_tracked_task("SSH 部署", "环境1", title="etmfrontend")
                second._task_phase = "部署失败"
                second._task_failed = True
                second._finish_tracked_task()
                self.assertEqual(second.task_summary()["state"], "failed")
                self.assertTrue(manager.task_center.expanded)
                manager.task_center.set_expanded(False)
                self.qt_app.processEvents()
                self.assertEqual(manager.task_dock.height(), 46)
                self.assertGreaterEqual(manager.task_center.toggle_button.y(), 0)
                self.assertLessEqual(
                    manager.task_center.toggle_button.geometry().bottom(),
                    manager.task_center.rect().bottom(),
                )
                manager.task_center.refresh_elapsed()
                self.assertFalse(manager.task_center.expanded)

                manager.remove_task(second)
                self.assertEqual(manager.task_stack.count(), 1)
                self.assertIs(manager.current_task, first)
                manager.close()
                self.qt_app.processEvents()

    def test_running_workspace_rejects_a_second_operation(self):
        overlay = SimpleNamespace(show_message=Mock())
        tool = SimpleNamespace(
            _task_runtime_active=True,
            message_overlay=overlay,
        )

        DeployTool.start_build(tool)
        DeployTool.start_deploy(tool)
        DeployTool.start_cloud_deploy(tool)

        self.assertEqual(overlay.show_message.call_count, 3)
        for call in overlay.show_message.call_args_list:
            self.assertEqual(call.args[0], "当前任务正在运行")

    @patch("main.time.sleep")
    @patch("main.time.monotonic", side_effect=[0, 0, 3, 3])
    def test_wait_for_remote_image_retries_until_registry_is_ready(
        self, monotonic, sleep
    ):
        tool = SimpleNamespace(
            _remote_command_succeeded=Mock(side_effect=[False, True]),
            emit=Mock(),
        )

        DeployTool._wait_for_remote_image(
            tool,
            Mock(),
            "registry.example/industry/etmbackend:1",
            timeout_seconds=30,
        )

        self.assertEqual(tool._remote_command_succeeded.call_count, 2)
        sleep.assert_called_once_with(3)
        self.assertTrue(
            any("等待仓库同步" in call.args[1] for call in tool.emit.call_args_list)
        )

    def test_wait_for_remote_image_honors_cancellation(self):
        cancel_event = threading.Event()
        cancel_event.set()
        tool = SimpleNamespace(
            _remote_command_succeeded=Mock(),
            emit=Mock(),
        )

        with self.assertRaisesRegex(PipelineCancelled, "等待目标镜像期间已取消"):
            DeployTool._wait_for_remote_image(
                tool,
                Mock(),
                "registry.example/industry/etmbackend:1",
                cancel_event=cancel_event,
            )

        tool._remote_command_succeeded.assert_not_called()

    def test_ssh_target_keys_allow_different_yamls_in_same_environment(self):
        first = DeployTool._deployment_target_key(
            "SSH 环境部署",
            environment={
                "host": "172.16.6.124",
                "port": "22",
                "yaml_dir": "/data/yaml/app/backend",
                "yaml_file": "etm.yaml",
            },
        )
        same = DeployTool._deployment_target_key(
            "SSH 环境部署",
            environment={
                "host": "172.16.6.124",
                "port": 22,
                "yaml_dir": "/data/yaml/app/backend",
                "yaml_file": "ETM.YAML",
            },
        )
        different = DeployTool._deployment_target_key(
            "SSH 环境部署",
            environment={
                "host": "172.16.2.23",
                "port": "22",
                "yaml_dir": "/data/yaml/app/backend",
                "yaml_file": "etm.yaml",
            },
        )
        same_host_different_product = DeployTool._deployment_target_key(
            "SSH 环境部署",
            environment={
                "host": "172.16.6.124",
                "port": "22",
                "yaml_dir": "/data/yaml/app/backend",
                "yaml_file": "esc.yaml",
            },
        )

        self.assertEqual(first, same)
        self.assertNotEqual(first, same_host_different_product)
        self.assertNotEqual(first, different)


if __name__ == "__main__":
    unittest.main()
