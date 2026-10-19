import base64
import copy
import json
import threading
import time
from urllib.parse import quote, urlsplit

import requests
from core import infer_cloud_deployment_config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


IAM_BASE_URL = "https://iam.digiwincloud.com.cn"
BFF_BASE_URL = "https://bff-service.digiwincloud.com.cn"
LOGIN_IV = b"ghUb#er57HBh(u%g"
AREA_LABELS = {
    "test": "测试",
    "prod": "正式",
    "pressure": "压力测试",
    "paas": "PaaS",
}


class OpsApiError(RuntimeError):
    pass


class OpsSubmissionUnknown(OpsApiError):
    pass


def service_type_id(value):
    normalized = "".join(str(value or "").lower().split())
    if normalized.endswith("后端") or normalized.endswith("backend"):
        return "backend"
    if normalized.endswith("前端") or normalized.endswith("frontend"):
        return "frontend"
    return normalized


def service_type_label(project, type_id):
    suffix = {"backend": "后端", "frontend": "前端"}.get(type_id, type_id)
    return f"{str(project).lower()} {suffix}".strip()


def target_regions(records, cloud):
    """Convert Ops deployment units into stable UI labels and API targets."""
    result = []
    seen = set()
    for record in records:
        area = str(record.get("area", "")).strip()
        if not area:
            continue
        base_label = AREA_LABELS.get(area.lower(), area)
        variants = [(base_label, area, "prod")]
        if record.get("enablePreEnv"):
            variants.append((f"预{base_label}", f"{area}-pre", "pre"))
        for region, api_area, state in variants:
            key = (cloud, region, record.get("appId"), record.get("typeId"))
            if key in seen:
                continue
            seen.add(key)
            item = dict(record)
            item.update(
                {
                    "cloud": cloud,
                    "region": region,
                    "region_label": f"{cloud} / {region}",
                    "api_area": api_area,
                    "state": state,
                }
            )
            result.append(item)
    return result


