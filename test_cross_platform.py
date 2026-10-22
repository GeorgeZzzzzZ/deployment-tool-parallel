import copy
import os
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
import test_jenkins_login


PLATFORMS = ("win32", "linux", "darwin")


class CrossPlatformFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    @contextmanager
    def workspace(self, platform, *, saved=None):
        store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        store.data["jenkins"].update(saved or {})
        with patch("main.sys.platform", platform), patch(
            "main.unprotect_secret", side_effect=lambda value: "test-token" if value else ""
        ), patch("main.protect_secret", return_value="encrypted-test-token"):
            self.pending = []
            with patch.object(main.DeployTool, "run_thread", side_effect=lambda fn, **kw: self.pending.append(fn)):
                tool = main.DeployTool(store=store, embedded=True)
                try:
                    self.app.processEvents()
                    yield tool
                finally:
                    tool._closing = True
                    tool.close()
                    tool.deleteLater()
                    self.app.processEvents()

    @staticmethod
    def cached_jobs():
        return [{
            "name": "cloud", "_class": "folder", "children": [{
                "name": "ETM", "_class": "folder", "children": [{
                    "name": "SERVICE", "_class": "project", "children": [],
                    "url": "https://jenkins.example/job/cloud/job/ETM/job/SERVICE/",
                }],
            }, {"name": "ESC", "_class": "folder", "children": []}],
        }]

    def login_cached(self, tool):
        tool.j_url.setText("https://jenkins.example")
        tool.j_user.setText("builder")
        tool.j_password.setText("test-token")
        tool.store.data["jenkins"]["job_cache"] = {
            "version": main.JENKINS_JOB_CACHE_VERSION,
            "url": "https://jenkins.example", "username": "builder",
            "jobs": self.cached_jobs(),
        }
        client = main.JenkinsClient("https://jenkins.example", "builder", "test-token")
        client.verify_authentication = Mock()
        client.list_jobs = Mock(side_effect=AssertionError("cached login must not fetch jobs"))
        with patch("main.JenkinsClient", return_value=client):
            tool.login_jenkins()
        self.assertEqual(len(self.pending), 1)
        self.pending.pop()()
        self.app.processEvents()
        return client

    def test_complete_saved_credentials_schedule_auto_login_on_every_platform(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform), self.workspace(platform, saved={
                "url": "https://jenkins.example", "username": "builder", "password": "encrypted",
            }) as tool:
                self.assertEqual(len(self.pending), 1)
                self.assertFalse(tool.login_button.isEnabled())
                self.assertIn("正在验证", tool.path_label.text())

    def test_cached_login_collapses_connection_and_keeps_searchable_descendants(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform), self.workspace(platform) as tool:
                client = self.login_cached(tool)
                self.assertIn("登录成功", tool.path_label.text())
                self.assertIsInstance(tool.jenkins_connection_card, main.CollapsibleCardWidget)
                self.assertFalse(tool.jenkins_connection_card._toggle.isChecked())
                self.assertFalse(tool.job_browser.search.isHidden())
                self.assertTrue(tool.job_browser.refresh.isEnabled())
                tool.job_browser.search.setText("etm")
                tool._start_job_search()
                root = tool.job_tree.topLevelItem(0)
                self.assertEqual(root.childCount(), 1)
                self.assertEqual(root.child(0).child(0).data(0, main.Qt.UserRole)["path"],
                                 ["cloud", "ETM", "SERVICE"])
                client.verify_authentication.assert_called_once()

    def test_trace_can_search_and_select_unexpanded_jobs_independently(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform), self.workspace(platform) as tool:
                self.login_cached(tool)
                browser = tool.trace_job_browser
                self.assertTrue(browser.refresh.isEnabled())
                self.assertFalse(tool.job_tree.topLevelItem(0).isExpanded())
                browser.search.setText("etm")
                tool._sync_trace_job_browser()
                root = browser.tree.topLevelItem(0)
                self.assertIsNotNone(root)
                self.assertEqual(root.childCount(), 1)
                tool._select_trace_job(root.child(0).child(0), 0)
                self.assertEqual(tool._trace_job_path, ["cloud", "ETM", "SERVICE"])
                self.assertEqual(tool.current_job_path, [])
                browser.search.clear()
                self.assertEqual(tool._trace_job_path, [])

    def test_refresh_from_trace_replaces_complete_cache_even_when_filtered(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform), self.workspace(platform) as tool:
                client = self.login_cached(tool)
                fresh = [{"name": "new-service", "_class": "project", "children": []}]
                client.list_jobs = Mock(return_value=copy.deepcopy(fresh))
                tool.trace_job_browser.search.setText("etm")
                tool.trace_job_browser.refresh.click()
                self.assertEqual(len(self.pending), 1)
                self.pending.pop()()
                self.app.processEvents()
                self.assertEqual(tool.store.data["jenkins"]["job_cache"]["jobs"][0]["name"], "new-service")
                self.assertEqual(tool.job_tree.topLevelItem(0).text(0), "new-service")
                self.assertEqual(tool.trace_job_browser.tree.topLevelItemCount(), 0)
                client.list_jobs.assert_called_once_with([])

    def test_build_only_setting_is_restored_editable_and_does_not_require_a_target(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform), self.workspace(platform, saved={"deploy_after_build": False}) as tool:
                self.assertFalse(main.DeployTool._deploy_after_build_enabled(tool))
                tool.jenkins = main.JenkinsClient("https://jenkins.example", "builder", "test-token")
                tool.current_job_path = ["cloud", "ETM", "SERVICE"]
                tool._update_build_cloud_ready()
                self.assertTrue(tool.build_button.isEnabled())
                self.assertFalse(tool.build_deploy_mode.isEnabled())
                tool.deploy_after_build_checkbox.setChecked(True)
                self.assertTrue(tool.store.data["jenkins"]["deploy_after_build"])
                self.assertTrue(tool.build_deploy_mode.isEnabled())

    def test_dashboard_default_browser_and_fallback_preserve_namespace(self):
        for platform in PLATFORMS:
            for namespace in ("etm", "pqm", "esc"):
                with self.subTest(platform=platform, namespace=namespace), patch("main.sys.platform", platform):
                    item = {"url": "https://dashboard.example/#/login", "namespace": namespace}
                    tool = SimpleNamespace(_copy_dashboard_token=Mock())
                    expected = "https://dashboard.example/#/pod?namespace=" + namespace
                    with patch("main.webbrowser.open") as browser, patch("main.QMessageBox.warning"):
                        main.DeployTool._open_dashboard_url(tool, item)
                        main.DeployTool._on_dashboard_fallback(tool, item, "test failure")
                        self.assertEqual([call.args for call in browser.call_args_list], [(expected,), (expected,)])
                    tool._copy_dashboard_token.assert_called_once_with(item)

class LinuxJenkinsLoginTests(test_jenkins_login.JenkinsLoginTests):
    def setUp(self):
        platform = patch("main.sys.platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)
        super().setUp()


class MacOSJenkinsLoginTests(test_jenkins_login.JenkinsLoginTests):
    def setUp(self):
        platform = patch("main.sys.platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        super().setUp()


if __name__ == "__main__":
    unittest.main()
