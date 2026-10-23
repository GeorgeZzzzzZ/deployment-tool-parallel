import re
import uuid
from urllib.parse import quote, urlencode, urlsplit, urlunsplit


IMAGE_REF_RE = re.compile(
    r"(?:(?:[\w.-]+(?::\d+)?)/)*(?P<name>[\w.-]+):(?P<tag>[\w.-]+)"
)
JOB_PROJECT_CODE_RE = re.compile(
    r"^\s*\d+\s*-\s*(?P<code>[A-Za-z][A-Za-z0-9_]*)\s*(?:-|$)"
)
GENERIC_JOB_CODES = {"WEB", "APP", "SERVICE"}


def dashboard_pods_url(url, namespace="etm"):
    """Return the Kubernetes Dashboard pod-list route for one namespace."""
    url = str(url or "").strip()
    if not url:
        return ""
    namespace = str(namespace or "").strip() or "etm"
    parts = urlsplit(url)
    path = parts.path or "/"
    fragment = "/pod?" + urlencode({"namespace": namespace})
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, fragment))


def extract_job_project_code(path):
    """Extract the system code from a numbered Jenkins display path folder."""
    if isinstance(path, (list, tuple)):
        parts = path
    else:
        parts = str(path or "").replace("\\", "/").split("/")
    for part in parts:
        match = JOB_PROJECT_CODE_RE.match(str(part))
        if match:
            code = match.group("code").upper()
            if code not in GENERIC_JOB_CODES:
                return code
    return ""


def cloud_project_matches_job(path, project):
    """Require the Ops deployment project to match the selected Jenkins system."""
    job_project = extract_job_project_code(path)
    project = str(project or "").strip().upper()
    parts = path if isinstance(path, (list, tuple)) else str(path or "").replace("\\", "/").split("/")
    app_job = bool(parts and re.match(r"^\s*\d+\s*-\s*APP(?:\W|$)", str(parts[-1]), re.I))
    return bool(job_project and (
        project == job_project or (app_job and project == f"{job_project}MOBILE")
    ))


def ensure_dashboard_bindings(config, id_factory=None):
    """Add stable Dashboard IDs and migrate legacy name-based environment links."""
    id_factory = id_factory or (lambda: uuid.uuid4().hex)
    dashboards = config.setdefault("dashboards", [])
    environments = config.setdefault("environments", [])
    changed = False
    used_ids = set()

    for dashboard in dashboards:
        dashboard_id = str(dashboard.get("id", "")).strip()
        if not dashboard_id or dashboard_id in used_ids:
            dashboard_id = id_factory()
            dashboard["id"] = dashboard_id
            changed = True
        used_ids.add(dashboard_id)

    dashboards_by_id = {item["id"]: item for item in dashboards}
    dashboards_by_name = {
        item.get("name", ""): item for item in dashboards if item.get("name")
    }
    for environment in environments:
        dashboard = dashboards_by_id.get(environment.get("dashboard_id", ""))
        if dashboard is None:
            dashboard = dashboards_by_name.get(environment.get("dashboard", ""))
        if dashboard is None:
            continue
        if environment.get("dashboard_id") != dashboard["id"]:
            environment["dashboard_id"] = dashboard["id"]
            changed = True
        if environment.get("dashboard") != dashboard.get("name", ""):
            environment["dashboard"] = dashboard.get("name", "")
            changed = True
    return changed


def rename_bound_dashboard(config, dashboard_id, new_name):
    """Rename a Dashboard and update every environment reference bound to its ID."""
    dashboards = config.get("dashboards", [])
    dashboard = next(
        (item for item in dashboards if item.get("id") == dashboard_id), None
    )
    if dashboard is None:
        raise ValueError("关联的 Dashboard 不存在")
    if any(
        item is not dashboard and item.get("name") == new_name
        for item in dashboards
    ):
        raise ValueError(f'Dashboard“{new_name}”已存在')

    old_name = dashboard.get("name", "")
    dashboard["name"] = new_name
    for environment in config.get("environments", []):
        if (
            environment.get("dashboard_id") == dashboard_id
            or (
                not environment.get("dashboard_id")
                and environment.get("dashboard") == old_name
            )
        ):
            environment["dashboard_id"] = dashboard_id
            environment["dashboard"] = new_name
    return dashboard


def is_secondary_build_path(path):
    if isinstance(path, (list, tuple)):
        parts = path
    else:
        parts = str(path).replace("\\", "/").split("/")
    return any(part == "custom-dev" or "二开打包" in part for part in parts)


def jenkins_job_api_url(base_url, path):
    url = base_url.rstrip("/")
    for part in path:
        url += "/job/" + quote(part, safe="")
    return url


def reorder_parameters(parameters, path):
    result = list(parameters)
    if not is_secondary_build_path(path):
        return result
    names = [item.get("name") for item in result]
    if "GIT_BRANCH" not in names or "GIT_BRANCH2" not in names:
        return result
    branch2 = names.index("GIT_BRANCH2")
    item = result.pop(branch2)
    branch = next(i for i, value in enumerate(result) if value.get("name") == "GIT_BRANCH")
    result.insert(branch, item)
    return result


def extract_package_number(text):
    preferred = []
    for line in (text or "").splitlines():
        if "naming to" in line.lower():
            preferred.extend(IMAGE_REF_RE.finditer(line))
    if preferred:
        return preferred[-1].group(0).rsplit("/", 1)[-1]
    matches = list(IMAGE_REF_RE.finditer(text or ""))
    if not matches:
        return ""
    value = matches[-1].group(0)
    return value.rsplit("/", 1)[-1]


