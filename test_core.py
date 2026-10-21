import unittest
import threading
import shlex
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import (
    cloud_project_matches_job,
    dashboard_pods_url,
    ensure_dashboard_bindings,
    extract_job_project_code,
    extract_package_number,
    is_secondary_build_path,
    reorder_parameters,
    rename_bound_dashboard,
    resolve_ssh_environment_for_package,
    update_image_list,
    update_yaml_image,
    yaml_image_reference,
)
from main import (
    _environment_validation_error,
    _portable_master_key,
    browser_session_token,
    clean_remote_output_line,
    connect_ssh_client,
    create_browser_driver,
    DeployTool,
    EnvironmentDialog,
    ENVIRONMENT_FIELDS,
    JENKINS_JOB_CACHE_VERSION,
    JenkinsClient,
    PipelineCancelled,
    exception_message,
    format_transfer_bytes,
    parse_transfer_progress,
    platform_config_dir,
    protect_secret,
    service_type_matches,
    shell_quote,
    unprotect_secret,
)
from main import RemoteCommandInterrupted
from ops_api import OpsApiClient, OpsApiError, OpsSubmissionUnknown, service_type_id, target_regions


PACKAGE = "etmbackend-yisen-5.2.0:5.2.0.1001.10345"


class OpsApiTests(unittest.TestCase):
    def test_record_query_failure_after_submission_is_unknown_and_never_reposts(self):
        client = OpsApiClient()
        client.resolve_target = Mock(return_value={
            "cloud": "Huawei", "api_area": "test", "appId": "etm",
            "typeId": "backend", "deploymentName": "etm",
        })
        client.verify_package = Mock(return_value=True)
        client._deployment_records = Mock(side_effect=[[], OpsApiError("offline")])
        client._bff = Mock(return_value={"code": 0})

        with self.assertRaisesRegex(OpsSubmissionUnknown, "禁止直接重试"):
            client.deploy({"package": PACKAGE})

        self.assertEqual(client._bff.call_count, 1)
        self.assertEqual(client._bff.call_args.args[0], "POST")

    def test_record_query_failure_before_submission_does_not_post(self):
        client = OpsApiClient()
        client.resolve_target = Mock(return_value={})
        client.verify_package = Mock(return_value=True)
        client._deployment_records = Mock(side_effect=OpsApiError("offline"))
        client._bff = Mock()

        with self.assertRaisesRegex(OpsApiError, "offline"):
            client.deploy({"package": PACKAGE})

        client._bff.assert_not_called()

    def test_ops_business_success_code_is_accepted(self):
        client = OpsApiClient()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"code": 10000, "msg": "OK", "data": []}
        client.session.request = Mock(return_value=response)

        payload = client._json_request("GET", "https://example.invalid/read")

        self.assertEqual(payload["code"], 10000)

    def test_service_type_and_pre_environment_mapping(self):
        self.assertEqual(service_type_id("esc 后端"), "backend")
        regions = target_regions(
            [
                {
                    "id": 12,
                    "area": "test",
                    "appId": "esc",
                    "typeId": "backend",
                    "enablePreEnv": True,
                }
            ],
            "Huawei",
        )

        self.assertEqual(
            [(item["region_label"], item["api_area"], item["state"]) for item in regions],
            [
                ("Huawei / 测试", "test", "prod"),
                ("Huawei / 预测试", "test-pre", "pre"),
            ],
        )

    def test_load_target_options_uses_api_records(self):
        client = OpsApiClient()
        client.authenticated = True
        client._bff = Mock(
            side_effect=[
                {
                    "data": [
                        {
                            "deploymentName": "esc",
                            "appId": "esc",
                            "typeId": "backend",
                        }
                    ]
                },
                {
                    "data": [
                        {
                            "id": 111,
                            "registryId": "industry",
                            "deploymentName": "esc",
                            "appId": "esc",
                            "typeId": "backend",
                            "area": "test",
                            "enablePreEnv": True,
                        }
                    ]
                },
            ]
        )

        options = client.load_target_options("ESC", "Huawei")

        self.assertEqual(options["types"], ["esc 后端"])
        self.assertEqual(
            options["regions"], ["Huawei / 测试", "Huawei / 预测试"]
        )
        target = client.resolve_target(
            {
                "project": "ESC",
                "deploy_type": "esc 后端",
                "cloud": "Huawei",
                "region": "预测试",
            }
        )
        self.assertEqual(target["api_area"], "test-pre")

    def test_deploy_posts_exact_endpoint_and_requires_new_record(self):
        client = OpsApiClient()
        client.authenticated = True
        client.user = {"userId": "u1", "userName": "Tester"}
        target = {
            "id": 11111119689,
            "cloud": "Huawei",
            "region": "预测试",
            "api_area": "test-pre",
            "state": "pre",
            "appId": "esc",
            "typeId": "backend",
            "deploymentName": "esc",
        }
        client._targets[("ESC", "backend", "huawei", "预测试")] = target
        client.verify_package = Mock(return_value=True)
        package = "escbackend-5.3.0:5.3.0.1001.10162"
        old = {"id": "old", "imageName": package, "status": "success"}
        new = {"id": "new", "imageName": package, "status": "pending"}
        client._bff = Mock(
            side_effect=[
                {"data": [old]},
                {"code": 0, "data": {"accepted": True}},
                {"data": [old, new]},
            ]
        )

        result = client.deploy(
            {
                "project": "ESC",
                "deploy_type": "esc 后端",
                "cloud": "Huawei",
                "region": "预测试",
                "package": package,
            }
        )

        post_call = next(
            call for call in client._bff.call_args_list if call.args[0] == "POST"
        )
        self.assertEqual(
            post_call.args[1],
            "/api/v1/deployment/build/huawei/test-pre/esc/backend/deploy/esc",
        )
        self.assertEqual(
            post_call.kwargs["json"],
            {
                "images": package,
                "userId": "u1",
                "userName": "Tester",
                "name": "escbackend",
            },
        )
        self.assertTrue(result["verified"])

    @patch("ops_api.time.sleep")
    def test_existing_same_package_does_not_verify_new_submission(self, sleep):
        client = OpsApiClient()
        client.authenticated = True
        client.user = {"userId": "u1", "userName": "Tester"}
        target = {
            "id": 111,
            "cloud": "Huawei",
            "region": "预测试",
            "api_area": "test-pre",
            "state": "pre",
            "appId": "esc",
            "typeId": "backend",
            "deploymentName": "esc",
        }
        client._targets[("ESC", "backend", "huawei", "预测试")] = target
        client.verify_package = Mock(return_value=True)
        package = "escbackend-5.3.0:5.3.0.1001.10162"
        old_response = {"data": [{"id": "old", "imageName": package}]}
        client._bff = Mock(
            side_effect=[old_response, {"code": 0}, *([old_response] * 6)]
        )

        with self.assertRaises(OpsSubmissionUnknown):
            client.deploy(
                {
                    "project": "ESC",
                    "deploy_type": "esc 后端",
                    "cloud": "Huawei",
                    "region": "预测试",
                    "package": package,
                }
            )


