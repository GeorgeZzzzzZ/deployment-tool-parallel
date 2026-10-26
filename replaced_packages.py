"""Best-effort, non-blocking snapshots of packages running before deployment."""
import json
import threading
import time
from shlex import quote as shell_quote

from core import infer_package_target, split_package_number


class ReplacedPackageCapture:
    def __init__(self, initial="待读取", timeout=30):
        self._initial = initial
        self._timeout = timeout
        self._lock = threading.Lock()
        self._entries = {}
        self._closed = False

    def start(self, key, reader):
        entry = {"cancel": threading.Event(), "deadline": time.monotonic() + self._timeout,
                 "pending": True, "packages": []}
        with self._lock:
            if self._closed:
                return None
            previous = self._entries.get(key)
            if previous:
                previous["cancel"].set()
            self._entries[key] = entry

        def run():
            packages = []
            try:
                if not entry["cancel"].is_set():
                    for image in reader(entry["cancel"]):
                        name, tag = split_package_number(image)
                        package = f"{name}:{tag}"
                        if package not in packages:
                            packages.append(package)
            except Exception:
                # Remote diagnostics can contain credentials. This optional
                # column only reports availability, never raw server output.
                packages = []
            with self._lock:
                if self._entries.get(key) is not entry:
                    return
                if not entry["cancel"].is_set() and time.monotonic() < entry["deadline"]:
                    entry["packages"] = packages
                entry["pending"] = False

        thread = threading.Thread(target=run, name="replaced-package-lookup", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                entry["pending"] = False
            return None
        return thread

    def freeze(self, key=None):
        # The deployment worker closes this gate synchronously before mutation;
        # a delayed UI event or HTTP/SSH response cannot reopen it.
        with self._lock:
            if key is None:
                self._closed = True
                if self._initial == "待读取":
                    self._initial = "未获取"
            entries = self._entries.values() if key is None else [self._entries.get(key)]
            for entry in entries:
                if entry:
                    entry["cancel"].set()
                    entry["pending"] = False

    def snapshot(self):
        with self._lock:
            packages, details = [], []
            pending = missing = False
            for key, entry in self._entries.items():
                if entry["pending"] and time.monotonic() >= entry["deadline"]:
                    entry["cancel"].set()
                    entry["pending"] = False
                values = entry["packages"]
                packages.extend(value for value in values if value not in packages)
                pending |= entry["pending"]
                missing |= not values and not entry["pending"]
                details.append(f"{key}：\n" + ("\n".join(values) or (
                    "获取中…" if entry["pending"] else "未获取")))
            suffix = "获取中…" if pending else "部分未获取" if missing and packages else ""
            text = "；".join(packages + ([suffix] if suffix else []))
            if not text:
                text = "未获取" if self._entries else self._initial
            return {"packages": packages, "text": text, "tooltip": "\n\n".join(details) or text}


def read_ssh_running_packages(client, environment, package, cancel):
    """Select live objects using the manifest, then read their running containers.

    Namespace plus controller UID ancestry prevents similarly named services or
    other tenants' Pods from contributing a package. Desired images are used
    only to identify container names, never as the reported running version.
    """
    if cancel.is_set():
        raise InterruptedError("运行包号查询已结束")

    def read(arguments):
        if cancel.is_set():
            raise InterruptedError("运行包号查询已结束")
        command = (f"cd {shell_quote(environment['yaml_dir'])} && kubectl get {arguments} "
                   "--request-timeout=8s -o json 2>/dev/null")
        stdin, stdout, stderr = client.exec_command(command, timeout=10)
        try:
            stdin.close()
            raw = stdout.read(4 * 1024 * 1024 + 1)
            if cancel.is_set():
                raise InterruptedError("运行包号查询已结束")
            if len(raw) > 4 * 1024 * 1024 or stdout.channel.recv_exit_status() != 0:
                raise ValueError("无法读取运行包号")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("运行资源响应无效")
            return payload.get("items", [payload])
        finally:
            stdout.close()
            stderr.close()

    expected = infer_package_target(package)

    def matches(image):
        try:
            target = infer_package_target(image)
            return (target["product"], target["service_type"]) == (
                expected["product"], expected["service_type"])
        except (ValueError, AttributeError, TypeError):
            return False

    workloads = read("-f " + shell_quote(environment["yaml_file"]))
    packages = []
    namespace_cache = {}
    for workload in workloads:
        if cancel.is_set():
            raise InterruptedError("运行包号查询已结束")
        kind = workload.get("kind")
        if kind not in ("Pod", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet",
                        "ReplicationController", "Job", "CronJob"):
            continue
        metadata, spec = workload.get("metadata", {}), workload.get("spec", {})
        namespace, uid = metadata.get("namespace", "default"), metadata.get("uid")
        if not uid:
            continue
        if kind == "CronJob":
            spec = spec.get("jobTemplate", {}).get("spec", {})
        container_spec = spec if kind == "Pod" else spec.get("template", {}).get("spec", {})
        names = {item.get("name") for item in container_spec.get("containers", [])
                 if matches(item.get("image"))}
        if not names:
            continue
        if kind == "Pod":
            objects = [workload]
        else:
            resources = "pods,replicasets" if kind == "Deployment" else "pods,jobs" if kind == "CronJob" else "pods"
            cache_key = (namespace, resources)
            if cache_key not in namespace_cache:
                namespace_cache[cache_key] = read(resources + " -n " + shell_quote(namespace))
            objects = namespace_cache[cache_key]
        objects = [item for item in objects if item.get("metadata", {}).get("namespace", "default") == namespace]
        descendants = {uid}
        while True:
            children = {item.get("metadata", {}).get("uid") for item in objects
                        if any(owner.get("controller") is True and owner.get("uid") in descendants
                               for owner in item.get("metadata", {}).get("ownerReferences", []))}
            children.discard(None)
            if children.issubset(descendants):
                break
            descendants.update(children)
        for item in objects:
            if item.get("kind") != "Pod" or item.get("metadata", {}).get("uid") not in descendants:
                continue
            for container in item.get("status", {}).get("containerStatuses", []):
                image = container.get("image")
                if (container.get("name") in names and "running" in container.get("state", {})
                        and matches(image) and image not in packages):
                    packages.append(image)
    return packages
