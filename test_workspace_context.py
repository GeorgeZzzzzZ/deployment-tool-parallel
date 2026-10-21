import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from PySide6.QtCore import QRect
from core import cloud_project_matches_job


class WorkspaceContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])
        cls.previous_style = cls.app.styleSheet()
        cls.app.setStyleSheet(main.GLOBAL_QSS)
        for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            main.QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"]) / "Fonts" / name))

    @classmethod
    def tearDownClass(cls):
        cls.app.setStyleSheet(cls.previous_style)

    def setUp(self):
        self.store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        self.login_patch = patch.object(main.DeployTool, "_auto_login_jenkins")
        self.login_patch.start()
        with patch.object(main.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.manager = main.ApplicationWindowManager(self.app, store=self.store)
        self.task = self.manager.create_window()
        self.manager.resize(1440, 960)
        self.manager.show()
        self.app.processEvents()

    def tearDown(self):
        self.manager._closing = True
        self.manager.close()
        self.manager.deleteLater()
        self.app.processEvents()
        self.login_patch.stop()

    @staticmethod
    def pages(task):
        return [task.tab_widget.widget(i).objectName() for i in range(task.tab_widget.count())]

    def test_drag_order_keeps_pages_icons_and_navigation_and_syncs_tasks(self):
        second = self.manager.create_window(self.task)
        self.task.tab_widget.tabBar().moveTab(2, 1)
        expected = ["JenkinsPage", "CloudDeployPage", "DeployPage", "PackageTracePage"]
        self.assertTrue(self.task.tab_widget.isMovable())
        for task in (self.task, second):
            self.assertEqual(self.pages(task), expected)
            self.assertIn("云端部署", task.tab_widget.tabText(1))
            task._on_select_deploy_tab()
            self.assertEqual(task.tab_widget.currentWidget().objectName(), "DeployPage")
            with patch.object(main.QMessageBox, "warning"):
                task.refresh_build_cloud_options()
            self.assertEqual(task.tab_widget.currentWidget().objectName(), "CloudDeployPage")
        self.assertEqual(self.store.data["tab_order"], expected)
        third = self.manager.create_window(self.task)
        self.assertEqual(self.pages(third), expected)
        self.assertEqual(third.tab_widget.currentWidget().objectName(), "JenkinsPage")

    def test_malformed_saved_tab_order_keeps_all_pages(self):
        self.task._apply_tab_order(["unknown", "CloudDeployPage", "CloudDeployPage", None])
        self.assertEqual(self.pages(self.task), ["CloudDeployPage", "JenkinsPage", "DeployPage", "PackageTracePage"])
        self.assertEqual(self.task.tab_widget.currentWidget().objectName(), "JenkinsPage")
        self.task._apply_tab_order("invalid")
        self.assertEqual(self.task.tab_widget.count(), 4)

    def test_image_progress_keeps_other_tasks_waiting_and_maps_overall_progress(self):
        tasks = [self.task, self.manager.create_window(self.task), self.manager.create_window(self.task)]
        images = [f"registry.example/{name}backend:1" for name in ("etm", "pqm", "mms")]
        for task in tasks:
            task._task_runtime_active = True
            task._task_state = "running"
            task.build_running = task.active_deploy_after_build = True
            task._on_deploy_progress(15, "等待拉取镜像...")
        progress = main.DockerImageProgress(images, (15, 78))
        for line in ("处理镜像: etmbackend:1", "a: Downloading 50MB/100MB"):
            image, percent, text = progress.feed(line)
            tasks[images.index(image)]._on_deploy_progress(percent, text)
        self.app.processEvents()
        self.assertEqual([task._task_progress for task in tasks], [75, 70, 70])
        self.assertEqual([self.manager.task_center._items[task].text(4) for task in tasks],
                         ["75%", "70%", "70%"])
        self.assertIn("50%", tasks[0].deploy_progress_label.text())
        self.assertTrue(all(task.deploy_progress_label.text() == "等待拉取镜像..." for task in tasks[1:]))
        for task in tasks:
            task.build_running = task.active_deploy_after_build = False
            task._task_runtime_active = False

    def test_build_controls_stay_visible_while_scrolling_at_small_and_large_sizes(self):
        self.task.build_cloud_card.show()
        self.task.params_card.set_expanded(True)
        self.task.build_summary_card.set_expanded(True)
        self.manager.task_center.set_expanded(True)
        for width, height in ((1080, 760), (1440, 960), (1800, 1240)):
            with self.subTest(size=(width, height)):
                self.manager.resize(width, height)
                self.app.processEvents()
                scroll = self.task.jenkins_scroll
                bar = scroll.verticalScrollBar()
                bar.setValue(0)
                self.app.processEvents()
                original = self.task.build_button.mapToGlobal(main.QPoint())
                breadcrumb = self.task.path_label.mapToGlobal(main.QPoint())
                bar.setValue(bar.maximum())
                self.app.processEvents()
                self.assertGreater(bar.maximum(), 0)
                self.assertEqual(self.task.build_button.mapToGlobal(main.QPoint()), original)
                self.assertEqual(self.task.path_label.mapToGlobal(main.QPoint()), breadcrumb)
                for widget in (self.task.build_button, self.task.cancel_button, self.task.build_deploy_mode):
                    point = widget.mapTo(self.task.build_action_card, main.QPoint())
                    self.assertTrue(self.task.build_action_card.rect().contains(QRect(point, widget.size())))
                self.assertGreater(scroll.viewport().height(), 50)

    def test_app_mobile_project_matches_without_weakening_other_module_checks(self):
        folder = ["云应用打包", "16-ETM-生产任务管理"]
        self.assertTrue(cloud_project_matches_job(folder + ["2-APP"], "etmmobile"))
        self.assertTrue(cloud_project_matches_job(folder + ["2-APP"], "etm"))
        for leaf, project in (("2-APP", "escmobile"), ("2-APP", "etmmobile-other"),
                              ("6-SERVICE-5.3.0", "etmmobile"), ("1-WEB", "etmmobile")):
            self.assertFalse(cloud_project_matches_job(folder + [leaf], project))
        self.task.current_job_path = ["cloud", "etm", "app"]
        self.task.current_display_path = folder + ["2-APP"]
        self.task._selected_module_path = self.task.current_display_path
        self.task.build_deploy_mode.blockSignals(True)
        self.task.build_deploy_mode.setCurrentText("云端部署")
        self.task.build_deploy_mode.blockSignals(False)
        settings = {"project": "etmmobile", "deploy_type": "etmmobile 后端", "cloud": "Huawei", "region": "测试"}
        self.task.build_cloud_target_dirty = False
        with patch.object(self.task, "_build_cloud_settings_from_ui", return_value=settings), patch.object(
            self.task, "_cloud_account_complete", return_value=True
        ):
            self.task._update_build_cloud_ready()
        self.assertEqual(self.task.build_cloud_loading_label.text(), "云端部署目标已就绪")
        self.assertTrue(self.task.build_button.isEnabled())
        with patch.object(self.task, "_queue_cloud_options") as queue, patch.object(
            self.task, "_cloud_account_complete", return_value=True
        ):
            self.task._sync_cloud_target_from_job()
        self.assertEqual(self.task.build_cloud_project.currentText(), "etmmobile")
        self.assertEqual(queue.call_args_list[0].args[0]["deploy_type"], "etmmobile 前端")

    def test_new_task_does_not_inherit_other_tasks_build_details(self):
        selection = (object(), ("etm",), "https://jenkins.example/job/etm/")
        self.task._summary_runs[selection] = {"build_url": selection[2] + "873/", "finished": True}
        second = self.manager.create_window(self.task)
        second.jenkins, path, second.current_job_url = selection
        second.current_job_path = list(path)
        with patch.object(second, "run_thread") as worker:
            second._select_build_summary()
        worker.assert_not_called()
        self.assertIn("尚未打包", second.build_summary_label.text())
        self.assertTrue(second.build_changes.isHidden())


if __name__ == "__main__":
    unittest.main()
