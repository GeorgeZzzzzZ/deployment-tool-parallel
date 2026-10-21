"""Read-only package provenance from retained Jenkins build records."""

import codecs
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from core import extract_job_project_code, infer_package_target, jenkins_job_api_url, split_package_number


class TraceCancelled(Exception):
    pass


class TraceAccessError(RuntimeError):
    pass


def trace_package_image(package):
    value = package.strip().strip("[]")
    try:
        split_package_number(value)
    except ValueError:
        # Exported package numbers may replace the image/tag colon with a dash.
        # Only split a final numeric version after an explicit platform version.
        match = re.fullmatch(r"(.+-\d+(?:\.\d+){1,3})-(\d+(?:\.\d+){2,})", value)
        if not match:
            raise
        value = match[1] + ":" + match[2]
        split_package_number(value)
    return value


def trace_package_target(package):
    image = trace_package_image(package)
    name, _ = split_package_number(image)
    mobile = re.match(r"([a-z][a-z0-9]*)mobile(?:[-_.]|$)", name.lower())
    if mobile:
        return {"product": mobile[1], "service_type": "mobile"}
    return infer_package_target(image)


def parse_packages(text):
    packages = []
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            if "://" in value or len(value) > 500:
                raise ValueError()
            trace_package_image(value)
        except ValueError:
            raise ValueError("每行输入一个包号，支持镜像名:版本号，或以横杠连接平台版本和数字包版本的格式。") from None
        if value not in packages:
            packages.append(value)
    if not packages or len(packages) > 20:
        raise ValueError("请输入 1～20 个包号，每行一个。")
    return packages


def safe_repository(value):
    value = str(value or "").strip()
    if "://" in value:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    # SSH's git@host:path is a repository address, not a password.
    return value.split("?", 1)[0].split("#", 1)[0]


