"""Synthetic fixtures for the published Ops runtime-image contract.

Evidence: https://cdm.digiwincloud.com.cn/4343-es2015.b6b359c152bb00ecc378.js
getK8sInstance sends the history imageName as image_name; getInstance displays
response.data.registry as runImageName. No authenticated response is saved here.
"""

import copy
import threading
import unittest
from unittest.mock import Mock

from ops_api import OpsApiClient, OpsApiError


SETTINGS = {
    "project": "ETM", "deploy_type": "etm 后端", "cloud": "Huawei", "region": "测试",
}
HISTORY_IMAGE = "etmbackend-5.3.0:5.3.0.1001.10400"
CURRENT_IMAGE = "registry.example:5000/industry/etmbackend-5.3.0:5.3.0.1001.10399"


class EnvironmentTraceApiTests(unittest.TestCase):
    def setUp(self):
        self.api = OpsApiClient()
        self.api.authenticated = True
        self.groups = [{"appId": "etm", "typeId": "backend", "deploymentName": "etm"}]
        self.records = [{
            "id": 111, "appId": "etm", "typeId": "backend", "deploymentName": "etm",
            "cloud": "Huawei", "area": "test", "enablePreEnv": True,
            "deleted_prod": "0", "deleted_pre": "0", "registryId": "industry",
        }]
        self.history = [{"imageName": HISTORY_IMAGE, "status": "pending"}]
        self.instance = {"registry": CURRENT_IMAGE, "instance_data": [], "envs": []}
        self.calls = []
        self.api._bff = Mock(side_effect=self.response)

    def response(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/api/v1/deployment/data/app/group/multi?cloud=Huawei":
            self.assertEqual(method, "POST")
            self.assertEqual(kwargs["json"], {"apps": ["etm"]})
            return {"data": copy.deepcopy(self.groups)}
        self.assertEqual(method, "GET", "Environment lookup must never submit a deployment")
        if path == "/api/v1/deployment/data":
            self.assertEqual(kwargs["params"], {
                "cloud": "Huawei", "app": "etm", "type": "backend",
                "deployment_name": "etm", "deleted_prod": 0,
            })
            return {"data": {"data": copy.deepcopy(self.records)}}
        if path == "/api/v3/deployment/record":
            return {"data": {"data": copy.deepcopy(self.history)}}
        if path == "/api/v3/deployment/instance/data":
            return {"data": copy.deepcopy(self.instance)}
        self.fail(f"Unexpected request: {method} {path}")

    def load(self, **settings):
        return self.api.load_current_packages({**SETTINGS, **settings})

    def test_running_registry_wins_over_pending_history_and_cached_target(self):
        self.api._targets[("ETM", "backend", "huawei", "测试")] = {
            **self.records[0], "id": 999, "imageName": HISTORY_IMAGE,
        }
        result = self.load()

        self.assertEqual(result["packages"], [CURRENT_IMAGE])
        self.assertEqual(result["target"]["id"], 111)
        self.assertEqual(result["target"]["state"], "prod")
        self.assertEqual(result["source"], "/api/v3/deployment/instance/data")
        self.assertEqual(self.calls[-1][2]["params"], {
            "cloud": "Huawei", "area": "test", "app_id": "etm", "type": "backend",
            "deployment_name": "etm", "state": "prod", "image_name": HISTORY_IMAGE,
        })

    def test_pre_environment_queries_its_state_without_using_prod_history(self):
        result = self.load(region="预测试")

        self.assertEqual(result["packages"], [CURRENT_IMAGE])
        self.assertEqual(result["target"]["state"], "pre")
        self.assertEqual(result["target"]["api_area"], "test-pre")
        self.assertEqual(self.calls[-2][2]["params"], {
            "data_id": 111, "cloud": "Huawei", "state": "pre",
        })
        self.assertEqual(self.calls[-1][2]["params"]["area"], "test")
        self.assertEqual(self.calls[-1][2]["params"]["state"], "pre")

    def test_each_lookup_reads_fresh_target_and_running_image(self):
        self.assertEqual(self.load()["packages"], [CURRENT_IMAGE])
        self.instance["registry"] = CURRENT_IMAGE.replace("10399", "10401")
        self.records[0]["id"] = 112

        result = self.load()

        self.assertEqual(result["packages"], [
            "registry.example:5000/industry/etmbackend-5.3.0:5.3.0.1001.10401"
        ])
        self.assertEqual(result["target"]["id"], 112)
        self.assertEqual(sum(path == "/api/v1/deployment/data" for _, path, _ in self.calls), 2)

    def test_missing_or_wrong_product_type_cloud_and_region_cannot_select_a_target(self):
        for change in ({"appId": "esc"}, {"typeId": "frontend"},
                       {"cloud": "Aliyun"}, {"area": "prod"}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.records)
                self.records[0].update(change)
                with self.assertRaisesRegex(OpsApiError, "未找到精确部署目标"):
                    self.load()
                self.records = original
        self.assertFalse(any("instance" in path or path.endswith("record")
                             for _, path, _ in self.calls))

    def test_two_targets_in_same_region_fail_instead_of_silently_deduplicating(self):
        self.records.append({**self.records[0], "id": 222})

        with self.assertRaisesRegex(OpsApiError, "多个|不唯一"):
            self.load()

        self.assertFalse(any("instance" in path for _, path, _ in self.calls))

    def test_deleted_or_disabled_pre_environment_is_not_selected(self):
        for change in ({"deleted_pre": "1"}, {"enablePreEnv": False}):
            with self.subTest(change=change):
                self.records[0].update({"deleted_pre": "0", "enablePreEnv": True, **change})
                with self.assertRaisesRegex(OpsApiError, "未找到精确部署目标"):
                    self.load(region="预测试")

    def test_current_image_requires_a_complete_reference_without_history_fallback(self):
        for value in (None, "", "5.3.0.1001.10399", "image:tag extra", "https://host/image:tag",
                      "image:tag\nother:tag", [CURRENT_IMAGE], {"image": CURRENT_IMAGE}):
            with self.subTest(value=value):
                self.instance["registry"] = value
                with self.assertRaisesRegex(OpsApiError, "当前运行镜像|完整镜像"):
                    self.load()

    def test_runtime_query_failure_does_not_fall_back_to_history(self):
        original = self.response

        def fail_instance(method, path, **kwargs):
            if path == "/api/v3/deployment/instance/data":
                raise OpsApiError("实例查询失败")
            return original(method, path, **kwargs)

        self.api._bff.side_effect = fail_instance
        with self.assertRaisesRegex(OpsApiError, "实例查询失败"):
            self.load()

    def test_missing_history_query_hint_does_not_guess_repository(self):
        for history in ([], [{"imageName": "5.3.0.1001.10400"}], [{"imageName": None}]):
            with self.subTest(history=history):
                self.history = history
                with self.assertRaisesRegex(OpsApiError, "查询.*镜像|镜像.*查询"):
                    self.load()

    def test_missing_settings_fail_before_network_access(self):
        for name in SETTINGS:
            with self.subTest(name=name):
                with self.assertRaises(OpsApiError):
                    self.load(**{name: ""})
        self.assertEqual(self.calls, [])

    def test_cancelled_lookup_makes_no_request(self):
        cancelled = threading.Event()
        cancelled.set()

        with self.assertRaisesRegex(InterruptedError, "环境查询已取消"):
            self.api.load_current_packages(SETTINGS, cancel_event=cancelled)

        self.assertEqual(self.calls, [])

    def test_cancellation_after_each_response_stops_the_remaining_requests(self):
        paths = [
            "/api/v1/deployment/data/app/group/multi?cloud=Huawei",
            "/api/v1/deployment/data",
            "/api/v3/deployment/record",
            "/api/v3/deployment/instance/data",
        ]
        for index, cancel_after in enumerate(paths):
            with self.subTest(cancel_after=cancel_after):
                cancelled = threading.Event()
                self.calls.clear()

                def respond_and_cancel(method, path, **kwargs):
                    result = self.response(method, path, **kwargs)
                    if path == cancel_after:
                        cancelled.set()
                    return result

                self.api._bff.side_effect = respond_and_cancel
                with self.assertRaisesRegex(InterruptedError, "环境查询已取消"):
                    self.api.load_current_packages(SETTINGS, cancel_event=cancelled)
                self.assertEqual([path for _, path, _ in self.calls], paths[:index + 1])

    def test_unset_cancellation_event_preserves_the_result_contract(self):
        result = self.api.load_current_packages(SETTINGS, cancel_event=threading.Event())

        self.assertEqual(result, {
            "packages": [CURRENT_IMAGE],
            "target": {
                "id": 111, "appId": "etm", "typeId": "backend", "deploymentName": "etm",
                "cloud": "Huawei", "area": "test", "region": "测试",
                "region_label": "Huawei / 测试", "api_area": "test", "state": "prod",
            },
            "source": "/api/v3/deployment/instance/data",
        })


if __name__ == "__main__":
    unittest.main()