class OpsApiClient:
    """HTTP client that mirrors the Ops web application's authenticated API calls."""

    def __init__(self, timeout=30, session=None):
        self.timeout = timeout
        self.session = session or requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.4,
            status_forcelist=(502, 503, 504),
            # POST may create a deployment. Retrying it after a gateway timeout
            # can create duplicate releases, so only idempotent reads may retry.
            allowed_methods=frozenset(("GET",)),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.headers = {"Client-Agent": "web"}
        self.user = {}
        self.authenticated = False
        self._targets = {}
        self._projects_cache = {}
        self._options_cache = {}
        self._options_lock = threading.RLock()

    @staticmethod
    def _public_key_pem(value):
        value = str(value or "").strip()
        if "BEGIN PUBLIC KEY" in value:
            return value.encode()
        return f"-----BEGIN PUBLIC KEY-----\n{value}\n-----END PUBLIC KEY-----".encode()

    def _json_request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("headers", self.headers)
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise OpsApiError(f"Ops API 请求失败：{type(exc).__name__}") from exc
        except ValueError as exc:
            raise OpsApiError("Ops API 返回了无法解析的数据") from exc
        if isinstance(payload, dict) and payload.get("code") not in (
            None, 0, 200, 10000, "0", "200", "10000"
        ):
            raise OpsApiError(str(payload.get("msg") or payload.get("message") or "Ops API 返回失败"))
        return payload

    @staticmethod
    def _data(payload):
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def login(self, platform_url, username, password):
        self.authenticated = False
        self._projects_cache.clear()
        self._options_cache.clear()
        self._targets.clear()
        origin = f"{urlsplit(platform_url).scheme}://{urlsplit(platform_url).netloc}"
        app_info = self._json_request(
            "GET", f"{origin}/assets/appinfo/modules-cn.json", headers={"Client-Agent": "web"}
        )
        app_token = app_info.get("appToken", "")
        if not app_token:
            raise OpsApiError("Ops 页面未返回应用认证信息")
        self.headers = {
            "Client-Agent": "web",
            "digi-middleware-auth-app": app_token,
            "digi-middleware-auth-user": "",
        }

        server_payload = self._json_request(
            "GET", f"{IAM_BASE_URL}/api/iam/v2/identity/publickey"
        )
        server_key = serialization.load_pem_public_key(
            self._public_key_pem(server_payload.get("publicKey"))
        )
        client_private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        client_pem = client_private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        client_public = client_pem.replace("-----BEGIN PUBLIC KEY-----\n", "").replace(
            "\n-----END PUBLIC KEY-----\n", ""
        )
        encrypted_public = base64.b64encode(
            server_key.encrypt(client_public.encode(), padding.PKCS1v15())
        ).decode()
        aes_payload = self._json_request(
            "POST",
            f"{IAM_BASE_URL}/api/iam/v2/identity/aeskey",
            json={"clientEncryptPublicKey": encrypted_public},
        )
        aes_key = client_private.decrypt(
            base64.b64decode(aes_payload["encryptAesKey"]), padding.PKCS1v15()
        )
        padder = PKCS7(128).padder()
        padded_password = padder.update(password.encode()) + padder.finalize()
        encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(LOGIN_IV)).encryptor()
        password_hash = base64.b64encode(
            encryptor.update(padded_password) + encryptor.finalize()
        ).decode()
        login_payload = self._json_request(
            "POST",
            f"{IAM_BASE_URL}/api/iam/v2/identity/login",
            json={
                "tenantId": "",
                "userId": username,
                "identityType": "service",
                "passwordHash": password_hash,
                "clientEncryptPublicKey": encrypted_public,
            },
        )
        token = login_payload.get("token", "")
        if not token:
            raise OpsApiError("Ops 登录成功响应中没有 Token")
        self.user = login_payload
        self.headers["digi-middleware-auth-user"] = token
        self.authenticated = True
        return True

    def _bff(self, method, path, **kwargs):
        if not self.authenticated:
            raise OpsApiError("Ops API 尚未登录")
        return self._json_request(method, BFF_BASE_URL + path, **kwargs)

    def list_projects(self, cloud, *, refresh=False):
        with self._options_lock:
            key = str(cloud).strip().lower()
            if refresh or key not in self._projects_cache:
                self._projects_cache[key] = self._fetch_projects(cloud)
            return list(self._projects_cache[key])

    def _fetch_projects(self, cloud):
        payload = self._data(
            self._bff(
                "GET",
                "/api/v1/app",
                params={"user_id": self.user.get("jobCode", ""), "cloud": cloud},
            )
        )
        rows = payload.get("data", []) if isinstance(payload, dict) else payload or []
        names = []
        for row in rows:
            name = str(row.get("id") or row.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    def load_target_options(self, project, cloud, *, refresh=False):
        with self._options_lock:
            key = (str(project).strip().upper(), str(cloud).strip().lower())
            if refresh or key not in self._options_cache:
                options = self._fetch_target_options(project, cloud)
                # Replace only after every query succeeds, including removed targets.
                self._targets = {
                    target_key: value for target_key, value in self._targets.items()
                    if (target_key[0], target_key[2]) != key
                }
                for target in options["targets"]:
                    self._targets[(key[0], str(target["typeId"]), key[1], target["region"])] = target
                self._options_cache[key] = options
            return copy.deepcopy(self._options_cache[key])

    def _fetch_target_options(self, project, cloud):
        app = str(project).strip().lower()
        groups = self._data(
            self._bff(
                "POST",
                f"/api/v1/deployment/data/app/group/multi?cloud={cloud}",
                json={"apps": [app]},
                timeout=60,
            )
        ) or []
        groups = [row for row in groups if str(row.get("appId", "")).lower() == app]
        types = []
        targets = []
        queried = set()
        for group in groups:
            type_id = str(group.get("typeId", "")).strip()
            deployment_name = str(group.get("deploymentName", "")).strip()
            if not type_id or not deployment_name:
                continue
            label = service_type_label(project, type_id)
            if label not in types:
                types.append(label)
            query_key = (type_id, deployment_name)
            if query_key in queried:
                continue
            queried.add(query_key)
            payload = self._data(
                self._bff(
                    "GET",
                    "/api/v1/deployment/data",
                    params={
                        "cloud": cloud,
                        "app": app,
                        "type": type_id,
                        "deployment_name": deployment_name,
                        "deleted_prod": 0,
                    },
                    timeout=60,
                )
            )
            records = payload.get("data", []) if isinstance(payload, dict) else payload or []
            for target in target_regions(records, cloud):
                target["project"] = project
                target["deploy_type_label"] = label
                targets.append(target)
        regions = list(dict.fromkeys(target["region_label"] for target in targets))
        return {"types": types, "regions": regions, "targets": targets}

    def load_package_target_options(self, package, cloud):
        """Resolve a package to actual Ops project/type options.

        The caller owns the current region selection: this method verifies the
        inferred project and service type but deliberately does not choose or
        change a cloud region.
        """
        inferred = infer_cloud_deployment_config(package)
        projects = self.list_projects(cloud)
        project = next(
            (
                value
                for value in projects
                if str(value).strip().lower() == inferred["project"].lower()
            ),
            "",
        )
        if not project:
            raise OpsApiError(
                f'OPS 中没有找到包号对应的部署专案“{inferred["project"]}”'
            )

        options = self.load_target_options(project, cloud)
        deploy_type = next(
            (
                value
                for value in options["types"]
                if service_type_id(value) == inferred["type_id"]
            ),
            "",
        )
        if not deploy_type:
            raise OpsApiError(
                f'部署专案“{project}”中没有找到包号对应的服务类型“{inferred["deploy_type"]}”'
            )
        return {
            **options,
            "projects": projects,
            "project": project,
            "deploy_type": deploy_type,
            "inferred": inferred,
        }

    def resolve_target(self, settings):
        type_id = service_type_id(settings.get("deploy_type"))
        key = (
            str(settings.get("project", "")).upper(),
            type_id,
            str(settings.get("cloud", "")).lower(),
            str(settings.get("region", "")),
        )
        target = self._targets.get(key)
        if target is None:
            self.load_target_options(settings.get("project", ""), settings.get("cloud", ""))
            target = self._targets.get(key)
        if target is None:
            raise OpsApiError(
                f'未找到精确部署目标：{settings.get("project")} / '
                f'{settings.get("deploy_type")} / {settings.get("cloud")} / '
                f'{settings.get("region")}'
            )
        return target

    def verify_package(self, target, package):
        image_name, separator, tag = str(package).partition(":")
        if not separator or not image_name or not tag:
            raise OpsApiError("部署包号格式无效")
        registry = str(target.get("registryId") or target.get("registryName") or "")
        repositories = self._data(
            self._bff(
                "GET",
                f'/api/v2/deployment/registry/{quote(str(target["cloud"]), safe="")}/repository',
                params={
                    "deploymentName": target.get("deploymentName", ""),
                    "projectName": registry,
                    "type": target.get("typeId", ""),
                    "realtime": "true",
                },
                timeout=60,
            )
        ) or []
        expected_repository = f"{registry}/{image_name}"
        if expected_repository not in repositories:
            raise OpsApiError(f"Ops 镜像仓库中未找到 {expected_repository}")
        tags = self._data(
            self._bff(
                "GET",
                f'/api/v2/deployment/registry/{quote(str(target["cloud"]), safe="")}/repository/'
                f'{quote(registry, safe="")}/{quote(image_name, safe="")}/tag',
                params={"start_version": tag, "end_version": "", "realtime": "true"},
                timeout=60,
            )
        ) or []
        if not any(str(item.get("name", "")) == tag for item in tags):
            raise OpsApiError(f"Ops 镜像仓库中未找到精确版本 {package}")
        return True

    def _deployment_records(self, target):
        records = self._data(
            self._bff(
                "GET",
                "/api/v1/deployment/record",
                params={
                    "data_id": target.get("id"),
                    "page_num": 1,
                    "page_size": 20,
                    "state": target.get("state"),
                    "cloud": target.get("cloud"),
                },
            )
        )
        return records.get("data", []) if isinstance(records, dict) else records or []

    @staticmethod
    def _record_fingerprint(record):
        # The Ops response differs slightly between environments. Serializing the
        # full row detects both a new record and an in-place status/time update.
        return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)

    def deploy(self, settings):
        target = self.resolve_target(settings)
        package = settings.get("package", "")
        self.verify_package(target, package)
        type_id = str(target.get("typeId", ""))
        deployment_name = str(target.get("deploymentName", ""))
        deploy_name = deployment_name if type_id == "general" else deployment_name + type_id
        old_records = {
            self._record_fingerprint(row)
            for row in self._deployment_records(target)
            if str(row.get("imageName", "")) == package
        }
        path = (
            f'/api/v1/deployment/build/{quote(str(target["cloud"]).lower(), safe="")}/'
            f'{quote(str(target["api_area"]), safe="")}/'
            f'{quote(str(target.get("appId", "")).lower(), safe="")}/'
            f'{quote(type_id, safe="")}/deploy/{quote(deployment_name, safe="")}'
        )
        body = {
            "images": package,
            "userId": self.user.get("userId", ""),
            "userName": self.user.get("userName", ""),
            "name": deploy_name,
        }
        try:
            response = self._bff("POST", path, json=body, timeout=60)
        except OpsApiError as exc:
            raise OpsSubmissionUnknown(
                "部署提交响应不明确，请先到 Ops 平台检查部署记录，禁止直接重试"
            ) from exc

        # Verify acceptance by querying the same deployment unit. This check is
        # read-only and prevents the client from reporting success on a lost click.
        for _ in range(6):
            try:
                rows = self._deployment_records(target)
            except OpsApiError as exc:
                raise OpsSubmissionUnknown(
                    "部署已提交，但无法查询部署记录，请先到 Ops 平台核对，禁止直接重试"
                ) from exc
            if any(
                str(row.get("imageName", "")) == package
                and self._record_fingerprint(row) not in old_records
                for row in rows
            ):
                return {"response": response, "verified": True, "target": target}
            time.sleep(2)
        raise OpsSubmissionUnknown(
            "部署接口已响应，但记录中暂未找到目标包，请到 Ops 平台确认后再操作"
        )
