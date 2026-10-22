import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main


class JenkinsLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.store = SimpleNamespace(data=main.ConfigStore._default_data(), save=Mock())
        self.store.data["jenkins"]["job_cache"] = {
            "version": main.JENKINS_JOB_CACHE_VERSION,
            "url": "https://jenkins.example",
            "username": "builder",
            "jobs": [{"name": "service", "children": [],
                      "url": "https://jenkins.example/job/service/"}],
        }
        self.tool = main.DeployTool(store=self.store, embedded=True)
        self.pending = []
        self.tool.run_thread = lambda fn, on_finished=None: self.pending.append(fn)
        self.tool.j_url.setText("https://jenkins.example")
        self.tool.j_user.setText("builder")
        self.tool.j_password.setText("test-token")
        self.app.processEvents()
        self.responses = [(200, {"authenticated": True, "anonymous": False, "name": "builder"})]
        self.urls = []
        self.http = patch("main.requests.Session.get", side_effect=self.respond)
        self.http.start()
        self.addCleanup(self.http.stop)
        self.secrets = patch("main.protect_secret", return_value="test-encrypted")
        self.secrets.start()
        self.addCleanup(self.secrets.stop)

    def tearDown(self):
        self.tool._closing = True
        self.tool.close()
        self.tool.deleteLater()
        self.app.processEvents()

    def respond(self, url, **kwargs):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        response = main.requests.Response()
        response.status_code = status
        response.url = url
        response.request = main.requests.Request("GET", url).prepare()
        response._content = (json.dumps(body) if isinstance(body, dict) else body).encode()
        return response

    def drain(self):
        pending, self.pending = self.pending, []
        for fn in pending:
            fn()
        self.app.processEvents()

    def assert_login_failed(self, detail):
        self.assertTrue(self.tool.jenkins_connection_card._toggle.isChecked())
        self.assertIn(detail, self.tool.path_label.text())
        self.assertIn(detail, self.tool.message_overlay.message_label.text())
        self.assertFalse(self.tool.message_overlay.isHidden())
        self.assertTrue(self.tool.login_button.isEnabled())
        self.assertFalse(self.tool.build_button.isEnabled())
        self.assertEqual(self.tool.loading_count, 0)

    def test_cached_tree_waits_for_identity_verification_before_collapsing(self):
        self.tool.login_jenkins()
        self.assertTrue(self.tool.jenkins_connection_card._toggle.isChecked())
        self.assertFalse(self.tool.login_button.isEnabled())
        self.assertFalse(self.tool.job_tree.isEnabled())
        self.assertFalse(self.tool.build_button.isEnabled())
        self.assertEqual(self.urls, [])
        self.drain()
        self.assertEqual(self.urls, ["https://jenkins.example/whoAmI/api/json"])
        self.assertFalse(self.tool.jenkins_connection_card._toggle.isChecked())
        self.assertIn("登录成功", self.tool.path_label.text())
        self.assertEqual(self.tool.job_tree.topLevelItemCount(), 1)
        self.assertTrue(self.tool.job_tree.isEnabled())
        self.assertTrue(self.tool.login_button.isEnabled())
        self.assertEqual(self.tool.loading_count, 0)

    def test_cached_tree_cannot_hide_401_or_replace_persisted_cache(self):
        cached = copy.deepcopy(self.store.data["jenkins"]["job_cache"])
        self.responses = [(401, "<title>Error 401 Unauthorized</title>")]
        self.tool.login_jenkins()
        self.drain()
        self.assert_login_failed("401")
        self.assertEqual(self.store.data["jenkins"]["job_cache"], cached)
        self.assertEqual(self.tool.job_tree.topLevelItemCount(), 0)

    def test_anonymous_200_is_not_a_successful_login(self):
        self.responses = [(200, {"authenticated": True, "anonymous": True, "name": "anonymous"})]
        self.tool.login_jenkins()
        self.drain()
        self.assert_login_failed("匿名")

    def test_missing_identity_is_not_a_successful_login(self):
        self.responses = [(200, {})]
        self.tool.login_jenkins()
        self.drain()
        self.assert_login_failed("身份")

    def test_network_error_remains_visible_and_retry_can_succeed(self):
        self.responses = [main.requests.ConnectionError("connection refused")] * 6
        with patch("main.time.sleep"):
            self.tool.login_jenkins()
            self.drain()
        self.assert_login_failed("connection refused")
        self.tool.message_overlay.dismiss()
        self.responses = [(200, {"authenticated": True, "anonymous": False, "name": "builder"})]
        self.tool.login_jenkins()
        self.drain()
        self.assertFalse(self.tool.jenkins_connection_card._toggle.isChecked())
        self.assertIn("登录成功", self.tool.path_label.text())

    def test_first_login_verifies_identity_then_loads_tree(self):
        self.store.data["jenkins"]["job_cache"] = None
        self.responses.append((200, {"jobs": [{"name": "service", "_class": "project"}]}))
        self.tool.login_jenkins()
        self.drain()
        self.assertEqual(self.urls, ["https://jenkins.example/whoAmI/api/json", "https://jenkins.example/api/json"])
        self.assertFalse(self.tool.jenkins_connection_card._toggle.isChecked())
        self.assertIn("登录成功", self.tool.path_label.text())
        self.assertEqual(self.tool.job_tree.topLevelItemCount(), 1)

    def test_task_permission_failure_keeps_connection_open(self):
        self.store.data["jenkins"]["job_cache"] = None
        self.responses.append((403, "<title>Missing Overall/Read permission</title>"))
        self.tool.login_jenkins()
        self.drain()
        self.assert_login_failed("403")
        self.assertIn("Overall/Read", self.tool.message_overlay.message_label.text())

    def test_old_login_result_cannot_collapse_new_connection(self):
        self.tool.login_jenkins()
        self.tool._job_cache_generation += 1
        self.drain()
        self.assertTrue(self.tool.jenkins_connection_card._toggle.isChecked())
        self.assertEqual(self.urls, [])
        self.assertEqual(self.tool.job_tree.topLevelItemCount(), 0)

    def test_saved_credentials_do_not_collapse_connection_before_auto_login(self):
        self.store.data["jenkins"].update({
            "url": "https://jenkins.example", "username": "builder", "password": "encrypted",
        })
        with patch("main.unprotect_secret", return_value="test-token"), patch.object(
            main.DeployTool, "_auto_login_jenkins"
        ):
            other = main.DeployTool(store=self.store, embedded=True)
            try:
                self.app.processEvents()
                self.assertTrue(other.jenkins_connection_card._toggle.isChecked())
            finally:
                other._closing = True
                other.close()
                other.deleteLater()

    def test_non_windows_cached_login_still_verifies_identity(self):
        with patch("main.sys.platform", "linux"):
            self.tool.login_jenkins()
            self.drain()
        self.assertEqual(self.urls, ["https://jenkins.example/whoAmI/api/json"])
        self.assertIn("登录成功", self.tool.path_label.text())
        self.assertEqual(self.tool.job_tree.topLevelItemCount(), 1)
        self.assertEqual(self.tool.loading_count, 0)

    def test_connection_change_during_verification_discards_late_result(self):
        original = self.respond

        def change_connection(url, **kwargs):
            result = original(url, **kwargs)
            self.tool._job_cache_generation += 1
            return result

        with patch("main.requests.Session.get", side_effect=change_connection):
            self.tool.login_jenkins()
            self.drain()
        self.assertTrue(self.tool.jenkins_connection_card._toggle.isChecked())
        self.assertEqual(self.tool.job_tree.topLevelItemCount(), 0)
        self.assertEqual(self.tool.loading_count, 0)


if __name__ == "__main__":
    unittest.main()