def build_time(timestamp):
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return "时间未知"
    return datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def build_revisions(data, log_revisions):
    revisions = []
    for action in data.get("actions") or []:
        revision = action.get("lastBuiltRevision") or {}
        commit = str(revision.get("SHA1") or "")
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", commit):
            continue
        branches = [str(item["name"]) for item in revision.get("branch") or [] if item.get("name")]
        repositories = action.get("remoteUrls") or [""]
        for repository in repositories:
            repository = safe_repository(repository)
            # Jenkins may retain ${CUSTOM_NAME} while checkout logs contain the
            # resolved URL. Only reconcile the same commit, branch and URL shape.
            parts = re.split(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", repository)
            if len(parts) > 1:
                pattern = r"[^/?#]+".join(re.escape(part) for part in parts)
                resolved = [row for row in log_revisions
                            if row["commit"] == commit and row["branch"] == ", ".join(branches)
                            and re.fullmatch(pattern, row["repository"])]
                if len(resolved) == 1:
                    revisions.append(resolved[0])
                    continue
            revisions.append({"repository": repository,
                              "branch": ", ".join(branches), "commit": commit,
                              "source": "Jenkins SCM 记录"})
    for revision in log_revisions:
        if not any(all(row[key] == revision[key] for key in ("repository", "branch", "commit"))
                   for row in revisions):
            revisions.append(revision)
    return revisions


def build_user(data, log_user=""):
    users = []
    for action in data.get("actions") or []:
        for cause in action.get("causes") or []:
            user = str(cause.get("userName") or cause.get("userId") or "").strip()
            if user and user not in users:
                users.append(user)
    return "；".join(users) or log_user or "未记录"


def job_service_type(path, display):
    # A multibranch leaf may be named master; its nearest service folder
    # determines whether it builds SERVICE, WEB or APP.
    for names in reversed(list(zip(path, display))):
        text = " ".join(names)
        for service, aliases in (("backend", "SERVICE|BACKEND"),
                                 ("frontend", "WEB|FRONTEND"),
                                 ("mobile", "APP|MOBILEFRONTEND|MOBILE")):
            if re.search(r"(?<![a-zA-Z0-9])(?:" + aliases + r")(?![a-zA-Z0-9])", text, re.I):
                return service
    return ""


class TraceLogParser:
    """Retain only output image evidence and checkout revisions, never raw logs."""

    def __init__(self, packages):
        self.packages = packages
        self._images = {package: trace_package_image(package) for package in packages}
        self.matches = []
        self.revisions = []
        self.started_by = ""
        self._line_count = 0
        self._repository = ""
        self._push_repository = ""

    def _record(self, image, kind, digest=""):
        try:
            split_package_number(image)
        except ValueError:
            return
        for package in self.packages:
            target_image = self._images[package]
            if image != target_image and ("/" in target_image or image.rsplit("/", 1)[-1] != target_image):
                continue
            old = next((row for row in self.matches
                        if row["package"] == package and row["image"] == image), None)
            if old:
                if kind == "镜像生成记录":
                    old["kind"] = kind
                if digest:
                    old["digest"] = digest
            else:
                self.matches.append({"package": package, "image": image, "kind": kind, "digest": digest})

    def feed(self, line):
        self._line_count += 1
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).strip()
        line = re.sub(r"^\[(?:\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]\s*", "", line)
        started = re.match(r"^Started by user (.+)$", line)
        if started and self._line_count <= 20 and not self.started_by:
            self.started_by = started[1].strip()
        # Shell commands and echo text are not proof that an image was produced.
        output = re.sub(r"^--->\s+", "", line)
        output = re.sub(r"^#\d+\s+(?:\d+(?:\.\d+)?s?\s+)?", "", output)
        tagged = re.match(r"^(?:naming to|Successfully tagged)\s+(\S+)(?:\s+(?:\d+(?:\.\d+)?s\s+)?done)?$", output)
        if tagged:
            self._record(tagged[1], "镜像生成记录")
        pushed = re.match(r"^The push refers to repository \[([^\]]+)\]$", output)
        if pushed:
            self._push_repository = pushed[1]
        digest = re.match(r"^([\w.-]+): digest: (sha256:[a-fA-F0-9]{64})\s+size:\s*\d+", output)
        if digest and self._push_repository:
            self._record(self._push_repository + ":" + digest[1], "仅推送记录", digest[2])
        repository = re.match(r"^(?:Fetching upstream changes from|Cloning repository)\s+(\S+)", line)
        if repository:
            self._repository = safe_repository(repository[1])
        checkout = re.match(r"^Checking out Revision ([a-fA-F0-9]{40,64})(?:\s+\((.*)\))?", line)
        if checkout:
            revision = {"repository": self._repository, "branch": checkout[2] or "",
                        "commit": checkout[1], "source": "控制台 checkout 记录"}
            if revision not in self.revisions:
                self.revisions.append(revision)