class CoreTests(unittest.TestCase):
    def test_ssh_password_connection_allows_slow_authentication(self):
        client = Mock()
        data = {
            "host": "172.16.2.47",
            "port": "22",
            "username": "root",
            "password": "secret",
        }

        connect_ssh_client(client, data)

        client.connect.assert_called_once_with(
            "172.16.2.47",
            port=22,
            username="root",
            password="secret",
            timeout=15,
            banner_timeout=15,
            auth_timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )

    @patch("main.QMessageBox.critical")
    def test_runtime_error_uses_in_app_overlay(self, critical):
        message_overlay = SimpleNamespace(show_message=Mock())
        tool = SimpleNamespace(
            message_overlay=message_overlay,
            cloud_options_loading=False,
            build_cloud_loading=False,
        )

        DeployTool._on_error(tool, "一键流程失败：Jenkins 构建结果为 FAILURE")

        message_overlay.show_message.assert_called_once_with(
            "操作失败",
            "一键流程失败：Jenkins 构建结果为 FAILURE",
            kind="error",
        )
        critical.assert_not_called()

    def test_remote_progress_parser_removes_terminal_redraw_noise(self):
        raw = "\x1b[2K\re4714b81ce88: Downloading  38.06MiB/172MiB\x1b[0m"

        self.assertNotIn("\x1b", clean_remote_output_line(raw))
        current, total = parse_transfer_progress(raw)
        self.assertAlmostEqual(current / 1024 ** 2, 38.06, places=2)
        self.assertAlmostEqual(total / 1024 ** 2, 172.0, places=2)

    @patch("main.time.sleep")
    def test_remote_exec_turns_repeated_tty_updates_into_progress(self, _sleep):
        class Channel:
            def __init__(self):
                self.chunks = [
                    (
                        b"pulling image\n"
                        b"layer: Downloading 37.53MiB/172MiB\r"
                        b"layer: Downloading 38.06MiB/172MiB\r"
                        b"download complete\n"
                    )
                ]

            def exit_status_ready(self):
                return not self.chunks

            def recv_ready(self):
                return bool(self.chunks)

            def recv(self, _size):
                return self.chunks.pop(0)

            def recv_stderr_ready(self):
                return False

            def recv_exit_status(self):
                return 0

        channel = Channel()
        stdout = SimpleNamespace(channel=channel)
        client = Mock()
        client.exec_command.return_value = (Mock(), stdout, Mock())
        tool = SimpleNamespace(emit=Mock())

        DeployTool._remote_exec(tool, client, "pull", progress_range=(15, 78))

        log_text = "\n".join(
            call.args[1]
            for call in tool.emit.call_args_list
            if call.args[0] == "deploy_log"
        )
        self.assertIn("pulling image", log_text)
        self.assertIn("download complete", log_text)
        self.assertNotIn("Downloading", log_text)
        self.assertTrue(any(
            call.args[0] == "deploy_progress"
            for call in tool.emit.call_args_list
        ))

    def test_transfer_byte_format_keeps_small_layer_progress_visible(self):
        self.assertEqual(format_transfer_bytes(512), "1 KB")
        self.assertEqual(format_transfer_bytes(32 * 1024), "32 KB")
        self.assertEqual(format_transfer_bytes(1.5 * 1024 ** 2), "1.5 MB")

    @patch("main.time.sleep")
    @patch("main.time.monotonic", side_effect=[0, 11, 22])
    def test_remote_exec_emits_heartbeat_while_docker_is_silent(
        self, _monotonic, _sleep
    ):
        class Channel:
            def __init__(self):
                self.checks = 0

            def exit_status_ready(self):
                self.checks += 1
                return self.checks >= 3

            def recv_ready(self):
                return False

            def recv_stderr_ready(self):
                return False

            def recv_exit_status(self):
                return 0

        stdout = SimpleNamespace(channel=Channel())
        client = Mock()
        client.exec_command.return_value = (Mock(), stdout, Mock())
        tool = SimpleNamespace(emit=Mock())

        DeployTool._remote_exec(tool, client, "pull", progress_range=(15, 78))

        statuses = [
            call.args[1][1]
            for call in tool.emit.call_args_list
            if call.args[0] == "deploy_progress"
        ]
        self.assertTrue(any("远端命令已运行 11 秒" in text for text in statuses))
        self.assertEqual(statuses[-1], "镜像搬运命令已结束，正在校验目标镜像...")

    @patch.object(DeployTool, "_terminate_remote_process_group")
    def test_remote_exec_interrupts_only_managed_command_group(self, terminate):
        class Channel:
            def __init__(self):
                self.closed = False

            def exit_status_ready(self):
                return self.closed

            def recv_ready(self):
                return False

            def recv_stderr_ready(self):
                return False

            def close(self):
                self.closed = True

        channel = Channel()
        stdout = SimpleNamespace(channel=channel)
        client = Mock()
        client.exec_command.return_value = (Mock(), stdout, Mock())
        tool = SimpleNamespace(emit=Mock(), _terminate_remote_process_group=terminate)
        interrupt = threading.Event()
        interrupt.set()

        with self.assertRaises(RemoteCommandInterrupted):
            DeployTool._remote_exec(
                tool,
                client,
                "managed-pull",
                progress_range=(15, 78),
                interrupt_event=interrupt,
                process_group_file="/tmp/command.pgid",
            )
        terminate.assert_called_once_with(client, "/tmp/command.pgid")

    def test_clicking_folder_does_not_request_build_parameters(self):
        item = Mock()
        item.data.return_value = {"path": ["folder"], "is_folder": True}
        item.childCount.return_value = 1
        item.text.return_value = "folder"
        item.parent.return_value = None
        tool = SimpleNamespace(
            jenkins=object(), run_thread=Mock(), emit=Mock(), path_label=Mock(),
            _sync_cloud_target_from_job=Mock(), _update_build_cloud_ready=Mock(),
            _select_build_summary=Mock(),
            build_button=Mock(),
        )

        DeployTool._tree_select(tool, item, 0)

        tool.run_thread.assert_not_called()
        tool.emit.assert_not_called()
        self.assertEqual(tool.current_job_path, [])
        tool._sync_cloud_target_from_job.assert_called_once()

    @patch("main.sys.platform", "win32")
    def test_visible_browser_uses_eager_page_loading(self):
        captured = {}

        class Options:
            def __init__(self):
                self.page_load_strategy = "normal"

            def add_argument(self, _value):
                pass

            def add_experimental_option(self, _name, _value):
                pass

        def driver(*, options, service=None):
            captured["options"] = options
            return object()

        def import_module(name):
            if name.endswith(".options"):
                return SimpleNamespace(Options=Options)
            if name.endswith(".webdriver"):
                return SimpleNamespace(WebDriver=driver)
            if name.endswith(".service"):
                return SimpleNamespace(Service=Mock)
            raise AssertionError(name)

        with patch("main.importlib.import_module", side_effect=import_module):
            create_browser_driver(headless=False)

        self.assertEqual(captured["options"].page_load_strategy, "eager")

    @patch("main.sys.platform", "darwin")
    def test_macos_uses_application_support_config_directory(self):
        path = platform_config_dir()

        self.assertEqual(path.name, "BuildDeployToolParallel")
        self.assertEqual(path.parent.name, "Application Support")

    @patch("main.sys.platform", "linux")
    @patch.dict("main.os.environ", {"XDG_CONFIG_HOME": "/tmp/deploy-console-test"})
    def test_linux_uses_xdg_config_directory(self):
        path = platform_config_dir()

        self.assertEqual(path.name, "BuildDeployToolParallel")
        self.assertIn("deploy-console-test", str(path))

    @patch("main.sys.platform", "linux")
    def test_portable_secret_round_trip(self):
        from cryptography.fernet import Fernet

        with patch("main._portable_master_key", return_value=Fernet.generate_key()):
            encrypted = protect_secret("portable-password")
            decrypted = unprotect_secret(encrypted)

        self.assertTrue(encrypted.startswith("vault1:"))
        self.assertEqual(decrypted, "portable-password")

    @patch("main.sys.platform", "linux")
    def test_linux_master_key_avoids_desktop_keyring(self):
        with (
            patch("main._file_master_key", return_value=b"local-key") as local_key,
            patch("main.importlib.import_module") as import_module,
        ):
            result = _portable_master_key()

        self.assertEqual(result, b"local-key")
        local_key.assert_called_once_with()
        import_module.assert_not_called()

    @patch("main.sys.platform", "darwin")
    def test_macos_master_key_avoids_keychain(self):
        with (
            patch("main._file_master_key", return_value=b"local-key") as local_key,
            patch("main.importlib.import_module") as import_module,
        ):
            result = _portable_master_key()

        self.assertEqual(result, b"local-key")
        local_key.assert_called_once_with()
        import_module.assert_not_called()

    def test_browser_session_token_reads_ops_login_state(self):
        browser = Mock()
        browser.execute_script.return_value = "session-token"

        self.assertEqual(browser_session_token(browser), "session-token")
        self.assertNotIn("session-token", browser.execute_script.call_args.args[0])

    def test_browser_session_token_handles_invalid_storage(self):
        browser = Mock()
        browser.execute_script.side_effect = RuntimeError("storage unavailable")

        self.assertEqual(browser_session_token(browser), "")

    def test_cloud_project_must_match_selected_job_system(self):
        path = ["云应用打包", "15-ESC-设备中心", "5-SERVICE-5.3.0"]

        self.assertEqual(extract_job_project_code(path), "ESC")
        self.assertTrue(cloud_project_matches_job(path, "esc"))
        self.assertFalse(cloud_project_matches_job(path, "ETM"))

    def test_cloud_project_validation_rejects_unrecognized_job_path(self):
        path = ["云应用打包", "设备中心", "5-SERVICE-5.3.0"]

        self.assertEqual(extract_job_project_code(path), "")
        self.assertFalse(cloud_project_matches_job(path, "ESC"))

    def test_dashboard_address_uses_system_default_browser(self):
        with (
            patch("main.sys.platform", "win32"),
            patch("main.webbrowser.open") as open_browser,
        ):
            DeployTool._open_dashboard_url(
                SimpleNamespace(), {"url": "https://dashboard.example"}
            )

        open_browser.assert_called_once_with(
            "https://dashboard.example/#/pod?namespace=etm"
        )

    def test_dashboard_pods_url_replaces_login_route(self):
        self.assertEqual(
            dashboard_pods_url(
                "https://dashboard.172.16.2.47.nip.io/#/login"
            ),
            "https://dashboard.172.16.2.47.nip.io/#/pod?namespace=etm",
        )

    def test_windows_dashboard_login_opens_pqm_pods(self):
        state = {"logged_in": False}
        token_input = Mock()
        token_input.is_displayed.return_value = True
        token_input.is_enabled.return_value = True
        login_button = Mock()
        login_button.is_displayed.return_value = True
        login_button.is_enabled.return_value = True
        login_button.click.side_effect = lambda: state.update(logged_in=True)
        driver = Mock()
        driver.current_url = ""

        def get(url):
            driver.current_url = url

        def find_elements(_by, selector):
            if "token" in selector or selector in ("textarea", "input[type='password']"):
                return [] if state["logged_in"] else [token_input]
            if selector in ("button[type='submit']", "button"):
                return [login_button]
            return []

        driver.get.side_effect = get
        driver.find_elements.side_effect = find_elements

        class Wait:
            def __init__(self, browser, _timeout):
                self.browser = browser

            def until(self, condition):
                result = condition(self.browser)
                if not result:
                    raise AssertionError("wait condition did not pass")
                return result

        def import_module(name):
            if name.endswith(".by"):
                return SimpleNamespace(By=SimpleNamespace(CSS_SELECTOR="css"))
            if name == "selenium.common.exceptions":
                return SimpleNamespace(StaleElementReferenceException=RuntimeError)
            return SimpleNamespace(WebDriverWait=Wait)

        tool = SimpleNamespace(emit=Mock(), dashboard_drivers=[])
        item = {
            "name": "环境3",
            "url": "https://dashboard.172.16.2.47.nip.io/",
            "token": "encrypted-token",
            "namespace": "pqm",
        }

        with (
            patch("main.sys.platform", "win32"),
            patch("main.create_browser_driver", return_value=driver),
            patch("main.unprotect_secret", return_value="dashboard-token"),
            patch("main.importlib.import_module", side_effect=import_module),
        ):
            DeployTool._open_dashboard_item(tool, item)

        self.assertEqual(
            [call.args[0] for call in driver.get.call_args_list],
            [
                "https://dashboard.172.16.2.47.nip.io/",
                "https://dashboard.172.16.2.47.nip.io/#/pod?namespace=pqm",
            ],
        )
        self.assertEqual(tool.dashboard_drivers, [driver])
        tool.emit.assert_any_call("dashboard_login_ok", ("环境3", "pqm"))

    def test_dashboard_login_retries_stale_token_input_and_login_button(self):
        class StaleElementReferenceException(RuntimeError):
            pass

        state = {"logged_in": False}
        stale_token = Mock()
        stale_token.is_displayed.return_value = True
        stale_token.is_enabled.return_value = True
        stale_token.clear.side_effect = StaleElementReferenceException()
        token_input = Mock()
        token_input.is_displayed.return_value = True
        token_input.is_enabled.return_value = True
        stale_button = Mock()
        stale_button.is_displayed.return_value = True
        stale_button.is_enabled.return_value = True
        stale_button.click.side_effect = StaleElementReferenceException()
        login_button = Mock()
        login_button.is_displayed.return_value = True
        login_button.is_enabled.return_value = True
        login_button.click.side_effect = lambda: state.update(logged_in=True)
        driver = Mock(current_url="")
        token_elements = iter([stale_token, token_input])
        button_elements = iter([stale_button, login_button])

        def get(url):
            driver.current_url = url

        def find_elements(_by, selector):
            if "token" in selector:
                if state["logged_in"]:
                    return []
                return [next(token_elements, token_input)]
            if selector in ("textarea", "input[type='password']"):
                return []
            if selector == "button[type='submit']":
                return [next(button_elements, login_button)]
            return []

        driver.get.side_effect = get
        driver.find_elements.side_effect = find_elements

        class Wait:
            def __init__(self, browser, _timeout):
                self.browser = browser

            def until(self, condition):
                for _ in range(5):
                    result = condition(self.browser)
                    if result:
                        return result
                raise AssertionError("wait condition did not pass")

        def import_module(name):
            if name.endswith(".by"):
                return SimpleNamespace(By=SimpleNamespace(CSS_SELECTOR="css"))
            if name == "selenium.common.exceptions":
                return SimpleNamespace(
                    StaleElementReferenceException=StaleElementReferenceException
                )
            return SimpleNamespace(WebDriverWait=Wait)

        tool = SimpleNamespace(emit=Mock(), dashboard_drivers=[])
        item = {
            "name": "环境3",
            "url": "https://dashboard.example/",
            "token": "encrypted-token",
        }

        with (
            patch("main.sys.platform", "win32"),
            patch("main.create_browser_driver", return_value=driver),
            patch("main.unprotect_secret", return_value="dashboard-token"),
            patch("main.importlib.import_module", side_effect=import_module),
        ):
            DeployTool._open_dashboard_item(tool, item)

        token_input.send_keys.assert_called_once_with("dashboard-token")
        login_button.click.assert_called_once_with()
        tool.emit.assert_any_call("dashboard_login_ok", ("环境3", "etm"))

    def test_dashboard_fallback_removes_webdriver_stacktrace(self):
        class BrowserError(RuntimeError):
            pass

        class Wait:
            def __init__(self, _browser, _timeout):
                pass

            def until(self, _condition):
                raise BrowserError("element failed\nStacktrace:\nwebdriver details")

        def import_module(name):
            if name.endswith(".by"):
                return SimpleNamespace(By=SimpleNamespace(CSS_SELECTOR="css"))
            if name == "selenium.common.exceptions":
                return SimpleNamespace(StaleElementReferenceException=RuntimeError)
            return SimpleNamespace(WebDriverWait=Wait)

        driver = Mock()
        tool = SimpleNamespace(emit=Mock(), dashboard_drivers=[])
        item = {
            "name": "环境3",
            "url": "https://dashboard.example/",
            "token": "encrypted-token",
        }

        with (
            patch("main.create_browser_driver", return_value=driver),
            patch("main.unprotect_secret", return_value="dashboard-token"),
            patch("main.importlib.import_module", side_effect=import_module),
        ):
            DeployTool._open_dashboard_item(tool, item)

        fallback = next(
            call.args[1][1]
            for call in tool.emit.call_args_list
            if call.args[0] == "dashboard_fallback"
        )
        self.assertEqual(fallback, "浏览器自动化失败：element failed")
        self.assertNotIn("Stacktrace:", fallback)

    def test_batch_opens_same_dashboard_only_once(self):
        first_owner = SimpleNamespace(emit=Mock())
        second_owner = SimpleNamespace(emit=Mock())
        dashboard = {"id": "dashboard-1", "name": "环境3"}

        opened = DeployTool._open_batch_dashboards(
            [
                {"owner": first_owner, "dashboard": dashboard},
                {
                    "owner": second_owner,
                    "dashboard": {"id": "DASHBOARD-1", "name": "新名称"},
                },
            ]
        )

        self.assertEqual(opened, 1)
        first_owner.emit.assert_called_once_with("open_dashboard_item", dashboard)
        second_owner.emit.assert_not_called()

    def test_batch_opens_each_distinct_dashboard(self):
        first_owner = SimpleNamespace(emit=Mock())
        second_owner = SimpleNamespace(emit=Mock())
        first_dashboard = {"url": "https://dashboard-one.example", "name": "环境1"}
        second_dashboard = {"url": "https://dashboard-two.example", "name": "环境2"}

        opened = DeployTool._open_batch_dashboards(
            [
                {"owner": first_owner, "dashboard": first_dashboard},
                {"owner": second_owner, "dashboard": second_dashboard},
            ]
        )

        self.assertEqual(opened, 2)
        first_owner.emit.assert_called_once_with("open_dashboard_item", first_dashboard)
        second_owner.emit.assert_called_once_with("open_dashboard_item", second_dashboard)

    @patch("main.QMessageBox.information")
    def test_dashboard_login_success_logs_without_a_popup(self, information):
        message_overlay = SimpleNamespace(show_message=Mock())
        tool = SimpleNamespace(message_overlay=message_overlay, deploy_log=Mock())

        with patch("main.sys.platform", "win32"):
            DeployTool._on_dashboard_login_ok(tool, "环境3（宏阳鑫）", "pqm")

        tool.deploy_log.appendPlainText.assert_called_once_with(
            "已自动登录并打开 pqm Pods：环境3（宏阳鑫）"
        )
        message_overlay.show_message.assert_not_called()
        information.assert_not_called()

    @patch("main.QMessageBox.information")
    @patch("main.QApplication.clipboard")
    @patch("main.unprotect_secret", return_value="saved-password")
    @patch("main.webbrowser.open")
    def test_environment_uses_default_browser_and_copies_password(
        self, open_browser, unprotect, clipboard, information
    ):
        DeployTool._open_dashboard_environment(
            SimpleNamespace(),
            {
                "env_url": "https://environment.example",
                "env_username": "operator",
                "env_password": "encrypted",
            },
        )

        open_browser.assert_called_once_with("https://environment.example")
        clipboard.return_value.setText.assert_called_once_with("saved-password")
        self.assertIn("operator", information.call_args.args[2])

    def test_dashboard_double_click_uses_saved_credentials(self):
        dashboard = {"name": "环境3", "url": "https://dashboard.example"}
        tree_item = Mock()
        tree_item.data.return_value = 0
        tool = SimpleNamespace(
            store=SimpleNamespace(data={"dashboards": [dashboard]}),
            _open_dashboard_environment=Mock(),
            _on_open_dashboard_item=Mock(),
        )

        DeployTool._on_dashboard_item_double_clicked(tool, tree_item, 0)
        tool._open_dashboard_environment.assert_called_once_with(dashboard)
        tool._on_open_dashboard_item.assert_not_called()

        DeployTool._on_dashboard_item_double_clicked(tool, tree_item, 1)
        tool._on_open_dashboard_item.assert_called_once_with(dashboard)

    @patch("main.QMessageBox.warning")
    @patch("main.QApplication.clipboard")
    @patch("main.webbrowser.open")
    def test_cloud_status_fallback_opens_page_and_copies_password(
        self, open_browser, clipboard, warning
    ):
        settings = {
            "url": "https://rancher.example/dashboard/pods",
            "username": "operator",
            "password_plain": "saved-password",
        }

        DeployTool._on_cloud_status_fallback(
            SimpleNamespace(), settings, "自动登录失败"
        )

        open_browser.assert_called_once_with(settings["url"])
        clipboard.return_value.setText.assert_called_once_with("saved-password")
        self.assertIn("operator", warning.call_args.args[2])

    def test_rancher_login_reopens_configured_pods_page(self):
        state = {"logged_in": False}
        username_input = Mock(is_displayed=Mock(return_value=True), is_enabled=Mock(return_value=True))
        password_input = Mock(is_displayed=Mock(return_value=True), is_enabled=Mock(return_value=True))
        login_button = Mock(is_displayed=Mock(return_value=True), is_enabled=Mock(return_value=True))
        driver = Mock(current_url="")
        target_url = "https://rancher.example/dashboard/c/test/explorer/pod"

        def get(url):
            driver.current_url = url

        def find_elements(_by, selector):
            if state["logged_in"]:
                return []
            if selector == "input[name='username']":
                return [username_input]
            if selector == "input[name='password']":
                return [password_input]
            if selector == "button[type='submit']":
                return [login_button]
            return []

        def click_login():
            state["logged_in"] = True
            driver.current_url = "https://rancher.example/dashboard/home"

        login_button.click.side_effect = click_login
        driver.get.side_effect = get
        driver.find_elements.side_effect = find_elements

        class Wait:
            def __init__(self, browser, _timeout):
                self.browser = browser

            def until(self, condition):
                result = condition(self.browser)
                if not result:
                    raise AssertionError("wait condition did not pass")
                return result

        def import_module(name):
            if name.endswith(".by"):
                return SimpleNamespace(By=SimpleNamespace(CSS_SELECTOR="css"))
            return SimpleNamespace(WebDriverWait=Wait)

        tool = SimpleNamespace(emit=Mock(), dashboard_drivers=[])
        settings = {
            "name": "Rancher Pod 状态页",
            "url": target_url,
            "username": "operator",
            "password_plain": "secret",
        }

        with (
            patch("main.create_browser_driver", return_value=driver),
            patch("main.importlib.import_module", side_effect=import_module),
        ):
            DeployTool._open_cloud_status_page(tool, settings)

        self.assertEqual(
            [call.args[0] for call in driver.get.call_args_list],
            [target_url, target_url],
        )
        self.assertEqual(tool.dashboard_drivers, [driver])
        tool.emit.assert_any_call("cloud_status_login_ok", "Rancher Pod 状态页")

    def test_cloud_deploy_opens_configured_rancher_status_page(self):
        api = Mock()
        api.deploy.return_value = {"target": {"id": "deploy-1"}}
        tool = SimpleNamespace(
            _ensure_cloud_login=Mock(return_value=api),
            emit=Mock(),
        )
        status_page = {
            "name": "Rancher Pod 状态页",
            "url": "https://rancher.example/dashboard/pods",
            "username": "operator",
            "password_plain": "secret",
        }
        settings = {
            "project": "ESC",
            "deploy_type": "esc 后端",
            "cloud": "Huawei",
            "region": "测试区",
            "package": PACKAGE,
            "status_page": status_page,
        }

        DeployTool._run_cloud_deploy(tool, settings)

        tool.emit.assert_any_call("open_cloud_status", status_page)

    def test_cloud_project_mismatch_disables_one_click_deploy(self):
        settings = {
            "url": "https://ops.example",
            "username": "user",
            "password_plain": "secret",
            "project": "ETM",
            "deploy_type": "esc 后端",
            "cloud": "Huawei",
            "region": "预测试",
        }
        tool = SimpleNamespace(
            build_deploy_mode=Mock(currentText=Mock(return_value="云端部署")),
            build_button=Mock(),
            build_cloud_loading_label=Mock(),
            build_cloud_loading=False,
            build_cloud_target_dirty=False,
            build_running=False,
            current_job_path=["cloud-package", "15-ESC", "service-5.3.0"],
            current_display_path=[
                "云应用打包", "15-ESC-设备中心", "5-SERVICE-5.3.0"
            ],
            _build_cloud_settings_from_ui=Mock(return_value=settings),
            _cloud_account_complete=DeployTool._cloud_account_complete,
        )

        DeployTool._update_build_cloud_ready(tool)

        tool.build_button.setEnabled.assert_called_once_with(False)
        status_text = tool.build_cloud_loading_label.setText.call_args.args[0]
        self.assertIn("ESC", status_text)
        self.assertIn("ETM", status_text)

    def test_cloud_project_is_not_validated_before_job_selection(self):
        settings = {
            "url": "https://ops.example",
            "username": "user",
            "password_plain": "secret",
            "project": "ESC",
            "deploy_type": "esc 后端",
            "cloud": "Huawei",
            "region": "预测试",
        }
        tool = SimpleNamespace(
            build_deploy_mode=Mock(currentText=Mock(return_value="云端部署")),
            build_button=Mock(),
            build_cloud_loading_label=Mock(),
            build_cloud_loading=False,
            build_cloud_target_dirty=False,
            build_running=False,
            current_job_path=[],
            current_display_path=[],
            _build_cloud_settings_from_ui=Mock(return_value=settings),
            _cloud_account_complete=DeployTool._cloud_account_complete,
        )

        DeployTool._update_build_cloud_ready(tool)

        tool.build_button.setEnabled.assert_called_once_with(False)
        status_text = tool.build_cloud_loading_label.setText.call_args.args[0]
        self.assertEqual(status_text, "任务层级尚未确认，暂不识别部署专案")
        self.assertNotIn("禁止", status_text)

    def test_build_only_mode_does_not_require_cloud_target(self):
        tool = SimpleNamespace(
            build_deploy_mode=Mock(currentText=Mock(return_value="云端部署")),
            deploy_after_build_checkbox=Mock(isChecked=Mock(return_value=False)),
            build_button=Mock(),
            build_running=False,
        )

        DeployTool._update_build_cloud_ready(tool)

        tool.build_button.setEnabled.assert_called_once_with(True)

    @patch("main.QMessageBox.warning")
    def test_start_build_only_skips_cloud_validation(self, warning):
        tool = SimpleNamespace(
            build_running=False,
            jenkins=Mock(),
            current_job_path=["job"],
            current_job_url="http://jenkins/job/job/",
            build_deploy_mode=Mock(currentText=Mock(return_value="云端部署")),
            deploy_after_build_checkbox=Mock(isChecked=Mock(return_value=False)),
            param_vars={},
            store=SimpleNamespace(
                data={"jenkins": {"values": {}}},
                save=Mock(),
            ),
            build_log=Mock(),
            cancel_event=threading.Event(),
            pipeline_phase="",
            pipeline_location="",
            build_button=Mock(),
            cancel_button=Mock(),
            build_progress=Mock(),
            deploy_log=Mock(),
            _update_build_status=Mock(),
            run_thread=Mock(),
        )

        DeployTool.start_build(tool)

        warning.assert_not_called()
        tool.run_thread.assert_called_once()
        self.assertFalse(tool.active_deploy_after_build)
        tool.deploy_after_build_checkbox.setEnabled.assert_called_once_with(False)

    def test_job_filter_keeps_matching_branch_and_hides_other_tasks(self):
        class TreeItem:
            def __init__(self, text, children=None, data=True):
                self._text = text
                self._children = children or []
                self._data = {"path": [text]} if data else None
                self.hidden = False
                self.expanded = False

            def childCount(self):
                return len(self._children)

            def child(self, index):
                return self._children[index]

            def data(self, _column, _role):
                return self._data

            def text(self, _column):
                return self._text

            def setHidden(self, hidden):
                self.hidden = hidden

            def setExpanded(self, expanded):
                self.expanded = expanded

        matching = TreeItem("3-SERVICE")
        sibling = TreeItem("2-APP")
        parent = TreeItem("16-ETM-生产任务管理", [sibling, matching])
        other = TreeItem("18-KMS-设备刀具管理")

        self.assertTrue(
            DeployTool._filter_job_item(SimpleNamespace(), parent, "service")
        )
        self.assertFalse(parent.hidden)
        self.assertTrue(parent.expanded)
        self.assertFalse(matching.hidden)
        self.assertTrue(sibling.hidden)
        self.assertFalse(
            DeployTool._filter_job_item(SimpleNamespace(), other, "service")
        )
        self.assertTrue(other.hidden)

    def test_job_cache_recurses_and_second_level_filter_keeps_full_subtree(self):
        jobs_by_path = {
            (): [
                {"name": "二开打包", "displayName": "二开打包", "_class": "folder"},
                {
                    "name": "et-root-only",
                    "displayName": "ET 根任务",
                    "_class": "project",
                },
            ],
            ("二开打包",): [
                {
                    "name": "16-ETM",
                    "displayName": "16-ETM-生产任务管理",
                    "_class": "folder",
                },
            ],
            ("二开打包", "16-ETM"): [
                {
                    "name": "3-SERVICE",
                    "displayName": "3-SERVICE",
                    "_class": "folder",
                },
            ],
            ("二开打包", "16-ETM", "3-SERVICE"): [
                {
                    "name": "etmbackend-hyx",
                    "displayName": "etmbackend-hyx",
                    "_class": "project",
                },
            ],
        }
        jenkins = SimpleNamespace(
            list_jobs=Mock(side_effect=lambda path: jobs_by_path[tuple(path)])
        )
        tool = SimpleNamespace(jenkins=jenkins)
        tool._fetch_job_cache = DeployTool._fetch_job_cache.__get__(tool)

        cache = tool._fetch_job_cache()
        request_count = jenkins.list_jobs.call_count
        filtered, match_count = DeployTool._filter_job_cache(cache, "et")

        self.assertEqual(match_count, 1)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["name"], "二开打包")
        self.assertEqual(
            filtered[0]["children"][0]["name"],
            "16-ETM",
        )
        service = filtered[0]["children"][0]["children"][0]
        self.assertEqual(service["name"], "3-SERVICE")
        self.assertEqual(service["children"][0]["name"], "etmbackend-hyx")
        self.assertEqual(jenkins.list_jobs.call_count, request_count)

    def test_job_cache_search_expands_roots_but_keeps_modules_collapsed(self):
        cache = [
            {
                "name": "二开打包",
                "children": [
                    {
                        "name": "16-ETM",
                        "children": [
                            {"name": "3-SERVICE", "children": []},
                        ],
                    },
                ],
            },
        ]

        class TreeItem:
            def __init__(self, node):
                self.node = node
                self.children = []
                self.expanded = None

            def setExpanded(self, expanded):
                self.expanded = expanded

        class Tree:
            def __init__(self):
                self.roots = []
                self.clear_count = 0

            def clear(self):
                self.roots = []
                self.clear_count += 1

        tree = Tree()

        def create_item(parent_item, _path, node, add_placeholder=True):
            self.assertFalse(add_placeholder)
            item = TreeItem(node)
            if parent_item is None:
                tree.roots.append(item)
            else:
                parent_item.children.append(item)
            return item

        tool = SimpleNamespace(
            job_tree=tree,
            _create_job_tree_item=create_item,
        )

        DeployTool._render_job_cache(tool, cache, expand_depth=1)

        self.assertEqual(tree.clear_count, 1)
        root = tree.roots[0]
        module = root.children[0]
        service = module.children[0]
        self.assertTrue(root.expanded)
        self.assertFalse(module.expanded)
        self.assertFalse(service.expanded)

    def test_saved_job_cache_requires_matching_jenkins_identity(self):
        jobs = [{"name": "custom-dev", "children": []}]
        store = SimpleNamespace(data={
            "jenkins": {
                "job_cache": {
                    "version": JENKINS_JOB_CACHE_VERSION,
                    "url": "https://jenkins.example",
                    "username": "builder",
                    "jobs": jobs,
                },
            },
        })
        tool = SimpleNamespace(
            store=store,
            jenkins=SimpleNamespace(
                url="https://jenkins.example",
                session=SimpleNamespace(auth=("builder", "token")),
            ),
        )

        self.assertIs(DeployTool._load_saved_job_cache(tool), jobs)
        tool.jenkins.url = "https://another-jenkins.example"
        self.assertIsNone(DeployTool._load_saved_job_cache(tool))

    def test_invalid_saved_job_cache_is_ignored(self):
        tool = SimpleNamespace(
            store=SimpleNamespace(data={
                "jenkins": {
                    "job_cache": {
                        "version": JENKINS_JOB_CACHE_VERSION,
                        "url": "https://jenkins.example",
                        "username": "builder",
                        "jobs": [{"name": "custom-dev"}],
                    },
                },
            }),
            jenkins=SimpleNamespace(
                url="https://jenkins.example",
                session=SimpleNamespace(auth=("builder", "token")),
            ),
        )

        self.assertIsNone(DeployTool._load_saved_job_cache(tool))

    def test_fresh_job_cache_persists_identity_and_tree(self):
        jobs = [{"name": "custom-dev", "children": []}]
        store = SimpleNamespace(data={"jenkins": {}}, save=Mock())
        tool = SimpleNamespace(
            store=store,
            jenkins=SimpleNamespace(
                url="https://jenkins.example/",
                session=SimpleNamespace(auth=("builder", "token")),
            ),
        )

        DeployTool._save_job_cache(tool, jobs)

        cache = store.data["jenkins"]["job_cache"]
        self.assertEqual(cache["version"], JENKINS_JOB_CACHE_VERSION)
        self.assertEqual(cache["url"], "https://jenkins.example")
        self.assertEqual(cache["username"], "builder")
        self.assertIs(cache["jobs"], jobs)
        self.assertRegex(cache["updated_at"], r"^\d{4}-\d{2}-\d{2} ")
        store.save.assert_called_once_with()

    def test_complete_saved_jenkins_credentials_trigger_auto_login(self):
        tool = SimpleNamespace(
            jenkins=None,
            j_url=Mock(text=Mock(return_value="http://jenkins")),
            j_user=Mock(text=Mock(return_value="builder")),
            j_password=Mock(text=Mock(return_value="token")),
            login_jenkins=Mock(),
        )

        DeployTool._auto_login_jenkins(tool)

        tool.login_jenkins.assert_called_once_with()

    def test_exception_message_falls_back_to_exception_type(self):
        self.assertEqual(exception_message(TimeoutError()), "TimeoutError")

    def test_exception_message_ignores_empty_selenium_message_prefix(self):
        exc = RuntimeError("Message: \nStacktrace:\nwebdriver details")
        self.assertEqual(exception_message(exc), "RuntimeError")

    def test_exception_message_preserves_cloud_step_context(self):
        exc = RuntimeError("Ops 登录失败：等待流水线部署页面加载超时")
        self.assertEqual(
            exception_message(exc),
            "Ops 登录失败：等待流水线部署页面加载超时",
        )

    def test_service_type_match_ignores_case_and_whitespace(self):
        self.assertTrue(service_type_matches("esc 后端", "ESC后端"))
        self.assertTrue(service_type_matches("esc 后端", "15-ESC / 后端"))
        self.assertFalse(service_type_matches("esc 后端", "ESC前端"))

    def test_deploy_environment_selector_loads_the_selected_environment(self):
        environments = [
            {"name": "环境1", "host": "172.16.6.124"},
            {"name": "环境2", "host": "172.16.2.23"},
        ]
        tool = SimpleNamespace(
            loading_environment=False,
            env_dirty=True,
            store=SimpleNamespace(data={"environments": environments}),
            env_combo=Mock(),
            build_env_combo=Mock(),
            _set_env_var=Mock(),
            refresh_environment=Mock(),
        )
        tool.env_combo.currentText.return_value = "环境1"
        tool.build_env_combo.currentText.return_value = "环境1"

        DeployTool.load_environment(tool, "环境2")

        expected_host = next(
            call.args[1]
            for call in tool._set_env_var.call_args_list
            if call.args[0] == "host"
        )
        self.assertEqual(expected_host, "172.16.2.23")
        tool.env_combo.setCurrentText.assert_called_with("环境2")
        tool.build_env_combo.setCurrentText.assert_called_with("环境2")

    def test_one_click_cloud_pipeline_skips_ssh_and_dashboard(self):
        jenkins = Mock()
        jenkins.build.return_value = "http://jenkins/queue/item/1"
        jenkins.wait_for_build.return_value = (
            {"result": "SUCCESS", "number": 12},
            "naming to registry.example/industry/" + PACKAGE + " done",
        )
        tool = SimpleNamespace(
            jenkins=jenkins,
            cancel_event=threading.Event(),
            emit=Mock(),
            _handle_build_output=Mock(),
            _handle_build_progress=Mock(),
            _raise_if_deploy_cancelled=Mock(),
            _run_cloud_deploy=Mock(),
            _execute_deploy=Mock(),
        )
        cloud_settings = {
            "project": "ESC",
            "deploy_type": "esc 后端",
            "cloud": "Huawei",
            "region": "测试区",
            "package": "",
        }

        DeployTool._run_pipeline(
            tool,
            ["job"],
            {},
            "http://jenkins/job/job/",
            "云端部署",
            None,
            None,
            cloud_settings,
        )

        tool._run_cloud_deploy.assert_called_once()
        deployed_settings, cancel_event = tool._run_cloud_deploy.call_args.args
        self.assertEqual(deployed_settings["package"], PACKAGE)
        self.assertIs(cancel_event, tool.cancel_event)
        tool._execute_deploy.assert_not_called()
        self.assertFalse(any(
            call.args[0] == "open_dashboard_item"
            for call in tool.emit.call_args_list
        ))

    def test_one_click_ssh_pipeline_resolves_yaml_from_final_package(self):
        package = "escbackend-hyx-5.2.0:5.2.0.1000.10000"
        jenkins = Mock()
        jenkins.build.return_value = "http://jenkins/queue/item/2"
        jenkins.wait_for_build.return_value = (
            {"result": "SUCCESS", "number": 13},
            "naming to registry.example/industry/" + package + " done",
        )
        tool = SimpleNamespace(
            jenkins=jenkins,
            cancel_event=threading.Event(),
            emit=Mock(),
            _handle_build_output=Mock(),
            _handle_build_progress=Mock(),
            _raise_if_deploy_cancelled=Mock(),
            _run_cloud_deploy=Mock(),
            _execute_deploy=Mock(),
        )
        saved_etm_environment = {
            "name": "环境3（宏阳鑫）",
            "host": "172.16.2.47",
            "port": "22",
            "yaml_dir": "/data/yaml/app/backend",
            "yaml_file": "etm.yaml",
            "apply_command": "kubectl apply -f etm.yaml",
        }

        DeployTool._run_pipeline(
            tool,
            ["job"],
            {},
            "http://jenkins/job/job/",
            "SSH 环境部署",
            saved_etm_environment,
            {"name": "环境3", "url": "https://dashboard"},
            None,
        )

        deployed_environment, deployed_package, cancel_event = (
            tool._execute_deploy.call_args.args
        )
        self.assertEqual(deployed_package, package)
        self.assertIs(cancel_event, tool.cancel_event)
        self.assertEqual(deployed_environment["yaml_file"], "esc.yaml")
        self.assertEqual(
            deployed_environment["apply_command"], "kubectl apply -f esc.yaml"
        )

    def test_merged_pipeline_opens_shared_dashboard_once(self):
        jenkins = Mock()
        jenkins.build.return_value = "http://jenkins/queue/item/3"
        jenkins.wait_for_build.return_value = (
            {"result": "SUCCESS", "number": 14},
            "naming to registry.example/industry/" + PACKAGE + " done",
        )
        coordinator = Mock()
        follower = SimpleNamespace(emit=Mock())
        dashboard = {
            "id": "dashboard-1",
            "name": "环境3",
            "url": "https://dashboard.example",
        }
        environment = {
            "name": "环境3（宏阳鑫）",
            "host": "172.16.2.47",
            "port": "22",
            "username": "root",
            "image_dir": "/data/images",
            "image_file": "images.txt",
            "pull_command": "docker pull",
            "yaml_dir": "/data/yaml/app/backend",
            "yaml_file": "etm.yaml",
            "apply_command": "kubectl apply -f etm.yaml",
        }
        tool = SimpleNamespace(
            jenkins=jenkins,
            cancel_event=threading.Event(),
            emit=Mock(),
            _handle_build_output=Mock(),
            _handle_build_progress=Mock(),
            _raise_if_deploy_cancelled=Mock(),
            _execute_deploy_batch=Mock(),
            window_manager=SimpleNamespace(batch_coordinator=coordinator),
        )
        entries = [
            {
                "owner": tool,
                "environment": environment,
                "package": PACKAGE,
                "dashboard": dashboard,
            },
            {
                "owner": follower,
                "environment": environment,
                "package": "etmfrontend-5.2.0:4.0.2.3430",
                "dashboard": dict(dashboard),
            },
        ]
        coordinator.wait_until_ready.return_value = (True, entries)

        DeployTool._run_pipeline(
            tool,
            ["job"],
            {},
            "http://jenkins/job/job/",
            "SSH 环境部署",
            environment,
            dashboard,
            None,
            batch_token=("environment", 1),
        )

        tool._execute_deploy_batch.assert_called_once_with(
            entries, batch_token=("environment", 1)
        )
        coordinator.complete.assert_called_once_with(("environment", 1), None)
        dashboard_calls = [
            call
            for call in tool.emit.call_args_list
            if call.args == ("open_dashboard_item", {**dashboard, "namespace": "etm"})
        ]
        self.assertEqual(len(dashboard_calls), 1)
        follower.emit.assert_not_called()

    def test_resolve_ssh_environment_keeps_connection_but_replaces_yaml(self):
        environment = {
            "host": "172.16.2.47",
            "username": "root",
            "yaml_file": "etm.yaml",
        }

        resolved = resolve_ssh_environment_for_package(
            environment, "escbackend-hyx-5.2.0:5.2.0.1000.10000"
        )

        self.assertEqual(resolved["host"], "172.16.2.47")
        self.assertEqual(resolved["username"], "root")
        self.assertEqual(resolved["yaml_dir"], "/data/yaml/app/backend")
        self.assertEqual(resolved["yaml_file"], "esc.yaml")
        self.assertEqual(resolved["apply_command"], "kubectl apply -f esc.yaml")

    def test_yaml_image_reference_returns_target_registry_image(self):
        content = (
            "image: registry.172.16.2.47.nip.io/industry/"
            "escbackend-hyx-5.2.0:old\n"
        )
        package = "escbackend-hyx-5.2.0:5.2.0.1000.10000"

        self.assertEqual(
            yaml_image_reference(content, package),
            "registry.172.16.2.47.nip.io/industry/" + package,
        )

    def test_isolated_image_workspace_uses_private_manifest_and_script_links(self):
        sftp = Mock()
        sftp.listdir.return_value = [
            "app_image_list.txt",
            "app_images_store.sh",
            "registry-login.json",
            ".deploy-console-old",
        ]
        manifest = Mock()
        manifest.__enter__ = Mock(return_value=manifest)
        manifest.__exit__ = Mock(return_value=False)
        sftp.open.return_value = manifest
        data = {
            "image_dir": "/home/setup/images",
            "image_file": "app_image_list.txt",
        }

        with patch("main.uuid.uuid4", return_value=SimpleNamespace(hex="task1")):
            workspace = DeployTool._create_isolated_image_workspace(
                sftp, data, PACKAGE
            )

        self.assertEqual(workspace, "/home/setup/images/.deploy-console-task1")
        sftp.mkdir.assert_called_once_with(workspace, mode=0o700)
        manifest.write.assert_called_once_with((PACKAGE + "\n").encode("utf-8"))
        linked_sources = {call.args[0] for call in sftp.symlink.call_args_list}
        self.assertEqual(
            linked_sources,
            {
                "/home/setup/images/app_images_store.sh",
                "/home/setup/images/registry-login.json",
            },
        )

    def test_build_only_pipeline_skips_all_deploy_and_dashboard_actions(self):
        jenkins = Mock()
        jenkins.build.return_value = "http://jenkins/queue/item/1"
        jenkins.wait_for_build.return_value = (
            {"result": "SUCCESS", "number": 12},
            "naming to registry.example/industry/" + PACKAGE + " done",
        )
        tool = SimpleNamespace(
            jenkins=jenkins,
            cancel_event=threading.Event(),
            emit=Mock(),
            _handle_build_output=Mock(),
            _handle_build_progress=Mock(),
            _raise_if_deploy_cancelled=Mock(),
            _run_cloud_deploy=Mock(),
            _execute_deploy=Mock(),
        )

        DeployTool._run_pipeline(
            tool,
            ["job"],
            {},
            "http://jenkins/job/job/",
            "云端部署",
            None,
            None,
            None,
            False,
        )

        tool._run_cloud_deploy.assert_not_called()
        tool._execute_deploy.assert_not_called()
        self.assertFalse(any(
            call.args[0] in ("open_dashboard_item", "select_deploy_tab")
            for call in tool.emit.call_args_list
        ))
        tool.emit.assert_any_call("build_status", "打包完成")
        tool.emit.assert_any_call("info", f"打包已完成：\n{PACKAGE}")

    def test_dashboard_binding_migrates_legacy_name_and_survives_rename(self):
        config = {
            "dashboards": [{"name": "环境1", "url": "https://dashboard"}],
            "environments": [{"name": "环境1", "dashboard": "环境1"}],
        }
        ids = iter(["dashboard-1"])

        self.assertTrue(ensure_dashboard_bindings(config, lambda: next(ids)))
        self.assertEqual(config["environments"][0]["dashboard_id"], "dashboard-1")

        rename_bound_dashboard(config, "dashboard-1", "环境2")
        self.assertEqual(config["dashboards"][0]["name"], "环境2")
        self.assertEqual(config["environments"][0]["dashboard"], "环境2")

    def test_dashboard_binding_rejects_duplicate_name(self):
        config = {
            "dashboards": [
                {"id": "dashboard-1", "name": "环境1"},
                {"id": "dashboard-2", "name": "环境2"},
            ],
            "environments": [],
        }

        with self.assertRaisesRegex(ValueError, "已存在"):
            rename_bound_dashboard(config, "dashboard-1", "环境2")

    def test_extract_package_number(self):
        log = (
            "naming to registry.example/industry/" + PACKAGE + " done\n"
            "later unrelated.example/base:latest"
        )
        self.assertEqual(extract_package_number(log), PACKAGE)

    def test_parameter_order_only_for_secondary_build(self):
        params = [{"name": "GIT_BRANCH"}, {"name": "X"}, {"name": "GIT_BRANCH2"}]
        result = reorder_parameters(params, "二开打包/16-ETM/3-SERVICE")
        self.assertEqual([x["name"] for x in result], ["GIT_BRANCH2", "GIT_BRANCH", "X"])
        internal = reorder_parameters(params, ["custom-dev", "16-ETM", "3-SERVICE"])
        self.assertEqual([x["name"] for x in internal], ["GIT_BRANCH2", "GIT_BRANCH", "X"])
        self.assertEqual(reorder_parameters(params, "普通打包"), params)

    def test_secondary_build_path_supports_display_and_internal_names(self):
        self.assertTrue(is_secondary_build_path("二开打包/16-ETM/3-SERVICE"))
        self.assertTrue(is_secondary_build_path(["custom-dev", "16-ETM", "3-SERVICE"]))
        self.assertFalse(is_secondary_build_path(["IappPack", "16-ETM", "3-SERVICE"]))

    def test_update_image_list(self):
        old = "etmbackend-yisen-5.2.0:5.2.0.1001.17171\n"
        self.assertEqual(update_image_list(old, PACKAGE), PACKAGE + "\n")

    def test_update_image_list_preserves_unrelated_packages_by_default(self):
        old = (
            "escbackend-hbxx-5.2.0:5.2.0.1000.10000\n"
            "etmbackend-yisen-5.2.0:5.2.0.1001.17171\n"
        )
        expected = "escbackend-hbxx-5.2.0:5.2.0.1000.10000\n" + PACKAGE + "\n"

        self.assertEqual(update_image_list(old, PACKAGE), expected)

    def test_windows_image_list_cleanup_keeps_only_current_package(self):
        old = (
            "eecbackend-hbxx-5.2.0:5.2.0.1001.10001\n"
            "eecfrontend-5.2.0:4.0.2.1596\n"
            "escbackend-hbxx-5.2.0:5.2.0.1000.10000\n"
            "etmbackend-hbxx-5.2.0:5.2.0.1001.10013\n"
        )

        self.assertEqual(
            update_image_list(old, PACKAGE, clear_existing=True),
            PACKAGE + "\n",
        )

    def test_update_yaml_tag_only(self):
        old = "  image: registry.172.16.6.124.nip.io/industry/etmbackend-yisen-5.2.0:old\n"
        expected = "  image: registry.172.16.6.124.nip.io/industry/" + PACKAGE + "\n"
        self.assertEqual(update_yaml_image(old, PACKAGE), expected)

    def test_update_yaml_replaces_variant_name_for_same_product_and_service(self):
        old = (
            "  image: registry.172.16.2.47.nip.io/industry/"
            "escbackend-hbxx-5.2.0:5.2.0.1000.9999\n"
        )
        package = "escbackend-hyx-5.2.0:5.2.0.1000.10000"
        expected = (
            "  image: registry.172.16.2.47.nip.io/industry/" + package + "\n"
        )

        self.assertEqual(update_yaml_image(old, package), expected)

    def test_jenkins_build_uses_exact_endpoint_and_normalizes_boolean(self):
        client = JenkinsClient("http://jenkins", "user", "token")
        response = Mock()
        response.ok = True
        response.headers = {"Location": "http://jenkins/queue/item/1/"}
        client.session.post = Mock(return_value=response)
        client._crumb_headers = Mock(return_value={})
        queue_url = client.build(
            ["ignored"],
            {"useLog": "True"},
            "http://jenkins/job/exact-name/",
        )
        self.assertEqual(queue_url, "http://jenkins/queue/item/1")
        args, kwargs = client.session.post.call_args
        self.assertEqual(args[0], "http://jenkins/job/exact-name/buildWithParameters")
        self.assertEqual(kwargs["data"]["useLog"], "true")

    def test_git_parameter_choices(self):
        client = JenkinsClient("http://jenkins", "user", "token")
        client.get_json = Mock(return_value={
            "values": [
                {"name": "master", "value": "origin/master"},
                {"name": "dev", "value": "origin/dev"},
            ]
        })
        choices = client._get_git_parameter_choices(
            "http://jenkins/job/project", "GIT_BRANCH"
        )
        self.assertEqual(choices, ["origin/master", "origin/dev"])

    def test_secondary_branch_refresh_uses_primary_choices_with_legacy_clients(self):
        params = [
            {"name": "GIT_BRANCH", "choices": ["origin/main", "origin/feature"]},
            {"name": "GIT_BRANCH2", "type": "StringParameterDefinition"},
        ]
        for single_parameter_api in (False, True):
            with self.subTest(single_parameter_api=single_parameter_api):
                client = SimpleNamespace(get_parameters=Mock(return_value=params))
                if single_parameter_api:
                    client.get_git_parameter_choices = Mock(return_value=object())
                tool = SimpleNamespace(emit=Mock())

                DeployTool._load_branch_choices(
                    tool, client, "https://jenkins.example/job/service/",
                    "GIT_BRANCH2", ["service"], 1, 2,
                )

                tool.emit.assert_called_once_with("branch_choices", (
                    client, ("service",), "GIT_BRANCH2", 1, 2,
                    ["origin/main", "origin/feature"],
                ))

    def test_job_list_requests_display_name(self):
        client = JenkinsClient("http://jenkins", "user", "token")
        client.get_json = Mock(return_value={"jobs": []})
        client.list_jobs([])
        _, params = client.get_json.call_args.args
        self.assertIn("displayName", params["tree"])

    @patch("main.time.sleep")
    def test_jenkins_get_retries_five_times_then_succeeds(self, sleep):
        client = JenkinsClient("http://jenkins", "user", "token")
        failures = []
        for _ in range(5):
            response = Mock(status_code=502, ok=False)
            failures.append(response)
        success = Mock(status_code=200, ok=True)
        success.json.return_value = {"jobs": []}
        client.session.get = Mock(side_effect=[*failures, success])

        self.assertEqual(client.get_json("http://jenkins/api/json"), {"jobs": []})
        self.assertEqual(client.session.get.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 5, 5])

    @patch("main.time.sleep")
    def test_jenkins_get_fails_after_five_retries(self, sleep):
        client = JenkinsClient("http://jenkins", "user", "token")
        response = Mock(status_code=502, ok=False, text="Bad Gateway")
        response.request.method = "GET"
        response.url = "http://jenkins/api/json"
        client.session.get = Mock(return_value=response)

        with self.assertRaisesRegex(RuntimeError, "Jenkins HTTP 502"):
            client.get_json("http://jenkins/api/json")

        self.assertEqual(client.session.get.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_cancel_queued_jenkins_build(self):
        client = JenkinsClient("http://jenkins", "user", "token")
        client._post_action = Mock()
        client.get_json = Mock(return_value={"cancelled": True})
        event = threading.Event()
        event.set()
        with self.assertRaises(PipelineCancelled):
            client.wait_for_build(
                "http://jenkins/queue/item/1",
                lambda line: None,
                cancel_event=event,
            )
        client._post_action.assert_called_once_with(
            "http://jenkins/queue/item/1/cancelQueue"
        )

    @patch("main.time.sleep")
    def test_queue_cancellation_follows_a_build_claimed_by_an_executor(self, sleep):
        client = JenkinsClient("http://jenkins", "user", "token")
        client._post_action = Mock()
        client.get_json = Mock(side_effect=[
            {"executable": {"url": "http://jenkins/job/a/12/"}},
            {"building": True},
            {"building": False, "result": "ABORTED"},
        ])
        event = threading.Event()
        event.set()

        with self.assertRaises(PipelineCancelled):
            client.wait_for_build("http://jenkins/queue/item/1", Mock(), cancel_event=event)

        self.assertEqual([call.args[0] for call in client._post_action.call_args_list], [
            "http://jenkins/queue/item/1/cancelQueue", "http://jenkins/job/a/12/stop",
        ])
        self.assertEqual(client.get_json.call_count, 3)

    @patch("main.time.sleep")
    def test_cancellation_never_claims_success_without_a_stopped_build(self, sleep):
        for response in ({"building": True}, {}):
            with self.subTest(response=response):
                client = JenkinsClient("http://jenkins", "user", "token")
                client._post_action = Mock()
                client.get_json = Mock(return_value=response)
                with self.assertRaisesRegex(RuntimeError, "仍处于运行状态"):
                    client.cancel_build("jenkins", "http://jenkins/job/a/12")
                self.assertEqual([call.args[0].rsplit("/", 1)[-1]
                                  for call in client._post_action.call_args_list],
                                 ["stop", "term", "kill"])

    @patch("main.time.sleep")
    def test_cancellation_confirms_the_final_kill_result(self, sleep):
        client = JenkinsClient("http://jenkins", "user", "token")
        client._post_action = Mock()
        client.get_json = Mock(side_effect=[{"building": True}] * 15 + [{"building": False}])
        client.cancel_build("jenkins", "http://jenkins/job/a/12")
        self.assertEqual(client._post_action.call_args.args[0], "http://jenkins/job/a/12/kill")

    def test_aborted_jenkins_build_is_reported_as_cancelled(self):
        jenkins = Mock()
        jenkins.build.return_value = "http://jenkins/queue/item/1"
        jenkins.wait_for_build.return_value = (
            {"result": "ABORTED", "number": 12},
            "cancelled",
        )
        tool = SimpleNamespace(
            jenkins=jenkins,
            cancel_event=threading.Event(),
            emit=Mock(),
            _handle_build_output=Mock(),
            _handle_build_progress=Mock(),
        )

        DeployTool._run_pipeline(
            tool, ["job"], {}, "http://jenkins/job/job/", "云端部署",
            None, None, {},
        )

        events = [call.args[:2] for call in tool.emit.call_args_list]
        self.assertIn(("build_status", "一键流程已取消"), events)
        self.assertFalse(any(event[0] == "error" for event in events))

    @patch("main.time.sleep")
    def test_cancel_running_build_escalates_until_stopped(self, sleep):
        client = JenkinsClient("http://jenkins", "user", "token")
        client._post_action = Mock()
        running = {"building": True, "result": None}
        stopped = {"building": False, "result": "ABORTED"}
        client.get_json = Mock(side_effect=[running] * 9 + [stopped])

        client.cancel_build("jenkins", "http://jenkins/job/a/12")

        self.assertEqual(
            [call.args[0] for call in client._post_action.call_args_list],
            [
                "http://jenkins/job/a/12/stop",
                "http://jenkins/job/a/12/term",
            ],
        )


class EnvironmentValidationTests(unittest.TestCase):
    def test_required_fields_dashboard_and_port_boundaries(self):
        data = {"name": "demo", "host": "example.invalid", "username": "tester", "port": "22"}
        for port in ("1", "22", "65535"):
            self.assertIsNone(_environment_validation_error({**data, "port": port}, True))
        for port in ("", "0", "65536", "invalid", None):
            with self.subTest(port=port):
                self.assertEqual(_environment_validation_error({**data, "port": port}, True)[0], "端口无效")
        for key in ("name", "host", "username"):
            self.assertEqual(_environment_validation_error({**data, key: ""}, True)[0], "缺少信息")
        self.assertEqual(_environment_validation_error(data, False)[0], "Dashboard 无效")

    @patch("main.protect_secret")
    @patch("main.QMessageBox.warning")
    def test_both_environment_forms_reject_invalid_ports_before_saving(self, warning, protect):
        data = {key: "" for _, key in ENVIRONMENT_FIELDS}
        data.update(name="demo", host="example.invalid", username="tester", port="65536", dashboard="demo")
        dialog = SimpleNamespace(vars=data, _get_value=data.__getitem__, accept=Mock())
        EnvironmentDialog._save(dialog)
        tool = SimpleNamespace(
            build_env_combo=Mock(), store=SimpleNamespace(data={
                "environments": [{"name": "demo"}],
                "dashboards": [{"id": "demo-id", "name": "demo"}],
            }, save=Mock()), _get_env_var=data.__getitem__,
        )
        tool.build_env_combo.currentText.return_value = "demo"
        DeployTool.save_environment(tool)
        self.assertEqual([call.args[1] for call in warning.call_args_list], ["端口无效", "端口无效"])
        protect.assert_not_called()
        dialog.accept.assert_not_called()
        tool.store.save.assert_not_called()

    def test_remote_shell_arguments_round_trip_without_expansion(self):
        for value in ("", "/data/yaml/app/backend", "space path", "a'b", "$(touch sentinel);*", "line\nbreak"):
            with self.subTest(value=value):
                self.assertEqual(shlex.split(shell_quote(value)), [value])


if __name__ == "__main__":
    unittest.main()
