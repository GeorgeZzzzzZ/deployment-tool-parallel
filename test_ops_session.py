import base64
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ops_api import OpsApiClient, OpsApiError, OpsSubmissionUnknown


class OfflineOpsSession:
    """Exercise real login/session code without network or saved credentials."""

    def __init__(self):
        self.server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.calls = []
        self.responses = {}
        self.login_count = 0

    def mount(self, *_args):
        pass

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path, dict(kwargs.get("headers", {}))))
        queued = self.responses.get(path)
        if queued:
            value = queued.pop(0)
            if isinstance(value, Exception):
                raise value
            status, payload = value
        else:
            status = 200
            if path == "/assets/appinfo/modules-cn.json":
                payload = {"appToken": "synthetic-app-token"}
            elif path.endswith("/publickey"):
                payload = {"publicKey": self.server_key.public_key().public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                ).decode()}
            elif path.endswith("/aeskey"):
                public = self.server_key.decrypt(base64.b64decode(
                    kwargs["json"]["clientEncryptPublicKey"]
                ), padding.PKCS1v15())
                client_key = serialization.load_pem_public_key(
                    b"-----BEGIN PUBLIC KEY-----\n" + public + b"\n-----END PUBLIC KEY-----"
                )
                payload = {"encryptAesKey": base64.b64encode(client_key.encrypt(
                    b"0123456789abcdef", padding.PKCS1v15()
                )).decode()}
            elif path.endswith("/login"):
                self.login_count += 1
                payload = {"token": f"synthetic-user-token-{self.login_count}",
                           "userId": "offline-user", "userName": "Offline"}
            elif path.endswith("/token/analyze"):
                if method != "POST" or kwargs.get("json") != {}:
                    raise AssertionError("Token analysis must match the published POST {} contract")
                payload = {"id": "offline-user", "name": "Offline"}
            else:
                payload = {"code": 0, "data": ["offline-result"]}
        response = requests.Response()
        response.status_code = status
        response.url = url
        response._content = json.dumps(payload).encode()
        return response

    def calls_to(self, path):
        return [call for call in self.calls if call[1] == path]


class OpsSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("ops_api.time.monotonic", return_value=1000).start()
        self.addCleanup(patch.stopall)
        self.transport = OfflineOpsSession()
        self.api = OpsApiClient(session=self.transport)
        self.api.login("https://offline.example", "offline-user", "synthetic-password")

    def test_idle_session_is_checked_when_due_without_logging_in_every_tick(self):
        self.assertTrue(hasattr(self.api, "maintain_session"), "Session maintenance is missing")
        self.assertFalse(self.api.maintenance_due())
        self.api.maintain_session()
        self.clock.return_value = 1301
        self.assertTrue(self.api.maintenance_due())
        self.api.maintain_session()
        self.assertFalse(self.api.maintenance_due())
        self.assertEqual(len(self.transport.calls_to("/api/iam/v2/identity/token/analyze")), 1)
        self.assertEqual(self.transport.login_count, 1)

    def test_periodic_relogin_keeps_same_identity_option_caches(self):
        self.assertTrue(hasattr(self.api, "maintain_session"), "Session maintenance is missing")
        self.api._projects_cache["huawei"] = ["etm"]
        self.api._options_cache[("ETM", "huawei")] = {"types": ["etm 后端"]}
        self.api._targets[("ETM", "backend", "huawei", "测试")] = {"id": 12}
        self.clock.return_value = 2801
        self.api.maintain_session()
        self.assertEqual(self.transport.login_count, 2)
        self.assertEqual(self.api.headers["digi-middleware-auth-user"], "synthetic-user-token-2")
        self.assertEqual(self.api.list_projects("Huawei"), ["etm"])
        self.assertEqual(self.api._options_cache[("ETM", "huawei")]["types"], ["etm 后端"])
        self.assertEqual(self.api._targets[("ETM", "backend", "huawei", "测试")]["id"], 12)

    def test_idle_expiration_reauthenticates_using_only_in_memory_credentials(self):
        self.assertTrue(hasattr(self.api, "maintain_session"), "Session maintenance is missing")
        self.transport.responses["/api/iam/v2/identity/token/analyze"] = [(401, {})]
        self.clock.return_value = 1301
        self.api.maintain_session()
        self.assertTrue(self.api.authenticated)
        self.assertEqual(self.transport.login_count, 2)

    def test_get_expiration_relogs_and_retries_once_with_new_token(self):
        self.transport.responses["/api/v1/app"] = [(401, {"message": "expired"})]
        result = self.api._bff("GET", "/api/v1/app")
        self.assertEqual(result["data"], ["offline-result"])
        calls = self.transport.calls_to("/api/v1/app")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1][2]["digi-middleware-auth-user"], "synthetic-user-token-2")

    def test_known_read_only_post_can_recover_business_401(self):
        path = "/api/v1/deployment/data/app/group/multi"
        self.transport.responses[path] = [(200, {"code": 401, "msg": "expired"})]
        result = self.api._bff("POST", path + "?cloud=Huawei", json={"apps": ["etm"]})
        self.assertEqual(result["data"], ["offline-result"])
        self.assertEqual(len(self.transport.calls_to(path)), 2)

    def test_repeated_401_stops_after_one_recovery_and_marks_session_invalid(self):
        self.transport.responses["/api/v1/app"] = [(401, {}), (401, {})]
        with self.assertRaises(OpsApiError) as caught:
            self.api._bff("GET", "/api/v1/app")
        self.assertEqual(type(caught.exception).__name__, "OpsAuthenticationError")
        self.assertFalse(self.api.authenticated)
        self.assertEqual(len(self.transport.calls_to("/api/v1/app")), 2)
        self.assertEqual(self.transport.login_count, 2)

    def test_403_is_permission_failure_and_never_triggers_relogin(self):
        self.transport.responses["/api/v1/app"] = [(403, {"message": "secret-response"})]
        with self.assertRaises(OpsApiError) as caught:
            self.api._bff("GET", "/api/v1/app?token=secret-query")
        self.assertEqual(type(caught.exception).__name__, "OpsPermissionError")
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(self.api.authenticated)
        self.assertEqual(self.transport.login_count, 1)

    def test_server_error_has_sanitized_status_and_does_not_reauthenticate(self):
        self.transport.responses["/api/v1/app"] = [(500, {"message": "secret-response"})]
        with self.assertRaises(OpsApiError) as caught:
            self.api._bff("GET", "/api/v1/app?password=secret-query")
        self.assertIn("500", str(caught.exception))
        self.assertIn("GET /api/v1/app", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.transport.login_count, 1)

    def test_http_error_without_response_is_reported_without_secondary_exception(self):
        self.transport.responses["/api/v1/app"] = [requests.HTTPError("secret-error")]
        with self.assertRaises(OpsApiError) as caught:
            self.api._bff("GET", "/api/v1/app?token=secret-query")
        self.assertIn("HTTPError", str(caught.exception))
        self.assertIn("GET /api/v1/app", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(self.api.authenticated)
        self.assertEqual(self.transport.login_count, 1)

    def test_unknown_business_message_is_not_exposed_or_treated_as_expiration(self):
        self.transport.responses["/api/v1/app"] = [(200, {"code": 9001, "msg": "secret-token"})]
        with self.assertRaises(OpsApiError) as caught:
            self.api._bff("GET", "/api/v1/app")
        self.assertIn("9001", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(self.api.authenticated)
        self.assertEqual(self.transport.login_count, 1)

    def test_unknown_business_code_cannot_inject_secrets_into_error_message(self):
        self.transport.responses["/api/v1/app"] = [(200, {
            "code": "secret-business-code", "msg": "secret-token",
        })]
        with self.assertRaises(OpsApiError) as caught:
            self.api._bff("GET", "/api/v1/app")
        self.assertNotIn("secret", str(caught.exception))

    def test_cancellation_during_pre_submit_login_prevents_deployment_request(self):
        path = "/api/v1/deployment/build/huawei/test/etm/backend/deploy/etm"
        package = "etmbackend-5.3.0:5.3.0.1001.10400"
        self.api.resolve_target = Mock(return_value={
            "cloud": "Huawei", "api_area": "test", "appId": "etm",
            "typeId": "backend", "deploymentName": "etm",
        })
        self.api.verify_package = Mock(return_value=True)
        self.api._deployment_records = Mock(side_effect=[[], [{"imageName": package}]])
        cancelled = threading.Event()
        request = self.transport.request

        def cancel_during_login(method, url, **kwargs):
            self.assertNotIn("before_request", kwargs)
            response = request(method, url, **kwargs)
            if urlsplit(url).path == "/api/iam/v2/identity/login":
                cancelled.set()
            return response

        def before_submit():
            if cancelled.is_set():
                raise InterruptedError("cancelled before submission")

        self.transport.request = cancel_during_login
        self.clock.return_value = 2801
        with self.assertRaisesRegex(InterruptedError, "cancelled before submission"):
            self.api.deploy({"package": package}, before_submit=before_submit)
        self.assertEqual(self.transport.login_count, 2)
        self.assertEqual(self.transport.calls_to(path), [])

    def test_submission_timeout_remains_unknown_without_replay_or_second_callback(self):
        path = "/api/v1/deployment/build/huawei/test/etm/backend/deploy/etm"
        self.transport.responses[path] = [requests.Timeout("secret-network-error")]
        self.api.resolve_target = Mock(return_value={
            "cloud": "Huawei", "api_area": "test", "appId": "etm",
            "typeId": "backend", "deploymentName": "etm",
        })
        self.api.verify_package = Mock(return_value=True)
        self.api._deployment_records = Mock(return_value=[])
        checks = []
        with self.assertRaises(OpsSubmissionUnknown):
            self.api.deploy({"package": "etmbackend-5.3.0:5.3.0.1001.10400"},
                            before_submit=lambda: checks.append("checked"))
        self.assertEqual(len(self.transport.calls_to(path)), 1)
        self.assertEqual(checks, ["checked"])
        self.assertEqual(self.transport.login_count, 1)

    def test_pre_submit_maintenance_failure_keeps_original_error_without_posting(self):
        path = "/api/v1/deployment/build/huawei/test/etm/backend/deploy/etm"
        self.transport.responses["/api/iam/v2/identity/token/analyze"] = [requests.Timeout()]
        self.api.resolve_target = Mock(return_value={
            "cloud": "Huawei", "api_area": "test", "appId": "etm",
            "typeId": "backend", "deploymentName": "etm",
        })
        self.api.verify_package = Mock(return_value=True)
        self.api._deployment_records = Mock(return_value=[])
        checks = []
        self.clock.return_value = 1301
        with self.assertRaises(OpsApiError) as caught:
            self.api.deploy({"package": "etmbackend-5.3.0:5.3.0.1001.10400"},
                            before_submit=lambda: checks.append("checked"))
        self.assertNotIsInstance(caught.exception, OpsSubmissionUnknown)
        self.assertIn("Timeout", str(caught.exception))
        self.assertEqual(self.transport.calls_to(path), [])
        self.assertEqual(checks, [])
        self.assertTrue(self.api.authenticated)

    def test_pre_submit_callback_error_is_not_reported_as_a_submission(self):
        path = "/api/v1/deployment/build/huawei/test/etm/backend/deploy/etm"
        self.api.resolve_target = Mock(return_value={
            "cloud": "Huawei", "api_area": "test", "appId": "etm",
            "typeId": "backend", "deploymentName": "etm",
        })
        self.api.verify_package = Mock(return_value=True)
        self.api._deployment_records = Mock(return_value=[])
        original_error = OpsApiError("Submission precondition failed")

        def before_submit():
            raise original_error

        with self.assertRaises(OpsApiError) as caught:
            self.api.deploy({"package": "etmbackend-5.3.0:5.3.0.1001.10400"},
                            before_submit=before_submit)
        self.assertIs(caught.exception, original_error)
        self.assertEqual(self.transport.calls_to(path), [])

    def test_deployment_401_is_unknown_and_never_replayed(self):
        path = "/api/v1/deployment/build/huawei/test/etm/backend/deploy/etm"
        self.transport.responses[path] = [(401, {})]
        self.api.resolve_target = Mock(return_value={
            "cloud": "Huawei", "api_area": "test", "appId": "etm",
            "typeId": "backend", "deploymentName": "etm",
        })
        self.api.verify_package = Mock(return_value=True)
        self.api._deployment_records = Mock(return_value=[])
        with self.assertRaises(OpsSubmissionUnknown):
            self.api.deploy({"package": "etmbackend-5.3.0:5.3.0.1001.10400"})
        self.assertEqual(len(self.transport.calls_to(path)), 1)
        self.assertFalse(self.api.authenticated)
        self.assertEqual(self.transport.login_count, 1)

    def test_unknown_post_is_not_replayed_even_for_explicit_401(self):
        self.transport.responses["/api/v1/unknown"] = [(401, {})]
        with self.assertRaises(OpsApiError):
            self.api._bff("POST", "/api/v1/unknown", json={})
        self.assertEqual(len(self.transport.calls_to("/api/v1/unknown")), 1)
        self.assertFalse(self.api.authenticated)
        self.assertEqual(self.transport.login_count, 1)

    def test_transient_validation_failure_does_not_discard_a_valid_session(self):
        self.assertTrue(hasattr(self.api, "maintain_session"), "Session maintenance is missing")
        self.transport.responses["/api/iam/v2/identity/token/analyze"] = [requests.Timeout()]
        self.clock.return_value = 1301
        with self.assertRaises(OpsApiError):
            self.api.maintain_session()
        self.assertTrue(self.api.authenticated)
        self.assertEqual(self.transport.login_count, 1)
        self.assertTrue(self.api.maintenance_due())

    def test_periodic_login_network_failure_keeps_previous_valid_token(self):
        self.transport.responses["/api/iam/v2/identity/login"] = [requests.Timeout()]
        self.clock.return_value = 2801
        with self.assertRaises(OpsApiError):
            self.api.maintain_session()
        self.assertTrue(self.api.authenticated)
        self.assertEqual(self.api.headers["digi-middleware-auth-user"], "synthetic-user-token-1")
        self.assertTrue(self.api.maintenance_due())

    def test_login_failure_during_preflight_is_not_retried_or_sent_to_bff(self):
        analyze = "/api/iam/v2/identity/token/analyze"
        login = "/api/iam/v2/identity/login"
        self.transport.responses[analyze] = [(401, {})]
        self.transport.responses[login] = [(401, {})]
        self.clock.return_value = 1301
        with self.assertRaises(OpsApiError):
            self.api._bff("GET", "/api/v1/app")
        self.assertFalse(self.api.authenticated)
        self.assertEqual(len(self.transport.calls_to(login)), 2)
        self.assertEqual(self.transport.calls_to("/api/v1/app"), [])

    def test_failed_explicit_login_cannot_reuse_a_previous_identity(self):
        self.transport.responses["/assets/appinfo/modules-cn.json"] = [requests.Timeout()]
        with self.assertRaises(OpsApiError):
            self.api.login("https://other.example", "different-user", "different-password")
        before = len(self.transport.calls)
        with self.assertRaises(OpsApiError):
            self.api.maintain_session()
        self.assertFalse(self.api.authenticated)
        self.assertEqual(len(self.transport.calls), before)

    def test_header_only_reader_keeps_working_without_credentials_or_maintenance(self):
        reader = OpsApiClient(session=self.transport)
        reader.headers = dict(self.api.headers)
        reader.authenticated = True
        self.assertEqual(reader._bff("GET", "/api/v1/app")["data"], ["offline-result"])
        self.assertEqual(self.transport.login_count, 1)
        self.assertEqual(self.transport.calls_to("/api/iam/v2/identity/token/analyze"), [])

    def test_parallel_expired_reads_share_a_single_relogin(self):
        self.transport.responses["/api/v1/app"] = [(401, {})]
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: self.api._bff("GET", "/api/v1/app"), range(3)))
        self.assertEqual([result["data"] for result in results], [["offline-result"]] * 3)
        self.assertEqual(self.transport.login_count, 2)


if __name__ == "__main__":
    unittest.main()
