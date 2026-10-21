import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from test_package_trace import MemorySource, PACKAGE


class PackageTraceInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.login_patch = patch.object(main.DeployTool, "_auto_login_jenkins")
        self.login_patch.start()
        store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        self.tool = main.DeployTool(store=store, embedded=True)
        self.pending = []
        self.tool.run_thread = lambda function, on_finished=None: self.pending.append(function)
        self.tool.jenkins = SimpleNamespace(url="https://jenkins.example")

    def tearDown(self):
        self.tool.close()
        self.tool.deleteLater()
        self.app.processEvents()
        self.login_patch.stop()

    def test_fourth_page_exists_and_old_saved_order_keeps_it(self):
        tabs = self.tool.tab_widget
        self.assertEqual(tabs.count(), 4)
        self.assertEqual(tabs.widget(3).objectName(), "PackageTracePage")
        self.assertIn("包号溯源", tabs.tabText(3))
        self.tool._apply_tab_order(["CloudDeployPage", "JenkinsPage", "DeployPage"])
        self.assertEqual(tabs.widget(3).objectName(), "PackageTracePage")

    def test_invalid_input_or_missing_login_never_starts_requests(self):
        self.tool.trace_input.setPlainText("invalid")
        self.tool._start_package_trace()
        self.assertEqual(self.pending, [])
        self.tool.trace_input.setPlainText(PACKAGE)
        self.tool.jenkins = None
        self.tool._start_package_trace()
        self.assertIn("登录", self.tool.trace_status.text())
        self.assertEqual(self.pending, [])

    def start_search(self):
        source = MemorySource()
        source.pages = {0: [source.build(3)]}
        self.tool.trace_input.setPlainText(PACKAGE)
        def factory(_client, event, **kwargs):
            source.cancel = event
            return source
        with patch.object(main, "JenkinsHistorySource", side_effect=factory):
            self.tool._start_package_trace()
            self.pending.pop()()
        return source

    def test_search_displays_and_copies_only_the_five_requested_fields(self):
        self.start_search()
        self.assertEqual(self.tool.trace_results.topLevelItemCount(), 1)
        item = self.tool.trace_results.topLevelItem(0)
        self.tool.trace_results.setCurrentItem(item)
        detail = self.tool.trace_details.toPlainText()
        expected = "\n".join([
            "包号：" + PACKAGE, "打包人：打包用户", "打包分支：origin/master",
            "打包时间：2026-05-22 13:10:41", "代码仓库：https://git.example/pqm.git",
        ])
        self.assertEqual(detail, expected)
        self.assertEqual([self.tool.trace_results.headerItem().text(index) for index in range(5)],
                         ["包号", "打包人", "打包分支", "打包时间", "代码仓库"])
        self.assertTrue(self.tool.trace_search_button.isEnabled())
        self.assertFalse(self.tool.trace_cancel_button.isEnabled())
        self.tool._copy_package_trace()
        self.assertEqual(self.app.clipboard().text(), expected)
        self.assertFalse(hasattr(self.tool, "trace_notes"))

    def test_copy_excludes_query_warnings(self):
        self.start_search()
        self.tool.trace_status.setToolTip("查询范围未完整读取")
        self.tool._copy_package_trace()
        self.assertNotIn("查询范围", self.app.clipboard().text())
        self.assertEqual(len(self.app.clipboard().text().splitlines()), 5)

    def test_query_uses_package_scope_without_visible_advanced_controls(self):
        page = self.tool.tab_widget.widget(3)
        self.assertEqual(page.findChildren(main.QComboBox), [])
        self.tool.current_job_path = ["unrelated", "SERVICE"]
        with patch.object(main, "trace_packages", wraps=main.trace_packages) as query:
            self.start_search()
        self.assertEqual(query.call_args.kwargs.get("scope", "auto"), "auto")
        self.assertIsNone(query.call_args.kwargs.get("selected"))

    def test_query_buttons_follow_requested_order_and_align_right(self):
        self.tool._select_page("PackageTracePage")
        self.tool.resize(1400, 850)
        self.tool.show()
        self.app.processEvents()
        buttons = [self.tool.trace_open_button, self.tool.trace_cancel_button,
                   self.tool.trace_copy_button, self.tool.trace_search_button]
        self.assertEqual([button.text() for button in sorted(buttons, key=lambda button: button.x())],
                         ["查看构建", "取消查询", "复制结果", "查询来源"])
        self.assertEqual(len({button.y() for button in buttons}), 1)
        self.assertEqual(buttons[-1].geometry().right(), self.tool.trace_input.geometry().right())

    def test_cancel_does_not_cancel_deployment(self):
        self.tool.trace_input.setPlainText(PACKAGE)
        self.tool._start_package_trace()
        self.tool._cancel_package_trace()
        self.assertTrue(self.tool._trace_request["cancel"].is_set())
        self.assertFalse(self.tool.cancel_event.is_set())
        self.assertFalse(self.tool.trace_cancel_button.isEnabled())

    def test_account_change_discards_old_results_and_releases_controls(self):
        self.tool.trace_input.setPlainText(PACKAGE)
        self.tool._start_package_trace()
        request = self.tool._trace_request
        self.tool.jenkins = SimpleNamespace(url="https://another.example")
        self.tool._on_package_trace({"request": request, "kind": "match", "data": {}})
        self.assertEqual(self.tool.trace_results.topLevelItemCount(), 0)
        self.tool._on_package_trace({"request": request, "kind": "finished", "data": {}})
        self.assertTrue(self.tool.trace_search_button.isEnabled())
        self.assertIsNone(self.tool._trace_request)

    def test_small_workspace_does_not_clip_query_controls(self):
        self.tool._select_page("PackageTracePage")
        self.tool.resize(1080, 480)
        self.tool.show()
        self.app.processEvents()
        for widget in (self.tool.trace_input, self.tool.trace_search_button,
                       self.tool.trace_copy_button, self.tool.trace_open_button):
            parent = widget.parentWidget()
            self.assertTrue(parent.rect().contains(widget.geometry()), widget.objectName() or widget.__class__.__name__)


if __name__ == "__main__":
    unittest.main()
