import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core import infer_cloud_deployment_config, infer_ssh_deployment_config
from main import DeployTool
from ops_api import OpsApiClient, OpsApiError


class PackageInferenceTests(unittest.TestCase):
    def test_cloud_examples(self):
        cases = {
            "etmbackend-5.3.0:5.3.0.1001.10581": ("etm", "backend", "etm 后端"),
            "etmfrontend-5.2.0:4.0.2.3430": ("etm", "frontend", "etm 前端"),
            "etmmobilefrontend-2.1.0:4.0.0.1000.12136": (
                "etmmobile", "frontend", "etmmobile 前端"
            ),
        }
        for package, expected in cases.items():
            with self.subTest(package=package):
                inferred = infer_cloud_deployment_config(package)
                self.assertEqual(
                    (inferred["project"], inferred["type_id"], inferred["deploy_type"]),
                    expected,
                )

    def test_mobile_and_mobilefrontend_share_deployment_targets(self):
        for product in ('pqm', 'etm'):
            for marker in ('mobile', 'mobilefrontend'):
                with self.subTest(product=product, marker=marker):
                    package = f'{product}{marker}-hbxx-2.1.0:4.0.0.1000.10001'
                    ssh = infer_ssh_deployment_config(package)
                    self.assertEqual(ssh['product'], product)
                    self.assertEqual(ssh['service_type'], 'mobile')
                    self.assertEqual(ssh['yaml_dir'], '/data/yaml/app/mobile')
                    self.assertEqual(ssh['yaml_file'], f'{product}mobile.yaml')
                    self.assertEqual(ssh['apply_command'], f'kubectl apply -f {product}mobile.yaml')
                    cloud = infer_cloud_deployment_config(package)
                    self.assertEqual((cloud['project'], cloud['type_id']), (product + 'mobile', 'frontend'))

    def test_mobile_alias_requires_a_service_name_boundary(self):
        for package in ('pqmmobilehelper:1', 'pqmmobile123:1'):
            with self.subTest(package=package), self.assertRaises(ValueError):
                infer_ssh_deployment_config(package)
        self.assertEqual(infer_ssh_deployment_config('pqmmobile:1')['yaml_file'], 'pqmmobile.yaml')

    def test_mobile_yaml_update_preserves_other_services_and_products(self):
        from core import update_yaml_image, yaml_image_reference
        package = 'pqmmobile-hbxx-2.1.0:4.0.0.1000.10001'
        other_images = 'image: registry.example/pqmfrontend:old\nimage: registry.example/etmmobile:old\n'
        for old_name in ('pqmmobile', 'pqmmobilefrontend'):
            with self.subTest(old_name=old_name):
                content = f'image: registry.example/{old_name}-other:old\n' + other_images
                expected = 'image: registry.example/' + package + '\n' + other_images
                self.assertEqual(update_yaml_image(content, package), expected)
                self.assertEqual(yaml_image_reference(content, package), 'registry.example/' + package)

    def test_ops_values_are_used_instead_of_unchecked_defaults(self):
        client = OpsApiClient()
        client.list_projects = Mock(return_value=["ESC", "ETM"])
        client.load_target_options = Mock(
            return_value={
                "types": ["etm 前端", "etm 后端"],
                "regions": ["Huawei / 测试"],
                "targets": [{"id": "target-1"}],
            }
        )

        result = client.load_package_target_options(
            "etmbackend-5.3.0:5.3.0.1001.10581", "Huawei"
        )

        self.assertEqual(result["project"], "ETM")
        self.assertEqual(result["deploy_type"], "etm 后端")
        client.load_target_options.assert_called_once_with("ETM", "Huawei")

    def test_mobile_project_must_exist_in_ops(self):
        client = OpsApiClient()
        client.list_projects = Mock(return_value=["ETM"])
        with self.assertRaisesRegex(OpsApiError, "etmmobile"):
            client.load_package_target_options(
                "etmmobilefrontend-2.1.0:4.0.0.1000.12136", "Huawei"
            )

    def test_enter_applies_ssh_fields_and_starts_cloud_lookup(self):
        package = "etmmobilefrontend-2.1.0:4.0.0.1000.12136"
        store = SimpleNamespace(data={}, save=Mock())
        tool = SimpleNamespace(
            package_input=Mock(text=Mock(return_value=package)),
            cloud_package=Mock(),
            message_overlay=SimpleNamespace(show_message=Mock()),
            _set_env_var=Mock(),
            env_dirty=False,
            deploy_log=Mock(),
            _cloud_settings_from_ui=Mock(
                return_value={
                    "url": "https://ops.example",
                    "username": "user",
                    "password_plain": "secret",
                    "cloud": "Huawei",
                }
            ),
            store=store,
            cloud_status_label=Mock(),
            _set_cloud_target_loading=Mock(),
            run_thread=Mock(),
            _load_cloud_package_target_options=Mock(),
            _queue_cloud_options=Mock(),
        )

        DeployTool.apply_deployment_targets_from_package(tool)

        values = {call.args[0]: call.args[1] for call in tool._set_env_var.call_args_list}
        self.assertEqual(values["yaml_dir"], "/data/yaml/app/mobile")
        self.assertEqual(values["yaml_file"], "etmmobile.yaml")
        self.assertEqual(values["apply_command"], "kubectl apply -f etmmobile.yaml")
        self.assertTrue(tool.env_dirty)
        tool._queue_cloud_options.assert_called_once()
        self.assertEqual(tool._queue_cloud_options.call_args.kwargs["package"], package)
        tool.cloud_package.setText.assert_called_once_with(package)
        self.assertEqual(store.data["last_package"], package)

    def test_ssh_variant_suffix_does_not_change_product(self):
        inferred = infer_ssh_deployment_config(
            "pqmbackend-bmgd-5.2.0:5.2.0.1001.10006"
        )
        self.assertEqual(inferred["yaml_file"], "pqm.yaml")


if __name__ == "__main__":
    unittest.main()
