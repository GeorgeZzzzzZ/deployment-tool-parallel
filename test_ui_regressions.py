import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from PySide6.QtTest import QTest


class UiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])
        if main.sys.platform == "win32" and os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            # The Windows offscreen backend does not discover system fonts.
            # Load real glyph metrics so layout checks match the desktop UI.
            for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
                main.QFontDatabase.addApplicationFont(
                    str(Path(os.environ["WINDIR"]) / "Fonts" / name)
                )
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
        with patch.object(main.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.manager = main.ApplicationWindowManager(self.app, store=store)
        self.task = self.manager.create_window()
        self.manager.resize(1440, 960)
        self.manager.task_center.set_expanded(True)
        self.manager.show()
        self.app.processEvents()

    def tearDown(self):
        self.manager._closing = True
        self.manager.close()
        self.manager.deleteLater()
        self.app.processEvents()

    def click_column(self, item, column):
        tree = self.manager.task_center.tree
        x = tree.header().sectionViewportPosition(column) + 15
        y = tree.visualItemRect(item).center().y()
        QTest.mouseClick(tree.viewport(), main.Qt.LeftButton, pos=main.QPoint(x, y))

    def test_click_package_copies_full_value_and_activates_its_task(self):
        package = "escbackend-5.3.0:5.3.0.1001.10303"
        self.task._task_package = package
        self.manager.task_center.update_task(self.task)
        second = self.manager.create_window(self.task)
        self.app.processEvents()
        self.assertIs(self.manager.current_task, second)
        self.click_column(self.manager.task_center._items[self.task], 1)
        self.assertEqual(self.app.clipboard().text(), package)
        self.assertIs(self.manager.current_task, self.task)
        self.assertEqual(self.manager.task_center.copy_feedback.text(), "已复制包号")

    def test_other_columns_and_empty_package_preserve_clipboard(self):
        self.app.clipboard().setText("existing clipboard")
        panel = self.manager.task_center
        self.click_column(panel._items[self.task], 1)
        self.task._task_package = "escbackend:1"
        panel.update_task(self.task)
        self.click_column(panel._items[self.task], 3)
        self.assertEqual(self.app.clipboard().text(), "existing clipboard")

    def test_ctrl_c_copies_selected_package(self):
        self.task._task_package = "etmbackend-5.3.0:5.3.0.1001.10787"
        panel = self.manager.task_center
        panel.update_task(self.task)
        panel.tree.setFocus()
        self.app.processEvents()
        QTest.keyClick(panel.tree, main.Qt.Key_C, main.Qt.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), self.task._task_package)

    def test_environment_fields_keep_height_and_are_scroll_reachable(self):
        self.task.tab_widget.setCurrentIndex(1)
        for width, height in ((1440, 960), (1080, 760), (1800, 1000)):
            with self.subTest(size=(width, height)):
                self.manager.resize(width, height)
                self.app.processEvents()
                entries = list(self.task.env_entries.values())
                for entry in entries:
                    self.assertGreaterEqual(entry.height(), entry.minimumSizeHint().height())
                    self.assertTrue(entry.parentWidget().rect().contains(entry.geometry()))
                for index in range(len(entries) - 2):
                    self.assertLess(entries[index].geometry().bottom(), entries[index + 2].y())
                scroll = self.task.deploy_scroll
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                for entry in entries:
                    scroll.ensureWidgetVisible(entry)
                    self.app.processEvents()
                    point = entry.mapTo(scroll.viewport(), entry.rect().center())
                    self.assertTrue(scroll.viewport().rect().contains(point))

    def test_build_environment_actions_fit_and_align_right(self):
        self.task.build_env_card.set_expanded(True)
        for width in (1080, 1440):
            self.manager.resize(width, 850)
            self.app.processEvents()
            buttons = {button.text(): button for button in self.task.build_env_card.findChildren(main.QPushButton)}
            save, refresh = buttons["保存环境配置"], buttons["刷新"]
            for button in (save, refresh):
                self.assertGreaterEqual(button.height(), button.minimumSizeHint().height())
                self.assertGreaterEqual(button.width(), button.minimumSizeHint().width())
                self.assertTrue(button.parentWidget().rect().contains(button.geometry()))
            self.assertGreater(save.x(), save.parentWidget().width() // 2)
            self.assertLess(save.geometry().right(), refresh.x())
            self.assertLessEqual(refresh.parentWidget().width() - refresh.geometry().right(), 2)

    def test_remote_layer_bytes_reach_deployment_log_and_waiting_label_fits(self):
        chunks = [b"a: Downloading [=====>     ] 50MB/100MB\r",
                  b"a: Extracting [==>        ] 25MB/100MB\r"]
        channel = SimpleNamespace(
            exit_status_ready=lambda: not chunks, recv_ready=lambda: bool(chunks),
            recv=lambda size: chunks.pop(0), recv_stderr_ready=lambda: False,
            recv_exit_status=lambda: 0,
        )
        client = Mock()
        client.exec_command.return_value = (None, SimpleNamespace(channel=channel), None)
        with patch("main.time.sleep"):
            self.task._remote_exec(client, "fixture", progress_range=(15, 78),
                                   transfer_images=["registry.example/app:1"])
        self.app.processEvents()
        self.assertIn("Downloading [=====>     ] 50MB/100MB", self.task.deploy_log.toPlainText())
        self.assertIn("Extracting [==>        ] 25MB/100MB", self.task.deploy_log.toPlainText())
        self.task.tab_widget.setCurrentIndex(1)
        for width in (1080, 1440):
            self.manager.resize(width, 960)
            self.task._on_deploy_progress(45, "等待远端：120s 无输出 / 120s 无变化")
            self.app.processEvents()
            self.assertGreater(self.task.deploy_progress.width(), 80)
            label = self.task.deploy_progress_label
            self.assertGreaterEqual(label.width(), label.fontMetrics().horizontalAdvance(label.text()))

    def test_delete_apply_option_fits_requested_positions_at_both_widths(self):
        for width in (1080, 1440):
            self.manager.resize(width, 960)
            self.task.tab_widget.setCurrentIndex(0)
            for expanded in (False, True, False):
                self.task.build_env_card.set_expanded(expanded)
                self.app.processEvents()
                card = self.task.build_env_card
                option = self.task.build_delete_option
                point = option.checkbox.mapTo(card, main.QPoint())
                self.assertTrue(card.rect().contains(point))
                if expanded:
                    self.assertLess(point.x(), card.width() // 2)
                else:
                    self.assertGreater(point.x(), card.width() // 2)
                self.assertLess(card._toggle.mapTo(card, main.QPoint()).x(), 40)
                self.assertTrue(option.checkbox.parentWidget().rect().contains(option.checkbox.geometry()))
                self.assertGreaterEqual(option.checkbox.width() - option.checkbox.TEXT_X,
                                        option.checkbox.fontMetrics().horizontalAdvance(option.checkbox.text()))
                if expanded:
                    self.assertLess(option.checkbox.geometry().right(), option.command.x())
                    self.assertGreater(option.command.width(), 100)
                    self.assertTrue(option.rect().contains(option.command.geometry()))
            self.task.tab_widget.setCurrentIndex(1)
            self.app.processEvents()
            option = self.task.deploy_delete_option
            container = option.parentWidget()
            self.assertGreater(option.x(), container.width() * 0.4)
            self.assertTrue(container.rect().contains(option.geometry()))
            self.assertLess(option.checkbox.geometry().right(), option.command.x())
            self.assertGreater(option.command.width(), 100)
            self.assertTrue(option.rect().contains(option.command.geometry()))

    def test_environment_trace_region_can_be_clicked_before_selecting_job(self):
        self.task.cloud_username.setText("fixture-user")
        self.task.cloud_password.setText("fixture-password")
        settings = self.task._cloud_settings_from_ui()
        for project in ("", "etm"):
            self.task._save_selection_cache("cloud_targets", self.task._cloud_options_cache_key(
                {**settings, "project": project}), {
                    "projects": ["etm"], "project": project, "types": ["etm 后端"] if project else [],
                    "regions_by_type": {"etm 后端": ["Huawei / 测试", "Huawei / 正式"]} if project else {},
                }, save=False)
        self.task.tab_widget.setCurrentIndex(3)
        self.task.trace_mode.setCurrentText("环境溯源")
        self.app.processEvents()
        region = self.task.trace_region
        self.assertTrue(region.isEnabled())
        QTest.mouseClick(region, main.Qt.LeftButton, pos=region.rect().center())
        self.app.processEvents()
        self.assertTrue(region.view().isVisible())
        index = region.model().index(region.findText("Huawei / 正式"), 0)
        QTest.mouseMove(region.view().viewport(), region.view().visualRect(index).center())
        QTest.mouseClick(region.view().viewport(), main.Qt.LeftButton,
                         pos=region.view().visualRect(index).center())
        self.app.processEvents()
        self.assertEqual(region.currentText(), "Huawei / 正式")
        self.assertEqual(self.task._trace_job_path, [])

    def test_secondary_branches_share_one_visible_refresh_button(self):
        self.task._show_parameters(["custom-dev", "etm"], [
            {"name": "CUSTOM_NAME", "type": "ChoiceParameterDefinition",
             "choices": ["yishen", "hbxx"], "defaultParameterValue": {"value": "hbxx"},
             "description": "yishen-示例\nhyx-示例\nthj-示例\nfydl-示例\nyzgd-示例\nbmgd-示例\nhbxx-示例"},
            {"name": "GIT_BRANCH2", "type": "StringParameterDefinition",
             "defaultParameterValue": {"value": ""}},
            {"name": "GIT_BRANCH", "type": "GitParameterDefinition",
             "choices": ["origin/main", "origin/f-case451-423-449"],
             "defaultParameterValue": {"value": "origin/main"}},
        ])
        primary = self.task.param_vars["GIT_BRANCH"][1]
        secondary = self.task.param_vars["GIT_BRANCH2"][1]
        customer = self.task.param_vars["CUSTOM_NAME"][1]
        button = self.task.param_refresh_buttons["GIT_BRANCH"]
        secondary.setCurrentText("f-case451-423-449")
        self.assertFalse(self.task.params_card._toggle.isChecked())
        for width in (1080, 1440):
            compact_geometry = None
            for expanded in (False, True, False):
                self.manager.resize(width, 960)
                self.task.params_card.set_expanded(expanded)
                self.app.processEvents()
                visible = [item for item in self.task.findChildren(main.QPushButton)
                           if item.text() == "刷新分支" and item.isVisible()]
                self.assertEqual(visible, [button])
                self.assertEqual(primary.isVisible(), expanded)
                self.assertTrue(secondary.isVisible())
                self.assertTrue(customer.isVisible())
                self.assertTrue(self.task.param_refresh_buttons["CUSTOM_NAME"].isVisible())
                self.assertEqual(secondary.currentText(), "f-case451-423-449")
                self.assertEqual(customer.currentText(), "hbxx")
                self.assertGreaterEqual(button.width(), button.minimumSizeHint().width())
                self.assertEqual(button.mapToGlobal(main.QPoint()).y(), secondary.mapToGlobal(main.QPoint()).y())
                self.assertTrue(button.parentWidget().rect().contains(button.geometry()))
                if expanded:
                    self.assertEqual(primary.width(), secondary.width())
                    bottom = primary.mapToGlobal(main.QPoint()).y() + primary.height()
                    self.assertEqual(button.mapToGlobal(main.QPoint()).y() + button.height(), bottom)
                    self.assertGreater(button.mapToGlobal(main.QPoint()).x(), secondary.mapToGlobal(main.QPoint()).x() + secondary.width())
                else:
                    self.assertEqual(button.height(), secondary.height())
                    self.assertLess(self.task.params_card.height(), 200)
                    self.assertLess(customer.mapToGlobal(main.QPoint()).y(), secondary.mapToGlobal(main.QPoint()).y())
                    geometry = (self.task.params_card.size(), customer.size(), secondary.size(), button.size())
                    if compact_geometry is not None:
                        self.assertEqual(geometry, compact_geometry)
                    compact_geometry = geometry
                with patch.object(self.task, "refresh_branch_choices") as refresh:
                    QTest.mouseClick(button, main.Qt.LeftButton,
                                     pos=main.QPoint(button.width() // 2, button.height() - 4))
                    refresh.assert_called_once_with("GIT_BRANCH")

    def test_custom_name_refresh_fits_to_right_of_selector(self):
        self.task._show_parameters(["service"], [
            {"name": "CUSTOM_NAME", "type": "ChoiceParameterDefinition",
             "choices": ["yishen", "hbxx"], "defaultParameterValue": {"value": "hbxx"}},
        ])
        self.task.params_card.set_expanded(True)
        widget = self.task.param_vars["CUSTOM_NAME"][1]
        button = self.task.param_refresh_buttons["CUSTOM_NAME"]
        for width in (1080, 1440):
            self.manager.resize(width, 960)
            self.app.processEvents()
            self.assertTrue(button.isVisible())
            self.assertEqual(button.text(), "刷新选项")
            self.assertEqual(button.height(), widget.height())
            self.assertGreaterEqual(button.width(), button.minimumSizeHint().width())
            self.assertTrue(button.parentWidget().rect().contains(button.geometry()))
            self.assertGreater(button.x(), widget.geometry().right())
            self.assertEqual(button.y(), widget.y())

    def test_login_button_requests_visible_browser_and_blocks_reentry(self):
        self.task.cloud_url.setText("https://example.invalid/cdm/deployment/flow-deploy")
        self.task.cloud_username.setText("test-user")
        self.task.cloud_password.setText("test-password")
        with patch.object(self.task, "_save_cloud_settings"), patch.object(
            self.task, "run_thread"
        ) as worker, patch.object(self.task, "_login_and_load_cloud") as login:
            self.task.cloud_login_button.click()
            self.task._on_cloud_status("已登录")
            self.assertFalse(self.task.cloud_login_button.isEnabled())
            self.task.login_cloud_platform(open_browser=True)
            worker.assert_called_once()
            worker.call_args.args[0]()
            self.assertTrue(login.call_args.kwargs["open_browser"])
            worker.call_args.kwargs["on_finished"]()
            self.assertTrue(self.task.cloud_login_button.isEnabled())
            self.assertFalse(self.task._cloud_login_in_progress)

    def test_browser_login_success_updates_feedback_without_modal_dialogs(self):
        self.task.cloud_status_login_button.setEnabled(False)
        with patch.object(main.QMessageBox, "information") as information, patch.object(
            self.task.message_overlay, "show_message"
        ) as overlay:
            self.task.sig_cloud_status_login_ok.emit("Rancher 示例状态页")
            self.task.emit("dashboard_login_ok", ("示例环境", "pqm"))
            self.app.processEvents()
        information.assert_not_called()
        overlay.assert_not_called()
        self.assertTrue(self.task.cloud_status_login_button.isEnabled())
        self.assertEqual(self.task.cloud_status_login_state.text(), "已登录")
        self.assertIn("已打开并登录：Rancher 示例状态页", self.task.cloud_log.toPlainText())
        self.assertIn("示例环境", self.task.deploy_log.toPlainText())
        if main.sys.platform == "win32":
            self.assertIn("pqm Pods", self.task.deploy_log.toPlainText())


class OpsBrowserLoginTests(unittest.TestCase):
    def tool(self):
        return SimpleNamespace(
            _ensure_cloud_login=Mock(), _load_cloud_options=Mock(),
            _ensure_cloud_browser_login=Mock(), emit=Mock(),
        )

    def test_background_options_do_not_open_browser(self):
        for source in ("tab", "build"):
            with self.subTest(source=source):
                tool = self.tool()
                main.DeployTool._login_and_load_cloud(tool, {}, source)
                tool._ensure_cloud_browser_login.assert_not_called()
                tool._load_cloud_options.assert_called_once_with({}, source)

    def test_explicit_login_opens_visible_browser_after_loading_data(self):
        tool = self.tool()
        settings = {"url": "https://example.invalid"}
        main.DeployTool._login_and_load_cloud(tool, settings, open_browser=True)
        tool._ensure_cloud_browser_login.assert_called_once_with(
            settings, headless=False, report_status=False
        )
        tool.emit.assert_any_call("cloud_status", "已登录，Ops 网页已打开")

    def test_browser_failure_preserves_api_and_loaded_data(self):
        tool = self.tool()
        api = object()
        tool.ops_api = api
        tool._ensure_cloud_browser_login.side_effect = RuntimeError("test browser unavailable")
        main.DeployTool._login_and_load_cloud(tool, {}, open_browser=True)
        self.assertIs(tool.ops_api, api)
        tool._load_cloud_options.assert_called_once()
        tool.emit.assert_any_call("cloud_status", "API 已登录，网页自动登录失败")
        self.assertTrue(any(call.args[0] == "info" for call in tool.emit.call_args_list))

    def test_api_failure_does_not_start_browser(self):
        tool = self.tool()
        tool._ensure_cloud_login.side_effect = RuntimeError("test API failure")
        with self.assertRaisesRegex(RuntimeError, "test API failure"):
            main.DeployTool._login_and_load_cloud(tool, {}, open_browser=True)
        tool._ensure_cloud_browser_login.assert_not_called()
        tool._load_cloud_options.assert_not_called()

    def test_visible_browser_fills_credentials_and_only_clicks_login(self):
        state = {"authenticated": False}
        account = Mock(is_displayed=Mock(return_value=True))
        account.get_attribute.side_effect = lambda name: {"name": "username"}.get(name, "")
        password = Mock(is_displayed=Mock(return_value=True), is_enabled=Mock(return_value=True))
        button = Mock(text="登录", is_displayed=Mock(return_value=True), is_enabled=Mock(return_value=True))
        button.get_attribute.return_value = "submit"
        button.click.side_effect = lambda: state.update(authenticated=True)
        driver = Mock(current_url="https://example.invalid/login")

        def find_elements(_by, selector):
            if selector == "app-flow-deploy":
                return [object()] if state["authenticated"] else []
            if selector == "input[type='password']":
                return [] if state["authenticated"] else [password]
            if selector == "input":
                return [account]
            if selector == "button":
                return [button]
            return []

        driver.find_elements.side_effect = find_elements
        tool = SimpleNamespace(
            cloud_driver=None, _cloud_driver_is_alive=Mock(return_value=False),
            _first_visible=main.DeployTool._first_visible, emit=Mock(),
        )
        settings = {"url": "https://example.invalid/deploy", "username": "test-user", "password_plain": "test-password"}
        with patch("main.create_browser_driver", return_value=driver) as create, patch(
            "main.browser_session_token", return_value=""
        ):
            main.DeployTool._ensure_cloud_browser_login(
                tool, settings, headless=False, report_status=False
            )
        create.assert_called_once_with(headless=False)
        account.send_keys.assert_any_call("test-user")
        password.send_keys.assert_any_call("test-password")
        button.click.assert_called_once_with()
        driver.get.assert_called_once_with(settings["url"])
        self.assertIs(tool.cloud_driver, driver)


if __name__ == "__main__":
    unittest.main()