class JenkinsHistorySource:
    def __init__(self, client, cancel, log_limit=32 * 1024 * 1024):
        self.client = client
        self.cancel = cancel
        self.log_limit = log_limit

    def _check(self):
        if self.cancel.is_set():
            raise TraceCancelled()

    def _get(self, url, **kwargs):
        self._check()
        # URLs are built from the logged-in server and actual job names. Do not
        # send the authenticated session to URLs exported by a reverse proxy.
        try:
            response = self.client.session.get(url, timeout=(5, 20), allow_redirects=False, **kwargs)
        except Exception:
            self._check()
            raise RuntimeError("Jenkins 网络请求失败或超时，请检查连接后重试") from None
        if response.status_code != 200:
            status = response.status_code
            response.close()
            error_type = TraceAccessError if status in (401, 403) else RuntimeError
            raise error_type(f"Jenkins HTTP {status}（记录不可读或登录已失效）")
        if self.cancel.is_set():
            response.close()
            raise TraceCancelled()
        return response

    def _json(self, url, **kwargs):
        response = self._get(url, **kwargs)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError):
            raise RuntimeError("Jenkins 返回了无效数据，请检查登录和服务器地址") from None
        finally:
            response.close()

    def build_url(self, job, number):
        return jenkins_job_api_url(self.client.url, job["path"]) + f"/{int(number)}/"

    def jobs(self, packages, scope, selected, progress, warnings):
        if scope == "selected":
            if not selected:
                raise ValueError("请先在 Jenkins 页选择具体构建任务。")
            return [{"path": list(selected), "label": " / ".join(selected)}]
        targets = set()
        if scope == "auto":
            for package in packages:
                try:
                    target = trace_package_target(package)
                    targets.add((target["product"].upper(), target["service_type"]))
                except ValueError:
                    raise ValueError("部分包号无法识别产品和服务，请检查包号前缀。") from None
        products = {product for product, _service in targets}
        jobs = []
        pending = [([], [])]
        visited = 0
        while pending:
            self._check()
            path, display = pending.pop(0)
            visited += 1
            if visited > 500 or len(jobs) >= 500:
                warnings.append("任务目录超过查询上限，部分任务尚未查询。")
                break
            progress("正在读取任务目录：" + (" / ".join(display) or "Jenkins"))
            try:
                data = self._json(jenkins_job_api_url(self.client.url, path) + "/api/json", params={
                    "tree": "jobs[name,displayName,_class]"})
            except TraceAccessError:
                raise
            except RuntimeError as exc:
                if not path:
                    raise
                warnings.append(f"目录 {' / '.join(display)}：{exc}")
                continue
            for node in data.get("jobs") or []:
                name = node.get("name")
                if not name:
                    continue
                node_path = path + [name]
                node_display = display + [node.get("displayName") or name]
                code = extract_job_project_code(node_display) or extract_job_project_code(node_path)
                if products and code and code not in products:
                    continue
                kind = node.get("_class", "").lower()
                if any(word in kind for word in ("folder", "multibranch", "organization")):
                    pending.append((node_path, node_display))
                else:
                    text = " / ".join(node_path + node_display)
                    service = job_service_type(node_path, node_display)
                    if not targets or any(
                        service == expected_service and (code == product or (not code and re.search(
                            r"(?<![a-zA-Z0-9])" + re.escape(product) + r"(?![a-zA-Z0-9])", text, re.I)))
                        for product, expected_service in targets
                    ):
                        jobs.append({"path": node_path, "label": " / ".join(node_display)})
        return jobs

    def builds(self, job, offset, size):
        data = self._json(jenkins_job_api_url(self.client.url, job["path"]) + "/api/json", params={
            "tree": "allBuilds[number,timestamp,result,building]" + f"{{{offset},{offset + size}}}"})
        if not isinstance(data.get("allBuilds"), list):
            raise RuntimeError("没有返回历史构建列表；请确认选中的是构建任务且拥有读取权限")
        return data["allBuilds"]

    def revision_data(self, job, number):
        return self._json(self.build_url(job, number) + "api/json", params={
            "tree": "actions[causes[userName,userId],remoteUrls,lastBuiltRevision[SHA1,branch[name]]]"})

    def console(self, job, number, parser):
        response = self._get(self.build_url(job, number) + "consoleText", stream=True)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        size = 0
        started = time.monotonic()
        try:
            for chunk in response.iter_content(chunk_size=65536):
                self._check()
                if time.monotonic() - started > 90:
                    return False
                size += len(chunk)
                if size > self.log_limit:
                    return False
                pending += decoder.decode(chunk)
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    parser.feed(line.rstrip("\r"))
                if len(pending) > 1024 * 1024:
                    return False
            pending += decoder.decode(b"", final=True)
            if pending:
                parser.feed(pending)
            return True
        except TraceCancelled:
            raise
        except Exception:
            raise RuntimeError("构建日志读取中断，请重试") from None
        finally:
            response.close()