def split_package_number(package):
    package = package.strip().strip("[]")
    match = IMAGE_REF_RE.fullmatch(package)
    if not match:
        raise ValueError("包号格式应类似 etmbackend-yisen-5.2.0:5.2.0.1001.10345")
    return match.group("name"), match.group("tag")


def infer_package_target(package):
    """Return the product and service type encoded in an image package."""
    image_name, _ = split_package_number(package)
    normalized_name = image_name.lower()
    variants = (
        ("mobilefrontend", "mobile", "移动端"),
        ("backend", "backend", "后端"),
        ("frontend", "frontend", "前端"),
    )
    for marker, directory_name, service_label in variants:
        marker_index = normalized_name.find(marker)
        if marker_index <= 0:
            continue
        product = normalized_name[:marker_index].rstrip("-_.")
        if not re.fullmatch(r"[a-z][a-z0-9]*", product):
            break
        return {
            "product": product,
            "service_type": directory_name,
            "service_label": service_label,
        }
    raise ValueError(
        "无法从包号识别产品和服务类型；镜像名需包含 backend、frontend 或 mobilefrontend"
    )


def parse_deployment_packages(text):
    """Validate the full batch before allowing any deployment side effect."""
    packages = []
    applications = {}
    for line_number, line in enumerate(str(text or "").splitlines(), 1):
        package = line.strip().strip("[]")
        if not package:
            continue
        try:
            target = infer_package_target(package)
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 行：{exc}") from exc
        marker = "mobilefrontend" if target["service_type"] == "mobile" else target["service_type"]
        application = target["product"] + marker
        if application in applications:
            raise ValueError(
                f"应用 {application} 重复（第 {applications[application]}、{line_number} 行）；"
                "同一应用只能部署一个包，客户名和版本号不同也不能重复。"
            )
        applications[application] = line_number
        packages.append(package)
    if not packages:
        raise ValueError("请至少输入一个包号，每行一个")
    return packages


def infer_ssh_deployment_config(package):
    """Infer the SSH YAML target from a deployment image package number."""
    target = infer_package_target(package)
    product = target["product"]
    directory_name = target["service_type"]
    is_mobile = directory_name == "mobile"
    yaml_stem = f"{product}mobile" if is_mobile else product
    yaml_file = f"{yaml_stem}.yaml"
    return {
        **target,
        "yaml_dir": f"/data/yaml/app/{directory_name}",
        "yaml_file": yaml_file,
        "apply_command": f"kubectl apply -f {yaml_file}",
    }


def resolve_ssh_environment_for_package(environment, package):
    """Return a frozen SSH environment whose YAML target matches the package."""
    resolved = dict(environment or {})
    inferred = infer_ssh_deployment_config(package)
    for key in ("yaml_dir", "yaml_file", "apply_command"):
        resolved[key] = inferred[key]
    return resolved


def infer_cloud_deployment_config(package):
    """Infer the expected Ops project and service type from a package number."""
    target = infer_package_target(package)
    product = target["product"]
    if target["service_type"] == "mobile":
        project = f"{product}mobile"
        type_id = "frontend"
    else:
        project = product
        type_id = target["service_type"]
    service_label = "前端" if type_id == "frontend" else "后端"
    return {
        **target,
        "project": project,
        "type_id": type_id,
        "deploy_type": f"{project} {service_label}",
    }


def update_image_list(content, package, clear_existing=False):
    """Update a pull manifest, optionally replacing it with one current image."""
    name, _ = split_package_number(package)
    if clear_existing:
        # Windows SSH deployment pulls one Jenkins artifact at a time. Keeping
        # historical entries makes the remote pull script process every old
        # image again, so its manifest must contain only the current package.
        return package + "\n"

    lines = content.splitlines()
    changed = False
    for index, line in enumerate(lines):
        match = IMAGE_REF_RE.search(line)
        if match and match.group("name") == name:
            prefix = line[: match.start()]
            suffix = line[match.end() :]
            lines[index] = prefix + package + suffix
            changed = True
    if not changed:
        lines.append(package)
    ending = "\n" if content.endswith("\n") else ""
    return "\n".join(lines) + ending


def update_yaml_image(content, package):
    name, _ = split_package_number(package)
    target = infer_package_target(package)
    lines = content.splitlines()
    changed = False
    for index, line in enumerate(lines):
        if not re.match(r"^\s*image\s*:", line):
            continue
        match = IMAGE_REF_RE.search(line)
        if not match:
            continue
        candidate_package = f'{match.group("name")}:{match.group("tag")}'
        try:
            candidate = infer_package_target(candidate_package)
        except ValueError:
            continue
        if (
            candidate["product"] != target["product"]
            or candidate["service_type"] != target["service_type"]
        ):
            continue
        current_reference = match.group(0)
        registry_prefix = (
            current_reference.rsplit("/", 1)[0] + "/"
            if "/" in current_reference
            else ""
        )
        lines[index] = (
            line[: match.start()]
            + registry_prefix
            + package
            + line[match.end() :]
        )
        changed = True
    if not changed:
        raise ValueError(f"YAML 中没有找到镜像 {name}")
    ending = "\n" if content.endswith("\n") else ""
    return "\n".join(lines) + ending


def yaml_image_reference(content, package):
    """Return the fully qualified target image after applying the package tag."""
    updated = update_yaml_image(content, package)
    name, _ = split_package_number(package)
    for line in updated.splitlines():
        if not re.match(r"^\s*image\s*:", line):
            continue
        match = IMAGE_REF_RE.search(line)
        if match and match.group("name") == name:
            return match.group(0)
    raise ValueError(f"YAML 中没有找到镜像 {name}")
