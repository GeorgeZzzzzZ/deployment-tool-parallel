import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import deploy_glass
import main
from PySide6.QtCore import QAbstractAnimation


class GlassInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])
        if sys.platform == "win32":
            for font in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
                main.QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"]) / "Fonts" / font))
        cls.previous_style = cls.app.styleSheet()
        cls.app.setStyleSheet(main.GLOBAL_QSS)

    @classmethod
    def tearDownClass(cls):
        cls.app.setStyleSheet(cls.previous_style)

    def setUp(self):
        store = SimpleNamespace(data={
            "jenkins": {"url": "", "username": "", "password": "", "values": {}},
            "environments": [], "dashboards": [], "last_package": "",
        }, save=Mock())
        self.build_mock = Mock()
        with patch.object(main.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False), patch.object(
            main.DeployTool, "start_build", self.build_mock
        ):
            self.manager = main.ApplicationWindowManager(self.app, store=store)
            self.task = self.manager.create_window()
        self.app.processEvents()

    def tearDown(self):
        self.manager._closing = True
        self.manager.close()
        self.manager.deleteLater()
        self.app.processEvents()

    def test_pages_and_tasks_keep_glass_layers_when_switching(self):
        self.assertIsInstance(self.task.centralWidget(), deploy_glass.GlassBackground)
        self.assertIsInstance(self.manager.task_center, deploy_glass.GlassBackground)
        self.assertTrue(self.task.findChildren(deploy_glass.GlassPanel))
        for index in range(3):
            self.task.tab_widget.setCurrentIndex(index)
            self.app.processEvents()
            self.assertTrue(self.task.tab_widget.currentWidget().isVisible())
            self.assertFalse(self.task.tab_widget.tabIcon(index).isNull())
        second = self.manager.create_window()
        self.assertIs(self.manager.current_task, second)
        self.manager.activate_task(self.task)
        self.assertIs(self.manager.current_task, self.task)

    def test_reduced_motion_changes_pixels_without_animating_or_resizing(self):
        card = next(card for card in self.task.findChildren(deploy_glass.GlassPanel) if card.isVisible())
        card._set_hover_amount(0)
        before = card.grab().toImage()
        size = card.size()
        with patch.object(deploy_glass, "is_reduced_motion", return_value=True):
            card._animate_to(1)
        self.assertEqual(card._anim.state(), QAbstractAnimation.Stopped)
        self.assertEqual(card.hover_amount, 1)
        self.assertEqual(card.size(), size)
        self.assertNotEqual(card.grab().toImage(), before)

    def test_build_controls_fit_compact_window_and_keep_callbacks(self):
        self.manager.resize(1080, 760)
        self.app.processEvents()
        button = self.task.build_button
        self.assertGreaterEqual(button.width(), button.sizeHint().width())
        controls = (button, self.task.cancel_button, self.task.build_deploy_mode, self.task.build_env_combo)
        for control in controls:
            self.assertTrue(control.parentWidget().rect().contains(control.geometry()))
        for index, control in enumerate(controls):
            for other in controls[index + 1:]:
                self.assertFalse(control.geometry().intersects(other.geometry()))
        self.assertFalse(self.task.cancel_button.isEnabled())
        self.assertFalse(button.icon().isNull())
        button.setEnabled(True)
        button.click()
        self.build_mock.assert_called_once()

    def test_build_toolbar_wraps_only_when_needed_and_reacts_to_mode_changes(self):
        for mode in ("SSH 环境部署", "云端部署", "SSH 环境部署"):
            self.task.build_deploy_mode.blockSignals(True)
            self.task.build_deploy_mode.setCurrentText(mode)
            self.task.build_deploy_mode.blockSignals(False)
            self.task._apply_build_deploy_mode(mode, prompt_for_account=False, refresh_cloud=False)
            for width in (1440, 1080, 1440):
                with self.subTest(mode=mode, width=width):
                    self.manager.resize(width, 960)
                    self.app.processEvents()
                    self.app.processEvents()
                    self.assertEqual(self.manager.width(), width)
                    controls = [
                        self.task.deploy_after_build_checkbox, self.task.build_button,
                        self.task.cancel_button, self.task.build_deploy_mode,
                        self.task.build_env_label, self.task.build_env_combo,
                        self.task.build_status_badge, self.task.build_progress,
                    ]
                    controls = [control for control in controls if control.isVisible()]
                    for index, control in enumerate(controls):
                        self.assertTrue(self.task.build_action_card.rect().contains(control.geometry()))
                        for other in controls[index + 1:]:
                            self.assertFalse(control.geometry().intersects(other.geometry()))
                    centers = [control.geometry().center().y() for control in controls]
                    if width == 1440:
                        self.assertLessEqual(max(centers) - min(centers), 1)
                    else:
                        self.assertGreater(max(centers) - min(centers), 20)

    def test_status_rows_keep_buttons_right_aligned_and_vertically_centered(self):
        self.task.tab_widget.setCurrentIndex(2)
        self.app.processEvents()
        for button, label, field in (
            (self.task.cloud_login_button, self.task.cloud_status_label, self.task.cloud_password),
            (self.task.cloud_status_login_button, self.task.cloud_status_login_state,
             self.task.cloud_status_password),
        ):
            self.assertLessEqual(abs(button.geometry().center().y() - label.geometry().center().y()), 1)
            self.assertEqual(button.geometry().right(), field.geometry().right())
            self.assertLess(label.geometry().right(), button.x())
        self.task.tab_widget.setCurrentIndex(0)
        self.task._apply_build_deploy_mode("云端部署", prompt_for_account=False, refresh_cloud=False)
        for loading in (False, True, False):
            self.task._set_build_cloud_loading(loading, "正在加载云端部署目标" if loading else "云端部署目标已就绪")
            self.app.processEvents()
            self.app.processEvents()
            button = self.task.build_cloud_refresh_button
            label = self.task.build_cloud_loading_label
            self.assertLessEqual(abs(button.geometry().center().y() - label.geometry().center().y()), 1)
            self.assertEqual(button.geometry().right(), self.task.build_cloud_region.geometry().right())
            self.assertLess(label.geometry().right(), button.x())

    def test_environment_toolbar_has_consistent_height_and_center(self):
        self.task.tab_widget.setCurrentIndex(1)
        self.app.processEvents()
        combo = self.task.env_combo
        buttons = combo.parentWidget().findChildren(main.QPushButton)
        self.assertEqual(len(buttons), 4)
        for button in buttons:
            self.assertEqual(button.height(), combo.height())
            self.assertEqual(button.geometry().center().y(), combo.geometry().center().y())

    def test_loaded_cloud_page_has_text_login_buttons_and_right_aligned_actions(self):
        self.task.tab_widget.setCurrentIndex(2)
        self.task._on_cloud_status("已登录")
        for width in (1440, 1080):
            with self.subTest(width=width):
                self.manager.resize(width, 960)
                self.app.processEvents()
                self.app.processEvents()
                for button in (self.task.cloud_login_button, self.task.cloud_status_login_button):
                    self.assertTrue(button.icon().isNull())
                refresh = self.task.cloud_refresh_button
                deploy = self.task.cloud_deploy_button
                self.assertEqual(deploy.geometry().right(), self.task.cloud_region.geometry().right())
                self.assertEqual(refresh.geometry().center().y(), deploy.geometry().center().y())
                self.assertGreaterEqual(deploy.x() - refresh.geometry().right() - 1, 8)
                self.assertTrue(deploy.parentWidget().rect().contains(deploy.geometry()))
                self.assertTrue(refresh.parentWidget().rect().contains(refresh.geometry()))

    def test_task_center_chevrons_are_visible_and_toggle_the_panel(self):
        panel = self.manager.task_center
        panel.set_expanded(False)
        collapsed = panel.toggle_button.icon().pixmap(22, 22).toImage()
        panel.toggle_button.click()
        self.app.processEvents()
        self.assertTrue(panel.expanded)
        expanded = panel.toggle_button.icon().pixmap(22, 22).toImage()
        self.assertNotEqual(collapsed, expanded)
        for image in (collapsed, expanded):
            bright_pixels = sum(
                image.pixelColor(x, y).alpha() > 200 and image.pixelColor(x, y).lightness() > 220
                for x in range(image.width()) for y in range(image.height())
            )
            self.assertGreater(bright_pixels, 10)
        panel.toggle_button.click()
        self.assertFalse(panel.expanded)


if __name__ == "__main__":
    unittest.main()