def trace_packages(source, packages, *, scope="auto", selected=None, limit=500,
                   page_size=50, progress=lambda _: None, on_match=lambda _: None):
    report = {"matches": [], "warnings": [], "unmatched": [], "cancelled": False,
              "complete": False, "jobs": 0, "scanned": 0}
    warnings = report["warnings"]
    try:
        jobs = source.jobs(packages, scope, selected, progress, warnings)
        report["jobs"] = len(jobs)
        if not jobs:
            warnings.append("未找到对应产品和服务的 Jenkins 任务，请检查包号和账号读取权限。")
        for index, job in enumerate(jobs, 1):
            offset = 0
            seen = set()
            while offset < limit:
                if source.cancel.is_set():
                    raise TraceCancelled()
                size = min(page_size, limit - offset)
                try:
                    page = source.builds(job, offset, size)
                except TraceAccessError:
                    raise
                except RuntimeError as exc:
                    warnings.append(f"{job['label']}：{exc}")
                    break
                if not page:
                    break
                fresh = [build for build in page[:size] if build.get("number") not in seen]
                if not fresh:
                    warnings.append(f"{job['label']}：历史分页重复，已停止该任务的查询。")
                    break
                for build in fresh:
                    if source.cancel.is_set():
                        raise TraceCancelled()
                    number = build["number"]
                    seen.add(number)
                    report["scanned"] += 1
                    progress(f"任务 {index}/{len(jobs)} · {job['label']} #{number} · 已查 {report['scanned']} 次构建")
                    parser = TraceLogParser(packages)
                    try:
                        complete_log = source.console(job, number, parser)
                    except TraceAccessError:
                        raise
                    except RuntimeError as exc:
                        warnings.append(f"{job['label']} #{number}：{exc}")
                        continue
                    if not complete_log or build.get("building"):
                        warnings.append(f"{job['label']} #{number}：日志未完整读取或构建尚未结束，查询可能有遗漏。")
                    if not parser.matches:
                        continue
                    try:
                        data = source.revision_data(job, number)
                    except TraceAccessError:
                        raise
                    except RuntimeError as exc:
                        data = {}
                        warnings.append(f"{job['label']} #{number}：代码来源读取失败，{exc}")
                    revisions = build_revisions(data, parser.revisions)
                    for match in parser.matches:
                        row = {**match, "job": job["label"], "number": number,
                               "time": build_time(build.get("timestamp")),
                               "url": source.build_url(job, number),
                               "result": "构建中" if build.get("building") else build.get("result") or "未知",
                               "builder": build_user(data, parser.started_by),
                               "revisions": revisions}
                        report["matches"].append(row)
                        on_match(row)
                offset += min(len(page), size)
                if len(page) < size:
                    break
                if offset >= limit:
                    warnings.append(f"{job['label']}：已达到每任务 {limit} 次上限，更早记录尚未查询。")
    except TraceCancelled:
        report["cancelled"] = True
    except TraceAccessError as exc:
        warnings.append(f"{exc}；已停止查询，请检查登录和读取权限。")
    found = {row["package"] for row in report["matches"]}
    report["unmatched"] = [package for package in packages if package not in found]
    report["complete"] = not warnings and not report["cancelled"]
    return report


def trace_result_fields(row):
    if row["kind"] == "仅推送记录":
        return [row["package"], "未确认", "未确认", "未确认", "未确认"]
    pairs = list(dict.fromkeys((revision.get("repository") or "未记录", revision.get("branch") or "未记录")
                              for revision in row["revisions"])) or [("未记录", "未记录")]
    branches, repositories = [], []
    for index, (repository, branch) in enumerate(pairs, 1):
        prefix = f"[{index}] " if len(pairs) > 1 else ""
        branches.append(prefix + branch)
        repositories.append(prefix + repository)
    values = [row["package"], row.get("builder") or "未记录", "；".join(branches),
              row["time"], "；".join(repositories)]
    return [" ".join(str(value).split()) for value in values]


def format_trace_match(row):
    labels = ("包号", "打包人", "打包分支", "打包时间", "代码仓库")
    return "\n".join(f"{label}：{value}" for label, value in zip(labels, trace_result_fields(row)))
