import base64
import codecs
import copy
import ctypes
import hashlib
import importlib
import json
import os
import posixpath
import re
import stat
import sys
import threading
import time
import uuid
import webbrowser
from ctypes import wintypes
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from shlex import quote as shell_quote
from urllib.parse import urlsplit

import paramiko
import requests
from cryptography.fernet import Fernet
from PySide6.QtCore import (
    QObject, QThread, Qt, Signal, Slot, QPoint, QSize, QRectF, QStringListModel,
    QSortFilterProxyModel, QTimer, QEvent, QEventLoop, QPropertyAnimation,
    QEasingCurve, QLockFile,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QAction,
    QLinearGradient, QPen, QBrush, QMouseEvent, QPolygon, QKeySequence,
    QShortcut, QPixmap, QPainterPath,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLayout, QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox,
    QTreeWidget, QTreeWidgetItem, QPlainTextEdit, QScrollArea,
    QTabWidget, QFrame, QSplitter, QProgressBar, QSizePolicy,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QMessageBox, QInputDialog, QDialog,
    QDialogButtonBox, QCompleter, QSpacerItem, QAbstractItemView,
    QHeaderView, QStackedWidget, QToolButton, QProxyStyle, QStyle, QFileDialog, QMenu,
    QDockWidget, QSystemTrayIcon,
)
from deploy_glass import GlassBackground, GlassPanel, WrappingActionLayout, is_reduced_motion


from core import (
    cloud_project_matches_job,
    dashboard_pods_url,
    ensure_dashboard_bindings,
    extract_job_project_code,
    extract_package_number,
    infer_cloud_deployment_config,
    infer_package_target,
    infer_ssh_deployment_config,
    is_secondary_build_path,
    jenkins_job_api_url,
    parse_deployment_packages,
    reorder_parameters,
    rename_bound_dashboard,
    resolve_ssh_environment_for_package,
    split_package_number,
    update_image_list,
    update_yaml_image,
    yaml_image_reference,
)
from ops_api import (
    OpsApiClient, OpsApiError, OpsSubmissionUnknown, service_type_id, service_type_label,
)
from replaced_packages import ReplacedPackageCapture, read_ssh_running_packages
from package_trace import (
    JenkinsHistorySource, format_trace_match, parse_packages, trace_packages, trace_result_fields,
    job_service_type, TraceCancelled,
)
from task_diagnostics import (
    configure_diagnostics, shutdown_diagnostics, diagnostics_status, new_task_trace,
    trace_event, trace_stage, trace_operation, RemoteTransferTrace,
)

# ── Color Palette (Glassmorphism / Frosted Glass / Modern SaaS) ─────
COLORS = {
    "bg": "#0B1020",
    "card": "rgba(255, 255, 255, 0.05)",
    "card_hover": "rgba(255, 255, 255, 0.08)",
    "border": "rgba(255, 255, 255, 0.10)",
    "border_focus": "#60A5FA",
    "text_primary": "#F8FAFC",
    "text_secondary": "#CBD5E1",
    "text_muted": "#A5B3C8",
    "accent": "#60A5FA",
    "accent_hover": "#93C5FD",
    "accent_light": "rgba(96, 165, 250, 0.16)",
    "primary_bg": "#2563EB",
    "primary_hover": "#3B82F6",
    "primary_pressed": "#1D4ED8",
    "primary_gradient_start": "#2563EB",
    "primary_gradient_end": "#4F46E5",
    "primary_hover_start": "#2F64D6",
    "primary_hover_end": "#5A4FD6",
    "success": "#34D399",
    "success_bg": "#123522",
    "success_hover": "#16A34A",
    "danger": "#F87171",
    "danger_bg": "rgba(239, 68, 68, 0.14)",
    "danger_border": "rgba(239, 68, 68, 0.35)",
    "warning": "#FBBF24",
    "input_bg": "rgba(10, 15, 26, 0.65)",
    "popup_bg": "#131929",
    "input_border": "rgba(255, 255, 255, 0.12)",
    "sidebar_bg": "rgba(10, 15, 26, 0.75)",
    "sidebar_selected": "#263A60",
    "tab_active": "#60A5FA",
    "tab_inactive": "#94A3B8",
    "progress_bg": "rgba(10, 15, 26, 0.70)",
    "progress_fill": "#34D399",
    "log_bg": "rgba(8, 12, 20, 0.88)",
    "log_fg": "#E2E8F0",
    "log_border": "rgba(255, 255, 255, 0.08)",
    "separator": "rgba(255, 255, 255, 0.08)",
    "badge_blue": "#172E52",
    "badge_blue_text": "#93C5FD",
    "purple": "#A78BFA",
    "purple_hover": "#C4B5FD",
    "title_bar": "rgba(11, 15, 25, 0.75)",
    "footer_bg": "rgba(11, 15, 25, 0.85)",
    "disabled_bg": "rgba(255, 255, 255, 0.03)",
    "disabled_border": "rgba(255, 255, 255, 0.06)",
    "disabled_text": "rgba(148, 163, 184, 0.45)",
}

if sys.platform == "darwin":
    FONT_FAMILY = "PingFang SC"
    FONT_MONO = "Menlo"
elif sys.platform.startswith("linux"):
    FONT_FAMILY = "Noto Sans CJK SC"
    FONT_MONO = "DejaVu Sans Mono"
else:
    FONT_FAMILY = "Microsoft YaHei UI"
    FONT_MONO = "Cascadia Code"

APP_NAME = "BuildDeployToolParallel"
LEGACY_APP_NAME = "BuildDeployTool"
JENKINS_JOB_CACHE_VERSION = 1
CONFIG_EXPORT_FORMAT = "deploy-console-local-config"
CONFIG_EXPORT_VERSION = 1


def resource_path(relative_path):
    """Resolve source assets and files bundled into a PyInstaller executable."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative_path


@lru_cache(maxsize=128)
def ui_icon(
    name: str,
    color=None,
    disabled_color=None,
    size: int = 40,
) -> QIcon:
    """Load local SVG icons with high-DPI and disabled-state support."""
    if color is None:
        color = COLORS["text_secondary"]
    icon_path = resource_path(
        "assets/uiverse/send.svg" if name == "send" else f"assets/lucide/{name}.svg"
    )
    if not icon_path.is_file():
        return QIcon()

    renderer = QSvgRenderer(str(icon_path))
    icon = QIcon()

    # 1. Normal state (rendered at 2x device pixel ratio for high DPI)
    normal_pixmap = QPixmap(size, size)
    normal_pixmap.fill(Qt.transparent)
    painter = QPainter(normal_pixmap)
    renderer.render(painter)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(normal_pixmap.rect(), QColor(color))
    painter.end()
    normal_pixmap.setDevicePixelRatio(2)
    icon.addPixmap(normal_pixmap, QIcon.Mode.Normal)

    # 2. Disabled state
    disabled_pixmap = QPixmap(size, size)
    disabled_pixmap.fill(Qt.transparent)
    painter = QPainter(disabled_pixmap)
    renderer.render(painter)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    d_color = (
        QColor(disabled_color)
        if disabled_color and not disabled_color.startswith("rgba")
        else QColor(148, 163, 184, 115)
    )
    painter.fillRect(disabled_pixmap.rect(), d_color)
    painter.end()
    disabled_pixmap.setDevicePixelRatio(2)
    icon.addPixmap(disabled_pixmap, QIcon.Mode.Disabled)

    return icon


def apply_ui_icons(root):
    icons = {
        "新增": "plus", "新建任务": "plus", "保存": "save",
        "保存环境配置": "save", "删除": "trash-2", "移除": "trash-2",
        "取消": "square", "停止": "square", "开始一键流程": "play",
        "刷新": "refresh-cw", "刷新云端目标": "refresh-cw",
        "刷新三个栏位": "refresh-cw", "配置迁移": "copy",
        "登录并加载": "arrow-down-to-line", "自动登录": "arrow-down-to-line",
        "执行环境部署": "send", "部署当前 Jenkins 包": "send",
        "编辑": "pencil",
    }
    for button in root.findChildren(QPushButton) + root.findChildren(QToolButton):
        if isinstance(button, QToolButton) and button.arrowType() != Qt.NoArrow:
            continue
        label = button.text().strip()
        name = icons.get(label)
        if not name and not label and "刷新" in button.toolTip():
            name = "refresh-cw"
        if not name:
            continue
        color = "#FFFFFF" if button.objectName() in ("PrimaryBtn", "SuccessBtn") else COLORS["text_secondary"]
        if button.objectName() == "DangerGhostBtn":
            color = COLORS["danger"]
        button.setIcon(ui_icon(name, color))
        button.setIconSize(QSize(16, 16))
        button.setCursor(Qt.PointingHandCursor)
        if isinstance(button, QToolButton) and label:
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        if not button.accessibleName():
            button.setAccessibleName(label or button.toolTip())


def platform_config_dir(app_name=APP_NAME):
    """Return the native per-user configuration directory."""
    if sys.platform == "win32":
        return Path(os.getenv("APPDATA", Path.home())) / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / app_name


CONFIG_DIR = platform_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_CONFIG_FILE = platform_config_dir(LEGACY_APP_NAME) / "config.json"
DEFAULT_DEPLOY = {
    "image_dir": "/home/setup/images",
    "image_file": "app_image_list.txt",
    "pull_command": "sh app_images_store.sh",
    "yaml_dir": "/data/yaml/app/backend",
    "yaml_file": "etm.yaml",
    "apply_command": "kubectl apply -f etm.yaml",
}


def _yaml_delete_command(yaml_file):
    filename = str(yaml_file or "").strip()
    if not filename:
        raise ValueError("未选择 YAML 文件，无法生成删除命令")
    return f"kubectl delete -f {shell_quote(filename)}"


def _deployment_delete_command(data):
    command = data.get("delete_command")
    if command is None:
        return _yaml_delete_command(data["yaml_file"])
    target = data.get("delete_command_target")
    if target and tuple(target) != (data["yaml_dir"], data["yaml_file"]):
        raise ValueError("删除命令对应的 YAML 与当前部署目标不一致，请重新核对包号和删除命令")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("已开启删除后重新应用，请填写删除命令，或关闭该开关")
    return command.strip()


ENVIRONMENT_FIELDS = [
    ("环境名称", "name"),
    ("IP/主机", "host"),
    ("SSH 端口", "port"),
    ("账号", "username"),
    ("密码", "password"),
    ("镜像目录", "image_dir"),
    ("镜像配置文件", "image_file"),
    ("拉取命令", "pull_command"),
    ("YAML 目录", "yaml_dir"),
    ("YAML 文件", "yaml_file"),
    ("应用命令", "apply_command"),
    ("关联 Dashboard", "dashboard"),
]


class PipelineCancelled(RuntimeError):
    pass


class RemoteCommandInterrupted(PipelineCancelled):
    """The managed remote image command was intentionally stopped for rebatching."""


class RemoteCommandUncertain(RuntimeError):
    """A remote command may still be running; do not reuse its deployment target."""


class DeploymentCancellation:
    def __init__(self, owners, *, require_all=False):
        self.owners = tuple(owners)
        self.require_all = require_all

    def is_set(self):
        states = [owner.cancel_event.is_set() for owner in self.owners]
        return bool(states) and (all(states) if self.require_all else any(states))


class BatchReassembled(RuntimeError):
    """Restart batch preparation with newly collected entries before transfer."""

    def __init__(self, entries):
        super().__init__("部署批次已加入新的待部署包")
        self.entries = entries


def exception_message(exc, fallback="未知错误"):
    """Return useful text for Selenium timeout and other empty-message errors."""
    message = str(exc).split("Stacktrace:", 1)[0].strip()
    if re.fullmatch(r"Message\s*:\s*", message, re.IGNORECASE):
        message = ""
    return message or type(exc).__name__ or fallback


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
TRANSFER_PROGRESS_RE = re.compile(
    r"(?P<current>\d+(?:\.\d+)?)\s*(?P<current_unit>[KMGT]?I?B)\s*/\s*"
    r"(?P<total>\d+(?:\.\d+)?)\s*(?P<total_unit>[KMGT]?I?B)",
    re.IGNORECASE,
)


def clean_remote_output_line(value):
    """Remove terminal control sequences before displaying remote output."""
    value = ANSI_ESCAPE_RE.sub("", str(value or "")).replace("\r", "")
    while "\b" in value:
        value = re.sub(r".?\x08", "", value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return value.strip()


def _transfer_bytes(value, unit):
    normalized = str(unit or "B").upper().replace("IB", "B")
    scale = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }
    return float(value) * scale.get(normalized, 1)


def parse_transfer_progress(value):
    """Return the largest byte transfer found in one terminal progress line."""
    matches = []
    for match in TRANSFER_PROGRESS_RE.finditer(clean_remote_output_line(value)):
        current = _transfer_bytes(match.group("current"), match.group("current_unit"))
        total = _transfer_bytes(match.group("total"), match.group("total_unit"))
        if total > 0:
            matches.append((current, total))
    return max(matches, key=lambda pair: pair[1]) if matches else None


def format_transfer_bytes(value):
    """Format small Docker layers without rounding useful progress to 0.0 MB."""
    value = max(0.0, float(value or 0))
    if value < 1024 ** 2:
        kilobytes = 0 if value == 0 else max(1, round(value / 1024))
        return f"{kilobytes} KB"
    if value < 1024 ** 3:
        return f"{value / 1024 ** 2:.1f} MB"
    return f"{value / 1024 ** 3:.2f} GB"


class DockerImageProgress:
    """Associate serial Docker output with one image and aggregate its layers."""

    def __init__(self, images, progress_range):
        self.states = {image: {"pull": {}, "push": {}, "percent": progress_range[0],
                               "ratios": {}, "last": None, "done": False}
                       for image in images}
        self.start, self.end = progress_range
        self.midpoint = self.start + (self.end - self.start) // 2
        self.current_image = next(iter(self.states)) if len(self.states) == 1 else ""
        self.phase = "pull"
        self.push_repository = ""

    def _resolve(self, reference):
        candidates = [image for image in self.states
                      if image == reference or image.endswith("/" + reference)]
        if not candidates:
            candidates = [image for image in self.states
                          if image.rsplit("/", 1)[-1] == reference.rsplit("/", 1)[-1]]
        return candidates[0] if len(candidates) == 1 else ""

    def _update(self, confirmed=False):
        if not self.current_image:
            return None
        state = self.states[self.current_image]
        if state["done"] and not confirmed:
            return None
        start, end = ((self.start, self.midpoint) if self.phase == "pull"
                      else (self.midpoint, self.end))
        if confirmed:
            ratio = 100
            status = ("镜像已拉取，等待推送..." if self.phase == "pull"
                      else "镜像已推送，等待目标校验与部署...")
            if self.phase == "push":
                state["done"] = True
        else:
            layers = state[self.phase]
            pending = [layer for layer in layers.values() if not layer.get("cached")]
            known = [layer["total"] for layer in pending if layer["total"] > 0]
            # Unannounced layer sizes are estimated from known layers. A full
            # progress bar still requires Docker's pull status or push receipt.
            average = sum(known) / len(known) if known else 1
            total = sum(layer["total"] or average for layer in pending)
            current = sum((layer["total"] or average) if layer["done"]
                          else layer["current"] for layer in pending)
            fraction = current / total if total else (1 if layers else 0)
            ratio = min(99, int(max(0, fraction) * 100))
            ratio = max(state["ratios"].get(self.phase, 0), ratio)
            action = "拉取" if self.phase == "pull" else "推送"
            status = f"正在{action}镜像：约 {ratio}%（多层汇总）"
        state["ratios"][self.phase] = ratio
        state["percent"] = max(state["percent"], start + int((end - start) * ratio / 100))
        update = (self.current_image, state["percent"], status)
        if state["last"] == update:
            return None
        state["last"] = update
        return update

    def feed(self, line):
        line = re.sub(r"^--->\s+", "", line)
        marker = re.match(r"^处理镜像[:：]\s*(\S+)$", line)
        pull = re.match(r"^(\S+): Pulling from (\S+)$", line)
        if marker or pull:
            reference = marker[1] if marker else pull[2] + ":" + pull[1]
            self.current_image = self._resolve(reference)
            self.phase = "pull"
            self.push_repository = ""
            return self._update()
        push = re.match(r"^The push refers to repository \[([^\]]+)\]$", line)
        if push:
            self.push_repository = push[1]
            candidates = [image for image in self.states if image.rsplit(":", 1)[0] == push[1]]
            if self.current_image not in candidates:
                self.current_image = candidates[0] if len(candidates) == 1 else ""
            self.phase = "push"
            return self._update()
        receipt = re.match(r"^([^\s:]+): digest: sha256:[a-fA-F0-9]{64}\s+size:\s*\d+", line)
        if receipt and self.push_repository:
            image = self.push_repository + ":" + receipt[1]
            if image in self.states:
                self.current_image, self.phase = image, "push"
                return self._update(confirmed=True)
        pulled = re.match(r"^Status: (?:Downloaded newer image|Image is up to date) for (\S+)$", line)
        if pulled:
            self.current_image, self.phase = self._resolve(pulled[1]), "pull"
            return self._update(confirmed=True)
        layer_line = re.match(
            r"^([\w.-]+): (Pulling fs layer|Waiting|Downloading|Extracting|Download complete|"
            r"Pull complete|Already exists|Preparing|Pushing|Pushed|Layer already exists|Mounted from)\b",
            line,
        )
        if not layer_line or not self.current_image:
            return None
        state = self.states[self.current_image]
        layer = state[self.phase].setdefault(layer_line[1], {"current": 0, "total": 0, "done": False})
        action = layer_line[2]
        transfer = parse_transfer_progress(line)
        if transfer and action in ("Downloading", "Pushing"):
            current, total = transfer
            layer.update(current=min(current, total), total=total)
        elif action in ("Download complete", "Pull complete", "Extracting", "Pushed"):
            layer["done"] = True
        elif action in ("Already exists", "Layer already exists", "Mounted from"):
            layer.update(done=True, cached=True)
        return self._update()

    def heartbeat(self, elapsed):
        if not self.current_image:
            return None
        state = self.states[self.current_image]
        if not state["last"] or state["done"]:
            return None
        return (self.current_image, state["percent"],
                f"{state['last'][2]}；等待远端输出，命令已运行 {elapsed} 秒")


@contextmanager
def ssh_request_deadline(client, timeout=30):
    """Channel IO timeouts do not bound Paramiko's request-acknowledgement wait."""
    expired = threading.Event()

    def expire():
        expired.set()
        client.close()

    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    except Exception as exc:
        if expired.is_set():
            raise TimeoutError("SSH 请求响应超时，连接已关闭") from exc
        raise
    finally:
        watchdog.cancel()
    if expired.is_set():
        raise TimeoutError("SSH 请求响应超时，连接已关闭")


def connect_ssh_client(client, data):
    """Connect with password-only settings suitable for slower SSH servers."""
    client.connect(
        data["host"],
        port=int(data.get("port") or 22),
        username=data["username"],
        password=data["password"],
        timeout=15,
        banner_timeout=15,
        # Some target hosts finish password authentication just after 10
        # seconds. Keep transport failures bounded while allowing that normal
        # server-side delay to complete.
        auth_timeout=30,
        channel_timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )


def service_type_matches(expected, actual):
    """Match Ops service tabs despite case and whitespace differences."""
    normalize = lambda value: re.sub(r"\s+", "", (value or "")).lower()
    expected_value = normalize(expected)
    actual_value = normalize(actual)
    if not expected_value or not actual_value:
        return False
    if expected_value in actual_value or actual_value in expected_value:
        return True
    expected_suffix = next(
        (suffix for suffix in ("后端", "前端") if expected_value.endswith(suffix)),
        "",
    )
    expected_name = expected_value.removesuffix(expected_suffix)
    return bool(
        expected_suffix
        and expected_name
        and expected_name in actual_value
        and actual_value.endswith(expected_suffix)
    )


def browser_session_token(browser):
    """Return the Ops token stored by IAM without logging or persisting it."""
    try:
        return browser.execute_script("""
            try {
                const value = sessionStorage.getItem('DwUserInfo');
                return value ? (JSON.parse(value).token || '') : '';
            } catch (error) {
                return '';
            }
        """) or ""
    except Exception:
        return ""


def create_browser_driver(headless=False):
    """Create a Chromium-family WebDriver suitable for the current platform."""
    if sys.platform != "win32":
        ChromeOptions = importlib.import_module(
            "selenium.webdriver.chrome.options"
        ).Options
        ChromeDriver = importlib.import_module(
            "selenium.webdriver.chrome.webdriver"
        ).WebDriver
        options = ChromeOptions()
        # Every workflow explicitly waits for the target controls. Waiting for
        # images and background requests only delays interactive automation.
        options.page_load_strategy = "eager"
        candidates = []
        if sys.platform == "darwin":
            candidates.extend([
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            ])
        else:
            candidates.extend([
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/chromium"),
                Path("/usr/bin/chromium-browser"),
            ])
        binary = next((path for path in candidates if path.is_file()), None)
        if binary:
            options.binary_location = str(binary)
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--start-maximized")
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1600,1000")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
        else:
            options.add_experimental_option("detach", True)
        return ChromeDriver(options=options)

    EdgeOptions = importlib.import_module(
        "selenium.webdriver.edge.options"
    ).Options
    EdgeDriver = importlib.import_module(
        "selenium.webdriver.edge.webdriver"
    ).WebDriver
    EdgeService = importlib.import_module(
        "selenium.webdriver.edge.service"
    ).Service
    options = EdgeOptions()
    options.page_load_strategy = "eager"
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--start-maximized")
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1600,1000")
        options.add_argument("--disable-gpu")
    else:
        options.add_experimental_option("detach", True)

    edge_majors = set()
    edge_roots = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application",
    ]
    for root in edge_roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and re.fullmatch(r"\d+(?:\.\d+){3}", child.name):
                edge_majors.add(child.name.split(".", 1)[0])

    cache_root = Path.home() / ".cache/selenium/msedgedriver/win64"
    cached = list(cache_root.glob("*/msedgedriver.exe")) if cache_root.is_dir() else []
    cached.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    matching = [
        path for path in cached
        if not edge_majors or path.parent.name.split(".", 1)[0] in edge_majors
    ]
    for driver_path in matching:
        try:
            return EdgeDriver(
                options=options,
                service=EdgeService(executable_path=str(driver_path)),
            )
        except Exception:
            continue
    return EdgeDriver(options=options)


# ── Cross-platform credential protection ─────────────────────────────
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


CRYPT32 = None
KERNEL32 = None


def _dpapi_libs():
    global CRYPT32, KERNEL32
    if CRYPT32 is None or KERNEL32 is None:
        CRYPT32 = ctypes.WinDLL("crypt32", use_last_error=True)
        KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
        CRYPT32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        CRYPT32.CryptProtectData.restype = wintypes.BOOL
        CRYPT32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        CRYPT32.CryptUnprotectData.restype = wintypes.BOOL
        KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
        KERNEL32.LocalFree.restype = ctypes.c_void_p
    return CRYPT32, KERNEL32


PORTABLE_SECRET_PREFIX = "vault1:"


def _file_master_key():
    """Load or create a private per-user key without a desktop keyring call."""
    key_file = CONFIG_DIR / ".credential.key"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not key_file.exists():
        key = Fernet.generate_key()
        key_file.write_bytes(key)
    try:
        # Repair permissions on every use as well as on first creation.
        key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return key_file.read_bytes().strip()


def _portable_master_key():
    """Use a private local key without blocking on a desktop credential vault."""
    # Deepin Secret Service and macOS Keychain may synchronously wait for a
    # desktop session or an unlock prompt. Credential saving happens before the
    # button workers start, so such a wait makes Jenkins/Ops/Rancher login
    # buttons appear unresponsive. Both portable platforms use the 0600 key.
    return _file_master_key()


def protect_secret(value):
    if not value:
        return ""
    if sys.platform != "win32":
        encrypted = Fernet(_portable_master_key()).encrypt(value.encode("utf-8"))
        return PORTABLE_SECRET_PREFIX + encrypted.decode("ascii")
    crypt32, kernel32 = _dpapi_libs()
    source, source_buffer = _blob(value.encode("utf-8"))
    output = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(source), APP_NAME, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        data = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(data).decode("ascii")
    finally:
        kernel32.LocalFree(output.pbData)


def unprotect_secret(value):
    if not value:
        return ""
    if sys.platform != "win32":
        if not value.startswith(PORTABLE_SECRET_PREFIX):
            # Windows DPAPI ciphertext cannot be migrated to another OS.
            return ""
        try:
            token = value[len(PORTABLE_SECRET_PREFIX):].encode("ascii")
            return Fernet(_portable_master_key()).decrypt(token).decode("utf-8")
        except Exception:
            return ""
    crypt32, kernel32 = _dpapi_libs()
    source, source_buffer = _blob(base64.b64decode(value))
    output = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        return ""
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output.pbData)


def _validate_config_payload(data):
    if not isinstance(data, dict):
        raise ValueError("配置内容必须是对象")
    for section in ("jenkins", "cloud_deploy", "cloud_status"):
        value = data.get(section, {})
        if not isinstance(value, dict):
            raise ValueError(f"配置栏位 {section} 格式不正确")
    for field in ("environments", "dashboards"):
        values = data.get(field, [])
        if not isinstance(values, list) or any(
            not isinstance(item, dict) for item in values
        ):
            raise ValueError(f"配置栏位 {field} 格式不正确")
    for environment in data.get("environments", []):
        if not str(environment.get("name", "")).strip():
            raise ValueError("环境配置缺少名称")
    for dashboard in data.get("dashboards", []):
        if not str(dashboard.get("name", "")).strip():
            raise ValueError("Dashboard 配置缺少名称")


# ── Shared configuration and deployment coordination ─────────────────
class ConfigStore:
    def __init__(self):
        self._save_lock = threading.RLock()
        self.data = self._default_data()
        self.load()

    @staticmethod
    def _default_data():
        return {
            "jenkins": {
                "url": "",
                "username": "",
                "password": "",
                "values": {},
                "job_cache": None,
            },
            "cloud_deploy": {
                "url": "https://ops.digiwincloud.com.cn/cdm/deployment/flow-deploy",
                "username": "",
                "password": "",
                "project": "",
                "deploy_type": "etm 后端",
                "cloud": "Huawei",
                "region": "",
            },
            "cloud_status": {
                "url": (
                    "https://rancher-hw.digiwincloud.com.cn/dashboard/"
                    "c/c-m-xpxltqjq/explorer/pod"
                ),
                "username": "",
                "password": "",
            },
            "environments": [],
            "dashboards": [],
            "last_package": "",
            "delete_before_apply": False,
        }

    @classmethod
    def _merge_with_defaults(cls, loaded):
        merged = cls._default_data()
        if not isinstance(loaded, dict):
            return merged
        for key, value in loaded.items():
            if (
                key in ("jenkins", "cloud_deploy", "cloud_status")
                and isinstance(value, dict)
            ):
                merged[key].update(copy.deepcopy(value))
            else:
                merged[key] = copy.deepcopy(value)
        ensure_dashboard_bindings(merged)
        return merged

    def load(self):
        source = CONFIG_FILE
        imported_legacy_config = False
        if not source.exists() and sys.platform == "win32" and LEGACY_CONFIG_FILE.exists():
            # The parallel edition starts from a read-only snapshot of the
            # original edition. Every later save goes to its own app folder.
            source = LEGACY_CONFIG_FILE
            imported_legacy_config = True
        if source.exists():
            try:
                loaded = json.loads(source.read_text(encoding="utf-8"))
                self.data = self._merge_with_defaults(loaded)
            except (OSError, ValueError):
                pass
        ensure_dashboard_bindings(self.data)
        if imported_legacy_config:
            self.save()

    def save(self):
        # Every task window shares this instance. Serializing atomic writes
        # prevents concurrent UI callbacks from competing for the same temp file.
        with self._save_lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temp = CONFIG_FILE.with_suffix(".tmp")
            temp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp.replace(CONFIG_FILE)

    def export_to(self, path):
        path = Path(path)
        with self._save_lock:
            exported_data = copy.deepcopy(self.data)
            exported_data.get("jenkins", {}).pop("job_cache", None)
            exported_data.pop("selection_cache", None)
        package = {
            "format": CONFIG_EXPORT_FORMAT,
            "version": CONFIG_EXPORT_VERSION,
            "exported_at": int(time.time()),
            "data": exported_data,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(path)
        return path

    def import_from(self, path):
        try:
            package = json.loads(Path(path).read_text(encoding="utf-8"))
            if package.get("format") != CONFIG_EXPORT_FORMAT:
                raise ValueError("不是 Deploy Console 配置包")
            if package.get("version") != CONFIG_EXPORT_VERSION:
                raise ValueError("配置包版本不受支持")
            imported = package["data"]
            _validate_config_payload(imported)
        except (OSError, KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("配置文件损坏或格式不正确") from exc
        except Exception as exc:
            raise ValueError("配置文件损坏或格式不正确") from exc

        with self._save_lock:
            merged = self._merge_with_defaults(imported)
            self.data.clear()
            self.data.update(merged)
            self.save()
        return self.data


class DeploymentTargetCoordinator:
    """Serialize releases to one runtime target while builds stay parallel."""

    def __init__(self):
        self._condition = threading.Condition()
        self._owners = {}
        self._blocked = {}

    def acquire(self, owner, key, cancel_event=None, on_wait=None):
        if not key:
            return
        announced = False
        with self._condition:
            while True:
                if key in self._blocked:
                    raise RemoteCommandUncertain(self._blocked[key])
                if cancel_event and cancel_event.is_set():
                    raise PipelineCancelled("等待部署目标期间已取消")
                if key not in self._owners or self._owners[key] is owner:
                    break
                if not announced and on_wait:
                    on_wait()
                    announced = True
                self._condition.wait(timeout=0.25)
            self._owners[key] = owner

    def block(self, owner, key, reason):
        with self._condition:
            if key and self._owners.get(key, owner) is owner:
                self._blocked[key] = reason
                self._condition.notify_all()

    def release(self, owner, key):
        if not key:
            return
        with self._condition:
            if self._owners.get(key) is owner:
                del self._owners[key]
                self._condition.notify_all()

    def release_all(self, owner):
        with self._condition:
            released = [key for key, value in self._owners.items() if value is owner]
            for key in released:
                del self._owners[key]
            if released:
                self._condition.notify_all()


class BatchDeploymentCoordinator:
    """Coordinate SSH batches and safely join late builds at phase boundaries."""

    def __init__(self):
        self._condition = threading.Condition()
        self._groups = {}
        self._sequence = 0

    @staticmethod
    def _new_batch():
        return {
            "members": {},
            "release_requested": True,
            "claimed": False,
            "done": False,
            "error": None,
            "phase": "building",
            "leader": None,
            "held_by": None,
            "pending_batch_id": None,
            "joined_batch_id": None,
            "join_before_transfer": False,
            "restart_requested": False,
            "interrupt_event": threading.Event(),
        }

    def register(self, owner, key):
        token, _ = self.register_with_state(owner, key)
        return token

    def register_with_state(self, owner, key):
        """Register a build and report any active deployment in the same environment."""
        with self._condition:
            group = self._groups.setdefault(key, {"current": 0, "batches": {}})
            active_id, active = self._active_batch(group)
            batch_id = group["current"]
            batch = group["batches"].get(batch_id)
            if batch is None or batch["claimed"] or batch["done"]:
                batch_id += 1 if batch is not None else 0
                group["current"] = batch_id
                batch = self._new_batch()
                group["batches"][batch_id] = batch
            self._sequence += 1
            batch["members"][owner] = {
                "state": "building",
                "payload": None,
                "sequence": self._sequence,
            }
            active_state = None
            if active and active is not batch:
                phase = active["phase"]
                active["pending_batch_id"] = batch_id
                if phase in ("claimed", "preparing"):
                    active["join_before_transfer"] = True
                    active["pending_batch_id"] = batch_id
                    batch["held_by"] = active_id
                active_state = {
                    "token": (key, active_id),
                    "phase": phase,
                    "restart_requested": active["restart_requested"],
                    "owners": list(active["members"]),
                }
            self._condition.notify_all()
            return (key, batch_id), active_state

    def request_start(self, token):
        with self._condition:
            batch = self._batch(token)
            if not batch or batch["done"]:
                return False
            batch["release_requested"] = True
            self._condition.notify_all()
            return True

    def wait_until_ready(self, owner, token, payload, cancel_event=None, on_wait=None):
        """Wait for explicit release; one caller receives all payloads as leader."""
        announced = False
        with self._condition:
            batch = self._batch(token)
            member = batch and batch["members"].get(owner)
            if not member:
                raise PipelineCancelled("合并部署任务已退出批次")
            member["state"] = "ready"
            member["payload"] = payload
            self._condition.notify_all()
            while True:
                if cancel_event and cancel_event.is_set():
                    self._remove_member(owner, token)
                    raise PipelineCancelled("等待合并部署期间已取消")
                batch = self._batch(token)
                if not batch:
                    raise PipelineCancelled("合并部署批次已结束")
                if batch["done"]:
                    if batch["error"]:
                        raise RuntimeError(batch["error"])
                    return False, []
                all_ready = batch["members"] and all(
                    item["state"] == "ready" for item in batch["members"].values()
                )
                if batch["release_requested"] and all_ready and not batch["claimed"]:
                    if batch["held_by"] is not None:
                        self._condition.wait(timeout=0.25)
                        continue
                    batch["claimed"] = True
                    batch["phase"] = "claimed"
                    batch["leader"] = owner
                    entries = [
                        item["payload"]
                        for item in sorted(
                            batch["members"].values(), key=lambda value: value["sequence"]
                        )
                    ]
                    return True, entries
                if not announced and on_wait:
                    on_wait(len(batch["members"]), all_ready)
                    announced = True
                self._condition.wait(timeout=0.25)

    def complete(self, token, error=None):
        with self._condition:
            batch = self._batch(token)
            if batch:
                batch["done"] = True
                batch["phase"] = "done" if not error else "failed"
                batch["error"] = str(error) if error else None
                key, _ = token
                joined = self._groups.get(key, {}).get("batches", {}).get(
                    batch["joined_batch_id"]
                )
                if joined:
                    joined["done"] = True
                    joined["phase"] = batch["phase"]
                    joined["error"] = batch["error"]
                pending = self._pending_batch(token, batch)
                if pending and pending is not joined:
                    pending["held_by"] = None
                self._condition.notify_all()

    def set_phase(self, token, phase):
        with self._condition:
            batch = self._batch(token)
            if batch and not batch["done"]:
                batch["phase"] = phase
                self._condition.notify_all()

    def interrupt_event(self, token):
        with self._condition:
            batch = self._batch(token)
            return batch["interrupt_event"] if batch else None

    def phase(self, token):
        with self._condition:
            batch = self._batch(token)
            return batch["phase"] if batch else "done"

    def request_restart(self, token):
        """Request interruption only while the last observed phase is source pull."""
        with self._condition:
            batch = self._batch(token)
            if not batch or batch["phase"] != "pull" or batch["done"]:
                return False
            pending = self._pending_batch(token, batch)
            if not pending:
                return False
            batch["restart_requested"] = True
            pending["held_by"] = token[1]
            batch["interrupt_event"].set()
            self._condition.notify_all()
            return True

    def cancel_restart(self, token):
        with self._condition:
            batch = self._batch(token)
            if not batch:
                return
            batch["restart_requested"] = False
            batch["interrupt_event"].clear()
            pending = self._pending_batch(token, batch)
            if pending:
                pending["held_by"] = None
            self._condition.notify_all()

    def collect_pending_entries(
        self, token, current_entries, cancel_event=None, on_wait=None
    ):
        """Wait for a held next batch, then return one combined deployment payload."""
        announced = False
        with self._condition:
            batch = self._batch(token)
            if not batch or batch["pending_batch_id"] is None:
                return current_entries
            pending = self._pending_batch(token, batch)
            while pending and pending["members"] and not all(
                item["state"] == "ready" for item in pending["members"].values()
            ):
                if cancel_event and cancel_event.is_set():
                    pending["held_by"] = None
                    self._condition.notify_all()
                    raise PipelineCancelled("等待重新合批期间已取消")
                if not announced and on_wait:
                    on_wait(len(pending["members"]))
                    announced = True
                self._condition.wait(timeout=0.25)
                pending = self._pending_batch(token, batch)
            if not pending or not pending["members"]:
                if pending:
                    pending["held_by"] = None
                batch["join_before_transfer"] = False
                batch["restart_requested"] = False
                batch["interrupt_event"].clear()
                batch["pending_batch_id"] = None
                return current_entries
            pending["claimed"] = True
            pending["phase"] = "joined"
            batch["joined_batch_id"] = batch["pending_batch_id"]
            batch["join_before_transfer"] = False
            batch["restart_requested"] = False
            batch["interrupt_event"].clear()
            entries = list(current_entries) + [
                item["payload"]
                for item in sorted(
                    pending["members"].values(), key=lambda value: value["sequence"]
                )
            ]
            self._condition.notify_all()
            return entries

    def should_join_before_transfer(self, token):
        with self._condition:
            batch = self._batch(token)
            return bool(batch and batch["join_before_transfer"])

    def release_all(self, owner):
        with self._condition:
            for group in self._groups.values():
                for batch in group["batches"].values():
                    batch["members"].pop(owner, None)
            self._condition.notify_all()

    def _batch(self, token):
        if not token:
            return None
        key, batch_id = token
        return self._groups.get(key, {}).get("batches", {}).get(batch_id)

    @staticmethod
    def _active_batch(group):
        active = [
            (batch_id, batch)
            for batch_id, batch in group["batches"].items()
            if batch["claimed"] and not batch["done"] and batch["phase"] != "joined"
        ]
        return max(active, default=(None, None), key=lambda item: item[0])

    def _pending_batch(self, token, batch=None):
        batch = batch or self._batch(token)
        if not batch or batch["pending_batch_id"] is None:
            return None
        key, _ = token
        return self._groups.get(key, {}).get("batches", {}).get(
            batch["pending_batch_id"]
        )

    def _remove_member(self, owner, token):
        batch = self._batch(token)
        if batch:
            batch["members"].pop(owner, None)
            self._condition.notify_all()


# ── Jenkins Client (unchanged) ────────────────────────────────────────
class JenkinsClient:
    GET_RETRY_COUNT = 5
    RETRYABLE_GET_STATUS_CODES = frozenset({502, 503, 504})

    def __init__(self, url, username, password):
        self.requests = requests
        self.url = url.rstrip("/")
        self.session = self.requests.Session()
        self.session.auth = (username, password)
        self.session.verify = False
        self._cancel_lock = threading.Lock()
        self.requests.packages.urllib3.disable_warnings()

    def get_json(self, url, params=None):
        retryable_errors = (self.requests.ConnectionError, self.requests.Timeout)
        for retry_index in range(self.GET_RETRY_COUNT + 1):
            try:
                response = self.session.get(url, params=params, timeout=20)
            except retryable_errors:
                if retry_index >= self.GET_RETRY_COUNT:
                    raise
                time.sleep(min(2 ** retry_index, 5))
                continue

            if (
                response.status_code in self.RETRYABLE_GET_STATUS_CODES
                and retry_index < self.GET_RETRY_COUNT
            ):
                time.sleep(min(2 ** retry_index, 5))
                continue

            self._raise_for_status(response)
            return response.json()

        raise RuntimeError("Jenkins GET 重试流程异常结束")

    def verify_authentication(self):
        identity = self.get_json(self.url + "/whoAmI/api/json")
        if not isinstance(identity, dict) or identity.get("authenticated") is not True:
            raise RuntimeError("Jenkins 未确认登录身份，请检查账号和密码/API Token")
        if identity.get("anonymous") is True or identity.get("name") == "anonymous":
            raise RuntimeError("Jenkins 当前识别为匿名用户，请检查账号和密码/API Token")
        if not identity.get("name"):
            raise RuntimeError("Jenkins 未返回有效的登录身份，请检查服务器地址")

    @staticmethod
    def _raise_for_status(response):
        if response.ok:
            return
        text = response.text or ""
        title = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
        heading = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.I | re.S)
        detail_parts = []
        for match in (title, heading):
            if match:
                clean = re.sub(r"<[^>]+>", "", match.group(1))
                clean = " ".join(clean.split())
                if clean and clean not in detail_parts:
                    detail_parts.append(clean)
        detail = "；".join(detail_parts) or " ".join(text.split())
        if len(detail) > 800:
            detail = detail[:800] + "..."
        hints = {
            401: "身份认证失败，请检查账号和密码/API Token",
            403: "访问被拒绝，请检查账号权限或 Jenkins 访问策略",
        }
        hint = hints.get(response.status_code)
        message = f"Jenkins HTTP {response.status_code}"
        if hint:
            message += f"：{hint}\n"
        else:
            message += ": "
        message += f"{response.request.method} {response.url}"
        if detail:
            message += f"\n返回信息：{detail}"
        raise RuntimeError(message)

    def get_build_summary(self, path, *, job_url="", build_url=""):
        """Read one build's status and SCM changes without fetching parameters."""
        if not build_url:
            base = (job_url or jenkins_job_api_url(self.url, path)).rstrip("/")
            last_build = self.get_json(
                base + "/api/json", {"tree": "lastBuild[number,url]"}
            ).get("lastBuild")
            if not last_build:
                return None
            build_url = last_build.get("url") or f"{base}/{last_build['number']}/"
        items = "items[commitId,id,msg,author[fullName]]"
        data = self.get_json(build_url.rstrip("/") + "/api/json", {
            "tree": "number,url,building,result,timestamp,duration,"
                    "actions[causes[userName,shortDescription],"
                    "lastBuiltRevision[SHA1,branch[name]]],"
                    f"changeSet[{items}],changeSets[{items}]",
        })
        data.setdefault("url", build_url)
        return data

    def list_jobs(self, path):
        url = jenkins_job_api_url(self.url, path) + "/api/json"
        data = self.get_json(
            url, {"tree": "jobs[name,displayName,fullName,url,color,_class]"}
        )
        return data.get("jobs", [])

    def get_parameters(self, path, *, job_url=""):
        job_url = job_url.rstrip("/") or jenkins_job_api_url(self.url, path)
        url = job_url + "/api/json"
        tree = (
            "property[parameterDefinitions["
            "name,type,description,choices,defaultParameterValue[value]"
            "]]"
        )
        data = self.get_json(url, {"tree": tree})
        result = []
        for prop in data.get("property", []):
            for item in prop.get("parameterDefinitions", []):
                if item.get("type") == "GitParameterDefinition":
                    item["choices"] = self.get_git_parameter_choices(
                        job_url, item.get("name", "")
                    )
                result.append(item)
        return reorder_parameters(result, "/".join(path))

    def _get_git_parameter_choices(self, job_url, name):
        plugin = (
            "net.uaznia.lukanus.hudson.plugins.gitparameter."
            "GitParameterDefinition"
        )
        url = (
            job_url.rstrip("/")
            + "/descriptorByName/"
            + plugin
            + "/fillValueItems"
        )
        data = self.get_json(url, {"param": name})
        return [item["value"] for item in data.get("values", []) if item.get("value")]

    get_git_parameter_choices = _get_git_parameter_choices

    def _get_parameter_choices(self, path, name, *, job_url=""):
        """Read one ordinary choice list without querying Git branch plugins."""
        job_url = job_url.rstrip("/") or jenkins_job_api_url(self.url, path)
        data = self.get_json(job_url + "/api/json", {
            "tree": "property[parameterDefinitions[name,choices]]",
        })
        for prop in data.get("property", []):
            for parameter in prop.get("parameterDefinitions", []):
                if parameter.get("name") == name:
                    choices = parameter.get("choices")
                    if not isinstance(choices, list):
                        raise RuntimeError(f"Jenkins 未返回 {name} 的候选列表，请检查任务参数配置")
                    return choices
        raise RuntimeError(f"Jenkins 当前任务中未找到参数 {name}")

    def _crumb_headers(self):
        try:
            data = self.get_json(self.url + "/crumbIssuer/api/json")
            return {data["crumbRequestField"]: data["crumb"]}
        except (self.requests.RequestException, RuntimeError, KeyError):
            return {}

    def _post_action(self, url):
        response = self.session.post(
            url,
            headers=self._crumb_headers(),
            timeout=20,
        )
        if response.status_code not in (200, 201, 302, 404):
            self._raise_for_status(response)
        return response.status_code

    def cancel_build(self, phase, location, log=None):
        """Cancel a queued/running build and verify Jenkins has stopped it."""
        with self._cancel_lock:
            if not location:
                raise RuntimeError("尚未取得 Jenkins 队列或构建地址，无法发送远程取消请求")
            if phase == "queue":
                self._post_action(location.rstrip("/") + "/cancelQueue")
                for _ in range(10):
                    try:
                        data = self.get_json(location.rstrip("/") + "/api/json")
                    except RuntimeError as exc:
                        if "Jenkins HTTP 404" in str(exc):
                            return
                        raise
                    if data.get("cancelled"):
                        return
                    executable = data.get("executable")
                    if executable and executable.get("url"):
                        # The executor may claim the item while cancellation is in flight.
                        location = executable["url"]
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("已发送 Jenkins 队列取消请求，但未确认任务离开队列")

            build_url = location.rstrip("/")
            current = self.get_json(
                build_url + "/api/json", {"tree": "building,result,number,url"}
            )
            if current.get("building") is False:
                return
            actions = (("stop", 8), ("term", 6), ("kill", 6))
            for action, checks in actions:
                if log:
                    log(f"正在向 Jenkins 发送 /{action} 终止请求...")
                self._post_action(f"{build_url}/{action}")
                for _ in range(checks):
                    data = self.get_json(
                        build_url + "/api/json", {"tree": "building,result,number,url"}
                    )
                    if data.get("building") is False:
                        return
                    time.sleep(1)
            raise RuntimeError("Jenkins 已接收终止请求，但构建仍处于运行状态，请在 Jenkins 中检查")

    def build(self, path, parameters, job_url=None):
        job_url = (job_url or jenkins_job_api_url(self.url, path)).rstrip("/")
        endpoint = "/buildWithParameters" if parameters else "/build"
        normalized = {}
        for name, value in parameters.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            elif str(value).lower() in ("true", "false"):
                value = str(value).lower()
            normalized[name] = value
        response = self.session.post(
            job_url + endpoint,
            params={"delay": "0sec"},
            data=normalized,
            headers=self._crumb_headers(),
            timeout=20,
        )
        self._raise_for_status(response)
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("Jenkins 未返回队列地址")
        return location.rstrip("/")

    def wait_for_build(
        self, queue_url, log, progress=None, cancel_event=None, location=None
    ):
        if progress:
            progress(0, "排队中，等待 Jenkins 执行器...")
        if location:
            location("queue", queue_url)
        while True:
            if cancel_event and cancel_event.is_set():
                self.cancel_build("queue", queue_url, log)
                raise PipelineCancelled("Jenkins 排队已取消")
            data = self.get_json(queue_url + "/api/json")
            if data.get("cancelled"):
                raise PipelineCancelled("Jenkins 排队已取消")
            executable = data.get("executable")
            if executable:
                build_url = executable["url"].rstrip("/")
                if location:
                    location("jenkins", build_url)
                break
            log("等待 Jenkins 分配执行器...")
            time.sleep(2)
        offset = 0
        full_log = ""
        while True:
            if cancel_event and cancel_event.is_set():
                self.cancel_build("jenkins", build_url, log)
                raise PipelineCancelled("Jenkins 构建已终止")
            response = self.session.get(
                build_url + "/logText/progressiveText",
                params={"start": offset},
                timeout=30,
            )
            self._raise_for_status(response)
            chunk = response.text
            if chunk:
                full_log += chunk
                log(chunk.rstrip())
            offset = int(response.headers.get("X-Text-Size", offset))
            data = self.get_json(
                build_url + "/api/json",
                {
                    "tree": (
                        "building,result,number,url,timestamp,duration,"
                        "estimatedDuration"
                    )
                },
            )
            if progress and data.get("building"):
                estimated = int(data.get("estimatedDuration") or 0)
                elapsed = max(0, int(time.time() * 1000) - int(data.get("timestamp") or 0))
                percent = min(99, int(elapsed * 100 / estimated)) if estimated else 1
                progress(percent, f"Jenkins 正在构建：{percent}%")
            if not data.get("building"):
                if cancel_event and cancel_event.is_set():
                    raise PipelineCancelled("Jenkins 构建已终止")
                if progress:
                    progress(100, "Jenkins 构建完成：100%")
                return data, full_log
            time.sleep(2)


# ══════════════════════════════════════════════════════════════════════
#  QThread Worker
# ══════════════════════════════════════════════════════════════════════
class Worker(QThread):
    """General purpose worker thread that executes a callable."""
    finished = Signal()
    error = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self._fn()
        except Exception as exc:
            self.error.emit(exception_message(exc))
        finally:
            self.finished.emit()


# ══════════════════════════════════════════════════════════════════════
#  Global QSS Stylesheet
# ══════════════════════════════════════════════════════════════════════
GLOBAL_QSS = f"""
QMainWindow {{
    background-color: {COLORS['bg']};
}}
QWidget#CentralWidget,
QWidget#JenkinsPage, QWidget#DeployPage, QWidget#CloudDeployPage, QWidget#PackageTracePage,
QWidget#JenkinsScrollContent, QWidget#CloudDeployScrollContent, QWidget#PackageTraceScrollContent,
QWidget#DeployScrollContent, QStackedWidget#TaskWorkspaceStack {{
    background: transparent;
}}

QAbstractScrollArea, QAbstractScrollArea > QWidget,
QAbstractScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QFrame#Card, QFrame#DarkCard {{
    background: transparent;
    border: none;
}}

QLabel {{
    color: {COLORS['text_primary']};
}}
QLabel#CardTitle {{
    font-family: "{FONT_FAMILY}";
    font-size: 14px;
    font-weight: 600;
    color: #F1F5F9;
}}

/* ── Inputs ────────────────────────────────────────────── */
QLineEdit, QPlainTextEdit {{
    background: {COLORS['input_bg']};
    border: 1px solid {COLORS['input_border']};
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 13px;
    font-family: "{FONT_FAMILY}";
    color: {COLORS['text_primary']};
    selection-background-color: {COLORS['accent_light']};
    selection-color: #FFFFFF;
}}
QLineEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {COLORS['border_focus']};
    background: rgba(12, 18, 32, 0.85);
}}
QLineEdit:disabled, QPlainTextEdit:disabled {{
    background: {COLORS['disabled_bg']};
    border: 1px solid {COLORS['disabled_border']};
    color: {COLORS['disabled_text']};
}}

/* ── ComboBox ──────────────────────────────────────────── */
QComboBox {{
    background: {COLORS['input_bg']};
    border: 1px solid {COLORS['input_border']};
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 13px;
    font-family: "{FONT_FAMILY}";
    color: {COLORS['text_primary']};
    min-height: 20px;
    selection-background-color: {COLORS['accent_light']};
}}
QComboBox:focus, QComboBox:on {{
    border: 1px solid {COLORS['border_focus']};
    background: rgba(12, 18, 32, 0.85);
}}
QComboBox:disabled {{
    background: {COLORS['disabled_bg']};
    border: 1px solid {COLORS['disabled_border']};
    color: {COLORS['disabled_text']};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 36px;
    border: none;
    background: transparent;
}}
QComboBox::down-arrow {{
    image: none;
    width: 0;
    height: 0;
}}
QComboBox QAbstractItemView {{
    background: {COLORS['popup_bg']};
    color: #FFFFFF;
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {COLORS['accent_light']};
    selection-color: #FFFFFF;
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    min-height: 32px;
    padding: 4px 10px;
    color: #FFFFFF;
    background: transparent;
    border-radius: 6px;
}}
QComboBox QAbstractItemView::item:hover {{
    color: #FFFFFF;
    background: rgba(255, 255, 255, 0.08);
}}
QComboBox QAbstractItemView::item:selected {{
    color: #FFFFFF;
    background: {COLORS['sidebar_selected']};
}}
QComboBox QAbstractItemView::item:disabled {{
    color: #94A3B8;
}}

/* ── Buttons ───────────────────────────────────────────── */
QPushButton, QToolButton#IconButton {{
    font-family: "{FONT_FAMILY}";
    font-size: 13px;
    border-radius: 8px;
    padding: 7px 14px;
    background: rgba(255, 255, 255, 0.07);
    border: 1px solid rgba(255, 255, 255, 0.16);
    color: {COLORS['text_primary']};
    min-width: 52px;
    min-height: 28px;
}}
QPushButton:hover, QToolButton#IconButton:hover {{
    background: rgba(255, 255, 255, 0.13);
    border-color: rgba(255, 255, 255, 0.32);
    color: #FFFFFF;
}}
QPushButton:pressed, QToolButton#IconButton:pressed {{
    background: rgba(255, 255, 255, 0.19);
    border-color: rgba(255, 255, 255, 0.42);
    padding: 8px 14px 6px 14px;
}}
QPushButton:focus, QToolButton#IconButton:focus {{
    border: 1px solid {COLORS['border_focus']};
}}
QPushButton:disabled, QToolButton#IconButton:disabled {{
    background: {COLORS['disabled_bg']};
    border: 1px solid {COLORS['disabled_border']};
    color: {COLORS['disabled_text']};
}}
QPushButton#PrimaryBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {COLORS['primary_gradient_start']}, stop:1 {COLORS['primary_gradient_end']});
    border: 1px solid rgba(255, 255, 255, 0.30);
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton#PrimaryBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {COLORS['primary_hover_start']}, stop:1 {COLORS['primary_hover_end']});
    border-color: rgba(255, 255, 255, 0.50);
    color: #FFFFFF;
}}
QPushButton#PrimaryBtn:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {COLORS['primary_pressed']}, stop:1 #4338CA);
    border-color: rgba(255, 255, 255, 0.25);
}}
QPushButton#PrimaryBtn:disabled {{
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    color: {COLORS['disabled_text']};
}}
QPushButton#SuccessBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #047857, stop:1 #0F766E);
    border: 1px solid rgba(255, 255, 255, 0.30);
    color: #FFFFFF;
    font-weight: 600;
}}
QPushButton#SuccessBtn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #06775A, stop:1 #0E726C);
    border-color: rgba(255, 255, 255, 0.50);
    color: #FFFFFF;
}}
QPushButton#SuccessBtn:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #065F46, stop:1 #075E57);
    border-color: rgba(255, 255, 255, 0.25);
}}
QPushButton#SuccessBtn:disabled, QPushButton#SuccessBtn:!enabled {{
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    color: {COLORS['disabled_text']};
}}
QPushButton#GhostBtn {{
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: {COLORS['text_primary']};
}}
QPushButton#GhostBtn:hover {{
    background: rgba(96, 165, 250, 0.16);
    border-color: rgba(96, 165, 250, 0.45);
    color: #93C5FD;
}}
QPushButton#GhostBtn:pressed {{
    background: rgba(96, 165, 250, 0.24);
}}
QPushButton#DangerBtn, QPushButton#DangerGhostBtn {{
    background: {COLORS['danger_bg']};
    border: 1px solid {COLORS['danger_border']};
    color: #FCA5A5;
    border-radius: 8px;
}}
QPushButton#DangerBtn:hover, QPushButton#DangerGhostBtn:hover {{
    background: rgba(239, 68, 68, 0.22);
    border-color: rgba(239, 68, 68, 0.55);
    color: #FFFFFF;
}}
QPushButton#DangerBtn:pressed, QPushButton#DangerGhostBtn:pressed {{
    background: rgba(239, 68, 68, 0.32);
}}
QPushButton#DangerBtn:disabled, QPushButton#DangerGhostBtn:disabled,
QPushButton#DangerGhostBtn:!enabled {{
    background: {COLORS['disabled_bg']};
    border: 1px solid {COLORS['disabled_border']};
    color: {COLORS['disabled_text']};
}}

/* ── Tab Widget ────────────────────────────────────────── */
QTabWidget#MainTabs {{
    background: transparent;
    border: none;
}}
QTabWidget::pane {{
    background: transparent;
    border: none;
}}
QTabBar {{
    background: transparent;
}}
QTabBar::tab {{
    background: transparent;
    color: {COLORS['text_muted']};
    padding: 10px 22px;
    font-size: 13px;
    font-family: "{FONT_FAMILY}";
    font-weight: 500;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 4px;
}}
QTabBar::tab:selected {{
    color: {COLORS['text_primary']};
    font-weight: 600;
    border-bottom: 2px solid {COLORS['accent']};
}}
QTabBar::tab:hover:!selected {{
    color: #FFFFFF;
    background: rgba(255, 255, 255, 0.05);
    border-radius: 6px 6px 0 0;
}}

/* ── Tree Widget ───────────────────────────────────────── */
QTreeWidget {{
    background: transparent;
    border: none;
    outline: none;
    font-size: 13px;
    font-family: "{FONT_FAMILY}";
    color: {COLORS['text_primary']};
}}
QTreeWidget::item {{
    padding: 7px 8px;
    border-radius: 6px;
    margin: 1px 4px;
}}
QTreeWidget::item:selected {{
    background: {COLORS['sidebar_selected']};
    color: #FFFFFF;
}}
QTreeWidget::item:hover:!selected {{
    background: rgba(255, 255, 255, 0.08);
}}
QHeaderView::section {{
    background: rgba(10, 15, 26, 0.70);
    color: {COLORS['text_secondary']};
    font-size: 12px;
    font-weight: 600;
    padding: 6px 10px;
    border: none;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}}

QSplitter::handle {{
    background: transparent;
}}

QMessageBox, QDialog {{
    background: #0E1322;
}}
QMessageBox QLabel {{
    color: {COLORS['text_primary']};
    min-width: 280px;
}}

/* ── Progress Bar ──────────────────────────────────────── */
QProgressBar {{
    background: rgba(10, 15, 26, 0.70);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 4px;
    max-height: 8px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2563EB, stop:1 #34D399);
    border-radius: 3px;
}}

/* ── Log Text ──────────────────────────────────────────── */
QPlainTextEdit#LogText {{
    background: transparent;
    color: {COLORS['log_fg']};
    border: none;
    font-family: "{FONT_MONO}";
    font-size: 11px;
    padding: 8px 12px;
    selection-background-color: {COLORS['accent_light']};
    selection-color: #FFFFFF;
}}

/* ── Scroll Bars ───────────────────────────────────────── */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: rgba(255, 255, 255, 0.15);
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: rgba(255, 255, 255, 0.28);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::horizontal {{
    background: transparent;
    height: 8px;
}}
QScrollBar::handle:horizontal {{
    background: rgba(255, 255, 255, 0.15);
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: rgba(255, 255, 255, 0.28);
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ── CheckBox ──────────────────────────────────────────── */
QCheckBox {{
    font-family: "{FONT_FAMILY}";
    font-size: 13px;
    color: {COLORS['text_primary']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1px solid rgba(255, 255, 255, 0.20);
    background: rgba(10, 15, 26, 0.70);
}}
QCheckBox::indicator:hover {{
    border-color: {COLORS['accent']};
}}
QCheckBox::indicator:checked {{
    background: {COLORS['primary_bg']};
    border-color: {COLORS['accent']};
    image: url("{(resource_path('assets/lucide/check.svg')).as_posix()}");
}}

/* ── Tooltips ──────────────────────────────────────────── */
QToolTip {{
    background: #182238;
    color: #F8FAFC;
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    font-family: "{FONT_FAMILY}";
}}

/* ── Menus ─────────────────────────────────────────────── */
QMenu {{
    background: {COLORS['popup_bg']};
    color: {COLORS['text_primary']};
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    color: {COLORS['text_primary']};
    padding: 7px 18px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background: {COLORS['accent_light']};
    color: #FFFFFF;
}}
"""


# ══════════════════════════════════════════════════════════════════════
#  Helper Widgets
# ══════════════════════════════════════════════════════════════════════

def add_card_shadow(widget):
    """Attach a subtle drop-shadow to a card widget."""
    # Dark operational surfaces use borders for hierarchy; shadows reduce clarity.
    return


class CardWidget(GlassPanel):
    """Frosted-glass rounded card with 16px radius, hover glow and optional title."""

    def __init__(self, title=None, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("Card")
        add_card_shadow(self)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 14, 16, 14)
        self._layout.setSpacing(10)

        if title:
            title_label = QLabel(title)
            title_label.setObjectName("CardTitle")
            title_label.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}";
                font-size: 14px;
                font-weight: 600;
                color: {COLORS['text_primary']};
                background: transparent;
                padding: 0;
            """)
            self._layout.addWidget(title_label)

    def content_layout(self):
        return self._layout


class ToggleSwitch(QCheckBox):
    """Accessible checkbox semantics rendered as a compact sliding switch."""

    CONTROL_WIDTH = 148
    CONTROL_HEIGHT = 40
    TRACK_X = 4
    TRACK_WIDTH = 46
    TRACK_HEIGHT = 24
    TEXT_X = 62

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.CONTROL_WIDTH, self.CONTROL_HEIGHT)

    def sizeHint(self):
        return QSize(self.CONTROL_WIDTH, self.CONTROL_HEIGHT)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        track = QRectF(
            self.TRACK_X,
            (self.height() - self.TRACK_HEIGHT) / 2,
            self.TRACK_WIDTH,
            self.TRACK_HEIGHT,
        )
        checked = self.isChecked()
        enabled = self.isEnabled()
        track_color = COLORS["success"] if checked else "#162032"
        border_color = COLORS["success"] if checked else "#30415A"
        if not enabled:
            track_color = "#1D283A"
            border_color = "#2A3A52"

        painter.setPen(QPen(QColor(border_color), 1.5))
        painter.setBrush(QBrush(QColor(track_color)))
        painter.drawRoundedRect(track, 12, 12)

        knob_size = 18
        knob_x = track.right() - knob_size - 3 if checked else track.left() + 3
        knob = QRectF(knob_x, track.top() + 3, knob_size, knob_size)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#FFFFFF" if enabled else COLORS["text_muted"])))
        painter.drawEllipse(knob)

        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(COLORS["border_focus"]), 2))
            painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), 14, 14)

        painter.setPen(QColor(
            COLORS["text_primary"] if enabled else COLORS["text_muted"]
        ))
        painter.setFont(self.font())
        painter.drawText(
            QRectF(
                self.TEXT_X,
                0,
                max(0, self.width() - self.TEXT_X),
                self.height(),
            ),
            Qt.AlignLeft | Qt.AlignVCenter,
            self.text(),
        )


class SuccessIcon(QWidget):
    """Paint a crisp success mark without relying on an emoji or icon font."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 44)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLORS["success_bg"]))
        painter.drawEllipse(1, 1, 42, 42)

        pen = QPen(QColor(COLORS["success"]), 3)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPolyline(QPolygon([QPoint(12, 23), QPoint(19, 30), QPoint(33, 15)]))


class ErrorIcon(QWidget):
    """Paint an error mark that remains crisp on high-DPI displays."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 44)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(64, 28, 37))
        painter.drawEllipse(1, 1, 42, 42)

        pen = QPen(QColor(COLORS["danger"]), 3)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPoint(14, 14), QPoint(30, 30))
        painter.drawLine(QPoint(30, 14), QPoint(14, 30))


class InAppMessageOverlay(QWidget):
    """Modal message layer rendered inside the main application window."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("MessageOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.hide()
        parent.installEventFilter(self)

        self.setStyleSheet(f"""
            QWidget#MessageOverlay {{
                background: rgba(3, 8, 18, 178);
            }}
            QFrame#MessageCard {{
                background: {COLORS['card']};
                border: 1px solid {COLORS['border_focus']};
                border-radius: 14px;
            }}
            QLabel#MessageTitle {{
                color: {COLORS['text_primary']};
                font-family: "{FONT_FAMILY}";
                font-size: 18px;
                font-weight: 600;
                background: transparent;
            }}
            QLabel#MessageText {{
                color: {COLORS['text_secondary']};
                font-family: "{FONT_FAMILY}";
                font-size: 14px;
                line-height: 1.5;
                background: transparent;
            }}
        """)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(24, 24, 24, 24)
        outer_layout.addStretch()

        self.card = QFrame(self)
        self.card.setObjectName("MessageCard")
        self.card.setMinimumWidth(420)
        self.card.setMaximumWidth(620)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(24, 22, 24, 20)
        card_layout.setSpacing(16)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)
        self.success_icon = SuccessIcon(self.card)
        self.error_icon = ErrorIcon(self.card)
        self.error_icon.hide()
        header_layout.addWidget(self.success_icon, 0, Qt.AlignTop)
        header_layout.addWidget(self.error_icon, 0, Qt.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(6)
        self.title_label = QLabel("完成", self.card)
        self.title_label.setObjectName("MessageTitle")
        self.message_label = QLabel(self.card)
        self.message_label.setObjectName("MessageText")
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.message_label)
        header_layout.addLayout(text_layout, 1)
        card_layout.addLayout(header_layout)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.choice_button = QPushButton("中断并重新合并", self.card)
        self.choice_button.setObjectName("DangerGhostBtn")
        self.choice_button.setMinimumSize(132, 44)
        self.choice_button.hide()
        self.choice_button.clicked.connect(
            lambda: self._finish_choice("interrupt")
        )
        button_layout.addWidget(self.choice_button)
        self.ok_button = QPushButton("确定", self.card)
        self.ok_button.setObjectName("PrimaryBtn")
        self.ok_button.setMinimumSize(96, 44)
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.dismiss)
        button_layout.addWidget(self.ok_button)
        card_layout.addLayout(button_layout)

        outer_layout.addWidget(self.card, 0, Qt.AlignHCenter)
        outer_layout.addStretch()

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._animation = QPropertyAnimation(self._opacity, b"opacity", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self.dismiss)
        self._choice_loop = None
        self._choice_result = None

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Resize:
            self.setGeometry(watched.rect())
        return super().eventFilter(watched, event)

    def show_message(self, title, text, kind="success"):
        self.choice_button.hide()
        self.ok_button.setText("确定")
        self._choice_result = None
        is_error = kind == "error"
        self.success_icon.setVisible(not is_error)
        self.error_icon.setVisible(is_error)
        border_color = COLORS["danger"] if is_error else COLORS["border_focus"]
        self.card.setStyleSheet(
            f"QFrame#MessageCard {{ border-color: {border_color}; }}"
        )
        self.ok_button.setObjectName("DangerGhostBtn" if is_error else "PrimaryBtn")
        self.ok_button.style().unpolish(self.ok_button)
        self.ok_button.style().polish(self.ok_button)
        self.title_label.setText(title)
        self.message_label.setText(text)
        self.setAccessibleName(f"{title}：{text}")
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self._animation.stop()
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()
        self.ok_button.setFocus(Qt.OtherFocusReason)

    def ask_batch_interruption(self, text):
        """Show a blocking choice inside this window; closing keeps the current pull."""
        self.show_message("同环境正在拉取镜像", text, kind="error")
        self.choice_button.show()
        self.ok_button.setText("继续当前轮（推荐）")
        self._choice_result = "continue"
        self._choice_loop = QEventLoop(self)
        self._choice_loop.exec()
        result = self._choice_result or "continue"
        self._choice_loop = None
        return result

    def _finish_choice(self, result):
        self._choice_result = result
        self._animation.stop()
        self.hide()
        if self._choice_loop and self._choice_loop.isRunning():
            self._choice_loop.quit()

    @Slot()
    def dismiss(self):
        self._animation.stop()
        self.hide()
        if self._choice_loop and self._choice_loop.isRunning():
            self._choice_result = self._choice_result or "continue"
            self._choice_loop.quit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)


class CollapsibleCardWidget(CardWidget):
    """Card with a compact header and content that can be expanded on demand."""

    expanded_changed = Signal(bool)

    def __init__(self, title, expanded=False, parent=None):
        super().__init__(parent=parent)
        root_layout = super().content_layout()

        self._toggle = QToolButton(self)
        self._toggle.setText(title)
        self._toggle.setCheckable(True)
        self._toggle.setAccessibleName(f"展开或收起{title}")
        self._toggle.setChecked(expanded)
        self._toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self._toggle.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                border: none;
                color: {COLORS['text_primary']};
                font-family: "{FONT_FAMILY}";
                font-size: 15px;
                font-weight: bold;
                padding: 2px 0;
                text-align: left;
            }}
            QToolButton:hover {{
                color: {COLORS['accent_hover']};
            }}
            QToolButton:focus {{
                color: {COLORS['accent_hover']};
                border: 1px solid {COLORS['border_focus']};
                border-radius: 4px;
            }}
        """)
        root_layout.addWidget(self._toggle)

        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 4, 0, 0)
        self._content_layout.setSpacing(8)
        self._content.setVisible(expanded)
        root_layout.addWidget(self._content)

        self._toggle.toggled.connect(self.set_expanded)

    def content_layout(self):
        return self._content_layout

    @Slot(bool)
    def set_expanded(self, expanded):
        if self._toggle.isChecked() != expanded:
            self._toggle.blockSignals(True)
            self._toggle.setChecked(expanded)
            self._toggle.blockSignals(False)
        self._toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self._content.setVisible(expanded)
        self.updateGeometry()
        self.expanded_changed.emit(expanded)


class DeploymentPackageInput(QPlainTextEdit):
    returnPressed = Signal()
    editingFinished = Signal()

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.textChanged.connect(self._resize_to_lines)
        self.setText(text)

    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self.setPlainText(text)
        self.setModified(False)

    def isModified(self):
        return self.document().isModified()

    def setModified(self, modified):
        self.document().setModified(modified)

    def _resize_to_lines(self):
        lines = min(4, max(1, self.document().blockCount()))
        self.setFixedHeight(max(44, lines * self.fontMetrics().lineSpacing() + 32))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.returnPressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.editingFinished.emit()


class DeleteApplyOption(QWidget):
    """One task option with a visible command, reusable on both SSH pages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.checkbox = ToggleSwitch("删除后重新应用")
        self.checkbox.setFixedWidth(168)
        self.checkbox.setAccessibleName("部署前删除当前 YAML 资源后重新应用")
        layout.addWidget(self.checkbox)
        self.command = QLineEdit()
        self.command.setAccessibleName("删除命令（可编辑，随 YAML 自动匹配）")
        self.command.setMinimumWidth(0)
        self.command.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout.addWidget(self.command, 1)

    def set_compact(self, compact):
        self.command.setVisible(not compact)

    def set_target(self, directory, filename, command=None):
        if command is None:
            command = _yaml_delete_command(filename) if filename.strip() else ""
        self.command.setPlaceholderText("请先选择 YAML 文件" if not filename.strip() else "请输入删除命令")
        if self.command.text() != command:
            self.command.setText(command)
            self.command.setCursorPosition(0)
        self.setToolTip(
            "自动记住开关状态。开启后会删除当前 YAML 中的资源再应用，期间服务可能短暂中断。\n"
            f"目录：{directory}\n{command}"
        )
        self.command.setToolTip(f"可编辑；本次任务按环境和 YAML 保留修改，部署时执行此命令。\n目录：{directory}\n{command}")


class StatusBadge(QFrame):
    """A small rounded badge for status indicators."""

    _PALETTE = {
        "success": (COLORS["success_bg"], COLORS["success"]),
        "danger": (COLORS["danger_bg"], COLORS["danger"]),
        "info": (COLORS["badge_blue"], COLORS["badge_blue_text"]),
        "muted": (COLORS["sidebar_bg"], COLORS["text_secondary"]),
    }

    def __init__(self, text="就绪", color="success", parent=None):
        super().__init__(parent)
        self._label = QLabel(text, self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 10, 2)
        layout.addWidget(self._label)
        self.set(text, color)

    def set(self, text, color="success"):
        bg, fg = self._PALETTE.get(color, self._PALETTE["muted"])
        self.setStyleSheet(f"""
            QFrame {{
                background: {bg};
                border-radius: 6px;
            }}
        """)
        self._label.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}";
            font-size: 12px;
            color: {fg};
            background: transparent;
        """)
        self._label.setText(text)


class ComboArrowIndicator(QWidget):
    """Font-independent arrow indicator used by styled combo boxes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_expanded(self, expanded):
        self._expanded = expanded
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLORS['text_secondary']))
        center_x = self.width() // 2
        center_y = self.height() // 2
        if self._expanded:
            points = [
                QPoint(center_x - 7, center_y + 4),
                QPoint(center_x + 7, center_y + 4),
                QPoint(center_x, center_y - 5),
            ]
        else:
            points = [
                QPoint(center_x - 7, center_y - 4),
                QPoint(center_x + 7, center_y - 4),
                QPoint(center_x, center_y + 5),
            ]
        painter.drawPolygon(QPolygon(points))


class ClearTreeBranchStyle(QProxyStyle):
    """Draw frameless chevrons centered in each job-tree branch cell."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PE_IndicatorBranch:
            return super().drawPrimitive(element, option, painter, widget)
        if not option.state & QStyle.State_Children:
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        # Floating-point centers avoid the half-pixel bias of QRect.center()
        # on even-sized rows, including when Windows scales the interface.
        center = QRectF(option.rect).center()
        color = COLORS['text_primary'] if option.state & QStyle.State_Selected else COLORS['text_secondary']
        pen = QPen(QColor(color), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        center_x = center.x()
        center_y = center.y()
        path = QPainterPath()
        if option.state & QStyle.State_Open:
            path.moveTo(center_x - 4, center_y - 2)
            path.lineTo(center_x, center_y + 2)
            path.lineTo(center_x + 4, center_y - 2)
        else:
            path.moveTo(center_x - 2, center_y - 4)
            path.lineTo(center_x + 2, center_y)
            path.lineTo(center_x - 2, center_y + 4)
        painter.drawPath(path)
        painter.restore()


class SearchableComboBox(QComboBox):
    """Combo box with optional search, a clear arrow and wheel-safe behavior."""

    def __init__(self, parent=None, editable=True):
        super().__init__(parent)
        self.setEditable(editable)
        self.setInsertPolicy(QComboBox.NoInsert)
        if editable:
            completer = QCompleter(self)
            completer.setFilterMode(Qt.MatchContains)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            self.setCompleter(completer)

        self._arrow_indicator = ComboArrowIndicator(self)
        self._all_values = []
        self._apply_popup_theme()

    def _apply_popup_theme(self):
        """Keep popup item text readable across native Windows Qt styles."""
        view = self.view()
        palette = view.palette()
        popup_bg = COLORS.get("popup_bg", "#131929")
        popup_border = COLORS.get("popup_border", "#2D3748")
        sel_bg = COLORS.get("sidebar_selected", "#1D4ED8")
        palette.setColor(QPalette.Base, QColor(popup_bg))
        palette.setColor(QPalette.Window, QColor(popup_bg))
        palette.setColor(QPalette.Text, QColor("#FFFFFF"))
        palette.setColor(QPalette.WindowText, QColor("#FFFFFF"))
        palette.setColor(QPalette.Highlight, QColor(sel_bg))
        palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#94A3B8"))
        view.setPalette(palette)
        view.setStyleSheet(f"""
            QAbstractItemView {{
                background: {popup_bg};
                color: #FFFFFF;
                border: 1px solid {popup_border};
                selection-background-color: {sel_bg};
                selection-color: #FFFFFF;
                outline: none;
            }}
            QAbstractItemView::item {{
                min-height: 34px;
                padding: 4px 10px;
                color: #FFFFFF;
            }}
            QAbstractItemView::item:hover {{
                background: #1E293B;
                color: #FFFFFF;
            }}
            QAbstractItemView::item:selected {{
                background: {sel_bg};
                color: #FFFFFF;
            }}
        """)
        if self.completer():
            self.completer().popup().setPalette(palette)
            self.completer().popup().setStyleSheet(view.styleSheet())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrow_indicator.setGeometry(max(0, self.width() - 38), 0, 32, self.height())
        self._arrow_indicator.raise_()

    def showPopup(self):
        self._apply_popup_theme()
        self._arrow_indicator.set_expanded(True)
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        self._arrow_indicator.set_expanded(False)

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()

    def set_values(self, values):
        self._all_values = list(values)
        current = self.currentText()
        self.clear()
        self.addItems(self._all_values)
        if current in self._all_values:
            self.setCurrentText(current)
        if self.completer():
            self.completer().setModel(self.model())

    def values(self):
        return list(self._all_values)


class ClickExpandTreeWidget(QTreeWidget):
    """Expand or collapse folders when their row body is activated."""

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        body_click = False
        was_expanded = False
        if item is not None and item.childCount():
            item_rect = self.visualItemRect(item)
            body_click = event.position().x() >= item_rect.left()
            was_expanded = item.isExpanded()
        super().mousePressEvent(event)
        if body_click:
            item.setExpanded(not was_expanded)

    def keyPressEvent(self, event):
        item = self.currentItem()
        if (
            item is not None
            and item.childCount()
            and event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space)
        ):
            item.setExpanded(not item.isExpanded())
            event.accept()
            return
        super().keyPressEvent(event)


class JenkinsJobBrowser(QWidget):
    """Shared task picker with independent selection and a shared cache supplied by its owner."""

    def __init__(self, parent=None, *, searchable=True):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.tree = ClickExpandTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(26)
        self.branch_style = ClearTreeBranchStyle()
        self.tree.setStyle(self.branch_style)

        if searchable:
            search_row = QHBoxLayout()
            search_row.setSpacing(6)
            self.search = QLineEdit()
            self.search.setPlaceholderText("搜索二级模块...")
            self.search.setClearButtonEnabled(False)
            self.search.setFixedHeight(34)
            self.search.setAccessibleName("任务模糊查询")
            self.search.setEnabled(False)
            search_row.addWidget(self.search, 1)
            self.refresh = QToolButton()
            self.refresh.setIcon(
                ui_icon("refresh-cw")
            )
            self.refresh.setIconSize(QSize(18, 18))
            self.refresh.setFixedSize(34, 34)
            self.refresh.setAccessibleName("重置 Jenkins 任务缓存")
            self.refresh.setToolTip(
                "刷新任务树；构建参数和分支在点击任务时读取并缓存"
            )
            self.refresh.setStyleSheet(f"""
                QToolButton {{
                    background: {COLORS['input_bg']};
                    border: 1px solid {COLORS['input_border']};
                    border-radius: 6px;
                }}
                QToolButton:hover {{
                    background: {COLORS['accent_light']};
                    border-color: {COLORS['border_focus']};
                }}
                QToolButton:pressed {{
                    background: {COLORS['card_hover']};
                }}
                QToolButton:focus {{
                    border: 2px solid {COLORS['border_focus']};
                }}
                QToolButton:disabled {{
                    background: {COLORS['sidebar_bg']};
                    border-color: {COLORS['border']};
                }}
            """)
            self.refresh.setEnabled(False)
            search_row.addWidget(self.refresh)
            layout.addLayout(search_row)
        layout.addWidget(self.tree)


# ══════════════════════════════════════════════════════════════════════
#  Environment Dialog (PySide6)
# ══════════════════════════════════════════════════════════════════════
def _environment_validation_error(data, dashboard_valid):
    if not all(data.get(key) for key in ("name", "host", "username")):
        return "缺少信息", "环境名称、IP/主机和账号不能为空"
    if not dashboard_valid:
        return "Dashboard 无效", "请选择已保存的 Dashboard"
    try:
        if not 1 <= int(data.get("port", "")) <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return "端口无效", "SSH 端口必须是 1-65535"
    return None


class EnvironmentDialog(QDialog):
    def __init__(self, parent, title, initial=None, dashboard_names=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.setModal(True)
        self.result = None
        initial = initial or {}
        dashboard_names = list(dashboard_names or [])
        self.vars = {}

        self.setStyleSheet(f"""
            QDialog {{
                background: {COLORS['bg']};
            }}
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 20, 24, 24)
        main_layout.setSpacing(12)

        # Title
        title_label = QLabel(title)
        title_label.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}";
            font-size: 18px;
            font-weight: bold;
            color: {COLORS['text_primary']};
        """)
        main_layout.addWidget(title_label)

        # Card
        card = CardWidget(parent=self)
        main_layout.addWidget(card)

        grid = QGridLayout()
        grid.setSpacing(8)
        card.content_layout().addLayout(grid)

        for row, (label, key) in enumerate(ENVIRONMENT_FIELDS):
            default = DEFAULT_DEPLOY.get(key, "22" if key == "port" else "")
            value = initial.get(key, default)
            if key == "password" and value:
                value = unprotect_secret(value)

            lbl = QLabel(label)
            lbl.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}";
                font-size: 13px;
                color: {COLORS['text_secondary']};
                background: transparent;
            """)
            grid.addWidget(lbl, row, 0, Qt.AlignLeft)

            if key == "dashboard":
                widget = SearchableComboBox(editable=False)
                widget.set_values(dashboard_names)
                if value in dashboard_names:
                    widget.setCurrentText(value)
                else:
                    widget.setCurrentText("")
                self.dashboard_widget = widget
            else:
                widget = QLineEdit()
                widget.setText(value)
                if key == "password":
                    widget.setEchoMode(QLineEdit.Password)
            grid.addWidget(widget, row, 1)
            self.vars[key] = widget

        grid.setColumnStretch(1, 1)

        # Action buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("GhostBtn")
        cancel_btn.setFixedSize(80, 36)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("PrimaryBtn")
        save_btn.setFixedSize(80, 36)
        save_btn.setIcon(ui_icon("save"))
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)

        card.content_layout().addLayout(btn_layout)

    def _get_value(self, key):
        widget = self.vars[key]
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        return widget.text().strip()

    def _save(self):
        data = {key: self._get_value(key) for key in self.vars}
        error = _environment_validation_error(data, bool(data["dashboard"]))
        if error:
            QMessageBox.warning(self, *error)
            return
        data["password"] = protect_secret(data["password"])
        self.result = data
        self.accept()


class DashboardDialog(QDialog):
    FIELDS = [
        ("名称", "name", "text"),
        ("环境 URL", "env_url", "text"),
        ("环境账号", "env_username", "text"),
        ("环境密码", "env_password", "password"),
        ("Dashboard 地址", "url", "text"),
        ("Dashboard Token", "token", "password"),
    ]

    def __init__(self, parent, title, initial=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.setModal(True)
        self.result = None
        self.vars = {}
        initial = initial or {}

        self.setStyleSheet(f"""
            QDialog {{
                background: {COLORS['bg']};
            }}
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 20, 24, 24)
        main_layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}";
            font-size: 18px;
            font-weight: bold;
            color: {COLORS['text_primary']};
        """)
        main_layout.addWidget(title_label)

        card = CardWidget(parent=self)
        main_layout.addWidget(card)

        grid = QGridLayout()
        grid.setSpacing(8)
        card.content_layout().addLayout(grid)

        for row, (label, key, kind) in enumerate(self.FIELDS):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}";
                font-size: 13px;
                color: {COLORS['text_secondary']};
                background: transparent;
            """)
            grid.addWidget(lbl, row, 0, Qt.AlignLeft)

            widget = QLineEdit()
            value = initial.get(key, "")
            if kind == "password":
                value = unprotect_secret(value)
                widget.setEchoMode(QLineEdit.Password)
            widget.setText(value)
            grid.addWidget(widget, row, 1)
            self.vars[key] = widget

        grid.setColumnStretch(1, 1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("GhostBtn")
        cancel_btn.setFixedSize(80, 36)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("PrimaryBtn")
        save_btn.setFixedSize(80, 36)
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)

        card.content_layout().addLayout(btn_layout)

    def _save(self):
        data = {key: widget.text().strip() for key, widget in self.vars.items()}
        if not data["name"] or not data["url"]:
            QMessageBox.warning(self, "缺少信息", "名称和 Dashboard 地址不能为空")
            return
        data["env_password"] = protect_secret(data["env_password"])
        data["token"] = protect_secret(data["token"])
        self.result = data
        self.accept()


# ══════════════════════════════════════════════════════════════════════
#  Custom Title Bar
# ══════════════════════════════════════════════════════════════════════
class TitleBar(QWidget):
    """Custom frameless title bar with drag support."""

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self._parent_window = parent_window
        self._drag_pos = None
        self.setFixedHeight(56)
        self.setObjectName("TitleBar")
        self.setStyleSheet(f"""
            QWidget#TitleBar {{
                background: {COLORS['title_bar']};
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 0, 18, 0)
        layout.setSpacing(10)

        # Stable vector-like product mark instead of a font glyph.
        mark = QFrame()
        mark.setFixedSize(10, 28)
        mark.setStyleSheet(f"background: {COLORS['success']}; border-radius: 5px;")
        layout.addWidget(mark)

        # Keep the active task identity visible while workspaces share one window.
        title = QLabel("Deploy Console 任务中心版")
        title.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}";
            font-size: 17px;
            font-weight: 600;
            color: {COLORS['text_primary']};
            background: transparent;
        """)
        layout.addWidget(title)

        task_badge = StatusBadge(
            f"任务 {parent_window.task_number}", "info", self
        )
        task_badge.setAccessibleName(
            f"并行部署任务 {parent_window.task_number}"
        )
        layout.addWidget(task_badge)

        # Subtitle
        subtitle = QLabel("Jenkins 构建  /  环境发布  /  云端部署")
        subtitle.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}";
            font-size: 12px;
            color: {COLORS['text_muted']};
            background: transparent;
        """)
        layout.addWidget(subtitle)

        layout.addStretch()

        # Loading indicator area
        self.loading_label = QLabel("")
        self.loading_label.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}";
            font-size: 12px;
            color: {COLORS['text_secondary']};
            background: transparent;
        """)
        layout.addWidget(self.loading_label)

        self.loading_bar = QProgressBar()
        self.loading_bar.setFixedSize(120, 4)
        self.loading_bar.setRange(0, 0)  # indeterminate
        self.loading_bar.setTextVisible(False)
        self.loading_bar.hide()
        layout.addWidget(self.loading_bar)

        header_button_style = f"""
            QToolButton {{
                background: {COLORS['input_bg']};
                border: 1px solid {COLORS['input_border']};
                border-radius: 7px;
                color: {COLORS['text_primary']};
                padding: 0 10px;
            }}
            QToolButton:hover {{
                background: {COLORS['accent_light']};
                border-color: {COLORS['border_focus']};
            }}
            QToolButton:focus {{
                border: 2px solid {COLORS['border_focus']};
            }}
        """
        self.new_task_button = QToolButton(self)
        self.new_task_button.setText("新建任务")
        self.new_task_button.setFixedSize(104, 34)
        self.new_task_button.setStyleSheet(header_button_style)
        self.new_task_button.setToolTip(
            f"在当前软件中新增任务；SSH 模式自动继承当前目标环境（{QKeySequence(QKeySequence.New).toString(QKeySequence.NativeText)}）"
        )
        self.new_task_button.setAccessibleName("在任务中心新建部署任务")
        self.new_task_button.clicked.connect(
            parent_window.request_new_task_window
        )
        layout.addWidget(self.new_task_button, 0, Qt.AlignVCenter)

        self.config_button = QToolButton(self)
        self.config_button.setText("配置迁移")
        self.config_button.setIcon(
            self.style().standardIcon(QStyle.SP_DialogSaveButton)
        )
        self.config_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.config_button.setPopupMode(QToolButton.InstantPopup)
        self.config_button.setFixedSize(132, 34)
        self.config_button.setAccessibleName("导入或导出 Deploy Console 配置")
        self.config_button.setToolTip("导出或导入当前电脑的 Deploy Console 配置")
        self.config_button.setStyleSheet(header_button_style + f"""
            QToolButton {{ padding: 0 24px 0 10px; }}
            QToolButton::menu-indicator {{
                subcontrol-origin: padding;
                subcontrol-position: right center;
                right: 7px;
                width: 12px;
                height: 12px;
                image: url("{resource_path('assets/lucide/chevron-down.svg').as_posix()}");
            }}
        """)
        config_menu = QMenu(self.config_button)
        config_menu.setStyleSheet(f"""
            QMenu {{
                background: {COLORS['popup_bg']};
                color: {COLORS['text_primary']};
                border: 1px solid {COLORS['input_border']};
                padding: 4px;
            }}
            QMenu::item {{
                color: {COLORS['text_primary']};
                padding: 7px 18px;
            }}
            QMenu::item:selected {{
                background: {COLORS['accent_light']};
                color: #FFFFFF;
            }}
        """)
        export_action = config_menu.addAction("导出配置...")
        export_action.setToolTip("导出当前电脑的 Deploy Console 配置")
        export_action.triggered.connect(parent_window.export_configuration)
        import_action = config_menu.addAction("导入配置...")
        import_action.setToolTip("导入配置包并覆盖当前全部配置")
        import_action.triggered.connect(parent_window.import_configuration)
        config_menu.addSeparator()
        exit_action = config_menu.addAction("退出应用")
        exit_action.setToolTip("退出 Deploy Console（运行中的任务需要先结束）")
        exit_handler = getattr(parent_window, "request_exit_application", None)
        if exit_handler is not None:
            exit_action.triggered.connect(exit_handler)
        else:
            exit_action.setEnabled(False)
        self.config_button.setMenu(config_menu)
        layout.addWidget(self.config_button, 0, Qt.AlignVCenter)

        layout.addSpacing(4)

    def _host_window(self):
        manager = getattr(self._parent_window, "window_manager", None)
        if getattr(self._parent_window, "embedded", False) and manager is not None:
            return manager
        return self._parent_window

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            host = self._host_window()
            self._drag_pos = event.globalPosition().toPoint() - host.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_pos and event.buttons() & Qt.LeftButton:
            self._host_window().move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        host = self._host_window()
        if host.isMaximized():
            host.showNormal()
        else:
            host.showMaximized()


# ══════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION (PySide6 Bento Box)
# ══════════════════════════════════════════════════════════════════════
class DeployTool(QMainWindow):
    # ── Signals (replace tkinter event queue) ──────────────────────
    sig_build_log = Signal(str)
    sig_deploy_log = Signal(str)
    sig_deploy_progress = Signal(int, str)
    sig_package = Signal(str)
    sig_error = Signal(str)
    sig_info = Signal(str)
    sig_jobs = Signal(object, object, object)  # parent_item, path, jobs
    sig_params = Signal(object, object)  # request, params
    sig_build_status = Signal(str)
    sig_build_progress = Signal(int)
    sig_build_finished = Signal()
    sig_open_dashboard_item = Signal(object)
    sig_select_deploy_tab = Signal()
    sig_remote_options = Signal(object)
    sig_remote_loading_done = Signal()
    sig_dashboard_fallback = Signal(object, str)
    sig_dashboard_login_ok = Signal(str, str)
    sig_loading_start = Signal(str)
    sig_loading_stop = Signal()
    sig_jenkins_login_finished = Signal(bool, str)
    sig_pipeline_location = Signal(str, str)
    sig_cloud_status = Signal(str)
    sig_cloud_log = Signal(str)
    sig_cloud_session_result = Signal(object)
    sig_cloud_options = Signal(object)
    sig_open_cloud_status = Signal(object)
    sig_cloud_status_fallback = Signal(object, str)
    sig_cloud_status_login_ok = Signal(str)
    sig_job_cache_loaded = Signal(int, object)
    sig_job_cache_error = Signal(int, str)
    sig_branch_choices = Signal(object)
    sig_branch_refresh_error = Signal(object)
    sig_build_summary = Signal(object)
    sig_package_trace = Signal(object)
    sig_trace_regions = Signal(object)

    def __init__(
        self, store=None, window_manager=None, task_number=1, embedded=False
    ):
        super().__init__()

        self.window_manager = window_manager
        self.task_number = task_number
        self.embedded = embedded

        # Task workspaces are ordinary widgets inside the shared main window.
        self.setWindowFlags(Qt.Widget if embedded else Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose, not embedded)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setWindowTitle(f"Deploy Console 任务中心版 - 任务 {task_number}")
        self.setWindowIcon(QIcon(str(resource_path("assets/deploy-console-parallel.png"))))
        self.resize(1440, 900)
        self.setMinimumSize(960 if embedded else 1080, 480 if embedded else 720)

        # ── State ──────────────────────────────────────────────────
        # All windows in this process share one store, while the parallel
        # edition keeps its files separate from the original application.
        self.store = store or ConfigStore()
        self.jenkins = None
        self.current_job_path = []
        self.current_job_url = ""
        self.current_display_path = []
        self.build_running = False
        self._last_build_snapshot = None
        self._active_build_client = None
        self.active_deploy_after_build = True
        self._job_filter_active = False
        self._job_filter_expanded_state = {}
        self._job_cache_generation = 0
        self._job_cache_loading = False
        self.job_tree_cache = []
        self.param_vars = {}  # name → (widget, type)
        self.param_refresh_buttons = {}
        self._parameter_requests = {}
        self._active_parameter_key = None
        self._displayed_parameter_key = None
        self._parameter_generation = 0
        self._branch_refresh_generation = 0
        self._branch_refresh_requests = {}
        self._branch_refresh_active_tokens = set()
        self._summary_selection = None
        self._summary_request = None
        self._summary_build_url = ""
        self._summary_runs = {}
        self._running_summary_selection = None
        self._running_summary_build_url = ""
        self._trace_request = None
        self._trace_report = None
        self._trace_environment_summary = ""
        self._trace_job_path = []
        self._trace_display_path = []
        self._trace_job_client = None
        self._trace_region_request = None
        self._trace_cloud = self.store.data.get("cloud_deploy", {}).get("cloud") or "Huawei"
        self.dashboard_drivers = []
        self.cloud_driver = None
        self.ops_api = None
        self._ops_api_identity = None
        self._ops_login_lock = threading.Lock()
        self._ops_session_state_lock = threading.Lock()
        self._ops_session_generation = 0
        self._ops_expected_identity = None
        self._ops_session_disposed = threading.Event()
        self._ops_session_thread = None
        self._ops_maintenance_failed = False
        self._cloud_option_requests = {"build": 0, "tab": 0}
        self._selected_module_path = []
        self._preview_ssh_target = None
        self.delete_before_apply = self.store.data.get("delete_before_apply") is True
        self._delete_options = []
        self._delete_command_overrides = {}
        self.cloud_project_names = []
        self.cloud_options_loading = False
        self._cloud_login_in_progress = False
        self.build_cloud_loading = False
        self.build_cloud_target_dirty = True
        self.remote_file_maps = {"image": {}, "yaml": {}}
        self._env_var_values = {
            key: DEFAULT_DEPLOY.get(key, "22" if key == "port" else "")
            for _, key in ENVIRONMENT_FIELDS
        }
        self.env_dirty = False
        self.loading_environment = False
        self.remote_loading = False
        self.cancel_event = threading.Event()
        self.pipeline_phase = ""
        self.pipeline_location = ""
        self.batch_token = None
        self.loading_count = 0
        self._workers = []  # prevent GC of QThread objects
        self._env_vars_blocked = False
        self._task_runtime_active = False
        self._task_state = "idle"
        self._task_kind = ""
        self._task_title = "等待配置"
        self._task_target = ""
        self._task_package = ""
        self._replaced_packages = ReplacedPackageCapture(initial="-")
        self._task_phase = "未开始"
        self._task_progress = 0
        self._task_failed = False
        self._task_started_at = None
        self._task_finished_at = None

        # ── Connect signals ────────────────────────────────────────
        self._connect_signals()

        # ── Build UI ───────────────────────────────────────────────
        self._build_ui()
        for field in (self.cloud_url, self.cloud_username, self.cloud_password):
            field.textEdited.connect(self._invalidate_cloud_account)
        self.message_overlay = InAppMessageOverlay(self.centralWidget())
        self._new_task_shortcut = QShortcut(QKeySequence.New, self)
        self._new_task_shortcut.setContext(
            Qt.WidgetWithChildrenShortcut if embedded else Qt.WindowShortcut
        )
        self._new_task_shortcut.activated.connect(self.request_new_task_window)
        self._ops_session_timer = QTimer(self)
        self._ops_session_timer.setInterval(60000)
        self._ops_session_timer.timeout.connect(self._maintain_cloud_session)
        self._ops_session_timer.start()

    def _connect_signals(self):
        self.sig_build_log.connect(self._on_build_log)
        self.sig_deploy_log.connect(self._on_deploy_log)
        self.sig_deploy_progress.connect(self._on_deploy_progress)
        self.sig_package.connect(self._on_package)
        self.sig_error.connect(self._on_error)
        self.sig_info.connect(self._on_info)
        self.sig_jobs.connect(self._on_jobs)
        self.sig_params.connect(self._on_params)
        self.sig_build_status.connect(self._on_build_status)
        self.sig_build_progress.connect(self._on_build_progress)
        self.sig_build_finished.connect(self._on_build_finished)
        self.sig_open_dashboard_item.connect(self._on_open_dashboard_item)
        self.sig_select_deploy_tab.connect(self._on_select_deploy_tab)
        self.sig_remote_options.connect(self._on_remote_options)
        self.sig_remote_loading_done.connect(self._on_remote_loading_done)
        self.sig_dashboard_fallback.connect(self._on_dashboard_fallback)
        self.sig_dashboard_login_ok.connect(self._on_dashboard_login_ok)
        self.sig_loading_start.connect(self._on_loading_start)
        self.sig_loading_stop.connect(self._on_loading_stop)
        self.sig_jenkins_login_finished.connect(self._on_jenkins_login_finished)
        self.sig_pipeline_location.connect(self._on_pipeline_location)
        self.sig_cloud_status.connect(self._on_cloud_status)
        self.sig_cloud_log.connect(self._on_cloud_log)
        self.sig_cloud_session_result.connect(self._on_cloud_session_result)
        self.sig_cloud_options.connect(self._on_cloud_options)
        self.sig_open_cloud_status.connect(self._on_open_cloud_status)
        self.sig_cloud_status_fallback.connect(self._on_cloud_status_fallback)
        self.sig_cloud_status_login_ok.connect(self._on_cloud_status_login_ok)
        self.sig_job_cache_loaded.connect(self._on_job_cache_loaded)
        self.sig_job_cache_error.connect(self._on_job_cache_error)
        self.sig_branch_choices.connect(self._on_branch_choices)
        self.sig_branch_refresh_error.connect(self._on_branch_refresh_error)
        self.sig_build_summary.connect(self._on_build_summary)
        self.sig_package_trace.connect(self._on_package_trace)
        self.sig_trace_regions.connect(self._on_trace_regions)

    # ── Emit helpers (thread-safe via signals) ─────────────────────
    def emit(self, target, payload=None):
        """Legacy emit bridge: maps string event names to typed signals."""
        if target == "pipeline_location" and payload:
            trace_event(self, "pipeline_phase", phase=payload[0])
        elif target == "package":
            trace_event(self, "package_identified", package=payload)
        sig_map = {
            "build_log": lambda: self.sig_build_log.emit(payload),
            "deploy_log": lambda: self.sig_deploy_log.emit(payload),
            "deploy_progress": lambda: self.sig_deploy_progress.emit(
                payload[0], payload[1]
            ),
            "package": lambda: self.sig_package.emit(payload),
            "error": lambda: self.sig_error.emit(payload),
            "info": lambda: self.sig_info.emit(payload),
            "jobs": lambda: self.sig_jobs.emit(payload[0], payload[1], payload[2]),
            "params": lambda: self.sig_params.emit(payload[0], payload[1]),
            "build_status": lambda: self.sig_build_status.emit(payload),
            "build_progress": lambda: self.sig_build_progress.emit(payload),
            "build_finished": lambda: self.sig_build_finished.emit(),
            "open_dashboard_item": lambda: self.sig_open_dashboard_item.emit(payload),
            "select_deploy_tab": lambda: self.sig_select_deploy_tab.emit(),
            "remote_options": lambda: self.sig_remote_options.emit(payload),
            "remote_loading_done": lambda: self.sig_remote_loading_done.emit(),
            "dashboard_fallback": lambda: self.sig_dashboard_fallback.emit(payload[0], payload[1]),
            "dashboard_login_ok": lambda: self.sig_dashboard_login_ok.emit(payload[0], payload[1]),
            "loading_start": lambda: self.sig_loading_start.emit(payload),
            "loading_stop": lambda: self.sig_loading_stop.emit(),
            "jenkins_login_finished": lambda: self.sig_jenkins_login_finished.emit(
                payload[0], payload[1]
            ),
            "pipeline_location": lambda: self.sig_pipeline_location.emit(payload[0], payload[1]),
            "cloud_status": lambda: self.sig_cloud_status.emit(payload),
            "cloud_log": lambda: self.sig_cloud_log.emit(payload),
            "cloud_options": lambda: self.sig_cloud_options.emit(payload),
            "open_cloud_status": lambda: self.sig_open_cloud_status.emit(payload),
            "cloud_status_fallback": lambda: self.sig_cloud_status_fallback.emit(
                payload[0], payload[1]
            ),
            "cloud_status_login_ok": lambda: self.sig_cloud_status_login_ok.emit(payload),
            "job_cache_loaded": lambda: self.sig_job_cache_loaded.emit(
                payload[0], payload[1]
            ),
            "job_cache_error": lambda: self.sig_job_cache_error.emit(
                payload[0], payload[1]
            ),
            "branch_choices": lambda: self.sig_branch_choices.emit(payload),
            "branch_refresh_error": lambda: self.sig_branch_refresh_error.emit(
                payload
            ),
        }
        fn = sig_map.get(target)
        if fn:
            fn()

    # ── Task center state ─────────────────────────────────────────
    def _notify_task_summary(self):
        manager = getattr(self, "window_manager", None)
        if manager is not None:
            manager.update_task(self)

    def _begin_tracked_task(self, kind, target, package="", title=""):
        self._replaced_packages.freeze()
        self._replaced_packages = ReplacedPackageCapture(
            initial="不涉及" if kind == "Jenkins 打包" else "待读取"
        )
        self._diagnostics = new_task_trace(
            getattr(self, "task_number", 0), kind=kind, target=target,
            package=package, title=title,
        )
        self._last_build_snapshot = None
        self._task_runtime_active = True
        self._task_state = "running"
        self._task_kind = kind
        self._task_title = title or kind
        self._task_target = target or "未指定"
        self._task_package = package
        self._task_phase = "准备执行..."
        self._task_progress = 0
        self._task_failed = False
        self._task_started_at = time.monotonic()
        self._task_finished_at = None
        self._notify_task_summary()

    def _finish_tracked_task(self):
        if not getattr(self, "_task_runtime_active", False):
            return
        self._replaced_packages.freeze()
        terminal_text = self._task_phase
        if self._task_failed or any(
            word in terminal_text for word in ("失败", "错误", "未知")
        ):
            self._task_state = "failed"
        elif any(word in terminal_text for word in ("取消", "终止")):
            self._task_state = "cancelled"
        else:
            self._task_state = "succeeded"
            self._task_progress = 100
            if not any(word in terminal_text for word in ("完成", "成功", "提交")):
                self._task_phase = "执行完成"
        self._task_runtime_active = False
        self._task_finished_at = time.monotonic()
        if getattr(self, "_diagnostics", None) is not None:
            self._diagnostics.finish(self._task_state)
        self._notify_task_summary()

    def _finish_standalone_task(self):
        self._finish_tracked_task()

    def has_active_work(self):
        return bool(
            self.build_running
            or self._task_runtime_active
            or any(worker.isRunning() for worker in self._workers)
        )

    def task_summary(self):
        replaced = self._replaced_packages.snapshot()
        end = time.monotonic()
        if self._task_finished_at is not None:
            end = self._task_finished_at
        elapsed = 0
        if self._task_started_at is not None:
            elapsed = max(0, int(end - self._task_started_at))
        return {
            "number": self.task_number,
            "state": self._task_state,
            "kind": self._task_kind,
            "title": self._task_title,
            "target": self._task_target,
            "package": self._task_package,
            "replaced_packages": replaced["packages"],
            "replaced_text": replaced["text"],
            "replaced_tooltip": replaced["tooltip"],
            "phase": self._task_phase,
            "progress": self._task_progress,
            "elapsed": elapsed,
            "can_cancel": self.build_running and not self.cancel_event.is_set(),
            "is_cancelling": self.build_running and self.cancel_event.is_set(),
            "restartable": self._task_state == "cancelled" and self._last_build_snapshot is not None,
            "can_resume": self._can_resume_build(),
            "can_remove": not self.has_active_work(),
        }

    def _can_resume_build(self):
        return (
            self._task_state == "cancelled"
            and self._last_build_snapshot is not None
            and not self.has_active_work()
            and not self._workers
        )

    def resume_build(self):
        if not self._can_resume_build():
            return
        self._launch_build(self._last_build_snapshot)

    def shutdown_resources(self):
        self._ops_session_disposed.set()
        self._ops_session_timer.stop()
        self._discard_cloud_session()
        self._summary_timer.stop()
        self._summary_request = None
        if self.window_manager:
            self.window_manager.target_coordinator.release_all(self)
            self.window_manager.batch_coordinator.release_all(self)
        if self.cloud_driver is not None:
            try:
                self.cloud_driver.quit()
            except Exception:
                pass
            self.cloud_driver = None

    # ── Signal Slots ───────────────────────────────────────────────
    @Slot(str)
    def _on_build_log(self, text):
        self.build_log.appendPlainText(text)

    @Slot(str)
    def _on_deploy_log(self, text):
        self.deploy_log.appendPlainText(text)

    @Slot(int, str)
    def _on_deploy_progress(self, percent, text):
        if hasattr(self, "deploy_progress"):
            self.deploy_progress.setValue(max(0, min(100, percent)))
        if hasattr(self, "deploy_progress_label"):
            self.deploy_progress_label.setText(text)
        if getattr(self, "_task_runtime_active", False):
            if self.build_running and self.active_deploy_after_build:
                self._task_progress = 65 + int(max(0, min(100, percent)) * 0.35)
            else:
                self._task_progress = max(0, min(100, percent))
            self._task_phase = text
            self._notify_task_summary()

    @Slot(str)
    def _on_package(self, pkg):
        if getattr(self, "_task_runtime_active", False):
            self._task_package = pkg
            self._notify_task_summary()
        self.package_input.setText(pkg)
        if hasattr(self, "cloud_package"):
            self.cloud_package.setText(pkg)
        self.store.data["last_package"] = pkg
        self.store.save()
        configuration = (getattr(self, "_last_build_snapshot", None) or {}).get("configuration", {})
        preview = ((configuration.get("environment") or {}).get("_preview_yaml")
                   or getattr(self, "_preview_ssh_target", None))
        if (
            self.build_running
            and self.active_deploy_after_build
            and hasattr(self, "build_deploy_mode")
            and self.build_deploy_mode.currentText() == "SSH 环境部署"
            and (not configuration or configuration.get("path") == self.current_job_path)
        ):
            try:
                ssh_target = infer_ssh_deployment_config(pkg)
                if preview and any(ssh_target[key] != preview[key] for key in preview):
                    # The pipeline reports a mismatch and stops before deployment.
                    # Keep the displayed preview that the user actually approved.
                    raise ValueError("Package differs from the frozen preview")
            except ValueError:
                # The pipeline reports the package parsing error with its full
                # context; leave the currently displayed profile untouched.
                pass
            else:
                DeployTool._apply_ssh_target_fields(self, ssh_target)
                self.deploy_log.appendPlainText(
                    "已按 Jenkins 最终包号更新界面目标："
                    f'{ssh_target["yaml_dir"]} / {ssh_target["yaml_file"]}'
                )
        if self.build_running:
            # 打包期间仅在 SSH 部署即将开始时切换 TAB2。只打包和云端部署
            # 都应留在当前页，避免用户被无关页面打断。
            if not self.active_deploy_after_build:
                return
            if (
                hasattr(self, "build_deploy_mode")
                and self.build_deploy_mode.currentText() == "云端部署"
            ):
                return
        DeployTool._select_page(self, "DeployPage")

    @Slot(str)
    def _on_error(self, text):
        if getattr(self, "_task_runtime_active", False):
            self._task_failed = True
            self._task_phase = "执行失败"
            self._notify_task_summary()
        if getattr(self, "cloud_options_loading", False):
            self._set_cloud_target_loading(False, "加载失败，请重试")
        if getattr(self, "build_cloud_loading", False):
            self._set_build_cloud_loading(False, "云端目标加载失败，请重试")
        if "stale element reference" in text.lower():
            text = "平台页面正在刷新，元素状态已变化，请稍后重试当前操作。"
        else:
            text = text.split("Stacktrace:", 1)[0].strip()
        if not text:
            text = "操作失败，但未收到具体错误信息，请查看执行日志。"
        # Runtime failures belong to the task that produced them. Keep the
        # feedback inside that task window so parallel work is not interrupted
        # by a detached native dialog.
        self.message_overlay.show_message("操作失败", text, kind="error")

    @Slot(str)
    def _on_info(self, text):
        # Keep completion feedback inside the application instead of opening a
        # separate native message box over other programs.
        self.message_overlay.show_message("完成", text)

    @Slot(object, object, object)
    def _on_jobs(self, parent_item, path, jobs):
        self._insert_jobs(parent_item, path, jobs)

    @Slot(object, object)
    def _on_params(self, request, params):
        key = request["key"]
        if self._parameter_requests.get(key) is not request:
            return
        self._parameter_requests.pop(key, None)
        if request["client"] is not self.jenkins:
            return
        if params is None:
            return
        self._save_parameter_cache(key, params)
        if key == self._active_parameter_key:
            self._displayed_parameter_key = key
            self._show_parameters(request["path"], params)
            self._update_build_cloud_ready()

    @Slot(str)
    def _on_build_status(self, text):
        self._update_build_status(text)

    @Slot(int)
    def _on_build_progress(self, percent):
        self.build_progress.setValue(percent)
        if getattr(self, "_task_runtime_active", False):
            if self.active_deploy_after_build:
                self._task_progress = int(max(0, min(100, percent)) * 0.65)
            else:
                self._task_progress = max(0, min(100, percent))
            self._notify_task_summary()

    @Slot()
    def _on_build_finished(self):
        self.build_running = False
        if getattr(self, "_running_summary_selection", None):
            self._summary_runs[self._running_summary_selection] = {
                "build_url": self._running_summary_build_url, "finished": True,
            }
            if self._running_summary_selection == self._summary_selection:
                self._summary_selection = None
                self._select_build_summary()
            self._running_summary_selection = None
            self._running_summary_build_url = ""
        if hasattr(self, "deploy_after_build_checkbox"):
            self.deploy_after_build_checkbox.setEnabled(True)
        if hasattr(self, "build_deploy_mode"):
            self.build_deploy_mode.setEnabled(
                DeployTool._deploy_after_build_enabled(self)
            )
        self._update_build_cloud_ready()
        self.cancel_button.setEnabled(False)
        self.pipeline_phase = ""
        self.pipeline_location = ""
        self.batch_token = None
        self._finish_tracked_task()

    @Slot(object)
    def _on_open_dashboard_item(self, item):
        self.run_thread(lambda: self._open_dashboard_item(item))

    @Slot()
    def _on_select_deploy_tab(self):
        DeployTool._select_page(self, "DeployPage")

    @Slot(object)
    def _on_remote_options(self, payload):
        self.remote_file_maps = payload.pop("_file_maps", self.remote_file_maps)
        for key, values in payload.items():
            for entries in (self.env_entries, self.build_env_entries):
                widget = entries.get(key)
                if isinstance(widget, SearchableComboBox):
                    widget.set_values(values)
        QMessageBox.information(self, "加载完成", "远程目录和文件选项已更新")

    @Slot()
    def _on_remote_loading_done(self):
        self.remote_loading = False
        self.remote_progress.hide()
        self.build_remote_progress.hide()
        self.remote_status_label.setText("")
        self.build_remote_status_label.setText("")

    @Slot(object, str)
    def _on_dashboard_fallback(self, item, reason):
        self._copy_dashboard_token(item)
        url = item.get("url", "").strip()
        url = dashboard_pods_url(url, item.get("namespace"))
        if url:
            webbrowser.open(url)
        QMessageBox.warning(
            self,
            "自动登录失败",
            f"{reason}\n\n已打开 Dashboard 并复制 Token，请手工粘贴。",
        )

    @Slot(str, str)
    def _on_dashboard_login_ok(self, name, namespace):
        message = f"已自动登录并打开 {namespace} Pods：{name}"
        self.deploy_log.appendPlainText(message)

    @Slot(str)
    def _on_loading_start(self, text):
        self.loading_count += 1
        self.title_bar.loading_label.setText(text)
        if self.loading_count == 1:
            self.title_bar.loading_bar.show()

    @Slot()
    def _on_loading_stop(self):
        self.loading_count = max(0, self.loading_count - 1)
        if self.loading_count == 0:
            self.title_bar.loading_bar.hide()
            self.title_bar.loading_label.setText("")

    @Slot(bool, str)
    def _on_jenkins_login_finished(self, success, message):
        self._job_cache_loading = False
        self._sync_trace_job_browser()
        self.trace_job_browser.setEnabled(success)
        self.job_tree.setEnabled(success)
        self.login_button.setEnabled(True)
        self.login_button.setText("登录并加载")
        self.path_label.setText(f"  {message}")
        self.path_label.setToolTip(message)
        if not success:
            self.build_button.setEnabled(False)
        if isinstance(self.jenkins_connection_card, CollapsibleCardWidget):
            self.jenkins_connection_card._toggle.setText(
                "Jenkins 连接 · 登录成功" if success else "Jenkins 连接 · 登录失败"
            )
            self.jenkins_connection_card.set_expanded(not success)

    @Slot(str, str)
    def _on_pipeline_location(self, phase, url):
        self.pipeline_phase = phase
        self.pipeline_location = url
        if phase == "jenkins" and self._running_summary_selection:
            self._running_summary_build_url = url
            self._summary_runs[self._running_summary_selection] = {
                "build_url": url, "finished": False,
            }
            if self._running_summary_selection == self._summary_selection:
                self._summary_selection = None
                self._select_build_summary()
        self._notify_task_summary()

    @Slot(str)
    def _on_cloud_status(self, text):
        self.cloud_status_label.setText(text)
        if getattr(self, "_task_runtime_active", False):
            self._task_phase = text
            if text == "部署请求已提交":
                self._task_progress = 100
            elif text in ("部署失败", "部署结果未知"):
                self._task_failed = True
            self._notify_task_summary()
        self.cloud_login_button.setEnabled(
            not getattr(self, "_cloud_login_in_progress", False)
        )
        if text == "已登录":
            self.cloud_deployment_card.show()
        elif text in (
            "登录失败", "部署失败", "部署请求已提交", "部署已取消", "部署结果未知"
        ):
            self._set_cloud_target_loading(False, text)
            if getattr(self, "build_cloud_loading", False):
                self._set_build_cloud_loading(False, text)

    @Slot(str)
    def _on_cloud_log(self, text):
        self.cloud_log.appendPlainText(text)

    @Slot(object)
    def _on_open_cloud_status(self, settings):
        self.run_thread(lambda: self._open_cloud_status_page(settings))

    @Slot(object, str)
    def _on_cloud_status_fallback(self, settings, reason):
        if hasattr(self, "cloud_status_login_button"):
            self.cloud_status_login_button.setEnabled(True)
        if hasattr(self, "cloud_status_login_state"):
            self.cloud_status_login_state.setText("自动登录失败，可手工登录")
        password = settings.get("password_plain", "")
        if password:
            QApplication.clipboard().setText(password)
        url = settings.get("url", "").strip()
        if url:
            webbrowser.open(url)
        credential_hint = (
            "登录密码已复制到剪贴板，请在浏览器中粘贴。"
            if password else "当前未保存登录密码，请手工输入。"
        )
        QMessageBox.warning(
            self,
            "Rancher 自动登录失败",
            f"{reason}\n\n账号：{settings.get('username') or '未配置'}\n{credential_hint}",
        )

    @Slot(str)
    def _on_cloud_status_login_ok(self, name):
        if hasattr(self, "cloud_status_login_button"):
            self.cloud_status_login_button.setEnabled(True)
        if hasattr(self, "cloud_status_login_state"):
            self.cloud_status_login_state.setText("已登录")
        self.cloud_log.appendPlainText(f"已打开并登录：{name}")

    @Slot(object)
    def _on_cloud_options(self, options):
        source = options.get("source", "tab")
        request_source = "build" if source == "build" else "tab"
        if (
            options.get("request_id") is not None
            and options["request_id"] != self._cloud_option_requests[request_source]
        ):
            return
        if "cache_entries" in options:
            for key, value in options["cache_entries"].items():
                self._save_selection_cache("cloud_targets", key, value, save=False)
            self._persist_selection_cache()
        if options.get("error"):
            if source == "build":
                self._set_build_cloud_loading(False, "加载失败，可重试或刷新")
            else:
                self._set_cloud_target_loading(False, "加载失败，可重试或刷新")
            self.message_overlay.show_message("云端目标加载失败", options["error"], kind="error")
            return
        if options.get("cache_warnings"):
            self.message_overlay.show_message(
                "云端缓存未全部完成", "\n".join(options["cache_warnings"][:5]), kind="error"
            )
        projects = options.get("projects", [])
        deploy_types = options.get("types", [])
        regions = options.get("regions", [])
        if source == "build" and hasattr(self, "build_cloud_project"):
            current_region = self.build_cloud_region.currentText()
            self.build_cloud_project.set_values(projects)
            self.build_cloud_type.set_values(deploy_types)
            self.build_cloud_region.set_values(regions)
            self.build_cloud_project.setCurrentText(options.get("project", ""))
            self.build_cloud_type.setCurrentText(options.get("type", ""))
            if current_region in regions:
                self.build_cloud_region.setCurrentText(current_region)
            elif current_region:
                self.build_cloud_region.setCurrentIndex(-1)
            self._set_build_cloud_loading(False, "云端部署目标已就绪")
            self.build_cloud_target_dirty = False
            self._update_build_cloud_ready()
            return

        current_region = self.cloud_region.currentText()
        self.cloud_deployment_card.show()
        self.cloud_project.set_values(projects)
        self.cloud_type.set_values(deploy_types)
        self.cloud_region.set_values(regions)
        package_source = source == "package"
        self.cloud_project.setCurrentText(options.get("project", ""))
        self.cloud_type.setCurrentText(options.get("type", ""))
        if current_region in regions:
            self.cloud_region.setCurrentText(current_region)
        elif current_region:
            self.cloud_region.setCurrentIndex(-1)
        status = "已按包号匹配部署目标" if package_source else "部署数据已就绪"
        self._set_cloud_target_loading(False, status)

    def _set_cloud_target_loading(self, loading, text=""):
        self.cloud_options_loading = loading
        if not hasattr(self, "cloud_target_progress"):
            return
        self.cloud_target_progress.setVisible(loading)
        self.cloud_target_loading_label.setText(text)
        for widget in (
            self.cloud_project,
            self.cloud_type,
            self.cloud_region,
            self.cloud_refresh_button,
            self.cloud_deploy_button,
        ):
            widget.setEnabled(not loading)

    # ── Threading ──────────────────────────────────────────────────
    def run_thread(self, function, on_finished=None):
        worker = Worker(function, self)
        self._workers.append(worker)
        worker.error.connect(self._on_error)
        if on_finished:
            worker.finished.connect(on_finished)
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        worker.start()

    def _cleanup_worker(self, worker):
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        self._notify_task_summary()

    def request_new_task_window(self):
        """Add a task workspace to the shared task center."""
        if self.window_manager is None:
            self.message_overlay.show_message(
                "无法新建任务", "当前窗口未启用并行任务管理器。"
            )
            return
        window = self.window_manager.create_window(self)
        if self.build_deploy_mode.currentText() == "SSH 环境部署":
            window.build_deploy_mode.setCurrentText("SSH 环境部署")
            window.build_env_combo.setCurrentText(self.build_env_combo.currentText())
            window.batch_status_label.setText("自动合批待命")

    def request_exit_application(self):
        """Request a real application exit from the title-bar menu."""
        manager = getattr(self, "window_manager", None)
        if manager is not None:
            request_exit = getattr(manager, "request_exit", None)
            if request_exit is not None:
                request_exit()
            else:
                manager.close()
            return
        # Standalone/legacy windows do not have a manager close guard.
        self._closing = True
        self.close()

    def _configuration_windows(self):
        if self.window_manager is None:
            return (self,)
        windows = self.window_manager.windows
        return windows or (self,)

    def _configuration_import_is_blocked(self):
        for window in self._configuration_windows():
            workers = getattr(window, "_workers", [])
            if getattr(window, "build_running", False) or any(
                worker.isRunning() for worker in workers
            ):
                return True
        return False

    def export_configuration(self):
        default_path = Path.home() / "DeployConsole配置.deployconfig"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出 Deploy Console 配置",
            str(default_path),
            "Deploy Console 配置包 (*.deployconfig)",
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".deployconfig"
        try:
            exported = self.store.export_to(path)
        except Exception as exc:
            self.message_overlay.show_message(
                "配置导出失败", exception_message(exc), kind="error"
            )
            return
        self.message_overlay.show_message(
            "配置已导出",
            f"已导出配置：\n{exported}\n\n登录凭据仍保持本机加密，不需要设置导出密码。",
        )

    def import_configuration(self):
        if self._configuration_import_is_blocked():
            self.message_overlay.show_message(
                "暂时无法导入",
                "当前仍有打包、部署或数据加载任务，请等待所有并行任务结束后再导入配置。",
                kind="error",
            )
            return
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "导入 Deploy Console 配置",
            str(Path.home()),
            "Deploy Console 配置包 (*.deployconfig);;所有文件 (*)",
        )
        if not path:
            return
        confirmation = QMessageBox.question(
            self,
            "确认导入配置",
            "导入会覆盖当前 Jenkins、SSH 环境、Dashboard、Ops 和 Rancher 配置。\n\n"
            "确认继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmation != QMessageBox.Yes:
            return
        try:
            self.store.import_from(path)
        except Exception as exc:
            self.message_overlay.show_message(
                "配置导入失败", exception_message(exc), kind="error"
            )
            return
        for window in self._configuration_windows():
            window._reload_configuration_widgets()
        self.message_overlay.show_message(
            "配置导入完成",
            "环境、Dashboard、Jenkins、Ops、Rancher 已恢复，并已同步到全部任务工作区。\n\n"
            "若配置包来自另一台电脑，请重新填写登录密码和 Dashboard Token。",
        )

    def _reload_configuration_widgets(self):
        self._delete_command_overrides.clear()
        saved = self.store.data.get("jenkins", {})
        self.jenkins = None
        self._job_cache_generation += 1
        self._job_cache_loading = False
        self.job_tree.setEnabled(True)
        self._parameter_requests.clear()
        self._active_parameter_key = None
        self._displayed_parameter_key = None
        self._discard_cloud_session()
        for source in self._cloud_option_requests:
            self._cloud_option_requests[source] += 1
        self._selected_module_path = []
        self.cloud_project_names = []
        self.j_url.setText(saved.get("url", ""))
        self.j_user.setText(saved.get("username", ""))
        self.j_password.setText(unprotect_secret(saved.get("password", "")))
        self.current_job_path = []
        self.current_display_path = []
        self.current_job_url = ""
        self._select_build_summary()
        self.job_tree_cache = []
        self.job_tree.clear()
        self.path_label.setText("  配置已导入，请重新登录 Jenkins")
        self._sync_trace_job_browser()
        if hasattr(self, "job_filter_input"):
            self.job_filter_input.clear()
            self.job_filter_input.setEnabled(False)
        if hasattr(self, "job_cache_refresh_button"):
            self.job_cache_refresh_button.setEnabled(False)

        deploy_after_build = bool(saved.get("deploy_after_build", True))
        if hasattr(self, "deploy_after_build_checkbox"):
            self.deploy_after_build_checkbox.blockSignals(True)
            self.deploy_after_build_checkbox.setChecked(deploy_after_build)
            self.deploy_after_build_checkbox.blockSignals(False)
        self._set_delete_before_apply(self.store.data.get("delete_before_apply") is True, persist=False)
        self._refresh_delete_options()
        mode = saved.get("deploy_mode", "SSH 环境部署")
        if mode not in self.build_deploy_mode.values():
            mode = "SSH 环境部署"
        self.build_deploy_mode.blockSignals(True)
        self.build_deploy_mode.setCurrentText(mode)
        self.build_deploy_mode.blockSignals(False)

        package = self.store.data.get("last_package", "")
        self.package_input.setText(package)
        if hasattr(self, "cloud_package"):
            self.cloud_package.setText(package)

        cloud = self.store.data.get("cloud_deploy", {})
        self.cloud_url.setText(cloud.get("url", ""))
        self.cloud_username.setText(cloud.get("username", ""))
        self.cloud_password.setText(unprotect_secret(cloud.get("password", "")))
        project = cloud.get("project", "")
        deploy_type = cloud.get("deploy_type", "")
        region = cloud.get("region", "")
        region_label = f'{cloud.get("cloud", "")} / {region}'.strip(" / ")
        for project_widget in (self.build_cloud_project, self.cloud_project):
            project_widget.setCurrentText(project)
        for type_widget in (self.build_cloud_type, self.cloud_type):
            type_widget.set_values([deploy_type] if deploy_type else [])
            type_widget.setCurrentText(deploy_type)
        for region_widget in (self.build_cloud_region, self.cloud_region):
            region_widget.set_values([region_label] if region_label else [])
            region_widget.setCurrentText(region_label)
        self.cloud_status_label.setText("未登录")
        self.cloud_deployment_card.hide()
        self.build_cloud_target_dirty = True

        cloud_status = self.store.data.get("cloud_status", {})
        self.cloud_status_url.setText(cloud_status.get("url", ""))
        self.cloud_status_username.setText(cloud_status.get("username", ""))
        self.cloud_status_password.setText(
            unprotect_secret(cloud_status.get("password", ""))
        )
        self.cloud_status_login_state.setText("部署成功后自动打开")

        self.refresh_dashboards()
        self.refresh_environment_names()
        self._apply_build_deploy_mode(
            mode, prompt_for_account=False, refresh_cloud=False
        )

    def closeEvent(self, event):
        active_workers = [
            worker for worker in getattr(self, "_workers", []) if worker.isRunning()
        ]
        if (
            getattr(self, "build_running", False)
            or getattr(self, "_task_runtime_active", False)
            or active_workers
        ):
            QMessageBox.warning(
                self,
                "任务仍在运行",
                "当前任务仍有打包、部署或数据加载操作。\n"
                "请先取消并等待状态结束后再关闭。",
            )
            event.ignore()
            return
        self.shutdown_resources()
        event.accept()

    # ── Build Status Helper ────────────────────────────────────────
    def _update_build_status(self, text):
        if "成功" in text or "完成" in text:
            color = "success"
        elif "失败" in text or "取消" in text or "错误" in text:
            color = "danger"
        elif "构建" in text or "提交" in text or "部署" in text:
            color = "info"
        else:
            color = "muted"
        self.build_status_badge.set(text, color)
        if getattr(self, "_task_runtime_active", False):
            self._task_phase = text
            if "失败" in text or "错误" in text:
                self._task_failed = True
            self._notify_task_summary()

    # ══════════════════════════════════════════════════════════════
    #  BUILD UI
    # ══════════════════════════════════════════════════════════════
    def _build_ui(self):
        central = GlassBackground()
        central.setObjectName("CentralWidget")
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Custom title bar ───────────────────────────────────────
        self.title_bar = TitleBar(self)
        root_layout.addWidget(self.title_bar)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {COLORS['border']};")
        root_layout.addWidget(sep)

        # ── Tab widget ─────────────────────────────────────────────
        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("MainTabs")
        self.tab_widget.setMovable(True)
        # Native document tabs on macOS/Linux may paint the unused tab-bar
        # area with the system light palette. Keep that native mode only on
        # Windows and let QSS own the complete portable-platform header.
        self.tab_widget.setDocumentMode(sys.platform == "win32")
        self.tab_widget.tabBar().setDrawBase(False)
        root_layout.addWidget(self.tab_widget)

        # Tab 1: Jenkins 打包
        jenkins_page = QWidget()
        jenkins_page.setObjectName("JenkinsPage")
        self.tab_widget.addTab(jenkins_page, "  Jenkins 打包  ")
        self._build_jenkins_tab(jenkins_page)

        # Tab 2: 环境部署与 Dashboard
        deploy_page = QWidget()
        deploy_page.setObjectName("DeployPage")
        self.tab_widget.addTab(deploy_page, "  环境部署与 Dashboard  ")
        self._build_deploy_tab(deploy_page)

        # TAB3 通过官方 Ops 平台完成云端部署。
        cloud_page = QWidget()
        cloud_page.setObjectName("CloudDeployPage")
        self.tab_widget.addTab(cloud_page, "  云端部署  ")
        self._build_cloud_deploy_tab(cloud_page)
        trace_page = QWidget()
        trace_page.setObjectName("PackageTracePage")
        self.tab_widget.addTab(trace_page, "  包号溯源  ")
        self._build_package_trace_tab(trace_page)
        for index, name in enumerate(("file-archive", "send", "cloud", "file-archive")):
            self.tab_widget.setTabIcon(index, ui_icon(name))
        self.tab_widget.setIconSize(QSize(18, 18))
        self._apply_tab_order(self.store.data.get("tab_order", []))
        self.tab_widget.tabBar().tabMoved.connect(self._save_tab_order)
        apply_ui_icons(self)
        QTimer.singleShot(
            0, lambda: self._apply_build_deploy_mode(
                self.build_deploy_mode.currentText(), prompt_for_account=False
            )
        )
        QTimer.singleShot(0, self._auto_login_jenkins)

    def _select_page(self, name):
        for index in range(self.tab_widget.count()):
            page = self.tab_widget.widget(index)
            if page.objectName() == name:
                self.tab_widget.setCurrentWidget(page)
                return

    def _apply_tab_order(self, order):
        if not isinstance(order, list):
            return
        names = [self.tab_widget.widget(i).objectName() for i in range(self.tab_widget.count())]
        ordered = []
        for name in order + names:
            if name in names and name not in ordered:
                ordered.append(name)
        bar = self.tab_widget.tabBar()
        current = self.tab_widget.currentWidget()
        self._applying_tab_order = True
        try:
            for target, name in enumerate(ordered):
                source = next(i for i in range(self.tab_widget.count())
                              if self.tab_widget.widget(i).objectName() == name)
                bar.moveTab(source, target)
        finally:
            self.tab_widget.setCurrentWidget(current)
            self._applying_tab_order = False

    def _save_tab_order(self, _from, _to):
        if getattr(self, "_applying_tab_order", False):
            return
        order = [self.tab_widget.widget(i).objectName() for i in range(self.tab_widget.count())]
        self.store.data["tab_order"] = order
        self.store.save()
        for window in self._configuration_windows():
            if window is not self:
                window._apply_tab_order(order)

    # ── Jenkins Tab ────────────────────────────────────────────────
    def _build_jenkins_tab(self, page):
        saved = self.store.data["jenkins"]
        saved_password = unprotect_secret(saved.get("password", ""))
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(10)

        # ── Connection Card ────────────────────────────────────────
        conn_card = CollapsibleCardWidget("Jenkins 连接", expanded=True)
        self.jenkins_connection_card = conn_card
        layout.addWidget(conn_card)

        conn_content = QHBoxLayout()
        conn_content.setSpacing(12)
        conn_card.content_layout().addLayout(conn_content)

        # Connection fields
        fields_layout = QVBoxLayout()
        fields_layout.setSpacing(6)

        self.j_url = QLineEdit(saved.get("url", ""))
        self.j_url.setPlaceholderText("https://jenkins.example.com")
        self.j_user = QLineEdit(saved.get("username", ""))
        self.j_user.setPlaceholderText("admin")
        self.j_password = QLineEdit(saved_password)
        self.j_password.setPlaceholderText("API Token 或密码")
        self.j_password.setEchoMode(QLineEdit.Password)

        for label_text, widget in [("地址", self.j_url), ("账号", self.j_user), ("密码/Token", self.j_password)]:
            row = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(80)
            lbl.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}"; font-size: 13px;
                color: {COLORS['text_secondary']}; background: transparent;
            """)
            row.addWidget(lbl)
            row.addWidget(widget)
            fields_layout.addLayout(row)

        conn_content.addLayout(fields_layout, 1)

        # Login button
        self.login_button = QPushButton("登录并加载")
        self.login_button.setObjectName("PrimaryBtn")
        self.login_button.setFixedSize(132, 112)
        self.login_button.clicked.connect(self.login_jenkins)
        conn_content.addWidget(self.login_button)

        # ── Main Work Area ─────────────────────────────────────────
        work_splitter = QSplitter(Qt.Horizontal)
        work_splitter.setStyleSheet("QSplitter::handle { background: transparent; width: 8px; }")
        layout.addWidget(work_splitter, 1)

        # Left: Job tree sidebar
        sidebar_card = CardWidget("任务层级")
        sidebar_card.setFixedWidth(280)
        work_splitter.addWidget(sidebar_card)

        self.job_browser = JenkinsJobBrowser()
        self.job_tree = self.job_browser.tree
        self.job_tree_branch_style = self.job_browser.branch_style
        self.job_tree.itemExpanded.connect(self._tree_open)
        self.job_tree.itemClicked.connect(self._tree_select)
        self.job_filter_input = self.job_browser.search
        self.job_cache_refresh_button = self.job_browser.refresh
        self.job_search_timer = QTimer(self)
        self.job_search_timer.setSingleShot(True)
        self.job_search_timer.setInterval(250)
        self.job_search_timer.timeout.connect(self._start_job_search)
        self.job_filter_input.textChanged.connect(self._on_job_filter_changed)
        self.job_cache_refresh_button.clicked.connect(self.refresh_job_cache)
        sidebar_card.content_layout().addWidget(self.job_browser)

        # Keep the selected job and build controls visible above the scroll area.
        right_column = QWidget()
        column_layout = QVBoxLayout(right_column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(10)
        work_splitter.addWidget(right_column)
        right_scroll = QScrollArea()
        self.jenkins_scroll = right_scroll
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.NoFrame)
        right_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        right_widget = QWidget()
        right_widget.setObjectName("JenkinsScrollContent")
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right_scroll.setWidget(right_widget)

        work_splitter.setStretchFactor(0, 0)
        work_splitter.setStretchFactor(1, 1)

        # Path breadcrumb
        self.path_label = QLabel("  请先登录 Jenkins")
        self.path_label.setStyleSheet(f"""
            background: {COLORS['sidebar_bg']};
            border-radius: 8px;
            padding: 8px 14px;
            font-family: "{FONT_FAMILY}";
            font-size: 12px;
            color: {COLORS['text_secondary']};
        """)
        self.path_label.setFixedHeight(36)
        column_layout.addWidget(self.path_label)

        # ── Action Bar Card ────────────────────────────────────────
        action_card = CardWidget()
        self.build_action_card = action_card
        column_layout.addWidget(action_card)
        column_layout.addWidget(right_scroll, 1)

        toolbar = WrappingActionLayout()
        action_card.content_layout().addLayout(toolbar)
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        toolbar.addLayout(action_row)
        target_row = QHBoxLayout()
        target_row.setSpacing(8)
        toolbar.addLayout(target_row)
        environment_row = QHBoxLayout()
        environment_row.setSpacing(8)
        toolbar.addLayout(environment_row)

        # 默认开启，保持升级前的一键流程行为。
        self.deploy_after_build_checkbox = ToggleSwitch("打包后部署")
        self.deploy_after_build_checkbox.setChecked(
            bool(saved.get("deploy_after_build", True))
        )
        self.deploy_after_build_checkbox.setToolTip(
            "开启：打包后继续部署并查看状态；关闭：仅执行 Jenkins 打包"
        )
        action_row.addWidget(self.deploy_after_build_checkbox)

        self.build_button = QPushButton("开始一键流程")
        self.build_button.setObjectName("SuccessBtn")
        self.build_button.setFixedHeight(44)
        self.build_button.setMinimumWidth(170)
        self.build_button.setStyleSheet("QPushButton { min-width: 196px; }")
        self.build_button.clicked.connect(self.start_build)
        action_row.addWidget(self.build_button)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("DangerGhostBtn")
        self.cancel_button.setFixedSize(82, 44)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_pipeline)
        action_row.addWidget(self.cancel_button)

        # Deployment mode and target selector
        mode_lbl = QLabel("部署方式")
        mode_lbl.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}"; font-size: 12px;
            color: {COLORS['text_secondary']}; background: transparent;
        """)
        target_row.addWidget(mode_lbl)

        self.build_deploy_mode = SearchableComboBox(editable=False)
        self.build_deploy_mode.set_values(["SSH 环境部署", "云端部署"])
        self.build_deploy_mode.setFixedWidth(130)
        saved_mode = saved.get("deploy_mode", "SSH 环境部署")
        if saved_mode not in self.build_deploy_mode.values():
            saved_mode = "SSH 环境部署"
        self.build_deploy_mode.setCurrentText(saved_mode)
        target_row.addWidget(self.build_deploy_mode)

        self.build_env_label = QLabel("目标环境")
        self.build_env_label.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}"; font-size: 12px;
            color: {COLORS['text_secondary']}; background: transparent;
        """)
        environment_row.addWidget(self.build_env_label)

        self.build_env_combo = SearchableComboBox(editable=False)
        self.build_env_combo.setFixedWidth(160)
        self.build_env_combo.currentTextChanged.connect(self.load_environment)
        environment_row.addWidget(self.build_env_combo)

        # Status badge
        self.build_status_badge = StatusBadge("就绪", "success")
        self.build_status_badge.setFixedHeight(28)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        toolbar.addLayout(status_row)
        status_row.addWidget(self.build_status_badge)

        # Progress bar
        self.build_progress = QProgressBar()
        self.build_progress.setFixedSize(100, 8)
        self.build_progress.setRange(0, 100)
        self.build_progress.setValue(0)
        self.build_progress.setTextVisible(False)

        status_row.addWidget(self.build_progress)
        self.batch_status_label = QLabel("自动合批待命")
        self.batch_status_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; background: transparent; font-size: 12px;"
        )
        self.batch_status_label.setAccessibleName("SSH 自动合批状态")
        action_card.content_layout().addWidget(self.batch_status_label)

        # ── Target Environment Config Card ─────────────────────────
        self.build_env_card = CollapsibleCardWidget("目标环境配置", expanded=False)
        right_layout.addWidget(self.build_env_card)

        env_content = QWidget()
        env_grid = QGridLayout(env_content)
        env_grid.setSpacing(6)
        self.build_env_card.content_layout().addWidget(env_content)

        self.build_env_entries = {}
        self._create_environment_fields(env_grid, self.build_env_entries, columns=2)

        # Env action buttons row
        env_actions = QHBoxLayout()
        env_actions.setSpacing(8)
        self.build_delete_option = self._create_delete_apply_option()
        env_actions.addWidget(self.build_delete_option, 1)

        save_env_btn = QPushButton("保存环境配置")
        save_env_btn.setObjectName("PrimaryBtn")
        save_env_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        save_env_btn.clicked.connect(self.save_environment)

        refresh_env_btn = QPushButton("刷新")
        refresh_env_btn.setObjectName("GhostBtn")
        refresh_env_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        refresh_env_btn.clicked.connect(self.refresh_environment)

        self.build_remote_status_label = QLabel("")
        self.build_remote_status_label.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}"; font-size: 11px;
            color: {COLORS['text_muted']}; background: transparent;
        """)
        env_actions.addWidget(self.build_remote_status_label)

        self.build_remote_progress = QProgressBar()
        self.build_remote_progress.setFixedSize(80, 4)
        self.build_remote_progress.setRange(0, 0)
        self.build_remote_progress.setTextVisible(False)
        self.build_remote_progress.hide()
        env_actions.addWidget(self.build_remote_progress)

        env_actions.addWidget(save_env_btn)
        env_actions.addWidget(refresh_env_btn)
        self.build_env_card.content_layout().addLayout(env_actions)
        self.build_env_actions = env_actions
        env_header = QWidget()
        self.build_env_header_layout = QHBoxLayout(env_header)
        self.build_env_header_layout.setContentsMargins(0, 0, 0, 0)
        self.build_env_card._layout.removeWidget(self.build_env_card._toggle)
        self.build_env_header_layout.addWidget(self.build_env_card._toggle, 0, Qt.AlignLeft)
        self.build_env_header_layout.addStretch(1)
        self.build_env_card._layout.insertWidget(0, env_header)
        self.build_env_card.expanded_changed.connect(self._place_build_delete_option)
        self._place_build_delete_option(self.build_env_card._toggle.isChecked())
        self.build_yaml_preview_label = QLabel("选择具体任务后自动匹配 YAML；部署前按最终包号校验。")
        self.build_yaml_preview_label.setWordWrap(True)
        self.build_env_card.content_layout().addWidget(self.build_yaml_preview_label)

        # 云端部署模式专用的目标配置区，SSH 模式下保持隐藏。
        self.build_cloud_card = CollapsibleCardWidget("云端部署目标配置", expanded=True)
        right_layout.addWidget(self.build_cloud_card)
        cloud_grid = QGridLayout()
        cloud_grid.setHorizontalSpacing(10)
        cloud_grid.setVerticalSpacing(8)
        self.build_cloud_card.content_layout().addLayout(cloud_grid)

        cloud_saved = self.store.data.get("cloud_deploy", {})
        self.build_cloud_project = SearchableComboBox(editable=True)
        self.build_cloud_project.setCurrentText(cloud_saved.get("project", ""))
        self.build_cloud_type = SearchableComboBox(editable=False)
        if cloud_saved.get("deploy_type"):
            self.build_cloud_type.set_values([cloud_saved["deploy_type"]])
        self.build_cloud_region = SearchableComboBox(editable=False)
        if cloud_saved.get("region"):
            region_label = (
                f'{cloud_saved.get("cloud", "")} / {cloud_saved["region"]}'
            ).strip(" / ")
            self.build_cloud_region.set_values([region_label])

        for row, (label, widget) in enumerate([
            ("部署专案", self.build_cloud_project),
            ("服务类型", self.build_cloud_type),
            ("云区节点", self.build_cloud_region),
        ]):
            cloud_grid.addWidget(QLabel(label), row, 0)
            cloud_grid.addWidget(widget, row, 1)
        cloud_grid.setColumnStretch(1, 1)

        cloud_loading_row = QHBoxLayout()
        cloud_loading_row.setSpacing(12)
        self.build_cloud_loading_label = QLabel("")
        self.build_cloud_loading_label.setStyleSheet(
            f'color: {COLORS["text_secondary"]}; background: transparent;'
        )
        cloud_loading_row.addWidget(self.build_cloud_loading_label, 0, Qt.AlignVCenter)
        self.build_cloud_progress = QProgressBar()
        self.build_cloud_progress.setRange(0, 0)
        self.build_cloud_progress.setTextVisible(False)
        self.build_cloud_progress.setFixedHeight(6)
        self.build_cloud_progress.hide()
        cloud_loading_row.addWidget(self.build_cloud_progress, 1)
        cloud_loading_row.addStretch()
        cloud_grid.addLayout(cloud_loading_row, 3, 1)

        self.build_cloud_refresh_button = QPushButton("刷新云端目标")
        self.build_cloud_refresh_button.setObjectName("GhostBtn")
        self.build_cloud_refresh_button.setFixedHeight(44)
        self.build_cloud_refresh_button.clicked.connect(
            lambda: self.refresh_build_cloud_options(force=True)
        )
        cloud_loading_row.addWidget(self.build_cloud_refresh_button, 0, Qt.AlignVCenter)

        self.build_cloud_project.activated.connect(
            lambda *_: self.refresh_build_cloud_options()
        )
        self.build_cloud_project.lineEdit().returnPressed.connect(
            self.refresh_build_cloud_options
        )
        self.build_cloud_project.lineEdit().textEdited.connect(
            self._invalidate_build_cloud_target
        )
        self.build_cloud_type.activated.connect(
            lambda *_: self.refresh_build_cloud_options()
        )
        self.build_cloud_region.activated.connect(lambda *_: self._update_build_cloud_ready())
        self.build_deploy_mode.currentTextChanged.connect(
            self._on_build_deploy_mode_changed
        )
        if hasattr(self, "deploy_after_build_checkbox"):
            self.deploy_after_build_checkbox.toggled.connect(
                self._on_deploy_after_build_changed
            )
        self.build_cloud_card.hide()

        # ── Build Parameters Card ──────────────────────────────────
        self.params_card = CollapsibleCardWidget("构建参数", expanded=False)
        right_layout.addWidget(self.params_card)
        self.params_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.branch_summary = QWidget()
        self.branch_summary_layout = QGridLayout(self.branch_summary)
        self.branch_summary_layout.setContentsMargins(0, 0, 0, 0)
        self.branch_summary_layout.setSpacing(6)
        self.branch_summary_layout.setColumnStretch(1, 1)
        self.params_card._layout.insertWidget(1, self.branch_summary)
        self.branch_summary.hide()
        self._branch_summary_rows = []
        self.params_card.expanded_changed.connect(self._set_parameters_expanded)

        self.params_scroll_area = QScrollArea()
        self.params_scroll_area.setWidgetResizable(True)
        self.params_scroll_area.setFrameShape(QFrame.NoFrame)
        self.params_scroll_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.params_scroll_area.setMinimumHeight(180)
        self.params_card.content_layout().addWidget(self.params_scroll_area)

        self.params_container = QWidget()
        self.params_container.setObjectName("ParamsContainer")
        self.params_container.setStyleSheet("background: transparent;")
        self.params_layout = QGridLayout(self.params_container)
        self.params_layout.setSpacing(6)
        self.params_layout.setColumnStretch(1, 1)
        self.params_scroll_area.setWidget(self.params_container)

        self.build_summary_card = CollapsibleCardWidget("状态集", expanded=True)
        self.build_summary_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        right_layout.addWidget(self.build_summary_card)
        summary_layout = self.build_summary_card.content_layout()
        summary_row = QHBoxLayout()
        self.build_summary_label = QLabel("请选择 Jenkins 构建任务")
        self.build_summary_label.setTextFormat(Qt.PlainText)
        self.build_summary_label.setWordWrap(True)
        self.build_summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        summary_row.addWidget(self.build_summary_label, 1)
        self.build_summary_open_button = QPushButton("在 Jenkins 查看")
        self.build_summary_open_button.setObjectName("GhostBtn")
        self.build_summary_open_button.setEnabled(False)
        self.build_summary_open_button.clicked.connect(self._open_build_summary)
        summary_row.addWidget(self.build_summary_open_button)
        self.build_summary_refresh_button = QPushButton("刷新状态集")
        self.build_summary_refresh_button.setObjectName("GhostBtn")
        self.build_summary_refresh_button.setEnabled(False)
        self.build_summary_refresh_button.clicked.connect(
            lambda: self._refresh_build_summary()
        )
        summary_row.addWidget(self.build_summary_refresh_button)
        summary_layout.addLayout(summary_row)
        self.build_summary_scm = QLabel()
        self.build_summary_scm.setTextFormat(Qt.PlainText)
        self.build_summary_scm.setWordWrap(True)
        self.build_summary_scm.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.build_summary_scm.hide()
        summary_layout.addWidget(self.build_summary_scm)
        self.build_changes_title = QLabel("Changes · 提交变更")
        summary_layout.addWidget(self.build_changes_title)
        self.build_changes = QPlainTextEdit()
        self.build_changes.setReadOnly(True)
        self.build_changes.setAccessibleName("Jenkins 提交变更记录")
        self.build_changes.setPlaceholderText("打包后显示本次构建的提交变更")
        self.build_changes.setFixedHeight(114)
        summary_layout.addWidget(self.build_changes)
        self.build_changes_title.hide()
        self.build_changes.hide()
        self._summary_timer = QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.setInterval(5000)
        self._summary_timer.timeout.connect(self._refresh_build_summary)

        # ── Build Log ──────────────────────────────────────────────
        log_card = CardWidget("构建日志")
        log_card.setObjectName("DarkCard")
        # Override title color for dark card
        for child in log_card.findChildren(QLabel):
            child.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}";
                font-size: 15px; font-weight: bold;
                color: {COLORS['log_fg']}; background: transparent;
            """)
        right_layout.addWidget(log_card, 2)

        self.build_log = QPlainTextEdit()
        self.build_log.setObjectName("LogText")
        self.build_log.setReadOnly(True)
        self.build_log.setMinimumHeight(120)
        log_card.content_layout().addWidget(self.build_log)

    def _build_package_trace_tab(self, page):
        outer = QHBoxLayout(page)
        outer.setContentsMargins(20, 16, 20, 16)
        work_splitter = QSplitter(Qt.Horizontal)
        work_splitter.setChildrenCollapsible(False)
        work_splitter.setHandleWidth(8)
        outer.addWidget(work_splitter)
        self.trace_job_panel = CardWidget("Jenkins 任务路径（可选）")
        self.trace_job_panel.setAccessibleName("Jenkins 溯源任务路径")
        self.trace_job_panel.setMinimumWidth(260)
        work_splitter.addWidget(self.trace_job_panel)
        scroll = QScrollArea()
        scroll.setMinimumWidth(600)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        content_widget = QWidget()
        content_widget.setObjectName("PackageTraceScrollContent")
        content_widget.setMinimumHeight(620)
        scroll.setWidget(content_widget)
        work_splitter.addWidget(scroll)
        work_splitter.setStretchFactor(0, 0)
        work_splitter.setStretchFactor(1, 1)
        work_splitter.setSizes([300, 1000])
        layout = QVBoxLayout(content_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        card = CardWidget("溯源查询")
        layout.addWidget(card)
        content = card.content_layout()
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("溯源方式"))
        self.trace_mode = SearchableComboBox(editable=False)
        self.trace_mode.set_values(["包号溯源", "环境溯源"])
        self.trace_mode.setAccessibleName("溯源方式")
        mode_row.addWidget(self.trace_mode, 1)
        content.addLayout(mode_row)
        self.trace_hint = QLabel("每行一个包号，按产品和服务筛选任务，支持后端、前端和 APP 包。")
        self.trace_hint.setWordWrap(True)
        content.addWidget(self.trace_hint)
        controls = QHBoxLayout()
        controls.addStretch()
        self.trace_open_button = QPushButton("查看构建")
        self.trace_open_button.clicked.connect(self._open_package_trace)
        self.trace_cancel_button = QPushButton("取消查询")
        self.trace_cancel_button.setObjectName("DangerBtn")
        self.trace_cancel_button.setEnabled(False)
        self.trace_cancel_button.clicked.connect(self._cancel_package_trace)
        self.trace_copy_button = QPushButton("复制结果")
        self.trace_copy_button.clicked.connect(self._copy_package_trace)
        self.trace_search_button = QPushButton("查询来源")
        self.trace_search_button.setObjectName("PrimaryBtn")
        self.trace_search_button.clicked.connect(self._start_package_trace)
        for widget in (self.trace_open_button, self.trace_cancel_button,
                       self.trace_copy_button, self.trace_search_button):
            controls.addWidget(widget)
        self.trace_input = QPlainTextEdit()
        self.trace_input.setAccessibleName("待溯源包号")
        self.trace_input.setPlaceholderText(
            "pqmbackend-5.2.0:5.2.0.1001.11047\npqmfrontend-5.2.0:4.0.2.1412\n"
            "pqmmobile-bmgd-2.1.0:4.0.0.1000.10007"
        )
        self.trace_input.setFixedHeight(74)
        content.addWidget(self.trace_input)
        self.trace_environment_controls = QWidget()
        region_row = QHBoxLayout(self.trace_environment_controls)
        region_row.setContentsMargins(0, 0, 0, 0)
        region_row.addWidget(QLabel("云区节点"))
        self.trace_region = SearchableComboBox(editable=False)
        self.trace_region.setAccessibleName("环境溯源云区节点")
        self.trace_region.setPlaceholderText("请选择云区节点")
        region_row.addWidget(self.trace_region, 1)
        self.trace_region_refresh = QPushButton("刷新云区")
        self.trace_region_refresh.clicked.connect(lambda: self._load_trace_regions(force=True))
        region_row.addWidget(self.trace_region_refresh)
        self.trace_environment_controls.hide()
        content.addWidget(self.trace_environment_controls)
        content.addLayout(controls)
        jenkins_layout = self.trace_job_panel.content_layout()
        self.trace_job_browser = JenkinsJobBrowser()
        self.trace_job_browser.setMinimumHeight(175)
        self.trace_job_browser.search.setAccessibleName("溯源任务模糊查询")
        self.trace_job_browser.tree.setToolTip("选择具体任务后只查该路径；清空搜索框或再次点击已选任务可取消选择。")
        self.trace_job_browser.tree.itemClicked.connect(self._select_trace_job)
        self.trace_job_browser.tree.itemExpanded.connect(self._tree_open)
        self.trace_job_search_timer = QTimer(self)
        self.trace_job_search_timer.setSingleShot(True)
        self.trace_job_search_timer.setInterval(250)
        self.trace_job_search_timer.timeout.connect(self._sync_trace_job_browser)
        self.trace_job_browser.search.textChanged.connect(self._on_trace_job_filter_changed)
        self.trace_job_browser.refresh.clicked.connect(self.refresh_job_cache)
        jenkins_layout.addWidget(self.trace_job_browser, 1)
        self.trace_status = QLabel("等待查询。")
        self.trace_status.setToolTip("打包时间使用 Jenkins 构建记录时间（UTC+8）。")
        self.trace_status.setTextFormat(Qt.PlainText)
        self.trace_status.setWordWrap(True)
        layout.addWidget(self.trace_status)
        splitter = QSplitter(Qt.Vertical)
        layout.addWidget(splitter, 1)
        self.trace_results = QTreeWidget()
        self.trace_results.setRootIsDecorated(False)
        self.trace_results.setHeaderLabels(["包号", "打包人", "打包分支", "打包时间", "代码仓库"])
        self.trace_results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.trace_results.setUniformRowHeights(True)
        self.trace_results.header().setSectionResizeMode(QHeaderView.Interactive)
        for index, width in enumerate((300, 120, 250, 175)):
            self.trace_results.setColumnWidth(index, width)
        self.trace_results.currentItemChanged.connect(self._show_package_trace_detail)
        self.trace_results.itemDoubleClicked.connect(self._open_package_trace)
        splitter.addWidget(self.trace_results)
        self.trace_details = QPlainTextEdit()
        self.trace_details.setReadOnly(True)
        self.trace_details.setPlaceholderText("包号：\n打包人：\n打包分支：\n打包时间：\n代码仓库：")
        splitter.addWidget(self.trace_details)
        splitter.setSizes([260, 170])
        self.trace_input.textChanged.connect(self._invalidate_package_trace_input)
        self.trace_region.currentTextChanged.connect(self._invalidate_package_trace_input)
        self.trace_mode.currentTextChanged.connect(self._on_trace_mode_changed)
        for field in (self.cloud_url, self.cloud_username, self.cloud_password):
            field.textChanged.connect(self._invalidate_trace_account)
        self._sync_trace_job_browser()

    def _invalidate_trace_account(self, _text=""):
        if self.trace_mode.currentText() != "环境溯源":
            return
        self._trace_region_request = None
        self.trace_region.set_values([])
        self.trace_region.setEnabled(False)
        self.trace_region_refresh.setEnabled(True)
        self._invalidate_package_trace_input()
        self.trace_status.setText("Ops 登录信息已修改，请刷新云区后重新查询。")

    def _on_trace_mode_changed(self, _text=""):
        environment = self.trace_mode.currentText() == "环境溯源"
        self.trace_input.setVisible(not environment)
        self.trace_environment_controls.setVisible(environment)
        self.trace_job_panel.findChild(QLabel, "CardTitle").setText(
            "Jenkins 构建任务" if environment else "Jenkins 任务路径（可选）")
        self.trace_hint.setText(
            "选择云区节点及左侧具体构建任务，查询该环境当前运行包的 Jenkins 打包来源。"
            if environment else "每行一个包号，按产品和服务筛选任务，支持后端、前端和 APP 包。")
        self._trace_region_request = None
        self._invalidate_package_trace_input()
        if environment:
            self._load_trace_regions()

    def _trace_environment_settings(self):
        product = (extract_job_project_code(self._trace_display_path)
                   or extract_job_project_code(self._trace_job_path)).lower()
        service = job_service_type(self._trace_job_path, self._trace_display_path)
        if not self._trace_job_path or not product or not service:
            raise ValueError("请在左侧选择可识别产品及 WEB、SERVICE 或 APP 的具体构建任务。")
        project = product + ("mobile" if service == "mobile" else "")
        type_id = "frontend" if service == "mobile" else service
        settings = self._cloud_settings_from_ui()
        region = self.trace_region.currentText().strip()
        cloud, region = region.split(" / ", 1) if " / " in region else (self._trace_cloud, region)
        settings.update(project=project, deploy_type=service_type_label(project, type_id),
                        cloud=cloud, region=region)
        return settings

    def _trace_target_identity(self):
        settings = self._cloud_settings_from_ui()
        return (self.jenkins, tuple(self._trace_job_path), tuple(self._trace_display_path),
                settings["url"], settings["username"], settings["password_plain"], self._trace_cloud)

    def _trace_input_identity(self):
        mode = self.trace_mode.currentText()
        if mode == "环境溯源":
            return (mode, self._trace_target_identity(), self.trace_region.currentText())
        return (mode, self.trace_input.toPlainText().strip())

    def _load_trace_regions(self, *, force=False):
        if self.trace_mode.currentText() != "环境溯源" or self._trace_request:
            return
        previous = self.trace_region.currentText() or (self._trace_region_request or {}).get("previous", "")
        self._trace_region_request = None
        self.trace_region.set_values([])
        self.trace_region.setEnabled(False)
        self.trace_region_refresh.setEnabled(True)
        try:
            settings = self._trace_environment_settings()
        except ValueError:
            # Before choosing a job, offer the account's known nodes across services.
            settings = {**self._cloud_settings_from_ui(), "project": "", "deploy_type": "",
                        "cloud": self._trace_cloud}
        if not self._cloud_account_complete(settings):
            self.trace_status.setText("请先在云端部署页填写 Ops 登录信息，再刷新云区。")
            return
        request = {"identity": self._trace_target_identity(), "previous": previous}
        self._trace_region_request = request
        cached = None if force else self._cached_cloud_options(settings, "trace")
        if cached is not None:
            if settings["project"] and service_type_matches(settings["deploy_type"], cached.get("type", "")):
                self._on_trace_regions({"request": request, "regions": cached["regions"]})
                return
            if not settings["project"]:
                regions = []
                for project in cached["projects"]:
                    project_settings = {**settings, "project": project}
                    options = self._cached_cloud_options(project_settings, "trace")
                    if options is not None:
                        data = self._cached_selection("cloud_targets", self._cloud_options_cache_key(project_settings))
                        for service in options["types"]:
                            regions.extend(data["regions_by_type"][service])
                if regions or not cached["projects"]:
                    self._on_trace_regions({"request": request, "regions": list(dict.fromkeys(regions))})
                    return
        self.trace_region_refresh.setEnabled(False)
        self.trace_status.setText(f"正在加载 {settings['deploy_type'] or '当前账号'} 的云区节点…")

        def load():
            event = {"request": request}
            try:
                if settings["project"]:
                    api = self._ensure_cloud_login(settings)
                    options = api.load_target_options(settings["project"], settings["cloud"], refresh=force)
                    event["regions"] = list(dict.fromkeys(
                        target["region_label"] for target in options["targets"]
                        if service_type_id(target.get("typeId")) == service_type_id(settings["deploy_type"])
                        and str(target.get("project", settings["project"])).casefold() == settings["project"].casefold()
                    ))
                else:
                    entries, failures = self._collect_cloud_cache(settings, force=force)
                    event["cache_entries"] = entries
                    event["regions"] = list(dict.fromkeys(
                        region for entry in entries.values() for service in entry["types"]
                        for region in entry["regions_by_type"][service]
                    ))
                    if failures:
                        event["error"] = "部分云区读取失败，已显示可用节点；可点击刷新云区重试。"
            except Exception:
                event["error"] = "云区节点读取失败，请检查 Ops 登录信息和网络后刷新。"
            self.sig_trace_regions.emit(event)
        self.run_thread(load)

    @Slot(object)
    def _on_trace_regions(self, event):
        request = event["request"]
        if (request is not self._trace_region_request or self.trace_mode.currentText() != "环境溯源"
                or request["identity"] != self._trace_target_identity()):
            return
        self._trace_region_request = None
        self.trace_region_refresh.setEnabled(True)
        if "cache_entries" in event:
            for key, value in event["cache_entries"].items():
                self._save_selection_cache("cloud_targets", key, value, save=False)
            self._persist_selection_cache()
        regions = event.get("regions", [])
        self.trace_region.set_values(regions)
        if request["previous"] in regions:
            self.trace_region.setCurrentText(request["previous"])
        else:
            # Initial loads and refreshes must not silently select the first node.
            self.trace_region.setCurrentIndex(-1)
        self.trace_region.setEnabled(bool(regions))
        if request["previous"] and request["previous"] not in regions and regions:
            self.trace_status.setText(f"当前任务未提供云区“{request['previous']}”，请重新选择云区节点。")
        else:
            self.trace_status.setText(event.get("error") or (
                "云区已加载，选择具体构建任务及云区后点击查询来源。" if regions else "当前没有可用云区，请刷新云区。"))

    def _on_trace_job_filter_changed(self, text):
        if not text.strip():
            self._clear_trace_job()
        self.trace_job_search_timer.start()

    def _clear_trace_job(self):
        self._trace_job_path = []
        self._trace_display_path = []
        self.trace_job_browser.tree.clearSelection()
        self.trace_job_browser.tree.setCurrentItem(None)
        self._invalidate_package_trace_input()
        self._load_trace_regions()

    def _select_trace_job(self, item, _column):
        if not self.jenkins or self._job_cache_loading or self._trace_request:
            return
        data = item.data(0, Qt.UserRole)
        if not data or data.get("is_folder") or list(data["path"]) == self._trace_job_path:
            self._clear_trace_job()
            return
        self._trace_job_client = self.jenkins
        self._trace_job_path = list(data["path"])
        display, current = [], item
        while current is not None:
            display.append(current.text(0))
            current = current.parent()
        self._trace_display_path = list(reversed(display))
        self.trace_job_browser.tree.setCurrentItem(item)
        self._invalidate_package_trace_input()
        self._load_trace_regions()

    def _sync_trace_job_browser(self):
        if not hasattr(self, "trace_job_browser"):
            return
        browser = self.trace_job_browser
        if self._trace_job_client is not self.jenkins:
            self._trace_job_client = self.jenkins
            self._clear_trace_job()
        enabled = self.jenkins is not None and not self._job_cache_loading
        browser.setEnabled(enabled)
        browser.search.setEnabled(enabled)
        browser.refresh.setEnabled(enabled)
        query = browser.search.text().strip()
        filtered, _count = self._filter_job_cache(self.job_tree_cache, query)
        self._render_job_cache(filtered, expand_depth=1 if query else 0, tree=browser.tree)
        if self._trace_job_path and not self._job_cache_loading:
            pending = [browser.tree.topLevelItem(index)
                       for index in range(browser.tree.topLevelItemCount())]
            while pending:
                item = pending.pop()
                if item.isHidden():
                    continue
                data = item.data(0, Qt.UserRole) or {}
                if data.get("path") == self._trace_job_path:
                    browser.tree.setCurrentItem(item)
                    parent = item.parent()
                    while parent is not None:
                        parent.setExpanded(True)
                        parent = parent.parent()
                    break
                pending.extend(item.child(index) for index in range(item.childCount()))
            else:
                # The tree is the only scope indicator; do not keep a hidden selection.
                self._clear_trace_job()

    def _invalidate_package_trace_input(self, *_args):
        request = self._trace_request
        if request and request["identity"] != self._trace_input_identity():
            request["cancel"].set()
        if not request:
            self._trace_report = None
            self.trace_results.clear()
            self.trace_details.clear()
            self._trace_environment_summary = ""
            self.trace_status.setText("等待查询。")
            self.trace_status.setToolTip("")

    def _set_package_trace_busy(self, busy):
        for widget in (self.trace_input, self.trace_search_button, self.trace_job_panel,
                       self.trace_mode, self.trace_environment_controls):
            widget.setEnabled(not busy)
        self.trace_cancel_button.setEnabled(busy)

    def _start_package_trace(self):
        if self._trace_request is not None:
            return
        try:
            if self.jenkins is None or self._job_cache_loading:
                self.trace_status.setText("请先在 Jenkins 页完成登录并加载任务，再查询包号。")
                return
            if self._trace_job_client is not self.jenkins:
                self._sync_trace_job_browser()
            selected = list(self._trace_job_path) or None
            request = {"client": self.jenkins}
            environment = self.trace_mode.currentText() == "环境溯源"
            if environment:
                settings = self._trace_environment_settings()
                if not self._cloud_account_complete(settings):
                    raise ValueError("请先在云端部署页填写 Ops 登录信息。")
                if self._trace_region_request or not settings["region"]:
                    raise ValueError("请先加载并选择云区节点。")
                request["settings"] = dict(settings)
                packages = []
            else:
                packages = parse_packages(self.trace_input.toPlainText())
        except ValueError as exc:
            self.trace_status.setText(str(exc))
            return
        request["cancel"] = threading.Event()
        request["input"] = self.trace_input.toPlainText().strip()
        request["identity"] = self._trace_input_identity()
        self._trace_request = request
        self._trace_report = None
        self.trace_results.clear()
        self.trace_details.clear()
        self._trace_environment_summary = ""
        self.trace_status.setToolTip("")
        self._set_package_trace_busy(True)
        self.trace_status.setText("正在读取历史构建，旧包查询可能需要较长时间；可以随时取消。")

        def send(kind, data):
            self.sig_package_trace.emit({"request": request, "kind": kind, "data": data})

        def query():
            try:
                query_packages = packages
                if request["cancel"].is_set():
                    raise TraceCancelled()
                if environment:
                    send("progress", "正在读取所选环境的 Ops 当前运行镜像…")
                    api = self._ensure_cloud_login(request["settings"])
                    if request["cancel"].is_set():
                        raise TraceCancelled()
                    current = api.load_current_packages(request["settings"], cancel_event=request["cancel"])
                    if request["cancel"].is_set():
                        raise TraceCancelled()
                    send("environment", current)
                    # Runtime registries can differ from the Jenkins build registry.
                    query_packages = list(dict.fromkeys(image.rsplit("/", 1)[-1] for image in current["packages"]))
                callbacks = {"progress": lambda value: send("progress", value),
                             "on_match": lambda value: send("match", value)}
                source = JenkinsHistorySource(request["client"], request["cancel"])
                report = trace_packages(source, query_packages, scope="selected" if selected else "auto",
                                        selected=selected, **callbacks)
            except (TraceCancelled, InterruptedError):
                report = {"matches": [], "warnings": [], "unmatched": [], "cancelled": True,
                          "complete": False, "jobs": 0, "scanned": 0}
            except (RuntimeError, ValueError) as exc:
                report = {"error": str(exc)}
            except Exception:
                # HTTP responses and console text may contain credentials.
                report = {"error": "查询异常结束，请检查 Jenkins 连接后重试。"}
            send("finished", report)

        self.run_thread(query)

    def _cancel_package_trace(self):
        if self._trace_request:
            self._trace_request["cancel"].set()
            self.trace_cancel_button.setEnabled(False)
            self.trace_status.setText("正在取消查询，等待当前读取结束；已找到的结果会保留。")

    @Slot(object)
    def _on_package_trace(self, event):
        request = event["request"]
        if request is not self._trace_request:
            return
        kind, data = event["kind"], event["data"]
        stale = (request["identity"] != self._trace_input_identity() or
                 request["client"] is not self.jenkins)
        if stale:
            request["cancel"].set()
            if kind == "finished":
                self._trace_request = None
                self._trace_report = None
                self._set_package_trace_busy(False)
                self.trace_results.clear()
                self.trace_details.clear()
                self._trace_environment_summary = ""
                self.trace_status.setText("查询条件或账号已切换，请重新查询。")
            return
        if kind == "progress":
            if not request["cancel"].is_set():
                self.trace_status.setText(data)
        elif kind == "environment":
            settings = request["settings"]
            self._trace_environment_summary = (
                f"环境：{settings['project']} / {settings['deploy_type']} / {settings['cloud']} / {settings['region']}\n"
                "Ops 当前运行镜像：\n" + "\n".join(data["packages"]))
            self._show_package_trace_detail(self.trace_results.currentItem())
        elif kind == "match":
            item = QTreeWidgetItem(trace_result_fields(data))
            item.setData(0, Qt.UserRole, data)
            for index in range(item.columnCount()):
                item.setToolTip(index, item.text(index))
            self.trace_results.addTopLevelItem(item)
            if not self.trace_results.currentItem():
                self.trace_results.setCurrentItem(item)
        elif kind == "finished":
            self._trace_request = None
            self._set_package_trace_busy(False)
            self._trace_report = data
            if data.get("error"):
                self.trace_status.setText(data["error"])
                return
            state = "已取消" if data["cancelled"] else "查询完成" if data["complete"] else "查询结束（范围未查全）"
            self.trace_status.setText(
                f"{state}：检查 {data['jobs']} 个任务、{data['scanned']} 次构建，找到 {len(data['matches'])} 条记录。"
            )
            notes = []
            if data["unmatched"]:
                notes.append("本次范围内未找到产出或推送证据：\n" + "\n".join(data["unmatched"]))
            notes.extend(data["warnings"])
            if any(row["kind"] == "仅推送记录" for row in data["matches"]):
                notes.append("部分记录只有镜像推送证据，打包来源尚未确认。")
            self.trace_status.setToolTip("\n".join(notes) or "打包时间使用 Jenkins 构建记录时间（UTC+8）。")

    def _show_package_trace_detail(self, current, _previous=None):
        sections = [self._trace_environment_summary]
        if current:
            sections.append(format_trace_match(current.data(0, Qt.UserRole)))
        self.trace_details.setPlainText("\n\n".join(section for section in sections if section))

    def _copy_package_trace(self):
        rows = [self.trace_results.topLevelItem(index).data(0, Qt.UserRole)
                for index in range(self.trace_results.topLevelItemCount())]
        sections = [format_trace_match(row) for row in rows]
        if self.trace_mode.currentText() == "环境溯源" and self._trace_environment_summary:
            sections.insert(0, self._trace_environment_summary)
        if sections:
            QApplication.clipboard().setText("\n\n".join(sections))

    def _open_package_trace(self, *_args):
        item = self.trace_results.currentItem()
        if item and item.data(0, Qt.UserRole).get("url"):
            webbrowser.open(item.data(0, Qt.UserRole)["url"])

    # ── Deploy Tab ─────────────────────────────────────────────────
    def _build_deploy_tab(self, page):
        layout = QHBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # Keep form rows at their natural height when the task center expands.
        self.deploy_scroll = QScrollArea()
        self.deploy_scroll.setWidgetResizable(True)
        self.deploy_scroll.setFrameShape(QFrame.NoFrame)
        self.deploy_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        deploy_content = QWidget()
        deploy_content.setObjectName("DeployScrollContent")
        self.deploy_scroll.setWidget(deploy_content)
        left_col = QVBoxLayout(deploy_content)
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(12)
        left_col.setSizeConstraint(QLayout.SetMinimumSize)

        # Package card
        pkg_card = CardWidget("包号")
        left_col.addWidget(pkg_card)
        self.package_input = DeploymentPackageInput(self.store.data.get("last_package", ""))
        self.package_input.setPlaceholderText("例如：etmbackend-yisen-5.2.0:5.2.0.1001.10345")
        self.package_input.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {COLORS['input_bg']};
                border: 1px solid {COLORS['input_border']};
                border-radius: 8px;
                padding: 10px 14px;
                font-family: "{FONT_MONO}";
                font-size: 13px;
                color: {COLORS['text_primary']};
            }}
            QPlainTextEdit:focus {{
                border: 2px solid {COLORS['border_focus']};
                background: #101D31;
            }}
        """)
        self.package_input.returnPressed.connect(
            self.apply_deployment_targets_from_package
        )
        self.package_input.editingFinished.connect(self._on_deployment_package_editing_finished)
        self.package_input.setToolTip("修改包号后，离开输入框或按回车即可同步两页的 YAML 目录、文件和应用命令")
        pkg_card.content_layout().addWidget(self.package_input)
        package_hint = QLabel("每行一个包号；Shift+回车换行，回车校验。多个包共用当前环境及删除开关。")
        package_hint.setWordWrap(True)
        package_hint.setStyleSheet(f"color: {COLORS['text_muted']};")
        pkg_card.content_layout().addWidget(package_hint)
        self.deploy_package_preview = QTreeWidget()
        self.deploy_package_preview.setHeaderLabels(["包号（选择后可编辑该包的删除命令）", "YAML 路径"])
        self.deploy_package_preview.setRootIsDecorated(False)
        self.deploy_package_preview.setSelectionMode(QAbstractItemView.SingleSelection)
        self.deploy_package_preview.header().setSectionResizeMode(QHeaderView.Stretch)
        self.deploy_package_preview.setMinimumHeight(100)
        self.deploy_package_preview.setMaximumHeight(160)
        self.deploy_package_preview.currentItemChanged.connect(self._on_deploy_package_selected)
        self.package_input.textChanged.connect(self.deploy_package_preview.hide)
        self.deploy_package_preview.hide()
        pkg_card.content_layout().addWidget(self.deploy_package_preview)

        # Environment card
        env_card = CardWidget("环境与部署命令")
        left_col.addWidget(env_card)

        # Env selector row
        env_selector = QHBoxLayout()
        env_selector.setSpacing(8)

        env_lbl = QLabel("环境")
        env_lbl.setFixedWidth(50)
        env_lbl.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}"; font-size: 13px;
            color: {COLORS['text_secondary']}; background: transparent;
        """)
        env_selector.addWidget(env_lbl, 0, Qt.AlignVCenter)

        self.env_combo = SearchableComboBox(editable=False)
        self.env_combo.setFixedHeight(44)
        self.env_combo.setStyleSheet("QComboBox { min-height: 28px; max-height: 28px; }")
        self.env_combo.currentTextChanged.connect(self.load_environment)
        env_selector.addWidget(self.env_combo, 1, Qt.AlignVCenter)

        # Env buttons
        for text, cmd in [
            ("新增", self.new_environment),
            ("保存", self.save_environment),
            ("删除", self.delete_environment),
            ("刷新", self.refresh_environment),
        ]:
            is_danger = text == "删除"
            btn = QPushButton(text)
            btn.setObjectName("DangerGhostBtn" if is_danger else "GhostBtn")
            btn.setFixedSize(82, 44)
            btn.clicked.connect(cmd)
            env_selector.addWidget(btn, 0, Qt.AlignVCenter)

        env_card.content_layout().addLayout(env_selector)

        # Environment fields
        env_fields_widget = QWidget()
        env_fields_grid = QGridLayout(env_fields_widget)
        env_fields_grid.setSpacing(6)
        env_card.content_layout().addWidget(env_fields_widget)

        self.env_entries = {}
        self._create_environment_fields(env_fields_grid, self.env_entries, columns=2)
        self.deploy_delete_option = self._create_delete_apply_option()
        env_fields_grid.addWidget(self.deploy_delete_option, len(ENVIRONMENT_FIELDS) // 2, 2, 1, 2)

        # Remote loading status
        remote_row = QHBoxLayout()
        hint_lbl = QLabel("在远程目录/文件框中按 Enter 加载选项")
        hint_lbl.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}"; font-size: 11px;
            color: {COLORS['text_muted']}; background: transparent;
        """)
        remote_row.addWidget(hint_lbl)

        self.remote_status_label = QLabel("")
        self.remote_status_label.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}"; font-size: 11px;
            color: {COLORS['text_secondary']}; background: transparent;
        """)
        remote_row.addWidget(self.remote_status_label)

        self.remote_progress = QProgressBar()
        self.remote_progress.setFixedSize(100, 4)
        self.remote_progress.setRange(0, 0)
        self.remote_progress.setTextVisible(False)
        self.remote_progress.hide()
        remote_row.addWidget(self.remote_progress)
        remote_row.addStretch()
        env_card.content_layout().addLayout(remote_row)

        # Deploy button
        deploy_btn = QPushButton("执行环境部署")
        deploy_btn.setObjectName("SuccessBtn")
        deploy_btn.setFixedHeight(40)
        deploy_btn.clicked.connect(self.start_deploy)
        left_col.addWidget(deploy_btn)

        # Deploy log
        deploy_log_card = CardWidget("部署日志")
        deploy_log_card.setObjectName("DarkCard")
        for child in deploy_log_card.findChildren(QLabel):
            child.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}";
                font-size: 15px; font-weight: bold;
                color: {COLORS['log_fg']}; background: transparent;
            """)
        left_col.addWidget(deploy_log_card, 1)

        deploy_progress_row = QHBoxLayout()
        deploy_progress_row.setSpacing(10)
        self.deploy_progress_label = QLabel("等待部署")
        self.deploy_progress_label.setMinimumWidth(190)
        self.deploy_progress_label.setStyleSheet(f"""
            font-family: "{FONT_FAMILY}"; font-size: 12px;
            color: {COLORS['text_secondary']}; background: transparent;
        """)
        deploy_progress_row.addWidget(self.deploy_progress_label)
        self.deploy_progress = QProgressBar()
        self.deploy_progress.setRange(0, 100)
        self.deploy_progress.setValue(0)
        self.deploy_progress.setTextVisible(False)
        self.deploy_progress.setFixedHeight(8)
        self.deploy_progress.setAccessibleName("环境部署进度")
        deploy_progress_row.addWidget(self.deploy_progress, 1)
        deploy_log_card.content_layout().addLayout(deploy_progress_row)

        self.deploy_log = QPlainTextEdit()
        self.deploy_log.setObjectName("LogText")
        self.deploy_log.setReadOnly(True)
        self.deploy_log.setMinimumHeight(100)
        deploy_log_card.content_layout().addWidget(self.deploy_log)

        layout.addWidget(self.deploy_scroll, 1)

        # Right column: Dashboard
        right_widget = QWidget()
        right_widget.setFixedWidth(400)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        layout.addWidget(right_widget)

        self._build_dashboard_panel(right_layout)
        self.refresh_environment_names()

    def _build_dashboard_panel(self, parent_layout):
        card = CardWidget("Kubernetes Dashboard")
        parent_layout.addWidget(card, 1)

        self.dashboard_tree = QTreeWidget()
        self.dashboard_tree.setColumnCount(2)
        self.dashboard_tree.setHeaderLabels(["环境名称", "Dashboard 地址"])
        self.dashboard_tree.setRootIsDecorated(False)
        self.dashboard_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.dashboard_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.dashboard_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.dashboard_tree.itemDoubleClicked.connect(self._on_dashboard_item_double_clicked)
        card.content_layout().addWidget(self.dashboard_tree, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        for text, cmd in [
            ("新增", self.add_dashboard),
            ("编辑", self.edit_dashboard),
            ("删除", self.delete_dashboard),
        ]:
            is_del = text == "删除"
            btn = QPushButton(text)
            btn.setObjectName("DangerGhostBtn" if is_del else "GhostBtn")
            btn.setFixedSize(72, 32)
            btn.clicked.connect(cmd)
            btn_row.addWidget(btn)

        btn_row.addStretch()

        login_btn = QPushButton("自动登录")
        login_btn.setObjectName("SuccessBtn")
        login_btn.setFixedHeight(32)
        login_btn.clicked.connect(self.open_dashboard)
        btn_row.addWidget(login_btn)

        card.content_layout().addLayout(btn_row)
        self.refresh_dashboards()

    def _build_cloud_deploy_tab(self, page):
        saved = self.store.data.get("cloud_deploy", {})
        status_saved = self.store.data.get("cloud_status", {})

        # The deployment target card becomes visible after login. Keep the page
        # scrollable so showing it never compresses sibling cards or overlaps
        # their action rows on shorter windows and high-DPI displays.
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        page_layout.addWidget(scroll)
        self.cloud_deploy_scroll = scroll

        content = QWidget()
        content.setObjectName("CloudDeployScrollContent")
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        scroll.setWidget(content)
        self.cloud_deploy_scroll_content = content

        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.setSizeConstraint(QLayout.SetMinimumSize)

        connection_card = CardWidget("鼎捷 Ops 云端平台")
        connection_card.setMinimumHeight(232)
        connection_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(connection_card)
        connection_grid = QGridLayout()
        connection_grid.setHorizontalSpacing(10)
        connection_grid.setVerticalSpacing(8)
        connection_card.content_layout().addLayout(connection_grid)

        self.cloud_url = QLineEdit(saved.get(
            "url", "https://ops.digiwincloud.com.cn/cdm/deployment/flow-deploy"
        ))
        self.cloud_username = QLineEdit(saved.get("username", ""))
        self.cloud_password = QLineEdit()
        self.cloud_password.setEchoMode(QLineEdit.Password)
        encrypted_password = saved.get("password", "")
        if encrypted_password:
            try:
                self.cloud_password.setText(unprotect_secret(encrypted_password))
            except Exception:
                self.cloud_password.setText("")

        for row, (label, widget) in enumerate([
            ("平台地址", self.cloud_url),
            ("登录账号", self.cloud_username),
            ("登录密码", self.cloud_password),
        ]):
            connection_grid.addWidget(QLabel(label), row, 0)
            connection_grid.addWidget(widget, row, 1)
        self.cloud_username.setPlaceholderText("用户账号、手机号或 E-mail")
        self.cloud_password.setPlaceholderText("平台登录密码")
        connection_grid.setColumnStretch(1, 1)

        connection_actions = QHBoxLayout()
        connection_actions.setSpacing(12)
        self.cloud_login_button = QPushButton("登录并加载部署数据")
        self.cloud_login_button.setObjectName("PrimaryBtn")
        self.cloud_login_button.setFixedHeight(44)
        self.cloud_login_button.setToolTip("加载部署数据，并打开浏览器自动登录 Ops 平台")
        self.cloud_login_button.clicked.connect(
            lambda: self.login_cloud_platform(open_browser=True)
        )

        self.cloud_status_label = QLabel("未登录")
        self.cloud_status_label.setStyleSheet(
            f'color: {COLORS["text_secondary"]}; background: transparent;'
        )
        connection_actions.addWidget(self.cloud_status_label, 0, Qt.AlignVCenter)
        connection_actions.addStretch()
        connection_actions.addWidget(self.cloud_login_button, 0, Qt.AlignVCenter)
        connection_grid.addLayout(connection_actions, 3, 1)

        status_card = CardWidget("云环境部署状态（Rancher）")
        status_card.setMinimumHeight(232)
        status_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(status_card)
        status_grid = QGridLayout()
        status_grid.setHorizontalSpacing(10)
        status_grid.setVerticalSpacing(8)
        status_card.content_layout().addLayout(status_grid)

        self.cloud_status_url = QLineEdit(status_saved.get(
            "url",
            (
                "https://rancher-hw.digiwincloud.com.cn/dashboard/"
                "c/c-m-xpxltqjq/explorer/pod"
            ),
        ))
        self.cloud_status_username = QLineEdit(status_saved.get("username", ""))
        self.cloud_status_password = QLineEdit()
        self.cloud_status_password.setEchoMode(QLineEdit.Password)
        status_password = status_saved.get("password", "")
        if status_password:
            try:
                self.cloud_status_password.setText(unprotect_secret(status_password))
            except Exception:
                self.cloud_status_password.setText("")

        for row, (label, widget) in enumerate([
            ("Pod 状态页", self.cloud_status_url),
            ("登录账号", self.cloud_status_username),
            ("登录密码", self.cloud_status_password),
        ]):
            status_grid.addWidget(QLabel(label), row, 0)
            status_grid.addWidget(widget, row, 1)
        self.cloud_status_username.setPlaceholderText("Rancher 登录账号")
        self.cloud_status_password.setPlaceholderText("Rancher 登录密码")
        status_grid.setColumnStretch(1, 1)

        status_actions = QHBoxLayout()
        status_actions.setSpacing(12)
        self.cloud_status_login_button = QPushButton("保存并测试登录")
        self.cloud_status_login_button.setObjectName("PrimaryBtn")
        self.cloud_status_login_button.setFixedHeight(44)
        self.cloud_status_login_button.clicked.connect(self.login_cloud_status_page)
        self.cloud_status_login_state = QLabel("部署成功后自动打开")
        self.cloud_status_login_state.setStyleSheet(
            f'color: {COLORS["text_secondary"]}; background: transparent;'
        )
        status_actions.addWidget(self.cloud_status_login_state, 0, Qt.AlignVCenter)
        status_actions.addStretch()
        status_actions.addWidget(self.cloud_status_login_button, 0, Qt.AlignVCenter)
        status_grid.addLayout(status_actions, 3, 1)

        self.cloud_deployment_card = CardWidget("选择云端部署目标")
        self.cloud_deployment_card.setMinimumHeight(300)
        self.cloud_deployment_card.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed
        )
        layout.addWidget(self.cloud_deployment_card)

        target_loading_row = QHBoxLayout()
        self.cloud_target_loading_label = QLabel("")
        self.cloud_target_loading_label.setStyleSheet(
            f'color: {COLORS["text_secondary"]}; background: transparent;'
        )
        target_loading_row.addWidget(self.cloud_target_loading_label)
        self.cloud_target_progress = QProgressBar()
        self.cloud_target_progress.setRange(0, 0)
        self.cloud_target_progress.setTextVisible(False)
        self.cloud_target_progress.setFixedHeight(6)
        self.cloud_target_progress.setMinimumWidth(240)
        self.cloud_target_progress.hide()
        target_loading_row.addWidget(self.cloud_target_progress, 1)
        self.cloud_deployment_card.content_layout().addLayout(target_loading_row)

        deploy_grid = QGridLayout()
        deploy_grid.setHorizontalSpacing(10)
        deploy_grid.setVerticalSpacing(8)
        self.cloud_deployment_card.content_layout().addLayout(deploy_grid)

        self.cloud_package = QLineEdit(self.store.data.get("last_package", ""))
        self.cloud_package.setPlaceholderText("默认带出 Jenkins 包号，可修改；按 Enter 匹配部署目标")
        self.cloud_package.setAccessibleName("云端部署包号")
        self.cloud_package.setToolTip("部署时以此处输入的包号为准")
        self.cloud_package.returnPressed.connect(self.apply_cloud_target_from_package)
        self.cloud_project = SearchableComboBox(editable=True)
        self.cloud_project.setPlaceholderText("选择或输入部署专案")
        self.cloud_project.setCurrentText(saved.get("project", ""))
        self.cloud_type = SearchableComboBox(editable=False)
        saved_type = saved.get("deploy_type", "")
        if saved_type:
            self.cloud_type.set_values([saved_type])
        self.cloud_region = SearchableComboBox(editable=False)
        saved_region = saved.get("region", "")
        saved_cloud = saved.get("cloud", "")
        if saved_region:
            self.cloud_region.set_values([f"{saved_cloud} / {saved_region}".strip(" / ")])
        self.cloud_project.activated.connect(lambda *_: self.refresh_cloud_options())
        self.cloud_project.lineEdit().returnPressed.connect(self.refresh_cloud_options)
        self.cloud_type.activated.connect(lambda *_: self.refresh_cloud_options())

        for row, (label, widget) in enumerate([
            ("包号", self.cloud_package),
            ("部署专案", self.cloud_project),
            ("服务类型", self.cloud_type),
            ("云区节点", self.cloud_region),
        ]):
            deploy_grid.addWidget(QLabel(label), row, 0)
            deploy_grid.addWidget(widget, row, 1)
        deploy_grid.setColumnStretch(1, 1)

        deploy_actions = QHBoxLayout()
        deploy_actions.setSpacing(8)
        deploy_actions.addStretch()
        self.cloud_refresh_button = QPushButton("刷新三个栏位")
        self.cloud_refresh_button.setObjectName("GhostBtn")
        self.cloud_refresh_button.setFixedHeight(44)
        self.cloud_refresh_button.clicked.connect(lambda: self.refresh_cloud_options(force=True))
        deploy_actions.addWidget(self.cloud_refresh_button, 0, Qt.AlignVCenter)

        self.cloud_deploy_button = QPushButton("部署此包号")
        self.cloud_deploy_button.setObjectName("SuccessBtn")
        self.cloud_deploy_button.setFixedHeight(44)
        self.cloud_deploy_button.clicked.connect(self.start_cloud_deploy)
        deploy_actions.addWidget(self.cloud_deploy_button, 0, Qt.AlignVCenter)
        deploy_grid.addLayout(deploy_actions, 4, 1)
        self.cloud_deployment_card.hide()

        log_card = CardWidget("云端部署日志")
        log_card.setObjectName("DarkCard")
        log_card.setMinimumHeight(220)
        log_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        for child in log_card.findChildren(QLabel):
            child.setStyleSheet(
                f'font-family: "{FONT_FAMILY}"; font-size: 15px; font-weight: bold; '
                f'color: {COLORS["log_fg"]}; background: transparent;'
            )
        layout.addWidget(log_card)
        self.cloud_log = QPlainTextEdit()
        self.cloud_log.setObjectName("LogText")
        self.cloud_log.setReadOnly(True)
        self.cloud_log.setMinimumHeight(150)
        log_card.content_layout().addWidget(self.cloud_log)

    # ── Helper: Create environment fields ──────────────────────────
    def _create_delete_apply_option(self):
        option = DeleteApplyOption()
        option.checkbox.setChecked(self.delete_before_apply)
        option.checkbox.toggled.connect(self._set_delete_before_apply)
        option.command.textEdited.connect(self._on_delete_command_edited)
        self._delete_options.append(option)
        self._refresh_delete_options()
        return option

    @staticmethod
    def _delete_command_key(data):
        return tuple(str(data.get(key) or ("22" if key == "port" else "")).strip()
                     for key in ("host", "port", "yaml_dir", "yaml_file"))

    def _refresh_delete_options(self):
        key = self._delete_command_key(self._env_var_values)
        command = self._delete_command_overrides.get(key)
        for option in self._delete_options:
            option.set_target(self._get_env_var("yaml_dir"), self._get_env_var("yaml_file"), command)

    def _on_delete_command_edited(self, text):
        key = self._delete_command_key(self._env_var_values)
        self._delete_command_overrides[key] = text
        self._refresh_delete_options()

    def _freeze_delete_options(self, data):
        data["delete_before_apply"] = getattr(self, "delete_before_apply", False)
        if not data["delete_before_apply"]:
            return
        key = DeployTool._delete_command_key(data)
        data["delete_command"] = getattr(self, "_delete_command_overrides", {}).get(
            key, _yaml_delete_command(data["yaml_file"]))
        data["delete_command_target"] = [data["yaml_dir"], data["yaml_file"]]
        _deployment_delete_command(data)

    def _set_delete_before_apply(self, checked, *, persist=True):
        self.delete_before_apply = bool(checked)
        for option in self._delete_options:
            option.checkbox.blockSignals(True)
            option.checkbox.setChecked(self.delete_before_apply)
            option.checkbox.blockSignals(False)
        if persist:
            self.store.data["delete_before_apply"] = self.delete_before_apply
            self.store.save()

    def _place_build_delete_option(self, expanded):
        option = self.build_delete_option
        self.build_env_actions.removeWidget(option)
        self.build_env_header_layout.removeWidget(option)
        if expanded:
            self.build_env_actions.insertWidget(0, option, 1)
        else:
            self.build_env_header_layout.addWidget(option, 0, Qt.AlignRight)
        option.set_compact(not expanded)
        option.show()

    def _create_environment_fields(self, grid, entries, start_row=0, columns=1):
        searchable = {"image_dir", "image_file", "yaml_dir", "yaml_file", "dashboard"}

        for index, (label, key) in enumerate(ENVIRONMENT_FIELDS):
            block = index % columns
            row = start_row + index // columns
            label_column = block * 2
            entry_column = label_column + 1

            lbl = QLabel(label)
            lbl.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}"; font-size: 12px;
                color: {COLORS['text_secondary']}; background: transparent;
            """)
            grid.addWidget(lbl, row, label_column, Qt.AlignLeft)

            if key in searchable:
                entry = SearchableComboBox(editable=key != "dashboard")
                if key == "dashboard":
                    entry.set_values(self._dashboard_names())
                entry.setCurrentText(self._env_var_values.get(key, ""))
                entry.currentTextChanged.connect(lambda text, k=key: self._on_env_var_changed(k, text))
                if key in {"image_dir", "image_file", "yaml_dir", "yaml_file"}:
                    entry.lineEdit().returnPressed.connect(self.load_remote_options)
                if key in ("image_dir", "yaml_dir"):
                    kind = "image" if key == "image_dir" else "yaml"
                    entry.currentTextChanged.connect(
                        lambda text, k=kind: self._apply_remote_file_choices(k)
                    )
            else:
                entry = QLineEdit()
                entry.setText(self._env_var_values.get(key, ""))
                if key == "password":
                    entry.setEchoMode(QLineEdit.Password)
                entry.textChanged.connect(lambda text, k=key: self._on_env_var_changed(k, text))

            grid.addWidget(entry, row, entry_column)
            entries[key] = entry

        for col in range(columns * 2):
            if col % 2 == 1:
                grid.setColumnStretch(col, 1)

    def _on_env_var_changed(self, key, text):
        self._env_var_values[key] = text
        if key in ("host", "port", "yaml_dir", "yaml_file"):
            self._refresh_delete_options()
        if not self.loading_environment:
            self.env_dirty = True

    def _get_env_var(self, key):
        """Get current value from environment variable entries."""
        return self._env_var_values.get(key, "")

    def _set_env_var(self, key, value):
        """Set value in environment variable entries and update all widgets."""
        self._env_var_values[key] = value
        for entries in (self.env_entries, self.build_env_entries):
            widget = entries.get(key)
            if widget is None:
                continue
            if isinstance(widget, QComboBox):
                widget.setCurrentText(value)
            elif isinstance(widget, QLineEdit):
                widget.setText(value)

    def _apply_ssh_target_fields(self, target, *, mark_dirty=False):
        """Apply a derived YAML target to the shared fields on both pages."""
        was_loading = getattr(self, "loading_environment", False)
        was_dirty = getattr(self, "env_dirty", False)
        self.loading_environment = True
        try:
            for key in ("yaml_dir", "yaml_file", "apply_command"):
                self._set_env_var(key, target[key])
        finally:
            self.loading_environment = was_loading
            self.env_dirty = was_dirty or mark_dirty

    # ── Jenkins Operations ─────────────────────────────────────────
    def _auto_login_jenkins(self):
        if self.jenkins:
            return
        if all(
            widget.text().strip()
            for widget in (self.j_url, self.j_user, self.j_password)
        ):
            self.login_jenkins()

    def login_jenkins(self):
        url = self.j_url.text().strip()
        if not url:
            QMessageBox.warning(self, "缺少信息", "请输入 Jenkins 地址")
            return
        if isinstance(self.jenkins_connection_card, CollapsibleCardWidget):
            self.jenkins_connection_card.set_expanded(True)
        try:
            self.store.data["jenkins"].update({
                "url": url,
                "username": self.j_user.text().strip(),
                "password": protect_secret(self.j_password.text()),
            })
            self.store.save()
            self.jenkins = JenkinsClient(
                url, self.j_user.text().strip(), self.j_password.text()
            )
        except Exception as exc:
            QMessageBox.critical(self, "Jenkins 登录失败", f"初始化 Jenkins 连接失败：{exc}")
            return
        self.current_job_path = []
        self.current_job_url = ""
        self._select_build_summary()
        if hasattr(self, "job_search_timer"):
            self.job_search_timer.stop()
        if hasattr(self, "job_filter_input"):
            self.job_filter_input.blockSignals(True)
            self.job_filter_input.clear()
            self.job_filter_input.blockSignals(False)
            self.job_filter_input.setEnabled(False)
        if hasattr(self, "job_cache_refresh_button"):
            self.job_cache_refresh_button.setEnabled(False)
        self._job_cache_generation += 1
        self._job_filter_active = False
        self._job_filter_expanded_state = {}
        self.job_tree_cache = []
        self.job_tree.clear()
        self.login_button.setEnabled(False)
        self.login_button.setText("正在验证...")
        self.path_label.setText("  正在验证 Jenkins 登录身份...")
        self.path_label.setToolTip("")
        if isinstance(self.jenkins_connection_card, CollapsibleCardWidget):
            self.jenkins_connection_card._toggle.setText("Jenkins 连接 · 正在验证")
        self._start_job_cache_load(cached_jobs=self._load_saved_job_cache())

    def _load_root_jobs(self):
        try:
            self.jenkins.verify_authentication()
            jobs = self.jenkins.list_jobs([])
            self.emit("jobs", (None, [], jobs))
            self.emit(
                "jenkins_login_finished",
                (True, f"Jenkins 登录成功，已加载 {len(jobs)} 个根任务"),
            )
        except Exception as exc:
            message = f"Jenkins 登录或加载失败：{exception_message(exc)}"
            self.emit("jenkins_login_finished", (False, message))
            self.emit("error", message)
        finally:
            self.emit("loading_stop", None)

    def _fetch_job_cache(self, path=None, *, client=None):
        client = client or self.jenkins
        path = list(path or [])
        cache = []
        for job in client.list_jobs(path):
            node = {
                "name": job.get("name", ""),
                "displayName": job.get("displayName") or job.get("name", ""),
                "url": job.get("url", ""),
                "_class": job.get("_class", ""),
                "children": [],
            }
            if "folder" in node["_class"].lower():
                node["children"] = self._fetch_job_cache(
                    path + [node["name"]], client=client
                )
            cache.append(node)
        return cache

    @classmethod
    def _valid_job_cache_nodes(cls, nodes):
        """Reject incomplete local cache data before rendering the task tree."""
        if not isinstance(nodes, list):
            return False
        for node in nodes:
            if not isinstance(node, dict):
                return False
            if not isinstance(node.get("name"), str) or not node["name"]:
                return False
            if not isinstance(node.get("children"), list):
                return False
            if not cls._valid_job_cache_nodes(node["children"]):
                return False
        return True

    @staticmethod
    def _job_cache_identity(jenkins):
        url = str(getattr(jenkins, "url", "")).strip().rstrip("/")
        auth = getattr(getattr(jenkins, "session", None), "auth", None)
        username = str(auth[0] if auth else "").strip()
        return url, username

    def _load_saved_job_cache(self):
        """Load a valid cache belonging to the active Jenkins address/account."""
        record = self.store.data.get("jenkins", {}).get("job_cache")
        if not isinstance(record, dict):
            return None
        if record.get("version") != JENKINS_JOB_CACHE_VERSION:
            return None
        url, username = DeployTool._job_cache_identity(self.jenkins)
        if record.get("url") != url or record.get("username") != username:
            return None
        jobs = record.get("jobs")
        if not DeployTool._valid_job_cache_nodes(jobs):
            return None
        return jobs

    def _save_job_cache(self, cache):
        """Persist a freshly fetched task tree for later launches."""
        url, username = DeployTool._job_cache_identity(self.jenkins)
        self.store.data.setdefault("jenkins", {})["job_cache"] = {
            "version": JENKINS_JOB_CACHE_VERSION,
            "url": url,
            "username": username,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "jobs": cache,
        }
        self.store.save()

    @staticmethod
    def _parameter_cache_key(client, path, job_url):
        return json.dumps([
            *DeployTool._job_cache_identity(client), path, job_url.rstrip("/")
        ], ensure_ascii=False)

    def _start_job_cache_load(self, cached_jobs=None):
        generation = self._job_cache_generation
        client = self.jenkins
        self._job_cache_loading = True
        self._sync_trace_job_browser()
        self.job_tree.setEnabled(False)
        self.job_cache_refresh_button.setEnabled(False)
        self.job_filter_input.setEnabled(False)
        self._update_build_cloud_ready()
        self.emit("loading_start", "正在验证 Jenkins 并加载任务树...")
        self.run_thread(lambda: self._load_job_cache(generation, client, cached_jobs))

    def _load_job_cache(self, generation, client, cached_jobs=None):
        try:
            if generation != self._job_cache_generation or client is not self.jenkins:
                return
            # A local task tree says nothing about the current credentials.
            client.verify_authentication()
            cache = cached_jobs if cached_jobs is not None else self._fetch_job_cache(client=client)
            self.emit("job_cache_loaded", (generation, {
                "client": client, "jobs": cache, "cached": cached_jobs is not None,
            }))
        except Exception as exc:
            self.emit("job_cache_error", (generation, exception_message(exc)))
        finally:
            self.emit("loading_stop", None)

    @staticmethod
    def _cache_node_matches(node, query):
        text = " ".join(
            str(node.get(key, ""))
            for key in ("name", "displayName")
        )
        return query in text.casefold()

    @classmethod
    def _filter_job_cache(cls, cache, query):
        """Match only immediate modules below each root and keep full subtrees."""
        query = query.strip().casefold()
        if not query:
            return list(cache), len(cache)
        filtered = []
        match_count = 0
        for root in cache:
            children = [
                child for child in root.get("children", [])
                if cls._cache_node_matches(child, query)
            ]
            if children:
                filtered.append({**root, "children": children})
                match_count += len(children)
        return filtered, match_count

    def _count_job_cache_nodes(self, nodes):
        return sum(
            1 + self._count_job_cache_nodes(node.get("children", []))
            for node in nodes
        )

    def _render_job_cache(self, cache, expand_depth=0, *, tree=None):
        """Render cached jobs and expand only nodes above ``expand_depth``."""
        destination = tree if tree is not None else self.job_tree
        destination.clear()

        def add_nodes(parent_item, path, nodes, depth):
            for node in nodes:
                options = {"tree": destination} if tree is not None else {}
                item = self._create_job_tree_item(parent_item, path, node, add_placeholder=False, **options)
                children = node.get("children", [])
                add_nodes(item, path + [node["name"]], children, depth + 1)
                item.setExpanded(depth < expand_depth and bool(children))

        add_nodes(None, [], cache, 0)

    def refresh_job_cache(self):
        if not self.jenkins:
            return
        if self.loading_count:
            return
        if hasattr(self, "job_search_timer"):
            self.job_search_timer.stop()
        self._job_cache_generation += 1
        self._start_job_cache_load()

    @Slot(int, object)
    def _on_job_cache_loaded(self, generation, result):
        if generation != self._job_cache_generation or result["client"] is not self.jenkins:
            return
        cache = result["jobs"]
        self._job_cache_loading = False
        self.job_tree.setEnabled(True)
        self._parameter_requests.clear()
        self._active_parameter_key = None
        self._displayed_parameter_key = None
        self.current_job_path = []
        self.current_job_url = ""
        self._select_build_summary()
        self.current_display_path = []
        self._show_parameters([], [])
        self.build_button.setEnabled(False)
        self.job_tree_cache = cache
        self._sync_trace_job_browser()
        try:
            if not result.get("cached"):
                self._save_job_cache(cache)
        except (OSError, TypeError, ValueError) as exc:
            self.emit(
                "error",
                f"Jenkins 任务已加载，但本地缓存保存失败：{exception_message(exc)}",
            )
        self.job_filter_input.setEnabled(True)
        self.job_cache_refresh_button.setEnabled(True)
        query = self.job_filter_input.text().strip()
        if query:
            filtered, count = self._filter_job_cache(cache, query)
            self._render_job_cache(filtered, expand_depth=1)
            self.path_label.setText(f"  找到 {count} 个匹配模块")
        else:
            self._render_job_cache(cache)
            self.path_label.setText(
                f"  Jenkins 已连接，已缓存 {self._count_job_cache_nodes(cache)} 个任务"
            )
        source = "已加载本地缓存" if result.get("cached") else "已缓存"
        message = (
            f"Jenkins 登录成功；{source} {self._count_job_cache_nodes(cache)} 个任务；"
            "点击任务后加载构建参数"
        )
        self.emit(
            "jenkins_login_finished",
            (True, message),
        )

    @Slot(int, str)
    def _on_job_cache_error(self, generation, message):
        if generation != self._job_cache_generation:
            return
        self._job_cache_loading = False
        self._sync_trace_job_browser()
        self.job_tree.setEnabled(True)
        self._update_build_cloud_ready()
        self.job_filter_input.setEnabled(bool(self.job_tree_cache))
        self.job_cache_refresh_button.setEnabled(bool(self.jenkins))
        if self.job_tree_cache:
            self.path_label.setText(f"  任务缓存刷新失败：{message}")
            self.emit("error", f"Jenkins 任务缓存刷新失败：{message}")
        else:
            message = f"Jenkins 登录或任务加载失败：{message}"
            self.emit("jenkins_login_finished", (False, message))
            self.emit("error", message)

    def _start_job_search(self):
        if not hasattr(self, "job_filter_input") or not self.jenkins:
            return
        query = self.job_filter_input.text().strip()
        if not query:
            return
        filtered, count = self._filter_job_cache(self.job_tree_cache, query)
        self._render_job_cache(filtered, expand_depth=1)
        self.path_label.setText(f"  找到 {count} 个匹配模块")

    def _insert_jobs(self, parent_item, path, jobs):
        if parent_item is not None:
            # Remove placeholder children
            while parent_item.childCount():
                parent_item.removeChild(parent_item.child(0))

        for job in jobs:
            self._create_job_tree_item(parent_item, path, job)

        if hasattr(self, "job_filter_input") and self.job_filter_input.text().strip():
            self._apply_job_filter(self.job_filter_input.text())

    def _create_job_tree_item(self, parent_item, path, job, add_placeholder=True, *, tree=None):
        item_path = path + [job["name"]]
        display_name = job.get("displayName") or job["name"]
        item = QTreeWidgetItem()
        item.setText(0, display_name)
        item.setData(0, Qt.UserRole, {
            "path": item_path,
            "url": job.get("url", ""),
            "name": job["name"],
            "display_name": display_name,
            "is_folder": "folder" in job.get("_class", "").lower(),
        })

        if parent_item is not None:
            parent_item.addChild(item)
        else:
            (tree if tree is not None else self.job_tree).addTopLevelItem(item)

        if add_placeholder and "folder" in job.get("_class", "").lower():
            placeholder = QTreeWidgetItem()
            placeholder.setText(0, "加载中...")
            placeholder.setData(0, Qt.UserRole, None)
            item.addChild(placeholder)
        return item

    def _job_tree_items(self):
        """Yield every currently loaded Jenkins task item."""
        stack = [
            self.job_tree.topLevelItem(index)
            for index in range(self.job_tree.topLevelItemCount() - 1, -1, -1)
        ]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(
                item.child(index)
                for index in range(item.childCount() - 1, -1, -1)
            )

    def _filter_job_item(self, item, query, ancestor_matched=False):
        """Filter one loaded task branch and keep matching ancestors visible."""
        data = item.data(0, Qt.UserRole)
        own_match = bool(data) and query in item.text(0).casefold()
        branch_matched = ancestor_matched or own_match
        descendant_matched = False
        for index in range(item.childCount()):
            child = item.child(index)
            if DeployTool._filter_job_item(
                self, child, query, branch_matched
            ):
                descendant_matched = True

        subtree_matched = own_match or descendant_matched
        visible = ancestor_matched or subtree_matched
        item.setHidden(not visible)
        if subtree_matched and item.childCount():
            item.setExpanded(True)
        return subtree_matched

    @Slot(str)
    def _on_job_filter_changed(self, text):
        if not hasattr(self, "job_search_timer"):
            return
        self.job_search_timer.stop()
        query = text.strip()
        if not query:
            self._job_filter_active = False
            self._job_filter_expanded_state = {}
            if self.job_tree_cache:
                self._render_job_cache(self.job_tree_cache)
                self.path_label.setText(
                    f"  Jenkins 已连接，已缓存 "
                    f"{self._count_job_cache_nodes(self.job_tree_cache)} 个任务"
                )
            return
        if self.jenkins and self.job_tree_cache:
            self.job_search_timer.start()

    @Slot(str)
    def _apply_job_filter(self, text):
        """Apply a case-insensitive substring filter to loaded Jenkins tasks."""
        query = text.strip().casefold()
        items = list(self._job_tree_items())
        if query and not self._job_filter_active:
            self._job_filter_expanded_state = {
                id(item): item.isExpanded() for item in items
            }
        if not query:
            for item in items:
                item.setHidden(False)
                if id(item) in self._job_filter_expanded_state:
                    item.setExpanded(self._job_filter_expanded_state[id(item)])
            self._job_filter_active = False
            self._job_filter_expanded_state = {}
            return

        for index in range(self.job_tree.topLevelItemCount()):
            DeployTool._filter_job_item(
                self, self.job_tree.topLevelItem(index), query
            )
        self._job_filter_active = True

    def _tree_item_path(self, item):
        data = item.data(0, Qt.UserRole)
        if data:
            return data.get("path", [])
        return []

    def _tree_open(self, item):
        if item.childCount() == 1:
            child = item.child(0)
            if child.data(0, Qt.UserRole) is None and child.text(0) == "加载中...":
                path = self._tree_item_path(item)
                self.emit("loading_start", "正在加载任务目录...")
                self.run_thread(lambda: self._load_children(item, path))

    def _load_children(self, item, path):
        try:
            self.emit("jobs", (item, path, self.jenkins.list_jobs(path)))
        except Exception as exc:
            self.emit("error", f"任务层级加载失败：{exc}")
        finally:
            self.emit("loading_stop", None)

    def _tree_select(self, item, column):
        if not self.jenkins or getattr(self, "_job_cache_loading", False):
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        # Build display path
        display_path = []
        current = item
        while current:
            display_path.append(current.text(0))
            current = current.parent()
        self.current_display_path = list(reversed(display_path))
        self._selected_module_path = self.current_display_path
        is_folder = data.get("is_folder") or item.childCount()
        path = data.get("path", [])
        self.current_job_path = [] if is_folder else path
        self.current_job_url = "" if is_folder else data.get("url", "")
        self.path_label.setText("  " + " / ".join(self.current_display_path))
        DeployTool._sync_ssh_target_from_job(self)
        self._sync_cloud_target_from_job()
        self._update_build_cloud_ready()
        self._select_build_summary()

        # Module selection updates targets, but only a leaf requests parameters.
        if is_folder:
            self._active_parameter_key = None
            self.build_button.setEnabled(False)
            return

        client = self.jenkins
        key = self._parameter_cache_key(client, path, self.current_job_url)
        self._active_parameter_key = key
        params = self._cached_parameters(key)
        if params is not None:
            if key == self._displayed_parameter_key:
                self._update_build_cloud_ready()
                return
            self._displayed_parameter_key = key
            self._show_parameters(path, params)
            self._update_build_cloud_ready()
            return
        if self._displayed_parameter_key is not None:
            self._displayed_parameter_key = None
            self._show_parameters(path, [])
        self._update_build_cloud_ready()
        if key in self._parameter_requests and self._parameter_requests[key]["client"] is client:
            return
        request = {"key": key, "path": list(path), "client": client, "job_url": self.current_job_url}
        self._parameter_requests[key] = request
        self.emit("loading_start", "正在加载构建参数...")
        self.run_thread(lambda: self._load_parameters(request))

    def _build_summary_selection(self):
        if not self.jenkins or not self.current_job_path:
            return None
        return (self.jenkins, tuple(self.current_job_path), self.current_job_url)

    def _select_build_summary(self):
        selection = self._build_summary_selection()
        if selection == self._summary_selection:
            return
        self._summary_selection = selection
        self._summary_request = None
        self._summary_build_url = ""
        self._summary_timer.stop()
        self.build_summary_scm.hide()
        self.build_changes.clear()
        self.build_changes.hide()
        self.build_changes_title.hide()
        self.build_changes_title.setText("Changes · 提交变更")
        self.build_summary_open_button.setEnabled(False)
        self.build_summary_refresh_button.setText("刷新状态集")
        self.build_summary_label.setToolTip("")
        self.build_summary_refresh_button.setEnabled(False)
        if not selection:
            self.build_summary_label.setText("请选择 Jenkins 构建任务")
            return
        run = self._summary_runs.get(selection)
        if not run:
            self.build_summary_label.setText("当前模块尚未打包，打包后显示本次构建详情")
            return
        self._summary_build_url = run["build_url"]
        if not self._summary_build_url:
            self.build_summary_label.setText(
                "本次打包已结束，未生成构建详情" if run["finished"]
                else "本次构建正在提交或排队，等待构建编号…"
            )
            return
        self.build_changes_title.show()
        self.build_changes.show()
        self.build_summary_label.setText("正在加载构建状态集…")
        self._refresh_build_summary()

    def _refresh_build_summary(self):
        selection = self._summary_selection
        if (not selection or selection != self._build_summary_selection()
                or self._summary_request or not self._summary_build_url):
            return
        self._summary_timer.stop()
        client, path, job_url = selection
        build_url = self._summary_build_url
        request = {"selection": selection, "build_url": build_url}
        self._summary_request = request
        self.build_summary_refresh_button.setEnabled(False)
        self.build_summary_refresh_button.setText("加载中…")

        def load():
            try:
                data = client.get_build_summary(
                    list(path), job_url=job_url, build_url=request["build_url"]
                )
                payload = {"request": request, "data": data}
            except Exception as exc:
                payload = {"request": request, "error": str(exc)}
            self.sig_build_summary.emit(payload)

        self.run_thread(load)

    @Slot(object)
    def _on_build_summary(self, payload):
        request = payload["request"]
        if (request is not self._summary_request
                or request["selection"] != self._build_summary_selection()):
            return
        self._summary_request = None
        self.build_summary_refresh_button.setEnabled(True)
        self.build_summary_refresh_button.setText("刷新状态集")
        if "error" in payload:
            self.build_summary_label.setText(
                "状态集加载失败，以下保留上次结果；请刷新重试"
                if self._summary_build_url else "状态集加载失败，请点击刷新重试"
            )
            self.build_summary_label.setToolTip(payload["error"])
            return
        self.build_summary_label.setToolTip("")
        data = payload["data"]
        if not data:
            self.build_summary_open_button.setEnabled(False)
            self.build_summary_scm.hide()
            self.build_changes_title.setText("Changes · 提交变更（0）")
            self.build_summary_label.setText("本次构建详情暂不可用，请刷新重试")
            self.build_changes.setPlainText("暂无提交变更")
            return
        self._summary_build_url = request["build_url"]
        self.build_summary_open_button.setEnabled(bool(self._summary_build_url))
        result = "构建中" if data.get("building") else {
            "SUCCESS": "成功", "FAILURE": "失败", "UNSTABLE": "不稳定",
            "ABORTED": "已取消", "NOT_BUILT": "未构建",
        }.get(data.get("result"), data.get("result") or "状态未知")
        timestamp = data.get("timestamp")
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp / 1000)) if timestamp else ""
        self.build_summary_label.setText("  ·  ".join(filter(None, (
            f"本次构建 #{data.get('number', '—')}  {result}", started,
        ))))
        details = []
        for action in data.get("actions") or []:
            for cause in action.get("causes") or []:
                text = f"启动用户：{cause['userName']}" if cause.get("userName") else cause.get("shortDescription", "")
                if text and text not in details:
                    details.append(text)
            revision = action.get("lastBuiltRevision") or {}
            branches = [branch["name"] for branch in revision.get("branch") or [] if branch.get("name")]
            if branches:
                details.append("分支：" + " / ".join(branches))
            if revision.get("SHA1"):
                details.append("Revision：" + revision["SHA1"])
        self.build_summary_scm.setText("\n".join(details))
        self.build_summary_scm.setVisible(bool(details))
        # Freestyle jobs expose changeSet; Pipeline jobs expose changeSets.
        change_sets = data.get("changeSets") or []
        if not change_sets and data.get("changeSet"):
            change_sets = [data["changeSet"]]
        changes = [item for change_set in change_sets for item in change_set.get("items") or []]
        lines = []
        for index, change in enumerate(changes, 1):
            message = str(change.get("msg") or "（无提交说明）").strip()
            author = (change.get("author") or {}).get("fullName", "")
            revision = str(change.get("commitId") or change.get("id") or "")[:12]
            metadata = " · ".join(filter(None, (author, revision)))
            lines.append(f"{index}. {message}" + (f"\n    {metadata}" if metadata else ""))
        self.build_changes_title.setText(f"Changes · 提交变更（{len(changes)}）")
        text = "\n\n".join(lines) if lines else (
            "构建进行中，Jenkins 暂未返回提交变更" if data.get("building") else "本次构建无提交变更"
        )
        if text != self.build_changes.toPlainText():
            self.build_changes.setPlainText(text)
        if data.get("building"):
            self._summary_timer.start()

    def _open_build_summary(self):
        if self._summary_build_url:
            webbrowser.open(self._summary_build_url)

    def _cached_selection(self, kind, key):
        record = self.store.data.get("selection_cache")
        if not isinstance(record, dict) or record.get("version") != 1:
            return None
        entries = record.get(kind)
        return copy.deepcopy(entries.get(key)) if isinstance(entries, dict) else None

    def _save_selection_cache(self, kind, key, value, *, save=True):
        record = self.store.data.get("selection_cache")
        if not isinstance(record, dict) or record.get("version") != 1:
            record = {"version": 1}
            self.store.data["selection_cache"] = record
        if not isinstance(record.get(kind), dict):
            record[kind] = {}
        if record[kind].get(key) == value:
            return
        if kind == "cloud_targets":
            identity = json.loads(key)[:3]
            for other_key, other in list(record[kind].items()):
                try:
                    other_identity = json.loads(other_key)
                except (TypeError, ValueError):
                    continue
                if not isinstance(other_identity, list) or other_identity[:3] != identity:
                    continue
                if not isinstance(other, dict):
                    continue
                project = other.get("project", "")
                if project and project not in value["projects"]:
                    record[kind].pop(other_key)
                else:
                    other["projects"] = list(value["projects"])
        record[kind][key] = copy.deepcopy(value)
        if save:
            self._persist_selection_cache()

    def _persist_selection_cache(self):
        try:
            self.store.save()
        except (OSError, TypeError, ValueError) as exc:
            self.emit("error", f"数据已加载，但本地缓存保存失败：{exception_message(exc)}")

    def _cached_parameters(self, key):
        params = self._cached_selection("parameters", key)
        if not isinstance(params, list):
            return None
        for param in params:
            if (
                not isinstance(param, dict)
                or not isinstance(param.get("name"), str)
                or not isinstance(param.get("type", ""), str)
                or not isinstance(param.get("defaultParameterValue", {}), dict)
                or not isinstance(param.get("choices", []), list)
            ):
                return None
        return params

    @staticmethod
    def _parameter_cache_value(params):
        cached = copy.deepcopy(params)
        for param in cached:
            # Password defaults are credentials, not reusable UI metadata.
            if "password" in str(param.get("type", "")).lower():
                param.pop("defaultParameterValue", None)
                param.pop("choices", None)
        return cached

    def _save_parameter_cache(self, key, params):
        self._save_selection_cache("parameters", key, self._parameter_cache_value(params))

    def _sync_ssh_target_from_job(self):
        path = getattr(self, "current_job_path", [])
        display = getattr(self, "current_display_path", [])
        product = extract_job_project_code(display) or extract_job_project_code(path)
        service = job_service_type(path, display)
        self._preview_ssh_target = None
        if not path or not product or not service:
            if hasattr(self, "build_yaml_preview_label"):
                self.build_yaml_preview_label.setText("选择可识别的具体任务后自动匹配 YAML；部署前按最终包号校验。")
            return
        filename = product.lower() + ("mobile" if service == "mobile" else "") + ".yaml"
        target = {"yaml_dir": f"/data/yaml/app/{service}", "yaml_file": filename,
                  "apply_command": f"kubectl apply -f {filename}"}
        self._preview_ssh_target = target
        DeployTool._apply_ssh_target_fields(self, target)
        self.build_yaml_preview_label.setText(
            f"已按当前任务自动匹配：{target['yaml_dir']}/{filename}（本次部署预览）")

    def _sync_cloud_target_from_job(self):
        project = extract_job_project_code(self._selected_module_path)
        if not project:
            return
        leaf = self._selected_module_path[-1].upper()
        preferred_type = ""
        if re.match(r"^\d+-WEB(?:\W|$)", leaf):
            preferred_type = f"{project.lower()} 前端"
        elif re.match(r"^\d+-SERVICE(?:\W|$)", leaf):
            preferred_type = f"{project.lower()} 后端"
        elif re.match(r"^\d+-APP(?:\W|$)", leaf):
            project = f"{project}mobile"
            preferred_type = f"{project.lower()} 前端"
        for source, project_widget in (
            ("build", self.build_cloud_project), ("tab", self.cloud_project)
        ):
            project_widget.setCurrentText(project.lower())
            settings = (
                self._build_cloud_settings_from_ui() if source == "build"
                else self._cloud_settings_from_ui()
            )
            if preferred_type:
                settings["deploy_type"] = preferred_type
            elif settings.get("deploy_type"):
                settings["deploy_type"] = service_type_label(
                    project, service_type_id(settings["deploy_type"])
                )
            if self._cloud_account_complete(settings):
                self._queue_cloud_options(settings, source)
            elif source == "build":
                self.build_cloud_target_dirty = True

    def _load_parameters(self, request):
        try:
            self.emit("params", (request, request["client"].get_parameters(
                request["path"], job_url=request["job_url"]
            )))
        except Exception as exc:
            self.emit("params", (request, None))
            self.emit("error", f"构建参数加载失败：{exc}")
        finally:
            self.emit("loading_stop", None)

    # ── Build Parameters (CRITICAL) ────────────────────────────────
    def _show_parameters(self, path, params):
        # Clear existing parameter widgets
        self._set_parameters_expanded(True)
        self._branch_summary_rows = []
        while self.params_layout.count():
            child = self.params_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        self.param_vars = {}
        self.param_refresh_buttons = {}
        self._parameter_generation = getattr(self, "_parameter_generation", 0) + 1
        self._branch_refresh_requests = {}
        saved = self.store.data["jenkins"].setdefault("values", {}).get("/".join(path), {})

        if not params:
            empty_label = QLabel("该任务没有构建参数")
            empty_label.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}"; font-size: 13px;
                color: {COLORS['text_muted']}; background: transparent;
            """)
            self.params_layout.addWidget(empty_label, 0, 0)
            return

        self.params_layout.setColumnStretch(1, 1)
        branch_params = {param.get("name"): param for param in params
                         if param.get("name") in ("GIT_BRANCH", "GIT_BRANCH2")}
        shared_branches = len(branch_params) == 2

        for row, param in enumerate(params):
            name = param.get("name", "")
            input_span = 1 if shared_branches and name in branch_params else 2
            default = saved.get(name, param.get("defaultParameterValue", {}).get("value", ""))
            if param.get("type") == "BooleanParameterDefinition":
                default = str(default).lower()
            choices = param.get("choices") or []
            if shared_branches and name == "GIT_BRANCH2":
                choices = branch_params["GIT_BRANCH"].get("choices") or []
            if name not in ("GIT_BRANCH2", "CUSTOM_NAME") and choices and default not in choices:
                remote_name = f"origin/{default}"
                default = remote_name if remote_name in choices else choices[0]

            refreshable_branch = self._is_refreshable_branch_parameter(name, param)
            refreshable_choice = name == "CUSTOM_NAME"

            # Name label
            name_label = QLabel(name)
            name_label.setStyleSheet(f"""
                font-family: "{FONT_FAMILY}"; font-size: 12px;
                color: {COLORS['text_secondary']}; background: transparent;
            """)
            self.params_layout.addWidget(name_label, row, 0, Qt.AlignLeft | Qt.AlignVCenter)

            # Input widget
            if choices or refreshable_branch or refreshable_choice:
                widget = SearchableComboBox()
                widget.set_values(choices)
                widget.setCurrentText("" if default is None else str(default))
                self.param_vars[name] = ("combo", widget)
            elif param.get("type") == "BooleanParameterDefinition":
                widget = QCheckBox()
                widget.setChecked(str(default).lower() == "true")
                self.param_vars[name] = ("bool", widget)
            else:
                widget = QLineEdit()
                widget.setText("" if default is None else str(default))
                self.param_vars[name] = ("text", widget)

            if refreshable_choice or (refreshable_branch and not (shared_branches and name == "GIT_BRANCH2")):
                # Keep the refresh control next to the branch selector so a
                # user can update only this remote list without resetting the
                # other Jenkins parameters already entered in the form.
                input_row = QWidget()
                input_layout = QHBoxLayout(input_row)
                input_layout.setContentsMargins(0, 0, 0, 0)
                input_layout.setSpacing(6)
                input_layout.addWidget(widget, 1)

                refresh_button = QPushButton("刷新选项" if refreshable_choice else "刷新分支")
                refresh_button.setObjectName("GhostBtn")
                # The global button padding/minimum used to override the
                # fixed 34px height and push this button below the combo.
                refresh_button.setStyleSheet(
                    "QPushButton { min-height: 0; padding: 0 14px; }"
                )
                widget.ensurePolished()
                row_height = widget.sizeHint().height()
                widget.setFixedHeight(row_height)
                refresh_button.setFixedHeight(row_height)
                refresh_button.setAutoDefault(False)
                shared_refresh = shared_branches and name == "GIT_BRANCH"
                refresh_button.setAccessibleName(
                    "刷新 GIT_BRANCH 和 GIT_BRANCH2 分支列表" if shared_refresh else f"刷新 {name} 分支列表")
                refresh_button.setToolTip(
                    "同时刷新 GIT_BRANCH 和 GIT_BRANCH2 的候选分支，保留各自填写值"
                    if shared_refresh else "重新从 Jenkins 获取最新的打包分支")
                if refreshable_choice:
                    refresh_button.setAccessibleName(f"刷新 {name} 选项")
                    refresh_button.setToolTip("从 Jenkins 获取当前任务的最新选项，保留当前填写值")
                refresh_button.clicked.connect(
                    lambda _checked=False, parameter=name: self.refresh_branch_choices(
                        parameter
                    )
                )
                input_layout.addWidget(refresh_button)
                self.param_refresh_buttons[name] = refresh_button
                self.params_layout.addWidget(input_row, row, 1, 1, input_span)
            elif shared_branches and name == "GIT_BRANCH2":
                input_row = QWidget()
                input_layout = QHBoxLayout(input_row)
                input_layout.setContentsMargins(0, 0, 0, 0)
                input_layout.setSpacing(6)
                widget.ensurePolished()
                widget.setFixedHeight(widget.sizeHint().height())
                input_layout.addWidget(widget, 1)
                self.params_layout.addWidget(input_row, row, 1, 1, input_span)
            else:
                self.params_layout.addWidget(widget, row, 1, 1, input_span)

            # Description
            description = param.get("description")
            if name == "GIT_BRANCH2" and is_secondary_build_path(path):
                description = "二开打包优先使用此分支，未填写时使用 GIT_BRANCH"
            if description:
                name_label.setToolTip(description)
                widget.setToolTip(description)
                desc_label = QLabel(description)
                desc_label.setStyleSheet(f"""
                    font-family: "{FONT_FAMILY}"; font-size: 11px;
                    color: {COLORS['text_muted']}; background: transparent;
                """)
                desc_label.setWordWrap(True)
                self.params_layout.addWidget(desc_label, row, 3, Qt.AlignLeft | Qt.AlignVCenter)

            if name in ("CUSTOM_NAME", "GIT_BRANCH2", "GIT_BRANCH"):
                self._branch_summary_rows.append((name, row, [
                    (column, self.params_layout.itemAtPosition(row, column).widget())
                    for column in (0, 1, 3)
                    if self.params_layout.itemAtPosition(row, column) is not None
                ]))

        if shared_branches:
            self.param_refresh_buttons["GIT_BRANCH2"] = self.param_refresh_buttons["GIT_BRANCH"]
        self._set_parameters_expanded(self.params_card._toggle.isChecked())

    def _set_parameters_expanded(self, expanded):
        # Move the same controls between layouts so edits and branch refresh
        # always operate on the values used by start_build.
        is_ssh = self.build_deploy_mode.currentText() == "SSH 环境部署"
        branch = "GIT_BRANCH2" if is_ssh and "GIT_BRANCH2" in self.param_vars else "GIT_BRANCH"
        compact_names = ("CUSTOM_NAME", branch) if is_ssh else ("GIT_BRANCH",)
        buttons = getattr(self, "param_refresh_buttons", {})
        shared_branches = "GIT_BRANCH" in buttons and buttons.get("GIT_BRANCH2") is buttons["GIT_BRANCH"]
        visible_rows = False
        for name, original_row, cells in self._branch_summary_rows:
            compact = not expanded and name in compact_names
            visible_rows = visible_rows or compact
            for column, widget in cells:
                # Long Jenkins descriptions stay in the expanded form; the
                # compact SSH fields retain them as tooltips.
                outside = compact and not (is_ssh and name in ("CUSTOM_NAME", "GIT_BRANCH2") and column == 3)
                target = self.branch_summary_layout if outside else self.params_layout
                self.params_layout.removeWidget(widget)
                self.branch_summary_layout.removeWidget(widget)
                input_span = 2 if (not outside and column == 1 and
                                   not (shared_branches and name in ("GIT_BRANCH", "GIT_BRANCH2"))) else 1
                target.addWidget(
                    widget, compact_names.index(name) if outside else original_row,
                    2 if outside and column == 3 else column, 1, input_span,
                    Qt.AlignVCenter,
                )
                widget.show()
        if shared_branches:
            button = buttons["GIT_BRANCH"]
            button.parentWidget().layout().removeWidget(button)
            input_row = self.param_vars[branch][1].parentWidget()
            rows = sorted(row for name, row, _cells in self._branch_summary_rows
                          if name in ("GIT_BRANCH", "GIT_BRANCH2"))
            # Only span adjacent fields; never cover unrelated Jenkins rows.
            span_rows = expanded and len(rows) == 2 and rows[1] == rows[0] + 1
            button.setSizePolicy(button.sizePolicy().horizontalPolicy(),
                                 QSizePolicy.Expanding if span_rows else QSizePolicy.Fixed)
            if span_rows:
                button.setMinimumHeight(0)
                button.setMaximumHeight(16777215)
                self.params_layout.addWidget(button, rows[0], 2, 2, 1)
            else:
                button.setFixedHeight(self.param_vars[branch][1].height())
                input_row.layout().addWidget(button)
            button.show()
        self.branch_summary.setVisible(visible_rows)

    @staticmethod
    def _is_refreshable_branch_parameter(name, param):
        """Return whether a Jenkins parameter should expose branch refresh."""
        normalized_name = str(name or "").strip().upper()
        parameter_type = str(param.get("type", "") or "").strip().lower()
        return (
            bool(normalized_name)
            and (
                normalized_name.startswith("GIT_BRANCH")
                or (
                    "gitparameterdefinition" in parameter_type
                    and "BRANCH" in normalized_name
                )
            )
        )

    @staticmethod
    def _preferred_branch_choice(current, choices):
        """Choose the closest refreshed value while avoiding stale branches."""
        choices = list(choices or [])
        current = str(current or "")
        if current in choices:
            return current
        if current.startswith("origin/") and current[7:] in choices:
            return current[7:]
        if current and f"origin/{current}" in choices:
            return f"origin/{current}"
        return choices[0] if choices else current

    def refresh_branch_choices(self, name):
        """Refresh one choice source and any selectors sharing it."""
        name = str(name or "").strip()
        if name == "GIT_BRANCH2" and "GIT_BRANCH" in getattr(self, "param_vars", {}):
            name = "GIT_BRANCH"
        client = getattr(self, "jenkins", None)
        path = list(getattr(self, "current_job_path", []) or [])
        info = getattr(self, "param_vars", {}).get(name)
        button = getattr(self, "param_refresh_buttons", {}).get(name)
        if (
            not name
            or client is None
            or not path
            or not info
            or info[0] != "combo"
        ):
            return

        requests = getattr(self, "_branch_refresh_requests", {})
        if name in requests:
            return

        self._branch_refresh_generation = getattr(
            self, "_branch_refresh_generation", 0
        ) + 1
        request_id = self._branch_refresh_generation
        parameter_generation = getattr(self, "_parameter_generation", 0)
        token = (name, parameter_generation, request_id)
        requests[name] = (parameter_generation, request_id)
        active_tokens = getattr(self, "_branch_refresh_active_tokens", set())
        active_tokens.add(token)
        self._branch_refresh_active_tokens = active_tokens
        self._branch_refresh_requests = requests

        if button is not None:
            button.setEnabled(False)
            button.setText("刷新中…")
        label = "选项" if name == "CUSTOM_NAME" else "分支"
        self.emit("loading_start", f"正在刷新 {name} {label}...")

        job_url = str(getattr(self, "current_job_url", "") or "").strip()
        if not job_url:
            job_url = jenkins_job_api_url(getattr(client, "url", ""), path)
        self.run_thread(
            lambda: self._load_branch_choices(
                client,
                job_url,
                name,
                path,
                parameter_generation,
                request_id,
            ),
            lambda: self._finish_branch_refresh(
                name, parameter_generation, request_id
            ),
        )

    def _load_branch_choices(
        self, client, job_url, name, path, parameter_generation, request_id
    ):
        # GIT_BRANCH2 is a free-text override backed by GIT_BRANCH's repository list.
        source_name = "GIT_BRANCH" if name == "GIT_BRANCH2" else name
        try:
            fetch_choices = (
                (lambda url, parameter: client._get_parameter_choices(path, parameter, job_url=url))
                if name == "CUSTOM_NAME" else getattr(client, "get_git_parameter_choices", None)
            )
            if fetch_choices is None:
                # Compatibility with injected/test clients that still expose
                # only the original private Jenkins helper.
                fetch_choices = getattr(client, "_get_git_parameter_choices", None)
            if fetch_choices is None:
                get_parameters = getattr(client, "get_parameters", None)
                refreshed = get_parameters(path) if get_parameters else []
                match = next(
                    (
                        item
                        for item in refreshed or []
                        if isinstance(item, dict) and item.get("name") == source_name
                    ),
                    {},
                )
                choices = match.get("choices") or []
            else:
                choices = fetch_choices(job_url, source_name)
            if isinstance(choices, str):
                choices = [choices]
            else:
                try:
                    choices = list(choices or [])
                except TypeError:
                    # A lightweight injected client may expose only the
                    # complete parameter endpoint.  Fall back to it when a
                    # stubbed single-parameter call has no iterable result.
                    get_parameters = getattr(client, "get_parameters", None)
                    if get_parameters is None:
                        choices = []
                    else:
                        refreshed = get_parameters(path)
                        match = next(
                            (
                                item
                                for item in refreshed or []
                                if isinstance(item, dict) and item.get("name") == source_name
                            ),
                            {},
                        )
                        choices = list(match.get("choices") or [])
            normalized = []
            seen = set()
            for value in choices:
                value = str(value).strip()
                if value and value not in seen:
                    normalized.append(value)
                    seen.add(value)
            self.emit(
                "branch_choices",
                (
                    client,
                    tuple(path),
                    name,
                    parameter_generation,
                    request_id,
                    normalized,
                ),
            )
        except Exception as exc:
            self.emit(
                "branch_refresh_error",
                (
                    name,
                    parameter_generation,
                    request_id,
                    exception_message(exc),
                ),
            )

    @Slot(object)
    def _on_branch_choices(self, payload):
        (
            client,
            path,
            name,
            parameter_generation,
            request_id,
            choices,
        ) = payload
        active = getattr(self, "_branch_refresh_requests", {}).get(name)
        if (
            active != (parameter_generation, request_id)
            or parameter_generation != getattr(self, "_parameter_generation", 0)
            or client is not getattr(self, "jenkins", None)
            or tuple(getattr(self, "current_job_path", []) or []) != tuple(path)
        ):
            return
        info = getattr(self, "param_vars", {}).get(name)
        if not info or info[0] != "combo":
            return
        shared_branches = name in ("GIT_BRANCH", "GIT_BRANCH2") and all(
            parameter in self.param_vars for parameter in ("GIT_BRANCH", "GIT_BRANCH2"))
        targets = ("GIT_BRANCH", "GIT_BRANCH2") if shared_branches else (name,)
        for parameter in targets:
            widget = self.param_vars[parameter][1]
            current = widget.currentText()
            widget.set_values(choices)
            # Refresh changes shared options, never either independent override.
            selected = (current if shared_branches or parameter in ("GIT_BRANCH2", "CUSTOM_NAME")
                        else self._preferred_branch_choice(current, choices))
            widget.setCurrentText(selected)
        key = getattr(self, "_displayed_parameter_key", None)
        params = self._cached_parameters(key) if key else None
        if params is not None:
            for param in params:
                if param.get("name") in targets:
                    param["choices"] = list(choices)
            self._save_parameter_cache(key, params)
        button = getattr(self, "param_refresh_buttons", {}).get(name)
        if button is not None:
            label = "选项" if name == "CUSTOM_NAME" else "分支"
            button.setToolTip(f"已获取 {len(choices)} 个最新{label}；点击可再次刷新")

    @Slot(object)
    def _on_branch_refresh_error(self, payload):
        name, parameter_generation, request_id, message = payload
        active = getattr(self, "_branch_refresh_requests", {}).get(name)
        if (
            active != (parameter_generation, request_id)
            or parameter_generation != getattr(self, "_parameter_generation", 0)
        ):
            return
        overlay = getattr(self, "message_overlay", None)
        if overlay is not None:
            overlay.show_message(
                "选项刷新失败" if name == "CUSTOM_NAME" else "分支刷新失败",
                f"{name}：{message}",
                kind="error",
            )
        else:
            self.emit("error", f"{name}：{message}")

    def _finish_branch_refresh(self, name, parameter_generation, request_id):
        token = (name, parameter_generation, request_id)
        active_tokens = getattr(self, "_branch_refresh_active_tokens", set())
        if token not in active_tokens:
            # QThread exposes its own ``finished`` signal and Worker also
            # emits a compatibility signal with the same name, so a callback
            # can arrive twice.  Only the first completion owns the loading
            # indicator and button state.
            return
        active_tokens.remove(token)
        active = getattr(self, "_branch_refresh_requests", {}).get(name)
        if active == (parameter_generation, request_id):
            self._branch_refresh_requests.pop(name, None)
            button = getattr(self, "param_refresh_buttons", {}).get(name)
            if button is not None:
                button.setEnabled(bool(getattr(self, "jenkins", None)))
                button.setText("刷新选项" if name == "CUSTOM_NAME" else "刷新分支")
        # Every request emits one loading_start, including requests that
        # became stale after the user selected another Jenkins job.
        self.emit("loading_stop", None)

    def _get_param_value(self, name):
        """Get the current value of a build parameter."""
        info = self.param_vars.get(name)
        if not info:
            return ""
        kind, widget = info
        if kind == "combo":
            return widget.currentText()
        elif kind == "bool":
            return "true" if widget.isChecked() else "false"
        else:
            return widget.text()

    # ── Build Pipeline ─────────────────────────────────────────────
    def _cloud_status_settings_from_ui(self):
        return {
            "name": "Rancher Pod 状态页",
            "url": self.cloud_status_url.text().strip(),
            "username": self.cloud_status_username.text().strip(),
            "password_plain": self.cloud_status_password.text(),
        }

    def _save_cloud_status_settings(self, settings, save=True):
        self.store.data["cloud_status"] = {
            "url": settings.get("url", "").strip(),
            "username": settings.get("username", "").strip(),
            "password": protect_secret(settings.get("password_plain", "")),
        }
        if save:
            self.store.save()

    def _build_cloud_settings_from_ui(self):
        region_label = self.build_cloud_region.currentText().strip()
        if " / " in region_label:
            cloud, region = region_label.split(" / ", 1)
        else:
            cloud = self.store.data.get("cloud_deploy", {}).get("cloud", "")
            region = region_label
        return {
            "url": self.cloud_url.text().strip(),
            "username": self.cloud_username.text().strip(),
            "password_plain": self.cloud_password.text(),
            "project": self.build_cloud_project.currentText().strip(),
            "deploy_type": self.build_cloud_type.currentText().strip(),
            "cloud": cloud.strip(),
            "region": region.strip(),
            "package": "",
            "status_page": self._cloud_status_settings_from_ui(),
        }

    @staticmethod
    def _cloud_account_complete(settings):
        return all(settings.get(key) for key in ("url", "username", "password_plain"))

    def _on_build_deploy_mode_changed(self, mode):
        self._apply_build_deploy_mode(mode, prompt_for_account=True)

    @staticmethod
    def _deploy_after_build_enabled(tool):
        checkbox = getattr(tool, "deploy_after_build_checkbox", None)
        return checkbox is None or checkbox.isChecked()

    def _on_deploy_after_build_changed(self, checked):
        self.store.data["jenkins"]["deploy_after_build"] = bool(checked)
        self.store.save()
        self._apply_build_deploy_mode(
            self.build_deploy_mode.currentText(), prompt_for_account=bool(checked)
        )

    def _handle_active_batch_registration(self, active_state):
        if not active_state:
            return
        phase = active_state["phase"]
        if phase in ("claimed", "preparing"):
            self.batch_status_label.setText("等待当前批次，拉镜像前自动合并")
            self.emit(
                "build_log",
                "检测到同环境批次尚未开始拉镜像，本任务打包完成后将自动并入。",
            )
            return
        if active_state["restart_requested"]:
            self.batch_status_label.setText("已加入待重新合并批次")
            return
        if phase == "pull":
            choice = self.message_overlay.ask_batch_interruption(
                "同环境上一批正在从源仓库拉取镜像。\n\n"
                "继续当前轮：当前搬运完成后，本任务进入下一轮。\n"
                "中断并重新合并：安全终止当前拉取，等待本任务打包完成后重新生成多包清单。\n\n"
                "如果当前阶段已经转为推送，软件会自动拒绝中断并改为下一轮。"
            )
            if choice == "interrupt":
                coordinator = self.window_manager.batch_coordinator
                if coordinator.request_restart(active_state["token"]):
                    self.batch_status_label.setText("等待安全中断并重新合批")
                    self.emit("build_log", "已请求安全中断当前镜像拉取并重新合批。")
                    for owner in active_state["owners"]:
                        owner.emit(
                            "deploy_progress",
                            (15, "收到同环境新任务，正在安全中断拉取以重新合批..."),
                        )
                else:
                    self.batch_status_label.setText("当前阶段已变化，本任务进入下一轮")
                    self.emit(
                        "build_log",
                        "确认时当前批次已离开拉取阶段，已按安全策略进入下一轮。",
                    )
            else:
                self.batch_status_label.setText("当前轮继续，本任务进入下一轮")
            return
        phase_labels = {
            "push": "上一批正在推送镜像",
            "apply": "上一批正在更新并应用 YAML",
        }
        self.batch_status_label.setText(
            f"{phase_labels.get(phase, '上一批正在部署')}，本任务进入下一轮"
        )

    def _apply_build_deploy_mode(
        self, mode, prompt_for_account, refresh_cloud=True
    ):
        is_cloud = mode == "云端部署"
        if hasattr(self, "params_card"):
            entered_ssh = not is_cloud and getattr(self, "_parameters_deploy_mode", None) != mode
            self._parameters_deploy_mode = mode
            self.params_card.set_expanded(False if entered_ssh else self.params_card._toggle.isChecked())
        deploy_after_build = DeployTool._deploy_after_build_enabled(self)
        self.build_deploy_mode.setEnabled(deploy_after_build and not self.build_running)
        self.build_env_label.setVisible(not is_cloud)
        self.build_env_combo.setVisible(not is_cloud)
        self.build_env_label.setEnabled(deploy_after_build)
        self.build_env_combo.setEnabled(deploy_after_build)
        self.build_env_card.setVisible(deploy_after_build and not is_cloud)
        self.build_cloud_card.setVisible(deploy_after_build and is_cloud)
        self.batch_status_label.setVisible(deploy_after_build and not is_cloud)
        if deploy_after_build and not is_cloud and not self.build_running:
            self.batch_status_label.setText("自动合批待命")
        if not deploy_after_build:
            self.build_button.setText("开始打包")
        else:
            self.build_button.setText(
                "开始打包并云端部署" if is_cloud else "开始打包、部署并查看"
            )
        self.store.data["jenkins"]["deploy_mode"] = mode
        self.store.save()
        if not deploy_after_build:
            self.build_button.setEnabled(not self.build_running)
            return
        if not is_cloud:
            self._update_build_cloud_ready()
            return

        self.build_cloud_target_dirty = True
        settings = self._build_cloud_settings_from_ui()
        if not self._cloud_account_complete(settings):
            self.build_cloud_loading_label.setText(
                "请先在“云端部署”页填写并保存 Ops 登录账号"
            )
            self.build_button.setEnabled(False)
            if prompt_for_account:
                QMessageBox.warning(
                    self,
                    "缺少云端账号",
                    "当前没有完整的 Ops 登录信息，请先到“云端部署”页填写账号和密码。",
                )
                DeployTool._select_page(self, "CloudDeployPage")
            return
        if not refresh_cloud:
            self.build_cloud_loading_label.setText(
                "配置已导入，点击“刷新云端目标”后即可部署"
            )
            self._update_build_cloud_ready()
            return
        self.refresh_build_cloud_options()

    def _set_build_cloud_loading(self, loading, text=""):
        self.build_cloud_loading = loading
        if not hasattr(self, "build_cloud_progress"):
            return
        self.build_cloud_progress.setVisible(loading)
        self.build_cloud_loading_label.setText(text)
        for widget in (
            self.build_cloud_project,
            self.build_cloud_type,
            self.build_cloud_region,
            self.build_cloud_refresh_button,
        ):
            widget.setEnabled(not loading)
        self._update_build_cloud_ready()

    def _invalidate_build_cloud_target(self, _text=""):
        if self.build_deploy_mode.currentText() != "云端部署":
            return
        if not DeployTool._deploy_after_build_enabled(self):
            return
        self.build_cloud_target_dirty = True
        self.build_cloud_type.setEnabled(False)
        self.build_cloud_region.setEnabled(False)
        self.build_button.setEnabled(False)
        self.build_cloud_loading_label.setText(
            "部署专案已修改，请按 Enter 或点击“刷新云端目标”完成确认"
        )

    def _update_build_cloud_ready(self):
        if not hasattr(self, "build_deploy_mode"):
            return
        if getattr(self, "_job_cache_loading", False):
            self.build_button.setEnabled(False)
            return
        if (
            getattr(self, "current_job_path", [])
            and getattr(self, "_active_parameter_key", None)
            != getattr(self, "_displayed_parameter_key", None)
        ):
            self.build_button.setEnabled(False)
            return
        if not DeployTool._deploy_after_build_enabled(self):
            self.build_button.setEnabled(not self.build_running)
            return
        if self.build_deploy_mode.currentText() != "云端部署":
            self.build_button.setEnabled(not self.build_running)
            return
        settings = self._build_cloud_settings_from_ui()
        # The saved Ops target may be loaded before Jenkins login or while the
        # user is still navigating the task tree. Do not classify that temporary
        # state as a project mismatch; validation starts after a task is selected.
        task_selected = bool(self.current_job_path and self.current_display_path)
        project_matches = task_selected and cloud_project_matches_job(
            self.current_display_path, settings.get("project", "")
        )
        ready = (
            self._cloud_account_complete(settings)
            and all(settings.get(key) for key in ("project", "deploy_type", "cloud", "region"))
            and not self.build_cloud_loading
            and not self.build_cloud_target_dirty
            and not self.build_running
            and task_selected
            and project_matches
        )
        self.build_button.setEnabled(ready)
        if (
            not self.build_cloud_loading
            and not self.build_cloud_target_dirty
            and settings.get("project")
        ):
            if not task_selected:
                self.build_cloud_loading_label.setStyleSheet(
                    f'color: {COLORS["text_secondary"]}; background: transparent;'
                )
                module = extract_job_project_code(self.current_display_path)
                self.build_cloud_loading_label.setText(
                    f"已匹配 {module}，请选择具体构建任务"
                    if module else "任务层级尚未确认，暂不识别部署专案"
                )
                return
            job_project = extract_job_project_code(self.current_display_path)
            if not job_project:
                self.build_cloud_loading_label.setStyleSheet(
                    f'color: {COLORS["danger"]}; background: transparent;'
                )
                self.build_cloud_loading_label.setText(
                    "无法从当前任务层级识别系统标识，已禁止云端部署"
                )
            elif not project_matches:
                self.build_cloud_loading_label.setStyleSheet(
                    f'color: {COLORS["danger"]}; background: transparent;'
                )
                self.build_cloud_loading_label.setText(
                    f'任务系统为 {job_project}，与部署专案“{settings["project"]}”不一致'
                )
            else:
                self.build_cloud_loading_label.setStyleSheet(
                    f'color: {COLORS["text_secondary"]}; background: transparent;'
                )
                self.build_cloud_loading_label.setText("云端部署目标已就绪")

    def refresh_build_cloud_options(self, *, force=False):
        if self.build_cloud_loading:
            return
        settings = self._build_cloud_settings_from_ui()
        if not self._cloud_account_complete(settings):
            self.build_button.setEnabled(False)
            QMessageBox.warning(
                self,
                "缺少云端账号",
                "请先到“云端部署”页填写 Ops 登录账号和密码。",
            )
            DeployTool._select_page(self, "CloudDeployPage")
            return
        self._queue_cloud_options(settings, "build", force=force)

    @staticmethod
    def _deployment_target_key(mode, environment=None, cloud_settings=None):
        """Return a stable lock key for one mutable runtime target."""
        normalize = lambda value: str(value or "").strip().casefold()
        if mode == "云端部署":
            settings = cloud_settings or {}
            values = (
                settings.get("project"),
                settings.get("deploy_type"),
                settings.get("cloud"),
                settings.get("region"),
            )
            return "cloud:" + "|".join(normalize(value) for value in values)
        settings = environment or {}
        values = (
            settings.get("host"),
            settings.get("port") or "22",
            settings.get("yaml_dir"),
            settings.get("yaml_file"),
        )
        return "ssh:" + "|".join(normalize(value) for value in values)

    @staticmethod
    def _batch_deployment_key(environment):
        """Group environments that can share one image-transfer command."""
        normalize = lambda value: str(value or "").strip().casefold()
        values = (
            environment.get("host"),
            environment.get("port") or "22",
            environment.get("username"),
            environment.get("image_dir"),
            environment.get("image_file"),
            environment.get("pull_command"),
        )
        return "ssh-batch:" + "|".join(normalize(value) for value in values)

    @staticmethod
    def _dashboard_identity(dashboard):
        """Return the stable identity used to avoid duplicate batch logins."""
        dashboard = dashboard or {}
        for field in ("id", "url", "name"):
            value = str(dashboard.get(field, "")).strip().casefold()
            if value:
                return field, value
        return None

    @staticmethod
    def _open_batch_dashboards(entries):
        """Open each Dashboard/product pair once from its owning task window."""
        opened = set()
        for entry in entries:
            dashboard = entry.get("dashboard") or {}
            owner = entry.get("owner")
            identity = DeployTool._dashboard_identity(dashboard)
            if owner is None or identity is None:
                continue
            if entry.get("package"):
                dashboard = {
                    **dashboard,
                    "namespace": infer_package_target(entry["package"])["product"],
                }
            identity = (identity, dashboard.get("namespace") or "etm")
            if identity in opened:
                continue
            opened.add(identity)
            owner.emit("open_dashboard_item", dashboard)
        return len(opened)

    @trace_operation("target_queue_wait")
    def _wait_for_deployment_target(self, key, cancel_event=None, log_target="build_log"):
        window_manager = getattr(self, "window_manager", None)
        if not window_manager:
            return

        def announce_wait():
            trace_event(self, "waiting", wait_reason="same_deployment_target")
            self.emit(log_target, "相同部署目标正在被另一个任务使用，已进入安全队列...")
            if log_target == "build_log":
                self.emit("build_status", "等待相同目标的部署任务完成...")
                self.emit(
                    "deploy_progress",
                    (0, "相同环境正在部署，等待前一任务完成..."),
                )

        window_manager.target_coordinator.acquire(
            self, key, cancel_event=cancel_event, on_wait=announce_wait
        )

    def _release_deployment_target(self, key):
        window_manager = getattr(self, "window_manager", None)
        if window_manager:
            window_manager.target_coordinator.release(self, key)

    def _block_deployment_target(self, key, reason):
        manager = getattr(self, "window_manager", None)
        coordinator = getattr(manager, "target_coordinator", None)
        if coordinator:
            coordinator.block(self, key, str(reason))

    def start_build(self):
        if getattr(self, "_task_runtime_active", False):
            self.message_overlay.show_message(
                "当前任务正在运行",
                f"请等待当前操作结束，或点击标题栏“新建任务”并行执行（{QKeySequence(QKeySequence.New).toString(QKeySequence.NativeText)}）。",
            )
            return
        if self.build_running:
            QMessageBox.information(self, "正在构建", "当前 Jenkins 构建尚未结束")
            return
        if not self.jenkins or not self.current_job_path:
            QMessageBox.warning(self, "未选择任务", "请登录并选择 Jenkins 任务")
            return
        deployment_mode = self.build_deploy_mode.currentText()
        deploy_after_build = DeployTool._deploy_after_build_enabled(self)
        environment = None
        dashboard = None
        cloud_settings = None
        if deploy_after_build and deployment_mode == "云端部署":
            if self.build_cloud_loading or self.build_cloud_target_dirty:
                QMessageBox.warning(self, "云端目标加载中", "请等待云端部署目标加载完成")
                return
            cloud_settings = self._build_cloud_settings_from_ui()
            if not self._cloud_account_complete(cloud_settings):
                QMessageBox.warning(
                    self, "缺少云端账号",
                    "请先到“云端部署”页填写 Ops 登录账号和密码。",
                )
                DeployTool._select_page(self, "CloudDeployPage")
                return
            if any(
                not cloud_settings.get(key)
                for key in ("project", "deploy_type", "cloud", "region")
            ):
                QMessageBox.warning(
                    self, "云端目标不完整",
                    "请选择部署专案、服务类型和云区节点，等待配置加载完成后再执行。",
                )
                return
            job_project = extract_job_project_code(self.current_display_path)
            if not job_project:
                QMessageBox.warning(
                    self,
                    "无法校验部署专案",
                    "无法从当前 Jenkins 任务层级识别系统标识，已禁止云端部署。\n"
                    "请确认任务目录名称类似“15-ESC-设备中心”。",
                )
                return
            if not cloud_project_matches_job(
                self.current_display_path, cloud_settings["project"]
            ):
                QMessageBox.warning(
                    self,
                    "部署专案不一致",
                    f'当前 Jenkins 任务属于“{job_project}”，但部署专案为'
                    f'“{cloud_settings["project"]}”。\n请重新选择正确的部署专案后再执行。',
                )
                return
            # 冻结并保存本次管线使用的精确目标，避免构建期间配置漂移。
            cloud_settings = dict(cloud_settings)
            self._save_cloud_settings(cloud_settings)
        elif deploy_after_build:
            if self.env_dirty:
                QMessageBox.warning(
                    self, "环境配置未保存",
                    '目标环境配置已修改，请先点击"保存环境配置"再执行一键流程。',
                )
                return
            environment = self._selected_environment()
            if not environment:
                QMessageBox.warning(
                    self, "未选择环境",
                    '请先在"环境部署与 Dashboard"页新增并选择目标环境',
                )
                return
            preview = getattr(self, "_preview_ssh_target", None)
            if preview:
                environment.update(preview)
                environment["_preview_yaml"] = dict(preview)
            try:
                DeployTool._freeze_delete_options(self, environment)
            except ValueError as exc:
                QMessageBox.warning(self, "删除命令无效", str(exc))
                return
            dashboard = self._dashboard_for_environment(environment)
            if not dashboard:
                QMessageBox.warning(
                    self, "未关联 Dashboard",
                    "当前环境没有关联有效的 Dashboard，请编辑环境后选择 Dashboard",
                )
                return
        params = {name: self._get_param_value(name) for name in self.param_vars}
        if is_secondary_build_path(self.current_job_path):
            branch2 = params.get("GIT_BRANCH2", "").strip()
            branch = params.get("GIT_BRANCH", "").strip()
            if "GIT_BRANCH2" in params:
                params["GIT_BRANCH2"] = branch2 or branch
        key = "/".join(self.current_job_path)
        self.store.data["jenkins"].setdefault("values", {})[key] = params
        self.store.save()
        # Keep credentials/client in memory only. Each attempt gets its own
        # mutable copy so later form edits and deployment inference cannot
        # change the configuration used by Continue.
        snapshot = {"client": self.jenkins, "configuration": copy.deepcopy({
            "path": list(self.current_job_path), "job_url": self.current_job_url,
            "display_path": getattr(self, "current_display_path", []),
            "params": params, "deployment_mode": deployment_mode,
            "environment": environment, "dashboard": dashboard,
            "cloud_settings": cloud_settings, "deploy_after_build": deploy_after_build,
        })}
        DeployTool._launch_build(self, snapshot)

    def _launch_build(self, snapshot):
        configuration = copy.deepcopy(snapshot["configuration"])
        path = configuration["path"]
        job_url = configuration["job_url"]
        params = configuration["params"]
        deployment_mode = configuration["deployment_mode"]
        environment = configuration["environment"]
        dashboard = configuration["dashboard"]
        cloud_settings = configuration["cloud_settings"]
        deploy_after_build = configuration["deploy_after_build"]
        self._active_build_client = snapshot["client"]
        self.cancel_event.clear()
        self.build_log.clear()
        self.build_running = True
        self.active_deploy_after_build = deploy_after_build
        if not deploy_after_build:
            task_target = "仅打包"
        elif deployment_mode == "云端部署":
            task_target = " / ".join(
                value for value in (
                    cloud_settings.get("project", ""),
                    cloud_settings.get("deploy_type", ""),
                    cloud_settings.get("region", ""),
                ) if value
            )
        else:
            task_target = environment.get("name", "")
        display_path = configuration["display_path"]
        task_title = (
            display_path[-1] if display_path else path[-1]
        )
        if hasattr(self, "_begin_tracked_task"):
            self._begin_tracked_task(
                "一键流程" if deploy_after_build else "Jenkins 打包",
                task_target,
                title=task_title,
            )
        self._last_build_snapshot = snapshot
        self.batch_token = None
        auto_merge_ssh_deployments = bool(
            deploy_after_build
            and deployment_mode == "SSH 环境部署"
            and self.window_manager
        )
        if auto_merge_ssh_deployments:
            batch_key = self._batch_deployment_key(environment)
            self.batch_token, active_state = (
                self.window_manager.batch_coordinator.register_with_state(
                self, batch_key
                )
            )
            if hasattr(self, "batch_status_label"):
                self.batch_status_label.setText("已加入批次，正在打包")
            self._handle_active_batch_registration(active_state)
        self.pipeline_phase = "submitting"
        self.pipeline_location = ""
        self.build_button.setEnabled(False)
        self.build_deploy_mode.setEnabled(False)
        if hasattr(self, "deploy_after_build_checkbox"):
            self.deploy_after_build_checkbox.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._update_build_status("正在提交构建...")
        self.build_progress.setValue(0)
        self.deploy_log.clear()
        if deploy_after_build and deployment_mode == "SSH 环境部署":
            self.emit("deploy_progress", (0, "等待 Jenkins 打包完成..."))
        if hasattr(self, "_summary_selection"):
            self._running_summary_selection = (snapshot["client"], tuple(path), job_url)
            self._running_summary_build_url = ""
            self._summary_runs[self._running_summary_selection] = {"build_url": "", "finished": False}
            if self._running_summary_selection == self._build_summary_selection():
                self._summary_selection = None
                self._select_build_summary()
        batch_token = self.batch_token
        self.run_thread(
            lambda: self._run_pipeline(
                path, params, job_url, deployment_mode,
                environment, dashboard, cloud_settings, deploy_after_build,
                batch_token,
            )
        )

    def _run_pipeline(
        self, path, params, job_url, deployment_mode,
        environment, dashboard, cloud_settings, deploy_after_build=True,
        batch_token=None,
    ):
        client = getattr(self, "_active_build_client", None) or self.jenkins
        try:
            trace_event(self, "pipeline_started", job=" / ".join(path), mode=deployment_mode)
            self.emit("build_log", f"提交构建：{' / '.join(path)}")
            with trace_stage(self, "jenkins_submit"):
                queue_url = client.build(path, params, job_url)
            self.emit("build_status", "已提交，等待 Jenkins 执行器...")
            with trace_stage(self, "jenkins_wait"):
                result, console = client.wait_for_build(
                    queue_url,
                    lambda line: self._handle_build_output(line),
                    lambda percent, status: self._handle_build_progress(percent, status),
                    self.cancel_event,
                    lambda phase, url: self.emit("pipeline_location", (phase, url)),
                )
            status = result.get("result")
            trace_event(self, "jenkins_result", result=status,
                        build_number=result.get("number"), jenkins_duration_ms=result.get("duration"))
            self.emit("build_log", f"构建结束：#{result.get('number')} {status}")
            if self.cancel_event.is_set() or status == "ABORTED":
                raise PipelineCancelled("Jenkins 构建已终止")
            if status != "SUCCESS":
                raise RuntimeError(f"Jenkins 构建结果为 {status}")
            self.emit("build_status", f"构建成功：#{result.get('number')}")
            package = extract_package_number(console)
            if package:
                self.emit("package", package)
            elif deploy_after_build:
                raise RuntimeError("构建成功，但控制台中未识别到包号，无法自动部署")
            if not deploy_after_build:
                self.emit("build_status", "打包完成")
                if package:
                    self.emit("info", f"打包已完成：\n{package}")
                else:
                    self.emit(
                        "info",
                        f"Jenkins 打包已完成：#{result.get('number')}\n"
                        "控制台中未识别到包号，请在 Jenkins 中查看构建日志。",
                    )
                return
            if deployment_mode == "云端部署":
                cloud_settings["package"] = package
                target = (
                    f'{cloud_settings["project"]} / {cloud_settings["deploy_type"]} / '
                    f'{cloud_settings["cloud"]} {cloud_settings["region"]}'
                )
                self.emit("build_status", f"构建成功，开始云端部署 {target}...")
                self.emit("pipeline_location", ("cloud_deploy", ""))
                self._raise_if_deploy_cancelled()
                target_key = DeployTool._deployment_target_key(
                    deployment_mode, cloud_settings=cloud_settings
                )
                DeployTool._wait_for_deployment_target(
                    self, target_key, self.cancel_event
                )
                try:
                    self._raise_if_deploy_cancelled()
                    self._run_cloud_deploy(cloud_settings, self.cancel_event)
                finally:
                    DeployTool._release_deployment_target(self, target_key)
                self.emit("build_status", "一键云端部署流程执行完成")
                self.emit("info", f"打包、云端部署已完成：\n{package}\n{target}")
            else:
                # Jenkins controls the final package number. Re-resolve the
                # deployment target here so a stale saved profile (for example
                # etm.yaml) cannot be used for an ESC/PQM build.
                preview = environment.get("_preview_yaml")
                inferred = infer_ssh_deployment_config(package)
                if preview and any(inferred[key] != preview[key] for key in preview):
                    raise RuntimeError(
                        f"最终包号对应的 YAML 与任务预览不一致：预览 {preview['yaml_dir']}/{preview['yaml_file']}，"
                        f"实际 {inferred['yaml_dir']}/{inferred['yaml_file']}。已停止部署，请核对 Jenkins 任务。")
                environment = resolve_ssh_environment_for_package(
                    environment, package
                )
                self.emit(
                    "deploy_log",
                    "已按 Jenkins 最终包号确认部署目标："
                    f'{environment["yaml_dir"]} / {environment["yaml_file"]}',
                )
                self.emit("build_status", f"构建成功，开始部署 {environment['name']}...")
                self.emit("pipeline_location", ("deploy", ""))
                self.emit("select_deploy_tab", None)
                if batch_token:
                    self.emit("build_status", "打包完成，等待合并部署...")
                    with trace_stage(self, "batch_wait", wait_reason="batch_ready_or_leader_completion"):
                        leader, entries = self.window_manager.batch_coordinator.wait_until_ready(
                            self,
                            batch_token,
                            {
                                "owner": self,
                                "environment": environment,
                                "package": package,
                                "dashboard": dashboard,
                            },
                            self.cancel_event,
                            lambda count, ready: self.emit(
                                "deploy_progress",
                                (0, f"已进入合并批次（{count} 个任务），打包完成后自动部署..."),
                            ),
                        )
                    if leader:
                        error = None
                        try:
                            active_entries = entries
                            while True:
                                try:
                                    self._execute_deploy_batch(
                                        active_entries, batch_token=batch_token
                                    )
                                    break
                                except BatchReassembled as reassembled:
                                    active_entries = reassembled.entries
                                    self.emit(
                                        "deploy_log",
                                        f"已按剩余有效任务重新合批，共 {len(active_entries)} 个任务。",
                                    )
                                except RemoteCommandInterrupted:
                                    self.emit(
                                        "deploy_progress",
                                        (0, "当前拉取已安全停止，等待新任务打包完成..."),
                                    )
                                    with trace_stage(self, "batch_rejoin_wait"):
                                        active_entries = (
                                            self.window_manager.batch_coordinator.collect_pending_entries(
                                                batch_token,
                                                active_entries,
                                                DeploymentCancellation(
                                                    [entry["owner"] for entry in active_entries], require_all=True,
                                                ),
                                                lambda count: self.emit(
                                                    "deploy_progress",
                                                    (
                                                        0,
                                                        f"等待 {count} 个新任务完成打包后重新合批...",
                                                    ),
                                                ),
                                            )
                                        )
                                    self.emit(
                                        "deploy_log",
                                        f"安全中断完成，重新合批后共有 {len(active_entries)} 个任务。",
                                    )
                        except Exception as exc:
                            error = exc
                            raise
                        finally:
                            self.window_manager.batch_coordinator.complete(
                                batch_token, error
                            )
                    self._raise_if_deploy_cancelled()
                    if leader:
                        dashboard_count = DeployTool._open_batch_dashboards(active_entries)
                        if dashboard_count:
                            self.emit(
                                "build_status",
                                f"合并部署完成，正在打开 {dashboard_count} 个 Dashboard...",
                            )
                        else:
                            self.emit("build_status", "合并部署完成")
                    else:
                        self.emit("build_status", "合并部署完成")
                    self.emit("build_status", "一键流程执行完成")
                    self.emit("info", f"打包、合并部署已完成：\n{package}")
                    return
                target_key = DeployTool._deployment_target_key(
                    deployment_mode, environment=environment
                )
                DeployTool._wait_for_deployment_target(
                    self, target_key, self.cancel_event
                )
                try:
                    self._raise_if_deploy_cancelled()
                    self._execute_deploy(environment, package, self.cancel_event)
                finally:
                    DeployTool._release_deployment_target(self, target_key)
                self._raise_if_deploy_cancelled()
                self.emit("build_status", "部署完成，正在打开 Dashboard...")
                self.emit("open_dashboard_item", {
                    **dashboard, "namespace": infer_package_target(package)["product"],
                })
                self.emit("build_status", "一键流程执行完成")
                self.emit("info", f"打包、部署已完成：\n{package}")
        except PipelineCancelled as exc:
            trace_event(self, "pipeline_result", result="cancelled", exception_type=type(exc).__name__)
            self.emit(
                "build_status",
                "一键流程已取消" if deploy_after_build else "打包已取消",
            )
            self.emit("build_log", str(exc))
            self.emit("info", str(exc))
        except Exception as exc:
            trace_event(self, "pipeline_result", result="failed", exception_type=type(exc).__name__)
            self.emit(
                "build_status",
                "一键流程执行失败" if deploy_after_build else "打包执行失败",
            )
            prefix = "一键流程失败" if deploy_after_build else "打包失败"
            self.emit("error", f"{prefix}：{exception_message(exc)}")
        finally:
            if batch_token and self.window_manager:
                self.window_manager.batch_coordinator.release_all(self)
            self.emit("build_finished", None)

    def _handle_build_output(self, line):
        self.emit("build_log", line)

    def _handle_build_progress(self, percent, status):
        self.emit("build_progress", percent)
        self.emit("build_status", status)

    def cancel_pipeline(self):
        if not self.build_running or self.cancel_event.is_set():
            return
        if self.pipeline_phase == "deploy":
            message = (
                "当前处于环境部署阶段。\n\n"
                "将停止本任务的镜像搬运和等待，跳过后续部署。共享批次中的其他任务会继续。\n"
                "若已开始删除或应用 Kubernetes 资源，将完成当前必要收尾。是否继续？"
            )
        elif self.pipeline_phase == "cloud_deploy":
            message = (
                "当前处于云端部署阶段。\n\n"
                "若部署请求尚未提交，将停止后续操作；Ops 已接受的部署请求无法在此撤回。"
                "是否继续？"
            )
        else:
            message = "确定终止当前 Jenkins 打包任务吗？"
        reply = QMessageBox.question(self, "确认取消", message)
        if reply != QMessageBox.Yes:
            return
        self.cancel_event.set()
        self.cancel_button.setEnabled(False)
        if self.pipeline_phase == "deploy":
            self._update_build_status("正在取消，确认远端停止并完成必要收尾...")
        elif self.pipeline_phase == "cloud_deploy":
            self._update_build_status("已请求取消，等待当前云端请求返回...")
        else:
            self._update_build_status("正在终止 Jenkins 构建...")
            phase = self.pipeline_phase
            location = self.pipeline_location
            self.run_thread(lambda: self._cancel_jenkins_remote(phase, location))

    def _cancel_jenkins_remote(self, phase, location):
        try:
            client = getattr(self, "_active_build_client", None) or self.jenkins
            client.cancel_build(
                phase,
                location,
                lambda message: self.emit("build_log", message),
            )
            self.emit("build_status", "Jenkins 构建已确认终止")
        except Exception as exc:
            self.emit("build_log", f"Jenkins 远程取消失败：{exception_message(exc)}")
            self.emit("error", f"Jenkins 远程取消失败：{exception_message(exc)}")

    def _raise_if_deploy_cancelled(self):
        if self.cancel_event.is_set():
            raise PipelineCancelled("当前部署步骤已结束，后续步骤已取消")

    # ── Environment Management ─────────────────────────────────────
    def _selected_environment(self):
        name = self.build_env_combo.currentText()
        stored = next(
            (
                item for item in self.store.data.get("environments", [])
                if item["name"] == name
            ),
            None,
        )
        if not stored:
            return None
        data = dict(stored)
        data["password"] = unprotect_secret(data.get("password", ""))
        return data

    def _dashboard_for_environment(self, environment):
        """Resolve the stable binding first, then support legacy name-only configs."""
        dashboard_id = environment.get("dashboard_id", "")
        if dashboard_id:
            dashboard = next(
                (
                    item for item in self.store.data.get("dashboards", [])
                    if item.get("id") == dashboard_id
                ),
                None,
            )
            if dashboard:
                return dashboard
        dashboard_name = environment.get("dashboard", "")
        return next(
            (
                item for item in self.store.data.get("dashboards", [])
                if item.get("name") == dashboard_name
            ),
            None,
        )

    def refresh_environment_names(self):
        names = [item["name"] for item in self.store.data.get("environments", [])]
        selected = self.env_combo.currentText() or self.build_env_combo.currentText()
        if selected not in names:
            selected = names[0] if names else ""

        # Rebuilding either combo emits currentTextChanged. Block environment
        # loading until both selectors contain the same final value.
        self.loading_environment = True
        self.env_combo.set_values(names)
        self.build_env_combo.set_values(names)
        self.env_combo.setCurrentText(selected)
        self.build_env_combo.setCurrentText(selected)
        self.loading_environment = False
        if names:
            self.load_environment(selected)
        else:
            self._clear_environment_display()

    def new_environment(self):
        dialog = EnvironmentDialog(
            self, "新增环境", dashboard_names=self._dashboard_names()
        )
        if dialog.exec() != QDialog.Accepted or not dialog.result:
            return
        if any(
            item["name"] == dialog.result["name"]
            for item in self.store.data["environments"]
        ):
            QMessageBox.warning(
                self, "名称重复",
                f'环境"{dialog.result["name"]}"已存在，请使用编辑功能',
            )
            return
        self.store.data["environments"].append(dialog.result)
        self.store.save()
        self.build_env_combo.setCurrentText(dialog.result["name"])
        self.env_combo.setCurrentText(dialog.result["name"])
        self.refresh_environment_names()

    def load_environment(self, name=None):
        if self.loading_environment:
            return
        # currentTextChanged supplies the selector that the user actually
        # changed. Previously this method always preferred build_env_combo,
        # causing selections on the deployment page to be ignored.
        name = (name or "").strip()
        if not name:
            name = self.env_combo.currentText() or self.build_env_combo.currentText()
        item = next((x for x in self.store.data["environments"] if x["name"] == name), None)
        if not item:
            self.refresh_environment()
            return
        self.loading_environment = True
        for _, key in ENVIRONMENT_FIELDS:
            value = item.get(key, DEFAULT_DEPLOY.get(key, ""))
            if key == "password":
                value = unprotect_secret(value)
            self._set_env_var(key, value)
        # Sync combo
        if self.env_combo.currentText() != name:
            self.env_combo.setCurrentText(name)
        if self.build_env_combo.currentText() != name:
            self.build_env_combo.setCurrentText(name)
        self.loading_environment = False
        self.env_dirty = False
        if (hasattr(self, "deploy_package_preview")
                and self.deploy_package_preview.topLevelItemCount() > 1
                and not self.deploy_package_preview.isHidden()
                and self.tab_widget.currentWidget().objectName() == "DeployPage"):
            DeployTool._preview_ssh_fields_from_package(self, report_error=False)
        elif hasattr(self, "build_yaml_preview_label"):
            DeployTool._sync_ssh_target_from_job(self)

    def save_environment(self):
        name = self.build_env_combo.currentText()
        if not name:
            name = self.env_combo.currentText()
        index = next(
            (i for i, item in enumerate(self.store.data["environments"])
             if item["name"] == name),
            None,
        )
        if index is None:
            QMessageBox.warning(self, "未选择环境", "请先选择要保存的环境")
            return
        data = {key: self._get_env_var(key).strip() for _, key in ENVIRONMENT_FIELDS}
        dashboard = next(
            (
                item for item in self.store.data.get("dashboards", [])
                if item.get("name") == data["dashboard"]
            ),
            None,
        )
        error = _environment_validation_error(data, dashboard is not None)
        if error:
            QMessageBox.warning(self, *error)
            return
        data["password"] = protect_secret(data["password"])
        if any(
            i != index and item["name"] == data["name"]
            for i, item in enumerate(self.store.data["environments"])
        ):
            QMessageBox.warning(self, "名称重复", f'环境"{data["name"]}"已存在')
            return
        data["dashboard_id"] = dashboard["id"]
        old_name = self.store.data["environments"][index].get("name", "")
        if data["name"] != old_name:
            try:
                # The environment-to-Dashboard binding is stable by ID, so a
                # renamed environment keeps its Dashboard and synchronizes its label.
                rename_bound_dashboard(
                    self.store.data, data["dashboard_id"], data["name"]
                )
                data["dashboard"] = data["name"]
            except ValueError as exc:
                QMessageBox.warning(self, "名称冲突", str(exc))
                return
        self.store.data["environments"][index] = data
        self.store.save()
        self.build_env_combo.setCurrentText(data["name"])
        self.env_combo.setCurrentText(data["name"])
        self.refresh_environment_names()
        self.refresh_dashboards()
        self.env_dirty = False
        QMessageBox.information(self, "保存成功", f'环境"{data["name"]}"已保存')

    def delete_environment(self):
        name = self.env_combo.currentText()
        if not name:
            return
        reply = QMessageBox.question(self, "确认删除", f'删除环境"{name}"？')
        if reply == QMessageBox.Yes:
            self.store.data["environments"] = [
                item for item in self.store.data["environments"] if item["name"] != name
            ]
            self.store.save()
            self.refresh_environment_names()

    def _clear_environment_display(self):
        self.loading_environment = True
        for _, key in ENVIRONMENT_FIELDS:
            self._set_env_var(key, DEFAULT_DEPLOY.get(key, "22" if key == "port" else ""))
        self.loading_environment = False
        self.env_dirty = False

    def refresh_environment(self):
        name = self.build_env_combo.currentText()
        if not name:
            name = self.env_combo.currentText()
        item = next(
            (
                environment
                for environment in self.store.data.get("environments", [])
                if environment["name"] == name
            ),
            None,
        )
        if item:
            self.load_environment()
        else:
            self._clear_environment_display()
        self.remote_file_maps = {"image": {}, "yaml": {}}
        for entries in (self.env_entries, self.build_env_entries):
            for key in ("image_dir", "image_file", "yaml_dir", "yaml_file"):
                widget = entries.get(key)
                if isinstance(widget, SearchableComboBox):
                    widget.set_values([])

    def _dashboard_names(self):
        return [item["name"] for item in self.store.data.get("dashboards", [])]

    # ── Remote Operations ──────────────────────────────────────────
    def load_remote_options(self):
        if self.remote_loading:
            return
        data = {key: self._get_env_var(key).strip() for _, key in ENVIRONMENT_FIELDS}
        if not data["host"] or not data["username"] or not data["password"]:
            QMessageBox.warning(self, "缺少 SSH 信息", "请先填写 IP、账号和密码")
            return
        self.remote_loading = True
        self.remote_status_label.setText("正在连接并加载...")
        self.build_remote_status_label.setText("正在加载...")
        self.remote_progress.show()
        self.build_remote_progress.show()
        self.run_thread(lambda: self._load_remote_options(data))

    def _load_remote_options(self, data):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_ssh_client(client, data)
            sftp = client.open_sftp()
            image_dirs = self._remote_directory_choices(
                sftp, data.get("image_dir") or "/home/setup/images"
            )
            yaml_dirs = self._remote_directory_choices(
                sftp, data.get("yaml_dir") or "/data/yaml/app/backend"
            )
            image_dir = data.get("image_dir") or image_dirs[0]
            yaml_dir = data.get("yaml_dir") or yaml_dirs[0]
            image_file_map = {
                directory: self._remote_file_choices(sftp, directory)
                for directory in image_dirs
            }
            yaml_file_map = {
                directory: self._remote_file_choices(sftp, directory)
                for directory in yaml_dirs
            }
            payload = {
                "image_dir": image_dirs,
                "image_file": image_file_map.get(image_dir, []),
                "yaml_dir": yaml_dirs,
                "yaml_file": yaml_file_map.get(yaml_dir, []),
                "_file_maps": {"image": image_file_map, "yaml": yaml_file_map},
            }
            self.emit("remote_options", payload)
        except Exception as exc:
            self.emit("error", f"加载远程目录失败：{exc}")
        finally:
            client.close()
            self.emit("remote_loading_done", None)

    @staticmethod
    def _remote_directory_choices(sftp, current):
        current = posixpath.normpath(current)
        parent = posixpath.dirname(current) or "/"
        choices = {current}
        for base in (parent, current):
            try:
                for item in sftp.listdir_attr(base):
                    if stat.S_ISDIR(item.st_mode):
                        choices.add(posixpath.join(base, item.filename))
            except OSError:
                continue
        return sorted(choices)

    @staticmethod
    def _remote_file_choices(sftp, directory):
        result = []
        for item in sftp.listdir_attr(directory):
            if not stat.S_ISDIR(item.st_mode):
                result.append(item.filename)
        return sorted(result)

    def _apply_remote_file_choices(self, kind):
        directory = self._get_env_var(f"{kind}_dir").strip()
        values = self.remote_file_maps.get(kind, {}).get(directory, [])
        for entries in (self.env_entries, self.build_env_entries):
            widget = entries.get(f"{kind}_file")
            if isinstance(widget, SearchableComboBox):
                widget.set_values(values)

    # ── Deploy ─────────────────────────────────────────────────────
    def start_deploy(self):
        if getattr(self, "_task_runtime_active", False):
            self.message_overlay.show_message(
                "当前任务正在运行",
                f"请等待当前操作结束，或点击标题栏“新建任务”并行执行（{QKeySequence(QKeySequence.New).toString(QKeySequence.NativeText)}）。",
            )
            return
        try:
            packages = parse_deployment_packages(self.package_input.text())
        except ValueError as exc:
            QMessageBox.warning(self, "包号无效", str(exc))
            return
        data = {key: self._get_env_var(key).strip() for _, key in ENVIRONMENT_FIELDS}
        if not data["host"] or not data["username"]:
            QMessageBox.warning(self, "缺少信息", "请先新增并选择一个环境")
            return
        entries = []
        try:
            for package in packages:
                environment = resolve_ssh_environment_for_package(data, package)
                DeployTool._freeze_delete_options(self, environment)
                entries.append({"owner": self, "package": package, "environment": environment,
                                "result": "未执行"})
        except ValueError as exc:
            QMessageBox.warning(self, "部署配置无效", str(exc))
            return
        self.deploy_log.clear()
        # Keep the legacy single-package default usable by the cloud page.
        self.store.data["last_package"] = packages[0]
        self.store.save()
        if hasattr(self, "cancel_event"):
            self.cancel_event.clear()
        multiple = len(entries) > 1
        package_label = "\n".join(packages)
        target = self.env_combo.currentText().strip() or data.get("host", "")
        if hasattr(self, "_begin_tracked_task"):
            self._begin_tracked_task(
                "SSH 批量部署" if multiple else "SSH 部署",
                target,
                package=package_label,
                title=f"批量部署 {len(entries)} 个包" if multiple else packages[0].split(":", 1)[0],
            )
        self.emit("deploy_progress", (0, "准备连接目标环境..."))
        self.run_thread(
            (lambda: self._run_manual_deployment_batch(entries)) if multiple else
            (lambda: self._run_deploy_with_target_lock(entries[0]["environment"], packages[0])),
            on_finished=getattr(self, "_finish_standalone_task", None),
        )

    def _run_manual_deployment_batch(self, entries):
        try:
            self._execute_deploy_batch(entries, unified_apply=True)
            self.emit("deploy_progress", (100, f"批量部署完成：{len(entries)} 个包"))
            self.emit("info", f"{len(entries)} 个包已完成环境部署")
        except PipelineCancelled as exc:
            self.emit("deploy_progress", (0, "批量部署已取消"))
            self.emit("deploy_log", exception_message(exc))
        except Exception as exc:
            self.emit("deploy_progress", (0, "批量部署未全部完成，请查看各包结果"))
            self.emit("error", f"批量部署失败：{exception_message(exc)}")
        finally:
            self.emit("deploy_log", "批量部署结果：")
            for entry in entries:
                result = entry.get("result", "未执行")
                self.emit("deploy_log", f'{entry["package"]}：{result}')
                trace_event(self, "batch_package_result", package=entry["package"], result=result)

    @trace_operation("remote_command")
    def _remote_exec(
        self,
        client,
        command,
        progress_range=None,
        interrupt_event=None,
        process_group_file=None,
        on_phase=None,
        interrupt_allowed=None,
        on_interrupt_failed=None,
        transfer_images=None,
        on_progress=None,
        on_image_progress=None,
        cancel_event=None,
        timeout_seconds=None,
        idle_timeout_seconds=None,
        on_image_log=None,
    ):
        """Execute a remote command without flooding Qt with TTY redraws."""
        if cancel_event and cancel_event.is_set():
            raise PipelineCancelled("远端命令启动前已取消")
        self.emit("deploy_log", "$ " + command)
        try:
            with ssh_request_deadline(client, 15):
                stdin, stdout, stderr = client.exec_command(command, get_pty=True, timeout=15)
        except Exception as exc:
            raise RemoteCommandUncertain("无法确认远端命令是否启动或停止，请检查服务器后重启工具") from exc
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        last_log_line = ""
        largest_total = 0
        last_progress = -1
        transfer_phase = ""
        transfer_range = progress_range
        command_started_at = time.monotonic()
        timeout_seconds = timeout_seconds if timeout_seconds is not None else (3600 if progress_range else 300)
        idle_timeout_seconds = idle_timeout_seconds if idle_timeout_seconds is not None else (600 if progress_range else 0)
        last_activity_at = command_started_at
        last_change_at = command_started_at
        last_heartbeat_at = command_started_at
        waiting_reported = False
        last_output_by_layer = {}
        pending_transfer_logs = {}
        transfer_log_times = {}
        image_progress = DockerImageProgress(transfer_images, progress_range) if transfer_images and progress_range else None
        transfer_trace = RemoteTransferTrace(self) if progress_range else None

        def report_log(line, image=""):
            if image and on_image_log:
                on_image_log(image, line)
            else:
                self.emit("deploy_log", line)

        def flush_transfer_logs(now, force=False, only_key=None):
            # Keep the newest real bytes per layer without flooding the Qt queue.
            for key, line in list(pending_transfer_logs.items()):
                if only_key is not None and key != only_key:
                    continue
                if force or now - transfer_log_times.get(key, float("-inf")) >= 1:
                    report_log(line, key[0])
                    transfer_log_times[key] = now
                    del pending_transfer_logs[key]

        def report_progress(percent, status):
            if on_progress:
                on_progress(percent, status)
            else:
                self.emit("deploy_progress", (percent, status))

        def report_image_progress(update):
            if update:
                if on_image_progress:
                    on_image_progress(*update)
                else:
                    report_progress(update[1], update[2])

        def consume(text, final=False):
            nonlocal pending, last_log_line, largest_total, last_progress
            nonlocal transfer_phase, transfer_range, last_activity_at, last_change_at
            nonlocal waiting_reported
            if text:
                last_activity_at = time.monotonic()
            pending += text.replace("\r\n", "\n").replace("\r", "\n")
            segments = pending.split("\n")
            pending = "" if final else segments.pop()
            if final and pending:
                segments.append(pending)
                pending = ""
            for raw_line in segments:
                line = clean_remote_output_line(raw_line)
                if not line:
                    continue
                previous_image = image_progress.current_image if image_progress else ""
                if image_progress:
                    report_image_progress(image_progress.feed(line))
                image = image_progress.current_image if image_progress else ""
                if image != previous_image:
                    flush_transfer_logs(last_activity_at, force=True)
                if transfer_trace:
                    transfer_trace.feed(
                        line, image=image_progress.current_image if image_progress else "",
                        phase=image_progress.phase if image_progress else transfer_phase,
                    )
                lower_line = line.casefold()
                next_phase = ""
                if "the push refers to repository" in lower_line:
                    next_phase = "push"
                elif "pulling from" in lower_line or "downloading" in lower_line:
                    next_phase = "pull"
                if next_phase and next_phase != transfer_phase and progress_range:
                    flush_transfer_logs(last_activity_at, force=True)
                    transfer_phase = next_phase
                    if on_phase:
                        on_phase(next_phase)
                    midpoint = progress_range[0] + (
                        progress_range[1] - progress_range[0]
                    ) // 2
                    if next_phase == "pull":
                        transfer_range = (progress_range[0], midpoint)
                        status = "正在从源仓库拉取镜像..."
                    else:
                        transfer_range = (midpoint, progress_range[1])
                        status = "正在推送镜像到目标仓库..."
                    largest_total = 0
                    last_progress = transfer_range[0] - 1
                    if not image_progress:
                        report_progress(transfer_range[0], status)
                transfer = parse_transfer_progress(line)
                layer = re.match(r"^(?:--->\s+)?([\w.-]+):\s+", line)
                key = (image, transfer_phase, layer[1] if layer else "")
                previous_line = last_output_by_layer.get(key)
                changed = previous_line != line
                if changed:
                    last_output_by_layer[key] = line
                    last_change_at = last_activity_at
                    if waiting_reported:
                        waiting_reported = False
                        if image:
                            report_image_progress(image_progress.states[image]["last"])
                        elif not image_progress and progress_range:
                            report_progress(max(progress_range[0], last_progress), "已收到新的远端输出...")
                if transfer and transfer_range:
                    if changed:
                        # Show an action change (download -> extract) immediately.
                        action = re.match(r"^(?:--->\s+)?[\w.-]+:\s+(\w+)", line)
                        if action and previous_line and action[1] not in previous_line:
                            flush_transfer_logs(last_activity_at, force=True, only_key=key)
                            transfer_log_times.pop(key, None)
                        pending_transfer_logs[key] = line
                        flush_transfer_logs(last_activity_at, only_key=key)
                    if image_progress:
                        continue
                    current, total = transfer
                    if total >= largest_total:
                        largest_total = total
                        ratio = max(0.0, min(1.0, current / total))
                        start, end = transfer_range
                        overall = int(start + (end - start) * ratio)
                        if overall > last_progress:
                            last_progress = overall
                            action = (
                                "推送镜像"
                                if transfer_phase == "push"
                                else "拉取镜像"
                            )
                            report_progress(
                                overall,
                                f"正在{action}（当前层）：{format_transfer_bytes(current)} / "
                                f"{format_transfer_bytes(total)}",
                            )
                    continue
                if changed:
                    flush_transfer_logs(last_activity_at, force=True,
                                        only_key=key if layer and previous_line else None)
                if changed or line != last_log_line:
                    report_log(line, image)
                    last_log_line = line

        code = None
        stopped = False

        def stop_command():
            nonlocal stopped
            try:
                if not process_group_file:
                    raise RuntimeError("当前命令没有可确认的进程组")
                self._terminate_remote_process_group(client, process_group_file)
                stopped = True
            except Exception as exc:
                raise RemoteCommandUncertain(
                    "无法确认远端命令已停止，已停止本地等待并阻止该目标的后续部署。"
                    "请检查服务器上的命令及资源状态，确认后重新启动工具。"
                ) from exc
            finally:
                stdout.channel.close()

        try:
            while not stdout.channel.exit_status_ready():
                if transfer_trace:
                    transfer_trace.tick()
                if stdout.channel.recv_ready():
                    consume(decoder.decode(stdout.channel.recv(65536)))
                if stdout.channel.recv_stderr_ready():
                    consume(decoder.decode(stdout.channel.recv_stderr(65536)))
                if cancel_event and cancel_event.is_set():
                    stop_command()
                    raise PipelineCancelled("远端镜像命令已确认停止，后续部署已取消")
                if interrupt_event and interrupt_event.is_set():
                    if interrupt_allowed and not interrupt_allowed():
                        interrupt_event.clear()
                        if on_interrupt_failed:
                            on_interrupt_failed()
                        self.emit(
                            "deploy_log",
                            "确认中断时阶段已转为推送，已按安全策略继续当前搬运。",
                        )
                    else:
                        try:
                            if not process_group_file:
                                raise RuntimeError("远端命令没有可确认的进程组文件")
                            self._terminate_remote_process_group(
                                client, process_group_file
                            )
                        except Exception as exc:
                            interrupt_event.clear()
                            if on_interrupt_failed:
                                on_interrupt_failed()
                            self.emit(
                                "deploy_log",
                                f"安全中断失败，当前镜像搬运将继续执行：{exception_message(exc)}",
                            )
                        else:
                            stdout.channel.close()
                            raise RemoteCommandInterrupted(
                                "镜像拉取已安全停止，准备重新合批"
                            )
                now = time.monotonic()
                flush_transfer_logs(now)
                if (now - command_started_at >= timeout_seconds or
                        (idle_timeout_seconds and now - last_activity_at >= idle_timeout_seconds)):
                    reason = "长时间没有远端输出" if idle_timeout_seconds and now - last_activity_at >= idle_timeout_seconds else "超过命令最长运行时间"
                    stop_command()
                    raise TimeoutError(f"远端命令超时（{reason}），已确认停止；后续步骤未执行")
                if (
                    progress_range
                    and now - last_change_at >= 10
                    and now - last_heartbeat_at >= 10
                ):
                    last_heartbeat_at = now
                    waiting_reported = True
                    elapsed = int(now - command_started_at)
                    idle, unchanged = int(now - last_activity_at), int(now - last_change_at)
                    waiting = (f"{idle} 秒未收到远端输出；"
                               f"{unchanged} 秒无进度或状态变化，"
                               "无法确认远端是否仍在推进")
                    report_log(waiting, image_progress.current_image if image_progress else "")
                    status = f"等待远端：{idle}s 无输出 / {unchanged}s 无变化"
                    heartbeat_progress = max(progress_range[0], last_progress)
                    if image_progress:
                        update = image_progress.heartbeat(elapsed)
                        if update:
                            report_image_progress((update[0], update[1], status))
                    else:
                        report_progress(heartbeat_progress, status)
                time.sleep(0.02)
            while stdout.channel.recv_ready():
                consume(decoder.decode(stdout.channel.recv(65536)))
            while stdout.channel.recv_stderr_ready():
                consume(decoder.decode(stdout.channel.recv_stderr(65536)))
            consume(decoder.decode(b"", final=True), final=True)
            code = stdout.channel.recv_exit_status()
            if code == -1:
                raise RemoteCommandUncertain("SSH 通道已断开但未取得退出状态，无法确认远端命令已停止；请检查服务器后重启工具")
        except (PipelineCancelled, RemoteCommandUncertain):
            raise
        except Exception as exc:
            if not stopped and code is None:
                try:
                    stop_command()
                except RemoteCommandUncertain as uncertain:
                    raise uncertain from exc
            raise
        finally:
            if pending_transfer_logs:
                flush_transfer_logs(time.monotonic(), force=True)
            if transfer_trace:
                transfer_trace.finish(code)
        trace_event(self, "remote_exit", exit_code=code)
        if code:
            raise RuntimeError(f"命令退出码 {code}：{command}")
        if progress_range and not image_progress:
            report_progress(
                progress_range[1], "镜像搬运命令已结束，正在校验目标镜像...",
            )

    @staticmethod
    def _quiet_remote_status(client, command):
        with ssh_request_deadline(client, 15):
            _, stdout, stderr = client.exec_command(command, timeout=15)
            stdout.read()
            stderr.read()
            return stdout.channel.recv_exit_status()

    def _terminate_remote_process_group(self, client, process_group_file):
        """Terminate only the managed image command group and verify it stopped."""
        pid_file = shell_quote(process_group_file)
        load_group = (
            f"i=0; while ! test -s {pid_file}; do "
            '[ "$i" -ge 20 ] && exit 2; sleep 0.1; i=$((i+1)); done; '
            f"pgid=$(tr -d '[:space:]' < {pid_file}); "
            "case $pgid in ''|*[!0-9]*) exit 3;; esac; [ \"$pgid\" -gt 1 ] || exit 3; "
        )
        term_status = self._quiet_remote_status(
            client, load_group + "kill -0 -- -$pgid 2>/dev/null || exit 0; kill -TERM -- -$pgid"
        )
        if term_status:
            raise RuntimeError(f"远端进程组无法接收 TERM，退出码 {term_status}")
        alive_command = load_group + "kill -0 -- -$pgid 2>/dev/null"

        def stopped():
            status = self._quiet_remote_status(client, alive_command)
            if status not in (0, 1):
                raise RuntimeError(f"远端进程组停止确认失败，退出码 {status}")
            return status == 1

        for _ in range(25):
            if stopped():
                return
            time.sleep(0.2)
        self._quiet_remote_status(client, load_group + "kill -KILL -- -$pgid")
        for _ in range(10):
            if stopped():
                return
            time.sleep(0.2)
        raise RuntimeError("远端镜像进程组在 KILL 后仍未退出")

    @staticmethod
    def _remote_command_succeeded(client, command):
        """Run a quiet read-only probe and return whether it succeeded."""
        with ssh_request_deadline(client, 30):
            _, stdout, stderr = client.exec_command(command, timeout=30)
            stdout.read()
            stderr.read()
            return stdout.channel.recv_exit_status() == 0

    @staticmethod
    def _probe_remote_image(client, image_ref):
        """Return visibility and a safe diagnostic without logging registry output."""
        command = (
            "docker manifest inspect --insecure "
            f"{shell_quote(image_ref)} >/dev/null"
        )
        with ssh_request_deadline(client, 30):
            _, stdout, stderr = client.exec_command(command, timeout=30)
            stdout.read()
            error = stderr.read().decode("utf-8", "replace").casefold()
            code = stdout.channel.recv_exit_status()
        if code == 0:
            return True, ""
        # Registry errors can contain credential-bearing URLs or headers. Emit
        # only known categories and the exit code, never the raw response.
        if any(part in error for part in (
            "unauthorized", "authentication required", "access denied", "denied:", "forbidden",
        )):
            reason = "仓库认证或权限失败，请检查目标主机的 Docker 登录与仓库权限"
        elif any(part in error for part in ("x509", "certificate", "tls")):
            reason = "TLS/证书校验失败，请检查仓库证书与连接配置"
        elif "no such host" in error:
            reason = "DNS 解析失败，请检查目标主机的仓库域名解析"
        elif any(part in error for part in (
            "timeout", "timed out", "connection refused", "connection reset",
            "network is unreachable", "no route to host",
        )):
            reason = "网络连接失败，请检查目标主机到仓库的连接"
        elif code in (126, 127) or any(part in error for part in (
            "command not found", "docker: not found", "unknown flag", "is not a docker command",
        )):
            reason = "Docker 命令不可用或参数不兼容，请检查目标主机的 Docker CLI"
        elif any(part in error for part in ("no such manifest", "manifest unknown", "name unknown")):
            return False, f"仓库返回镜像未找到，等待仓库同步（退出码 {code}）"
        else:
            reason = "未识别的探测错误，原始输出已省略以保护认证信息"
        return False, f"镜像探测失败，是否存在尚无法确认（退出码 {code}）：{reason}"

    @staticmethod
    def _managed_transfer_command(workspace, command):
        pid_file = posixpath.join(workspace, ".deploy-console-command.pgid")
        body = (f"echo $$ > {shell_quote(pid_file)}; "
                f"cd {shell_quote(workspace)} && exec sh -c {shell_quote(command)}")
        # Disable job control so setsid does not fork; works with old setsid too.
        wait_body = f"set +m; setsid sh -c {shell_quote(body)} & " + 'wait "$!"'
        return f"sh -c {shell_quote(wait_body)}", pid_file

    @trace_operation("image_visibility_wait")
    def _wait_for_remote_image(
        self, client, image_ref, timeout_seconds=600, cancel_event=None
    ):
        """Wait for the pushed image to become visible in the target registry."""
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        last_diagnostic = "尚未取得探测结果"
        attempt = 0
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                raise PipelineCancelled("等待目标镜像期间已取消")
            attempt += 1
            with trace_stage(self, "image_probe", image=image_ref, attempt=attempt):
                visible, last_diagnostic = self._probe_remote_image(client, image_ref)
            trace_event(self, "image_probe_result", image=image_ref, attempt=attempt, visible=visible)
            if visible:
                return
            remaining = max(1, int(deadline - time.monotonic()))
            self.emit(
                "deploy_log",
                f"{last_diagnostic}：{image_ref}（剩余约 {remaining} 秒）",
            )
            time.sleep(3)
        raise RuntimeError(f"目标镜像确认超时：{image_ref}；最后一次探测：{last_diagnostic}")

    @trace_operation("sftp_update")
    def _sftp_update(self, sftp, remote_path, transform, allow_unchanged=False):
        temp_dir = CONFIG_DIR / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        local = temp_dir / Path(remote_path).name
        attributes = sftp.stat(remote_path)
        sftp.get(remote_path, str(local))
        content = local.read_text(encoding="utf-8")
        updated = transform(content)
        if updated == content:
            if allow_unchanged:
                self.emit("deploy_log", f"文件已是目标内容，无需修改：{remote_path}")
                return False
            raise RuntimeError(f"文件内容未发生变化：{remote_path}")
        local.write_text(updated, encoding="utf-8", newline="\n")
        backup = remote_path + ".bak." + time.strftime("%Y%m%d%H%M%S")
        sftp.rename(remote_path, backup)
        try:
            sftp.put(str(local), remote_path)
            sftp.chmod(remote_path, attributes.st_mode)
        except Exception:
            try:
                sftp.remove(remote_path)
            except OSError:
                pass
            sftp.rename(backup, remote_path)
            raise
        self.emit("deploy_log", f"已修改 {remote_path}，备份：{backup}")
        return True

    @staticmethod
    def _create_isolated_image_workspace(sftp, data, package):
        """Create a per-task working directory for scripts using a fixed list name."""
        image_dir = data["image_dir"]
        image_file = posixpath.basename(data["image_file"])
        workspace = posixpath.join(
            image_dir, f".deploy-console-{uuid.uuid4().hex}"
        )
        sftp.mkdir(workspace, mode=0o700)
        try:
            for entry in sftp.listdir(image_dir):
                if entry == image_file or entry.startswith(".deploy-console-"):
                    continue
                sftp.symlink(
                    posixpath.join(image_dir, entry),
                    posixpath.join(workspace, entry),
                )
            manifest_path = posixpath.join(workspace, image_file)
            packages = [package] if isinstance(package, str) else list(package)
            with sftp.open(manifest_path, "w") as manifest:
                manifest.write(("\n".join(packages) + "\n").encode("utf-8"))
            return workspace
        except Exception:
            DeployTool._remove_isolated_image_workspace(sftp, workspace)
            raise

    @staticmethod
    def _remove_isolated_image_workspace(sftp, workspace):
        if not workspace:
            return
        try:
            entries = sftp.listdir(workspace)
        except OSError:
            return
        for entry in entries:
            try:
                sftp.remove(posixpath.join(workspace, entry))
            except OSError:
                pass
        try:
            sftp.rmdir(workspace)
        except OSError:
            pass

    def _run_deploy(self, data, package):
        try:
            self._execute_deploy(data, package)
            self.emit("info", "镜像拉取、YAML 修改和 kubectl apply 已完成")
        except Exception as exc:
            self.emit("deploy_progress", (0, "部署失败，请查看日志"))
            self.emit("error", f"部署失败：{exc}")

    def _run_deploy_with_target_lock(self, data, package):
        data = resolve_ssh_environment_for_package(data, package)
        target_key = self._deployment_target_key("SSH 环境部署", environment=data)
        self._wait_for_deployment_target(target_key, log_target="deploy_log")
        try:
            self._run_deploy(data, package)
        finally:
            self._release_deployment_target(target_key)

    def _start_ssh_replaced_packages(self, data, package):
        capture = getattr(self, "_replaced_packages", None)
        if capture is None:
            return
        environment = dict(data)
        key = posixpath.join(environment["yaml_dir"], environment["yaml_file"])

        def read(cancel):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                if cancel.is_set():
                    return []
                connect_ssh_client(client, environment)
                return read_ssh_running_packages(client, environment, package, cancel)
            finally:
                client.close()

        capture.start(key, read)

    def _freeze_replaced_packages(self, data=None):
        capture = getattr(self, "_replaced_packages", None)
        if capture is not None:
            capture.freeze(posixpath.join(data["yaml_dir"], data["yaml_file"]) if data else None)

    @trace_operation("ssh_deploy")
    def _execute_deploy(self, data, package, cancel_event=None):
        if cancel_event and cancel_event.is_set():
            raise PipelineCancelled("连接部署环境前已取消")
        data = resolve_ssh_environment_for_package(data, package)
        if data.get("delete_before_apply") is True:
            _deployment_delete_command(data)
        DeployTool._start_ssh_replaced_packages(self, data, package)
        started_at = time.monotonic()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.emit("deploy_progress", (2, "正在连接 SSH..."))
            self.emit("deploy_log", f"连接 {data['host']}:{data.get('port', '22')} ...")
            with trace_stage(self, "ssh_connect", host=data["host"], port=data.get("port", "22")):
                connect_ssh_client(client, data)
            if cancel_event and cancel_event.is_set():
                raise PipelineCancelled("SSH 连接后已取消，未开始部署")
            self.emit("deploy_progress", (8, "SSH 已连接，正在更新镜像清单..."))
            with trace_stage(self, "sftp_connect"), ssh_request_deadline(client):
                sftp = client.open_sftp()
                sftp.get_channel().settimeout(30)
            yaml_path = posixpath.join(data["yaml_dir"], data["yaml_file"])
            with trace_stage(self, "yaml_read", yaml_path=yaml_path):
                with sftp.open(yaml_path, "r") as remote_yaml:
                    yaml_content = remote_yaml.read().decode("utf-8", "replace")
            # Validate before the expensive Docker transfer. This turns a bad
            # package/YAML mapping into an immediate error instead of a failure
            # after twenty minutes of pulling and pushing layers.
            target_image = yaml_image_reference(yaml_content, package)
            self.emit(
                "deploy_log",
                f"部署目标校验通过：{yaml_path} -> {target_image}",
            )

            manifest_command = (
                "docker manifest inspect --insecure "
                f"{shell_quote(target_image)} >/dev/null 2>&1"
            )
            with trace_stage(self, "image_exists_probe", image=target_image):
                image_already_present = self._remote_command_succeeded(
                    client, manifest_command
                )
            trace_event(self, "image_exists_result", image=target_image, visible=image_already_present)
            if image_already_present:
                self.emit(
                    "deploy_log",
                    f"目标仓库已存在镜像，跳过重复拉取和推送：{target_image}",
                )
                self.emit("deploy_progress", (78, "目标镜像已存在，跳过镜像搬运"))
            else:
                workspace = ""
                preserve_workspace = False
                try:
                    with trace_stage(self, "workspace_prepare"):
                        workspace = self._create_isolated_image_workspace(
                            sftp, data, package
                        )
                    self.emit(
                        "deploy_log",
                        f"已创建任务独立镜像清单：{workspace}/{posixpath.basename(data['image_file'])}",
                    )
                    if cancel_event and cancel_event.is_set():
                        raise PipelineCancelled("镜像配置完成，后续步骤已取消")
                    transfer_started_at = time.monotonic()
                    self.emit("deploy_progress", (15, "正在搬运镜像..."))
                    managed_command, pid_file = DeployTool._managed_transfer_command(workspace, data["pull_command"])
                    with trace_stage(self, "image_transfer"):
                        self._remote_exec(
                            client,
                            managed_command,
                            progress_range=(15, 78),
                            transfer_images=[target_image],
                            process_group_file=pid_file,
                            cancel_event=cancel_event,
                        )
                    self.emit(
                        "deploy_log",
                        "镜像拉取及推送完成，耗时 "
                        f"{time.monotonic() - transfer_started_at:.1f} 秒",
                    )
                    self._wait_for_remote_image(
                        client, target_image, cancel_event=cancel_event
                    )
                    self.emit("deploy_log", f"目标镜像已确认可见：{target_image}")
                except RemoteCommandUncertain:
                    preserve_workspace = True
                    raise
                finally:
                    if not preserve_workspace:
                        with trace_stage(self, "workspace_cleanup"):
                            self._remove_isolated_image_workspace(sftp, workspace)
            if cancel_event and cancel_event.is_set():
                raise PipelineCancelled("镜像拉取步骤完成，后续步骤已取消")
            self.emit("deploy_progress", (82, "正在更新 Kubernetes YAML..."))
            with trace_stage(self, "yaml_update", yaml_path=yaml_path):
                self._sftp_update(
                    sftp,
                    yaml_path,
                    lambda text: update_yaml_image(text, package),
                    allow_unchanged=True,
                )
            if cancel_event and cancel_event.is_set():
                raise PipelineCancelled("YAML 修改完成，后续应用步骤已取消")
            DeployTool._apply_deployment_yaml(self, client, data, cancel_event=cancel_event)
            elapsed = time.monotonic() - started_at
            self.emit("deploy_progress", (100, f"部署完成，共耗时 {elapsed:.1f} 秒"))
            self.emit("deploy_log", f"部署流程执行完成，总耗时 {elapsed:.1f} 秒")
        except RemoteCommandUncertain as exc:
            DeployTool._block_deployment_target(self, self._deployment_target_key("SSH 环境部署", data), exc)
            raise
        finally:
            DeployTool._freeze_replaced_packages(self, data)
            client.close()

    def _apply_deployment_yaml(self, client, data, *, owner=None, cancel_event=None):
        owner = owner if owner is not None else self
        if cancel_event and cancel_event.is_set():
            raise PipelineCancelled("应用 YAML 前已取消，未删除资源")
        DeployTool._freeze_replaced_packages(owner, data)
        yaml_path = posixpath.join(data["yaml_dir"], data["yaml_file"])
        prefix = f"cd {shell_quote(data['yaml_dir'])} && "
        recreate = data.get("delete_before_apply") is True
        if recreate:
            command = _deployment_delete_command(data)
            owner.emit("deploy_progress", (88, "正在删除旧 Kubernetes 资源，随后重新应用..."))
            owner.emit("deploy_log", f"删除后重新应用：{yaml_path}\n$ {command}")
            try:
                with trace_stage(owner, "yaml_delete", yaml_path=yaml_path):
                    self._remote_exec(client, prefix + command)
            except RemoteCommandUncertain:
                raise
            except Exception as exc:
                raise RuntimeError(f"删除 YAML 资源失败，已停止后续应用（可能已有部分资源被删除）：{yaml_path}；{exception_message(exc)}") from exc
        # Once deletion begins, finish apply even if cancellation was requested
        # meanwhile; stopping between these commands would leave resources absent.
        owner.emit("deploy_progress", (92, "正在应用 Kubernetes 配置..."))
        try:
            with trace_stage(owner, "yaml_apply", yaml_path=yaml_path):
                self._remote_exec(client, prefix + data["apply_command"])
        except RemoteCommandUncertain:
            raise
        except Exception as exc:
            if recreate:
                raise RuntimeError(f"旧资源已删除，但重新应用失败，请检查并重试部署：{yaml_path}；{exception_message(exc)}") from exc
            raise

    def _apply_manual_deployment_batch(self, client, sftp, entries):
        for entry in entries:
            entry.setdefault("result", "未执行")
        # Prepare every manifest before deleting anything. A later read/write
        # failure must not leave an earlier application's resources deleted.
        for index, entry in enumerate(entries, 1):
            if self.cancel_event.is_set():
                raise PipelineCancelled("批量应用前已取消，未删除资源")
            self.emit("deploy_progress", (80, f"正在更新 YAML（{index}/{len(entries)}）..."))
            try:
                with trace_stage(self, "yaml_update", yaml_path=entry["yaml_path"]):
                    self._sftp_update(
                        sftp, entry["yaml_path"],
                        lambda text, package=entry["package"]: update_yaml_image(text, package),
                        allow_unchanged=True,
                    )
            except Exception as exc:
                entry["result"] = f"YAML 更新失败：{exception_message(exc)}"
                raise

        if self.cancel_event.is_set():
            raise PipelineCancelled("批量应用前已取消，未删除资源")
        for entry in entries:
            DeployTool._freeze_replaced_packages(entry["owner"], entry["environment"])
        attempted = []
        errors = []
        apply_entries = entries
        for index, entry in enumerate(entries, 1):
            data = entry["environment"]
            if not data.get("delete_before_apply"):
                continue
            attempted.append(entry)
            self.emit("deploy_progress", (88, f"正在统一删除资源（{index}/{len(entries)}）..."))
            try:
                with trace_stage(self, "yaml_delete", yaml_path=entry["yaml_path"]):
                    self._remote_exec(
                        client, f"cd {shell_quote(data['yaml_dir'])} && " + _deployment_delete_command(data),
                    )
            except RemoteCommandUncertain:
                raise
            except Exception as exc:
                entry["result"] = f"删除失败：{exception_message(exc)}"
                errors.append(f'{entry["package"]} {entry["result"]}')
                # A failed delete can have removed some resources. Reapply
                # every attempted target, including that partially deleted one.
                apply_entries = attempted
                self.emit("deploy_log", "删除失败，停止后续删除；正在重新应用已尝试删除的 YAML 以恢复资源。")
                break

        recovering = bool(errors)
        for index, entry in enumerate(apply_entries, 1):
            # Once deletion starts, finish the necessary applies even if a
            # cancellation arrives in between, so resources are not stranded.
            if not attempted and self.cancel_event.is_set():
                raise PipelineCancelled("批量部署已取消；已应用的包不会自动回滚")
            data = entry["environment"]
            self.emit("deploy_progress", (92, f"正在{'恢复' if recovering else '应用'}资源（{index}/{len(apply_entries)}）..."))
            previous_error = entry["result"] if entry["result"].startswith("删除失败") else ""
            try:
                with trace_stage(self, "yaml_apply", yaml_path=entry["yaml_path"], phase="recovery" if recovering else "apply"):
                    self._remote_exec(client, f"cd {shell_quote(data['yaml_dir'])} && " + data["apply_command"])
                entry["result"] = (previous_error + "；" if previous_error else "") + (
                    "已重新应用以恢复资源" if recovering else "应用成功"
                )
            except RemoteCommandUncertain:
                raise
            except Exception as exc:
                entry["result"] = (previous_error + "；" if previous_error else "") + f"应用失败：{exception_message(exc)}"
                errors.append(f'{entry["package"]} {entry["result"]}')
            self.emit("deploy_log", f'{entry["package"]}：{entry["result"]}')
        if errors:
            raise RuntimeError("批量部署未全部成功：\n" + "\n".join(errors))

    @trace_operation("ssh_batch_deploy")
    def _execute_deploy_batch(self, entries, batch_token=None, *, unified_apply=False):
        """Transfer all batch images once, then update and apply each YAML."""
        if unified_apply:
            parse_deployment_packages("\n".join(entry["package"] for entry in entries))
        coordinator = getattr(self.window_manager, "batch_coordinator", None)
        batch_cancel = DeploymentCancellation([entry["owner"] for entry in entries], require_all=True)
        if coordinator and batch_token:
            coordinator.set_phase(batch_token, "preparing")
            if coordinator.should_join_before_transfer(batch_token):
                with trace_stage(self, "batch_rejoin_wait"):
                    combined = coordinator.collect_pending_entries(
                        batch_token,
                        entries,
                        batch_cancel,
                        lambda count: self.emit(
                            "deploy_progress",
                            (0, f"检测到 {count} 个同环境任务，等待打包后一起拉镜像..."),
                        ),
                    )
                if combined is not entries:
                    raise BatchReassembled(combined)
        active = [entry for entry in entries if not entry["owner"].cancel_event.is_set()]
        if not active:
            raise PipelineCancelled("合并部署批次中的任务均已取消")

        # One YAML cannot end on two image tags. Registration order is stable,
        # so the last task wins and every superseded window receives a clear log.
        selected = {}
        for entry in active:
            data = entry["environment"]
            yaml_key = (
                str(data.get("yaml_dir", "")).casefold(),
                str(data.get("yaml_file", "")).casefold(),
            )
            previous = selected.get(yaml_key)
            if previous:
                if unified_apply:
                    raise ValueError(f'多个应用指向同一 YAML：{data["yaml_dir"]}/{data["yaml_file"]}')
                previous["owner"].emit(
                    "deploy_log",
                    f'同一 YAML 存在多个包，已由后登记包 {entry["package"]} 覆盖。',
                )
            selected[yaml_key] = entry
        active = list(selected.values())
        for entry in active:
            if entry["environment"].get("delete_before_apply") is True:
                _deployment_delete_command(entry["environment"])
        owners = list({id(entry["owner"]): entry["owner"] for entry in active}.values())
        transfer_cancel = DeploymentCancellation(owners)
        leader_run = getattr(getattr(self, "_diagnostics", None), "run_id", "")
        participants = [getattr(getattr(owner, "_diagnostics", None), "run_id", "")
                        for owner in owners]
        for entry in active:
            trace_event(entry["owner"], "batch_link", leader=leader_run,
                        participants=participants, members=len(active), package=entry["package"],
                        batch_id=str(batch_token[1]) if batch_token else "")

        def broadcast(target, payload):
            for owner in owners:
                owner.emit(target, payload)

        def update_image_progress(image, percent, status):
            for entry in active:
                if entry.get("target_image") == image and not entry["owner"].cancel_event.is_set():
                    entry["owner"].emit("deploy_progress", (percent, status))
                    if percent == 78:
                        trace_event(entry["owner"], "image_push_complete", image=image,
                                    wait_reason="remaining_batch_images_then_validation_and_apply")

        def update_image_log(image, line):
            for entry in active:
                if entry.get("target_image") == image and not entry["owner"].cancel_event.is_set():
                    entry["owner"].emit("deploy_log", line)

        lock_keys = sorted({
            self._deployment_target_key("SSH 环境部署", entry["environment"])
            for entry in active
        })
        acquired = []
        connected = False
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            for key in lock_keys:
                self._wait_for_deployment_target(key, cancel_event=transfer_cancel, log_target="deploy_log")
                acquired.append(key)
            for entry in active:
                DeployTool._start_ssh_replaced_packages(
                    entry["owner"], entry["environment"], entry["package"]
                )
            data = active[0]["environment"]
            broadcast("deploy_progress", (2, f"合并部署 {len(active)} 个包，正在连接 SSH..."))
            with trace_stage(self, "ssh_connect", host=data["host"], port=data.get("port", "22")):
                connect_ssh_client(client, data)
            connected = True
            with trace_stage(self, "sftp_connect"), ssh_request_deadline(client):
                sftp = client.open_sftp()
                sftp.get_channel().settimeout(30)
            transfer_packages = []
            for entry in active:
                environment = entry["environment"]
                package = entry["package"]
                if transfer_cancel.is_set():
                    raise PipelineCancelled("镜像校验期间已取消")
                yaml_path = posixpath.join(
                    environment["yaml_dir"], environment["yaml_file"]
                )
                with trace_stage(entry["owner"], "yaml_read", yaml_path=yaml_path):
                    with sftp.open(yaml_path, "r") as remote_yaml:
                        yaml_content = remote_yaml.read().decode("utf-8", "replace")
                target_image = yaml_image_reference(yaml_content, package)
                entry["yaml_path"] = yaml_path
                entry["target_image"] = target_image
                entry["owner"].emit(
                    "deploy_log", f"部署目标校验通过：{yaml_path} -> {target_image}"
                )
                command = (
                    "docker manifest inspect --insecure "
                    f"{shell_quote(target_image)} >/dev/null 2>&1"
                )
                with trace_stage(entry["owner"], "image_exists_probe", image=target_image):
                    image_already_present = self._remote_command_succeeded(client, command)
                trace_event(entry["owner"], "image_exists_result", image=target_image,
                            visible=image_already_present)
                if image_already_present:
                    entry["owner"].emit(
                        "deploy_log", f"目标仓库已有镜像，跳过搬运：{target_image}"
                    )
                    entry["owner"].emit("deploy_progress", (78, "目标镜像已存在，等待部署..."))
                else:
                    transfer_packages.append(package)
                    entry["owner"].emit("deploy_progress", (15, "等待拉取镜像..."))

            if coordinator and batch_token and coordinator.should_join_before_transfer(
                batch_token
            ):
                with trace_stage(self, "batch_rejoin_wait"):
                    combined = coordinator.collect_pending_entries(
                        batch_token,
                        entries,
                        batch_cancel,
                        lambda count: broadcast(
                            "deploy_progress",
                            (0, f"拉镜像前发现 {count} 个新任务，等待打包后重新合批..."),
                        ),
                    )
                if combined is not entries:
                    raise BatchReassembled(combined)

            if transfer_packages:
                workspace = ""
                preserve_workspace = False
                try:
                    with trace_stage(self, "workspace_prepare"):
                        workspace = self._create_isolated_image_workspace(
                            sftp, data, transfer_packages
                        )
                    managed_command, pid_file = DeployTool._managed_transfer_command(workspace, data["pull_command"])
                    if coordinator and batch_token:
                        coordinator.set_phase(batch_token, "pull")
                    with trace_stage(self, "image_transfer"):
                        self._remote_exec(
                            client,
                            managed_command,
                            progress_range=(15, 78),
                            transfer_images=[entry["target_image"] for entry in active
                                             if entry["package"] in transfer_packages],
                            on_image_progress=update_image_progress,
                            on_image_log=update_image_log,
                            cancel_event=transfer_cancel,
                            interrupt_event=(
                                coordinator.interrupt_event(batch_token)
                                if coordinator and batch_token
                                else None
                            ),
                            process_group_file=pid_file,
                            on_phase=(
                                lambda phase: coordinator.set_phase(batch_token, phase)
                                if coordinator and batch_token
                                else None
                            ),
                            interrupt_allowed=(
                                lambda: coordinator.phase(batch_token) == "pull"
                                if coordinator and batch_token
                                else None
                            ),
                            on_interrupt_failed=(
                                lambda: coordinator.cancel_restart(batch_token)
                                if coordinator and batch_token
                                else None
                            ),
                        )
                    for entry in active:
                        if entry.get("target_image") and not entry["owner"].cancel_event.is_set():
                            self._wait_for_remote_image(
                                client,
                                entry["target_image"],
                                cancel_event=entry["owner"].cancel_event,
                            )
                            entry["owner"].emit(
                                "deploy_log",
                                f"目标镜像已确认可见：{entry['target_image']}",
                            )
                            entry["owner"].emit("deploy_progress", (78, "目标镜像已确认，等待部署..."))
                except RemoteCommandUncertain:
                    preserve_workspace = True
                    raise
                finally:
                    if not preserve_workspace:
                        with trace_stage(self, "workspace_cleanup"):
                            self._remove_isolated_image_workspace(sftp, workspace)
            else:
                broadcast("deploy_progress", (78, "本轮镜像均已存在，跳过搬运"))

            if coordinator and batch_token and coordinator.should_join_before_transfer(
                batch_token
            ):
                with trace_stage(self, "batch_rejoin_wait"):
                    combined = coordinator.collect_pending_entries(
                        batch_token,
                        entries,
                        batch_cancel,
                        lambda count: broadcast(
                            "deploy_progress",
                            (0, f"应用 YAML 前发现 {count} 个新任务，重新合批..."),
                        ),
                    )
                if combined is not entries:
                    raise BatchReassembled(combined)

            if coordinator and batch_token:
                coordinator.set_phase(batch_token, "apply")
            remaining = [
                entry for entry in active
                if not entry["owner"].cancel_event.is_set()
            ]
            if unified_apply:
                if len(remaining) != len(active):
                    raise PipelineCancelled("批量应用前已取消，未删除资源")
                DeployTool._apply_manual_deployment_batch(self, client, sftp, remaining)
                return
            for index, entry in enumerate(remaining, 1):
                environment = entry["environment"]
                owner = entry["owner"]
                if owner.cancel_event.is_set():
                    continue
                owner.emit(
                    "deploy_progress",
                    (80, f"正在更新并应用 YAML（{index}/{len(remaining)}）..."),
                )
                with trace_stage(owner, "yaml_update", yaml_path=entry["yaml_path"]):
                    self._sftp_update(
                        sftp,
                        entry["yaml_path"],
                        lambda text, package=entry["package"]: update_yaml_image(text, package),
                        allow_unchanged=True,
                    )
                try:
                    DeployTool._apply_deployment_yaml(
                        self, client, environment, owner=owner, cancel_event=owner.cancel_event,
                    )
                except PipelineCancelled:
                    continue
                owner.emit("deploy_progress", (100, "合并部署完成"))
        except RemoteCommandInterrupted:
            raise
        except PipelineCancelled:
            remaining = [entry for entry in active if not entry["owner"].cancel_event.is_set()]
            if remaining and not unified_apply:
                raise BatchReassembled(remaining)
            raise
        except RemoteCommandUncertain as exc:
            if connected:
                for key in acquired:
                    DeployTool._block_deployment_target(self, key, exc)
            raise
        finally:
            for entry in active:
                DeployTool._freeze_replaced_packages(entry["owner"], entry["environment"])
            client.close()
            for key in reversed(acquired):
                self._release_deployment_target(key)

    # ── Digiwin Ops Cloud Deployment ──────────────────────────────
    def _cloud_settings_from_ui(self):
        region_label = self.cloud_region.currentText().strip()
        if " / " in region_label:
            cloud, region = region_label.split(" / ", 1)
        else:
            cloud = self.store.data.get("cloud_deploy", {}).get("cloud", "")
            region = region_label
        return {
            "url": self.cloud_url.text().strip(),
            "username": self.cloud_username.text().strip(),
            "password_plain": self.cloud_password.text(),
            "project": self.cloud_project.currentText().strip(),
            "deploy_type": self.cloud_type.currentText().strip(),
            "cloud": cloud.strip(),
            "region": region.strip(),
            "package": self.cloud_package.text().strip(),
            "status_page": self._cloud_status_settings_from_ui(),
        }

    def _save_cloud_settings(self, settings):
        self.store.data["cloud_deploy"] = {
            "url": settings["url"],
            "username": settings["username"],
            "password": protect_secret(settings["password_plain"]),
            "project": settings["project"],
            "deploy_type": settings["deploy_type"],
            "cloud": settings["cloud"],
            "region": settings["region"],
        }
        status_page = settings.get("status_page")
        if status_page:
            self._save_cloud_status_settings(status_page, save=False)
        self.store.save()

    def login_cloud_status_page(self):
        settings = self._cloud_status_settings_from_ui()
        if any(
            not settings.get(key)
            for key in ("url", "username", "password_plain")
        ):
            QMessageBox.warning(
                self,
                "缺少登录信息",
                "Rancher 状态页地址、登录账号和登录密码不能为空",
            )
            return
        self._save_cloud_status_settings(settings)
        self.cloud_status_login_button.setEnabled(False)
        self.cloud_status_login_state.setText("正在打开并登录...")
        self.run_thread(lambda: self._open_cloud_status_page(settings))

    def _open_cloud_status_page(self, settings):
        self.emit("loading_start", "正在打开云环境部署状态...")
        url = settings.get("url", "").strip()
        username = settings.get("username", "").strip()
        password = settings.get("password_plain", "")
        if not url:
            self.emit("cloud_status_fallback", (settings, "Rancher 状态页地址为空"))
            self.emit("loading_stop", None)
            return
        if not username or not password:
            self.emit(
                "cloud_status_fallback",
                (settings, "Rancher 登录账号或密码未配置"),
            )
            self.emit("loading_stop", None)
            return

        By = importlib.import_module("selenium.webdriver.common.by").By
        WebDriverWait = importlib.import_module(
            "selenium.webdriver.support.ui"
        ).WebDriverWait
        driver = None
        try:
            driver = create_browser_driver()
            driver.get(url)
            wait = WebDriverWait(driver, 30)

            def first_visible(browser, selectors):
                for selector in selectors:
                    for element in browser.find_elements(By.CSS_SELECTOR, selector):
                        if element.is_displayed() and element.is_enabled():
                            return element
                return False

            username_selectors = [
                "input[name='username']",
                "input[autocomplete='username']",
                "input[type='email']",
                "input[type='text']",
            ]
            password_selectors = [
                "input[name='password']",
                "input[autocomplete='current-password']",
                "input[type='password']",
            ]
            username_input = wait.until(
                lambda browser: first_visible(browser, username_selectors)
            )
            password_input = wait.until(
                lambda browser: first_visible(browser, password_selectors)
            )
            username_input.clear()
            username_input.send_keys(username)
            password_input.clear()
            password_input.send_keys(password)

            def find_login_button(browser):
                submit = first_visible(browser, ["button[type='submit']"])
                if submit:
                    return submit
                for element in browser.find_elements(By.CSS_SELECTOR, "button"):
                    text = (element.text or "").strip().lower()
                    if element.is_displayed() and element.is_enabled() and any(
                        word in text for word in ("登录", "登入", "log in", "login", "sign in")
                    ):
                        return element
                return False

            wait.until(find_login_button).click()
            wait.until(
                lambda browser: not first_visible(browser, password_selectors)
                and "/auth/login" not in (browser.current_url or "").lower()
            )
            driver.get(url)
            self.dashboard_drivers.append(driver)
            self.emit(
                "cloud_status_login_ok",
                settings.get("name", "Rancher Pod 状态页"),
            )
        except Exception as exc:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            self.emit(
                "cloud_status_fallback",
                (settings, f"浏览器自动化失败：{exception_message(exc)}"),
            )
        finally:
            self.emit("loading_stop", None)

    def _cloud_driver_is_alive(self):
        if self.cloud_driver is None:
            return False
        try:
            self.cloud_driver.current_url
            return True
        except Exception:
            self.cloud_driver = None
            return False

    def _cloud_api_is_alive(self):
        return bool(self.ops_api and self.ops_api.authenticated)

    @staticmethod
    def _cloud_login_identity(settings):
        return (
            settings["url"].strip().rstrip("/"), settings["username"].strip(),
            hashlib.sha256(settings["password_plain"].encode()).digest(),
        )

    def _discard_cloud_session(self, expected_identity=None):
        with self._ops_session_state_lock:
            self._ops_session_generation += 1
            self.ops_api = None
            self._ops_api_identity = None
            self._ops_expected_identity = expected_identity
        self._ops_maintenance_failed = False

    def _maintain_cloud_session(self):
        if self._ops_session_disposed.is_set():
            return
        if self._ops_session_thread and self._ops_session_thread.is_alive():
            return
        with self._ops_session_state_lock:
            client = self.ops_api
            generation = self._ops_session_generation
            identity = self._ops_api_identity
        if client is None or identity is None or not client.maintenance_due():
            return

        def maintain():
            failed = False
            try:
                client.maintain_session()
            except Exception:
                failed = True
            if not self._ops_session_disposed.is_set():
                try:
                    self.sig_cloud_session_result.emit({
                        "generation": generation, "client": client,
                        "initial": False, "failed": failed,
                    })
                except RuntimeError:
                    # The task widget may have been removed while HTTP was pending.
                    pass

        # Idle maintenance must not keep a removable task or application alive.
        self._ops_session_thread = threading.Thread(target=maintain, daemon=True)
        self._ops_session_thread.start()

    @Slot(object)
    def _on_cloud_session_result(self, result):
        if self._ops_session_disposed.is_set():
            return
        with self._ops_session_state_lock:
            if result["generation"] != self._ops_session_generation:
                return
            if result["client"] is not self.ops_api:
                return
        if result["initial"]:
            if result["failed"]:
                self._on_cloud_status("登录失败")
                self._on_cloud_log(f'Ops API 登录失败：{result["error"]}')
            else:
                self._on_cloud_log("Ops API 登录成功。")
                self._on_cloud_status("已登录")
            return
        if result["failed"] and not self._ops_maintenance_failed:
            self._on_cloud_log("Ops 会话维护暂未成功，将自动重试；也可重新登录。")
        elif not result["failed"] and self._ops_maintenance_failed:
            self._on_cloud_log("Ops 会话维护已恢复。")
        self._ops_maintenance_failed = result["failed"]

    def _invalidate_cloud_account(self, _text=""):
        self._discard_cloud_session(self._cloud_login_identity({
            "url": self.cloud_url.text(), "username": self.cloud_username.text(),
            "password_plain": self.cloud_password.text(),
        }))
        for source in self._cloud_option_requests:
            self._cloud_option_requests[source] += 1
        self.build_cloud_target_dirty = True
        self._set_build_cloud_loading(False, "登录信息已修改，请重新加载目标")
        self._set_cloud_target_loading(False, "登录信息已修改，请重新加载目标")

    @staticmethod
    def _cloud_options_cache_key(settings):
        return json.dumps([
            settings.get("url", "").strip().rstrip("/"),
            settings.get("username", "").strip(),
            (settings.get("cloud") or "Huawei").strip().casefold(),
            settings.get("project", "").strip().casefold(),
        ], ensure_ascii=False)

    def _cached_cloud_options(self, settings, source):
        data = self._cached_selection("cloud_targets", self._cloud_options_cache_key(settings))
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("project"), str):
            return None
        if data["project"].casefold() != settings.get("project", "").strip().casefold():
            return None
        for field in ("projects", "types"):
            if not isinstance(data.get(field), list) or not all(
                isinstance(value, str) for value in data[field]
            ):
                return None
        if data["project"] and data["project"] not in data["projects"]:
            return None
        regions_by_type = data.get("regions_by_type")
        if not isinstance(regions_by_type, dict):
            return None
        for deploy_type in data["types"]:
            regions = regions_by_type.get(deploy_type)
            if not isinstance(regions, list) or not all(isinstance(value, str) for value in regions):
                return None
        return self._cloud_options_payload(settings, source, data)

    @staticmethod
    def _cloud_options_payload(settings, source, data):
        preferred = settings.get("deploy_type", "")
        selected = next(
            (value for value in data["types"] if service_type_matches(preferred, value)),
            data["types"][0] if data["types"] else "",
        )
        return {
            "projects": data["projects"], "types": data["types"],
            "project": data["project"], "type": selected,
            "regions": data["regions_by_type"].get(selected, []), "source": source,
            "request_id": settings.get("_options_request_id"),
        }

    def _cloud_cache_is_complete(self, settings):
        root = self._cached_cloud_options({**settings, "project": ""}, "tab")
        return root is not None and all(
            self._cached_cloud_options({**settings, "project": project}, "tab") is not None
            for project in root["projects"]
        )

    def _collect_cloud_cache(self, settings, *, force=False, progress=None):
        root_settings = {**settings, "project": ""}
        root = self._cached_cloud_options(root_settings, "tab") if not force else None
        api = None
        cloud = settings.get("cloud") or "Huawei"
        if root is not None:
            projects = root["projects"]
        else:
            api = self._ensure_cloud_login(settings)
            projects = api.list_projects(cloud, refresh=force)
        entries = {
            self._cloud_options_cache_key(root_settings): {
                "projects": projects, "project": "", "types": [], "regions_by_type": {},
            },
        }
        failures = {}
        for index, project in enumerate(projects, 1):
            project_settings = {**settings, "project": project}
            key = self._cloud_options_cache_key(project_settings)
            if progress:
                progress(f"正在缓存云端目标 {index}/{len(projects)}：{project}")
            if not force and self._cached_cloud_options(project_settings, "tab") is not None:
                value = self._cached_selection("cloud_targets", key)
                value["projects"] = list(projects)
            else:
                if api is None:
                    api = self._ensure_cloud_login(settings)
                try:
                    options = api.load_target_options(project, cloud, refresh=force)
                except Exception as exc:
                    failures[project] = exception_message(exc)
                    if self._cached_cloud_options(project_settings, "tab") is not None:
                        previous = self._cached_selection("cloud_targets", key)
                        previous["projects"] = list(projects)
                        entries[key] = previous
                    continue
                value = {
                    "projects": projects, "project": project, "types": options["types"],
                    "regions_by_type": {
                        service: list(dict.fromkeys(
                            target["region_label"] for target in options["targets"]
                            if target.get("deploy_type_label") == service
                        )) for service in options["types"]
                    },
                }
            entries[key] = value
        return entries, failures

    def _queue_cloud_options(
        self, settings, source="tab", *, force=False, package="",
        open_browser=False, on_finished=None,
    ):
        request_source = "build" if source == "build" else "tab"
        self._cloud_option_requests[request_source] += 1
        settings = dict(settings)
        settings["_options_request_id"] = self._cloud_option_requests[request_source]
        settings["_refresh_options"] = force
        if not force and not package and not open_browser:
            cached = self._cached_cloud_options(settings, source)
            if cached is not None and self._cloud_cache_is_complete(settings):
                self._on_cloud_options(cached)
                if on_finished:
                    on_finished()
                return
        if source == "build":
            self.build_cloud_target_dirty = True
            self._set_build_cloud_loading(True, "正在加载云端目标（优先使用缓存）...")
        else:
            self._set_cloud_target_loading(True, "正在加载部署目标（优先使用缓存）...")

        def load():
            try:
                if package:
                    self._load_cloud_package_target_options(settings, package)
                else:
                    self._login_and_load_cloud(settings, source, open_browser=open_browser)
            except Exception as exc:
                self.emit("cloud_options", {
                    "source": source, "request_id": settings["_options_request_id"],
                    "error": exception_message(exc),
                })

        self.run_thread(load, on_finished=on_finished)

    def login_cloud_platform(self, *, open_browser=False):
        if self._cloud_login_in_progress:
            return
        settings = self._cloud_settings_from_ui()
        if not settings["url"] or not settings["username"] or not settings["password_plain"]:
            QMessageBox.warning(self, "缺少登录信息", "平台地址、登录账号和密码不能为空")
            return
        self._save_cloud_settings(settings)
        # Explicit login must also recover an expired API session.
        self._discard_cloud_session(self._cloud_login_identity(settings))
        self._cloud_login_in_progress = True
        self.cloud_login_button.setEnabled(False)
        self.cloud_status_label.setText("正在登录...")
        self._queue_cloud_options(
            settings, force=True, open_browser=open_browser,
            on_finished=self._finish_cloud_login,
        )

    def _finish_cloud_login(self):
        self._cloud_login_in_progress = False
        self.cloud_login_button.setEnabled(True)

    def refresh_cloud_options(self, *, force=False):
        if self.cloud_options_loading:
            return
        settings = self._cloud_settings_from_ui()
        if not self._cloud_account_complete(settings):
            QMessageBox.warning(self, "缺少登录信息", "平台地址、登录账号和密码不能为空")
            return
        self._queue_cloud_options(settings, force=force)

    def apply_cloud_target_from_package(self):
        package = self.cloud_package.text().strip().strip("[]")
        try:
            infer_cloud_deployment_config(package)
        except ValueError as exc:
            self.message_overlay.show_message("包号无法解析", str(exc))
            return
        settings = self._cloud_settings_from_ui()
        if not self._cloud_account_complete(settings):
            self.cloud_target_loading_label.setText("请先补全 Ops 登录信息")
            return
        self.cloud_package.setText(package)
        self._queue_cloud_options(settings, "package", package=package)

    def _on_deployment_package_editing_finished(self):
        # Jenkins also writes this input. Only user edits should change a
        # preview on blur, and Enter already resets the modified flag.
        if self.package_input.isModified():
            DeployTool._preview_ssh_fields_from_package(self, report_error=False)

    def _preview_ssh_fields_from_package(self, *, report_error=True):
        try:
            packages = parse_deployment_packages(self.package_input.text())
            package = packages[0]
            if hasattr(self, "deploy_package_preview"):
                preview = self.deploy_package_preview
                current = preview.currentItem()
                if current and current.text(0) in packages:
                    package = current.text(0)
                preview.blockSignals(True)
                preview.clear()
                for item_package in packages:
                    target = infer_ssh_deployment_config(item_package)
                    item = QTreeWidgetItem([item_package, posixpath.join(target["yaml_dir"], target["yaml_file"])])
                    item.setToolTip(0, item_package)
                    item.setToolTip(1, item.text(1))
                    preview.addTopLevelItem(item)
                    if item_package == package:
                        preview.setCurrentItem(item)
                preview.blockSignals(False)
                preview.setFixedHeight(min(160, max(100, preview.header().sizeHint().height()
                                                    + len(packages) * 32 + 4)))
                preview.setVisible(len(packages) > 1)
            ssh_target = infer_ssh_deployment_config(package)
        except ValueError as exc:
            if report_error:
                self.message_overlay.show_message("包号无法解析", str(exc))
            return

        # These values remain unsaved until the user explicitly saves the
        # environment, so trying a package cannot silently rewrite its profile.
        DeployTool._apply_ssh_target_fields(self, ssh_target, mark_dirty=True)
        self.package_input.setModified(False)
        self.deploy_log.appendPlainText(
            "已按包号解析 SSH 配置："
            f'{ssh_target["yaml_dir"]} / {ssh_target["yaml_file"]} / '
            f'{ssh_target["apply_command"]}'
        )
        return package if len(packages) == 1 else None

    def _on_deploy_package_selected(self, current, previous=None):
        if current:
            DeployTool._apply_ssh_target_fields(
                self, infer_ssh_deployment_config(current.text(0)), mark_dirty=True,
            )

    def apply_deployment_targets_from_package(self):
        """Resolve SSH and Ops deployment targets when package input presses Enter."""
        package = DeployTool._preview_ssh_fields_from_package(self)
        if package is None:
            return

        settings = self._cloud_settings_from_ui()
        if any(
            not settings.get(key)
            for key in ("url", "username", "password_plain")
        ):
            self.cloud_status_label.setText("包号已解析；请先补全 Ops 登录信息")
            self.message_overlay.show_message(
                "包号已解析",
                "SSH 配置已自动填写；补全 Ops 平台地址、账号和密码后，再按回车即可自动匹配云端部署目标。",
            )
            return

        self.store.data["last_package"] = package
        self.store.save()
        self.cloud_package.setText(package)
        self.cloud_status_label.setText("正在按包号匹配部署目标...")
        self._queue_cloud_options(settings, "package", package=package)

    def start_cloud_deploy(self):
        if getattr(self, "_task_runtime_active", False):
            self.message_overlay.show_message(
                "当前任务正在运行",
                f"请等待当前操作结束，或点击标题栏“新建任务”并行执行（{QKeySequence(QKeySequence.New).toString(QKeySequence.NativeText)}）。",
            )
            return
        if self.cloud_options_loading:
            QMessageBox.warning(self, "数据加载中", "请等待部署目标刷新完成后再部署")
            return
        settings = self._cloud_settings_from_ui()
        required = (
            "url", "username", "password_plain", "project", "deploy_type",
            "cloud", "region", "package",
        )
        if any(not settings[key] for key in required):
            QMessageBox.warning(
                self, "部署信息不完整",
                "请先登录，并选择部署专案、服务类型和云区节点；包号不能为空",
            )
            return
        message = (
            "将在鼎捷 Ops 平台执行云端部署：\n\n"
            f'部署专案：{settings["project"]}\n'
            f'服务类型：{settings["deploy_type"]}\n'
            f'目标区域：{settings["cloud"]} / {settings["region"]}\n'
            f'部署包号：{settings["package"]}\n\n'
            "该操作会改变云端运行环境，是否继续？"
        )
        if QMessageBox.question(self, "确认云端部署", message) != QMessageBox.Yes:
            return
        self._save_cloud_settings(settings)
        self.cloud_deploy_button.setEnabled(False)
        self.cloud_login_button.setEnabled(False)
        self.cloud_status_label.setText("正在执行云端部署...")
        self._set_cloud_target_loading(True, "正在提交云端部署...")
        self.cloud_log.clear()
        target = " / ".join(
            value for value in (
                settings.get("project", ""),
                settings.get("deploy_type", ""),
                settings.get("region", ""),
            ) if value
        )
        if hasattr(self, "_begin_tracked_task"):
            self._begin_tracked_task(
                "Ops 云端部署",
                target,
                package=settings["package"],
                title=settings.get("deploy_type", "") or settings.get("project", ""),
            )
        self.run_thread(
            lambda: self._run_cloud_deploy_with_target_lock(settings),
            on_finished=getattr(self, "_finish_standalone_task", None),
        )

    @staticmethod
    def _first_visible(elements):
        for element in elements:
            if element.is_displayed() and element.is_enabled():
                return element
        return False

    def _ensure_cloud_browser_login(self, settings, *, headless=True, report_status=True):
        By = importlib.import_module("selenium.webdriver.common.by").By
        Keys = importlib.import_module("selenium.webdriver.common.keys").Keys
        WebDriverWait = importlib.import_module("selenium.webdriver.support.ui").WebDriverWait

        self.emit("cloud_log", "正在打开鼎捷 Ops 云端平台...")
        try:
            # An explicit login uses a fresh visible session so changed account
            # credentials cannot silently reuse a previous browser identity.
            if not headless and self._cloud_driver_is_alive():
                self.cloud_driver.quit()
                self.cloud_driver = None
            if not self._cloud_driver_is_alive():
                self.cloud_driver = create_browser_driver(headless=headless)
            driver = self.cloud_driver
            driver.get(settings["url"])
            wait = WebDriverWait(driver, 30)

            def page_context():
                current_url = ""
                title = ""
                try:
                    current_url = driver.current_url
                    title = driver.title
                except Exception:
                    pass
                details = [value for value in (title, current_url) if value]
                return "；当前页面：" + " / ".join(details) if details else ""

            def wait_for(condition, step, recovery):
                try:
                    return wait.until(condition)
                except Exception as exc:
                    raise RuntimeError(
                        f"{step}超时（{exception_message(exc)}）{page_context()}。{recovery}"
                    ) from exc

            def password_field(browser):
                return self._first_visible(
                    browser.find_elements(By.CSS_SELECTOR, "input[type='password']")
                )

            def login_page_ready(browser):
                return (
                    browser.find_elements(By.CSS_SELECTOR, "app-flow-deploy")
                    or password_field(browser)
                    or "/login" in browser.current_url.lower()
                    or "/signin" in browser.current_url.lower()
                    or "iam" in browser.current_url.lower()
                )

            # IAM 路由会调整，使用页面控件与 URL 组合判断登录状态。
            wait_for(
                login_page_ready,
                "等待 Ops 登录页或流水线页面加载",
                "请检查平台地址、网络连通性以及 Ops 平台是否可正常访问",
            )

            if password_field(driver) or any(
                marker in driver.current_url.lower()
                for marker in ("/login", "/signin", "iam")
            ):
                self.emit("cloud_log", "平台要求登录，正在填写账号凭据...")

                def login_account(browser):
                    for element in browser.find_elements(By.CSS_SELECTOR, "input"):
                        placeholder = (element.get_attribute("placeholder") or "").lower()
                        name = (element.get_attribute("name") or "").lower()
                        autocomplete = (element.get_attribute("autocomplete") or "").lower()
                        input_type = (element.get_attribute("type") or "").lower()
                        if element.is_displayed() and (
                            "用户" in placeholder or "帐号" in placeholder
                            or "账号" in placeholder or "手机号" in placeholder
                            or "e-mail" in placeholder or "email" in placeholder
                            or "user" in placeholder or "account" in placeholder
                            or name in ("username", "account", "email", "login")
                            or autocomplete == "username"
                            or input_type == "email"
                        ):
                            return element
                    return False

                def login_password(browser):
                    return password_field(browser)

                account_input = wait_for(
                    login_account,
                    "查找 Ops 登录账号输入框",
                    "请确认 Ops 登录页面结构未变，并尝试在“云端部署”页重新登录",
                )
                password_input = wait_for(
                    login_password,
                    "查找 Ops 登录密码输入框",
                    "请确认登录页面已完整加载",
                )
                account_input.clear()
                account_input.send_keys(settings["username"])
                # IAM uses Angular touched/valid state to enable the login button.
                account_input.send_keys(Keys.TAB)
                password_input.clear()
                password_input.send_keys(settings["password_plain"])
                password_input.send_keys(Keys.TAB)

                def login_button(browser):
                    for button in browser.find_elements(By.CSS_SELECTOR, "button"):
                        text = (button.text or "").strip().lower()
                        button_type = (button.get_attribute("type") or "").lower()
                        if button.is_displayed() and button.is_enabled() and (
                            text in ("登入", "登录", "sign in", "login")
                            or button_type == "submit"
                        ):
                            return button
                    return False

                previous_token = browser_session_token(driver)
                wait_for(
                    login_button,
                    "查找 Ops 登录按钮",
                    "请确认账号密码输入完成后登录按钮可用",
                ).click()

                def authentication_complete(browser):
                    if browser.find_elements(By.CSS_SELECTOR, "app-flow-deploy"):
                        return True
                    token = browser_session_token(browser)
                    if token and token != previous_token:
                        return True
                    # Surface a concrete IAM validation message instead of
                    # waiting for the unrelated deployment page to time out.
                    for selector in (
                        ".ant-form-item-explain-error",
                        ".ant-message-error",
                        "[role='alert']",
                    ):
                        for element in browser.find_elements(By.CSS_SELECTOR, selector):
                            message = (element.text or "").strip()
                            if element.is_displayed() and message:
                                raise RuntimeError(f"Ops 登录失败：{message}")
                    return False

                # IAM login is asynchronous. Wait for the actual session token;
                # a fixed sleep could reload the page before authentication lands.
                wait_for(
                    authentication_complete,
                    "等待 Ops 身份认证完成",
                    "请检查账号密码、登录权限和 IAM 服务状态",
                )
                driver.execute_script("sessionStorage.setItem('cloud', 'Huawei')")
                if not driver.find_elements(By.CSS_SELECTOR, "app-flow-deploy"):
                    driver.get(settings["url"])
            wait_for(
                lambda browser: browser.find_elements(By.CSS_SELECTOR, "app-flow-deploy"),
                "等待 Ops 流水线部署页面加载",
                "请检查账号密码和流水线部署权限；若浏览器中能登录，请重新保存 Ops 密码后再试",
            )
            driver.execute_script("sessionStorage.setItem('cloud', 'Huawei')")
            self.emit("cloud_log", "登录成功，流水线部署页面已加载。")
            if report_status:
                self.emit("cloud_status", "已登录")
            return driver
        except Exception as exc:
            if report_status:
                self.emit("cloud_status", "登录失败")
            reason = exception_message(exc)
            self.emit("cloud_log", f"登录失败：{reason}")
            raise RuntimeError(f"Ops 登录失败：{reason}") from exc

    def _ensure_cloud_login(self, settings):
        identity = self._cloud_login_identity(settings)
        generation = self._ops_session_generation
        with self._ops_login_lock:
            with self._ops_session_state_lock:
                if (self._ops_session_disposed.is_set()
                        or generation != self._ops_session_generation
                        or (self._ops_expected_identity is not None
                            and self._ops_expected_identity != identity)):
                    raise OpsApiError("Ops 登录信息已变更，请重新加载目标")
                client = self.ops_api if self._ops_api_identity == identity else None
            if client is not None:
                client.maintain_session()
                with self._ops_session_state_lock:
                    if generation != self._ops_session_generation or self.ops_api is not client:
                        raise OpsApiError("Ops 登录信息已变更，请重新加载目标")
                return client
            self.emit("cloud_log", "正在通过 Ops API 登录...")
            try:
                client = OpsApiClient()
                client.login(
                    settings["url"], settings["username"], settings["password_plain"]
                )
                with self._ops_session_state_lock:
                    if generation != self._ops_session_generation:
                        raise OpsApiError("Ops 登录信息已变更，请重新加载目标")
                    self.ops_api = client
                    self._ops_api_identity = identity
                self.sig_cloud_session_result.emit({
                    "generation": generation, "client": client,
                    "initial": True, "failed": False,
                })
                return client
            except OpsApiError as exc:
                with self._ops_session_state_lock:
                    if generation != self._ops_session_generation:
                        raise
                    self.ops_api = None
                    self._ops_api_identity = None
                self.sig_cloud_session_result.emit({
                    "generation": generation, "client": None, "initial": True,
                    "failed": True, "error": exception_message(exc),
                })
                raise

    def _login_and_load_cloud(self, settings, source="tab", *, open_browser=False):
        self._ensure_cloud_login(settings)
        self._load_cloud_options(settings, source)
        if open_browser:
            self.emit("cloud_status", "部署数据已加载，正在打开并登录网页...")
            try:
                self._ensure_cloud_browser_login(
                    settings, headless=False, report_status=False
                )
                self.emit("cloud_status", "已登录，Ops 网页已打开")
            except Exception as exc:
                self.emit("cloud_status", "API 已登录，网页自动登录失败")
                self.emit(
                    "info",
                    "部署数据已加载，但 Ops 网页自动登录失败："
                    f"{exception_message(exc)}\n可在浏览器中手动登录，或再次点击登录按钮重试。",
                )

    def _load_cloud_options(self, settings, source="tab"):
        refresh = settings.get("_refresh_options", False)
        self.emit("cloud_log", "正在全量刷新云端目标..." if refresh else "正在补齐全部云端目标缓存...")
        entries, failures = self._collect_cloud_cache(
            settings, force=refresh, progress=lambda message: self.emit("cloud_log", message)
        )
        root_key = self._cloud_options_cache_key({**settings, "project": ""})
        projects = entries[root_key]["projects"]
        target_project = settings.get("project", "").strip()
        if target_project:
            matched_project = next(
                (value for value in projects if value.casefold() == target_project.casefold()), ""
            )
            if not matched_project:
                raise OpsApiError(f'Ops 中没有找到部署专案“{target_project}”')
            target_project = matched_project
        key = self._cloud_options_cache_key({**settings, "project": target_project})
        warnings = [f"{project}：{message}" for project, message in failures.items()]
        if target_project in failures:
            payload = {
                "source": source, "request_id": settings.get("_options_request_id"),
                "error": f"{target_project}：{failures[target_project]}；已有缓存保留，可刷新重试",
            }
        else:
            payload = self._cloud_options_payload(settings, source, entries[key])
            payload["cache_warnings"] = warnings
        payload["cache_entries"] = entries
        self.emit("cloud_options", payload)
        for warning in warnings:
            self.emit("cloud_log", f"云端缓存读取失败：{warning}")
        self.emit(
            "cloud_log",
            f"云端缓存未全部完成，{len(failures)} 个专案读取失败，已有缓存保留。" if failures
            else f"已全量缓存 {len(projects)} 个云端专案的服务类型和云区节点。",
        )
        self.emit("cloud_status", "已登录")

    def _load_cloud_package_target_options(self, settings, package):
        """Load the exact Ops project/type for a package and keep region behavior."""
        api = self._ensure_cloud_login(settings)
        cloud = settings.get("cloud") or "Huawei"
        self.emit("cloud_log", f"正在按包号解析云端部署目标：{package}")
        options = api.load_package_target_options(package, cloud)
        deploy_type = options["deploy_type"]
        regions = [
            target["region_label"]
            for target in options["targets"]
            if target.get("deploy_type_label") == deploy_type
        ]
        regions = list(dict.fromkeys(regions))
        self.emit(
            "cloud_options",
            {
                "projects": options["projects"],
                "types": options["types"],
                "regions": regions,
                "project": options["project"],
                "type": deploy_type,
                "source": "package",
                "request_id": settings.get("_options_request_id"),
            },
        )
        self.emit(
            "cloud_log",
            f'包号已匹配：{options["project"]} / {deploy_type}；云区节点沿用原选择逻辑。',
        )
        self.emit("cloud_status", "已登录")

    @trace_operation("cloud_deploy")
    def _run_cloud_deploy(self, settings, cancel_event=None):
        try:
            api = self._ensure_cloud_login(settings)
            if cancel_event and cancel_event.is_set():
                raise PipelineCancelled("云端部署提交前已取消")
            self.emit(
                "cloud_log",
                f'正在校验部署目标：{settings["project"]} / '
                f'{settings["deploy_type"]} / {settings["cloud"]} / '
                f'{settings["region"]}',
            )
            self.emit("cloud_log", f'正在校验镜像包：{settings["package"]}')
            capture = getattr(self, "_replaced_packages", None)

            def before_submit():
                if cancel_event and cancel_event.is_set():
                    raise PipelineCancelled("云端部署提交前已取消")
                if capture is not None:
                    capture.freeze()

            if capture is None:
                result = api.deploy(settings, before_submit=before_submit)
            else:
                def start_lookup(target):
                    # Isolate HTTP session/cache state from the submitting client.
                    headers = dict(api.headers)
                    cookies = api.session.cookies.copy()
                    frozen_target = copy.deepcopy(target)

                    def read(cancel):
                        reader = OpsApiClient(timeout=8)
                        reader.headers = headers
                        reader.session.cookies.update(cookies)
                        reader.authenticated = True
                        try:
                            return reader._load_current_packages_for_target(
                                frozen_target, cancel, request_timeout=8
                            )["packages"]
                        finally:
                            reader.session.close()

                    capture.start(settings["package"], read)

                result = api.deploy(settings, on_target_resolved=start_lookup,
                                    before_submit=before_submit)
            target = result["target"]
            self.emit(
                "cloud_log",
                f'部署请求已由 API 接收，并在部署记录中确认：{settings["package"]}；'
                f'目标记录 ID：{target.get("id", "-")}',
            )
            self.emit("cloud_status", "部署请求已提交")
            status_page = settings.get("status_page", {})
            if status_page.get("url"):
                self.emit("cloud_log", "部署已提交，正在打开 Rancher Pod 状态页...")
                self.emit("open_cloud_status", status_page)
            return result
        except PipelineCancelled as exc:
            self.emit("cloud_log", str(exc))
            self.emit("cloud_status", "部署已取消")
            raise
        except OpsSubmissionUnknown as exc:
            self.emit("cloud_log", f"{exception_message(exc)}；禁止直接重试，以免重复部署。")
            self.emit("cloud_status", "部署结果未知")
            raise
        except Exception as exc:
            self.emit("cloud_log", f"云端部署失败：{exception_message(exc)}")
            self.emit("cloud_status", "部署失败")
            raise
        finally:
            DeployTool._freeze_replaced_packages(self)

    def _run_cloud_deploy_with_target_lock(self, settings):
        target_key = self._deployment_target_key(
            "云端部署", cloud_settings=settings
        )
        self._wait_for_deployment_target(target_key, log_target="cloud_log")
        try:
            return self._run_cloud_deploy(settings)
        finally:
            self._release_deployment_target(target_key)

    def _load_cloud_options_browser(self, settings, source="tab"):
        By = importlib.import_module("selenium.webdriver.common.by").By
        Keys = importlib.import_module("selenium.webdriver.common.keys").Keys
        WebDriverWait = importlib.import_module("selenium.webdriver.support.ui").WebDriverWait
        driver = self.cloud_driver
        wait = WebDriverWait(driver, 30)
        self.emit("cloud_log", "正在读取部署专案、服务类型和云区节点...")

        def visible_project_names(browser):
            try:
                values = []
                for element in browser.find_elements(
                    By.CSS_SELECTOR, "app-flow-deploy .menu .anchor"
                ):
                    text = (element.text or "").strip()
                    if text and text not in values:
                        values.append(text)
                return values or False
            except Exception:
                return False

        visible_projects = wait.until(visible_project_names)
        # 页面搜索会直接过滤左侧 DOM，不能用过滤后的少量结果覆盖完整列表。
        if len(visible_projects) > len(self.cloud_project_names):
            self.cloud_project_names = visible_projects
        projects = list(self.cloud_project_names or visible_projects)

        deploy_types = []
        selected_type = ""
        regions = []
        target_project = settings.get("project", "")
        if target_project:
            def search_project(browser):
                try:
                    search = self._first_visible(browser.find_elements(
                        By.CSS_SELECTOR, "app-flow-deploy input[nz-input]"
                    ))
                    if not search:
                        return False
                    search.clear()
                    search.send_keys(target_project)
                    search.send_keys(Keys.ENTER)
                    return True
                except Exception:
                    return False

            wait.until(search_project)

            def select_project(browser):
                try:
                    for element in browser.find_elements(
                        By.CSS_SELECTOR, "app-flow-deploy .menu .anchor"
                    ):
                        if element.is_displayed() and (element.text or "").strip() == target_project:
                            browser.execute_script("arguments[0].click()", element)
                            return True
                except Exception:
                    return False
                return False

            wait.until(select_project)
            if target_project not in projects:
                projects.append(target_project)

            def service_types(browser):
                try:
                    values = []
                    for element in browser.find_elements(By.CSS_SELECTOR, ".ant-tabs-tab"):
                        text = (element.text or "").strip()
                        if (
                            element.is_displayed()
                            and text.endswith(("后端", "前端"))
                            and text.lower().startswith(target_project.lower())
                            and text not in values
                        ):
                            values.append(text)
                    return values or False
                except Exception:
                    return False

            deploy_types = wait.until(service_types)
            preferred_suffix = "前端" if settings.get("deploy_type", "").endswith("前端") else "后端"
            selected_type = next(
                (value for value in deploy_types if value.endswith(preferred_suffix)),
                deploy_types[0],
            )

            def select_service_tab(browser):
                try:
                    for element in browser.find_elements(By.CSS_SELECTOR, ".ant-tabs-tab"):
                        if element.is_displayed() and (element.text or "").strip() == selected_type:
                            browser.execute_script("arguments[0].click()", element)
                            return True
                except Exception:
                    return False
                return False

            wait.until(select_service_tab)
            # 预测试（PaaS）节点与普通区域分两次异步渲染，稍候再统一读取。
            time.sleep(2)
            def region_labels(browser):
                try:
                    values = []
                    cards = browser.find_elements(
                        By.CSS_SELECTOR, "app-flow-deploy-paas, app-flow-deploy-area"
                    )
                    for card in cards:
                        lines = [
                            line.strip() for line in (card.text or "").splitlines()
                            if line.strip()
                        ]
                        if len(lines) >= 2:
                            label = f"{lines[0]} / {lines[1]}"
                            if label not in values:
                                values.append(label)
                    return values or False
                except Exception:
                    return False

            regions = wait.until(region_labels)

        self.emit(
            "cloud_options",
            {
                "projects": projects,
                "types": deploy_types,
                "regions": regions,
                "project": target_project,
                "type": selected_type,
                "source": source,
            },
        )
        self.emit(
            "cloud_log",
            f"已加载 {len(projects)} 个部署专案、{len(deploy_types)} 个服务类型、"
            f"{len(regions)} 个云区节点。",
        )
        if regions and not any("预测试" in value for value in regions):
            self.emit(
                "cloud_log",
                "提示：当前专案/服务类型未启用预测试，Ops 平台仅返回其余云区节点。",
            )
        self.emit("cloud_status", "已登录")

    def _run_cloud_deploy_browser(self, settings, cancel_event=None):
        By = importlib.import_module("selenium.webdriver.common.by").By
        Keys = importlib.import_module("selenium.webdriver.common.keys").Keys
        ActionChains = importlib.import_module(
            "selenium.webdriver.common.action_chains"
        ).ActionChains
        WebDriverWait = importlib.import_module("selenium.webdriver.support.ui").WebDriverWait

        def wait_for(wait, condition, step):
            try:
                return wait.until(condition)
            except Exception as exc:
                raise RuntimeError(
                    f"{step}失败：{exception_message(exc)}。请确认 Ops 页面数据已加载后重试"
                ) from exc

        try:
            driver = self._ensure_cloud_browser_login(settings)
            if cancel_event and cancel_event.is_set():
                raise PipelineCancelled("云端部署提交前已取消")
            wait = WebDriverWait(driver, 30)
            self.emit("cloud_log", f'正在搜索部署专案：{settings["project"]}')

            def search_input(browser):
                return self._first_visible(browser.find_elements(
                    By.CSS_SELECTOR, "app-flow-deploy input[nz-input]"
                ))

            def fill_search(browser):
                try:
                    search = search_input(browser)
                    if not search:
                        return False
                    search.clear()
                    search.send_keys(settings["project"])
                    search.send_keys(Keys.ENTER)
                    return True
                except Exception:
                    return False

            wait_for(wait, fill_search, "搜索部署专案")

            def select_project(browser):
                try:
                    for element in browser.find_elements(
                        By.CSS_SELECTOR, "app-flow-deploy .menu .anchor"
                    ):
                        if element.is_displayed() and (element.text or "").strip() == settings["project"]:
                            browser.execute_script("arguments[0].click()", element)
                            return True
                except Exception:
                    return False
                return False

            wait_for(wait, select_project, f'选择部署专案“{settings["project"]}”')
            self.emit("cloud_log", "部署专案已选择，正在切换服务类型...")

            def select_service_tab(browser):
                try:
                    for element in browser.find_elements(By.CSS_SELECTOR, ".ant-tabs-tab"):
                        if element.is_displayed() and service_type_matches(
                            settings["deploy_type"], element.text or ""
                        ):
                            browser.execute_script("arguments[0].click()", element)
                            return True
                except Exception:
                    return False
                return False

            wait_for(wait, select_service_tab, f'切换服务类型“{settings["deploy_type"]}”')
            if cancel_event and cancel_event.is_set():
                raise PipelineCancelled("云端部署提交前已取消")

            def target_area(browser):
                try:
                    for element in browser.find_elements(
                        By.CSS_SELECTOR, "app-flow-deploy-paas, app-flow-deploy-area"
                    ):
                        text = element.text or ""
                        if (
                            element.is_displayed()
                            and settings["cloud"].lower() in text.lower()
                            and settings["region"] in text
                        ):
                            return element
                except Exception:
                    return False
                return False

            wait_for(
                wait,
                target_area,
                f'定位云区“{settings["cloud"]} / {settings["region"]}”',
            )
            self.emit(
                "cloud_log",
                f'已定位目标区域：{settings["cloud"]} / {settings["region"]}',
            )

            def deploy_action(browser):
                try:
                    area = target_area(browser)
                    if not area:
                        return False
                    # Ops renders different deployment cards with either an in-card
                    # action or a detached hover layer. Trigger both CSS and JS hover.
                    ActionChains(browser).move_to_element(area).perform()
                    browser.execute_script(
                        """
                        for (const type of ['mouseover', 'mouseenter', 'mousemove']) {
                          arguments[0].dispatchEvent(new MouseEvent(type, {
                            bubbles: true, view: window
                          }));
                        }
                        """,
                        area,
                    )

                    candidates = area.find_elements(
                        By.CSS_SELECTOR,
                        "button, a, [role='button'], .click, [translate], [title], [aria-label]",
                    )
                    area_rect = area.rect
                    for element in browser.find_elements(
                        By.CSS_SELECTOR,
                        "body > .cdk-overlay-container button, "
                        "body > .cdk-overlay-container a, "
                        "body > .cdk-overlay-container [role='button'], "
                        "body > .cdk-overlay-container .click, "
                        ".ant-popover button, .ant-popover a, .ant-popover [role='button'], "
                        ".ant-popover .click",
                    ):
                        rect = element.rect
                        # Detached action layers must still be visually close to the target card.
                        if (
                            rect["x"] <= area_rect["x"] + area_rect["width"] + 80
                            and rect["x"] + rect["width"] >= area_rect["x"] - 80
                            and rect["y"] <= area_rect["y"] + area_rect["height"] + 80
                            and rect["y"] + rect["height"] >= area_rect["y"] - 80
                        ):
                            candidates.append(element)

                    preferred = []
                    fallback = []
                    for element in candidates:
                        if not element.is_displayed() or not element.is_enabled():
                            continue
                        label = " ".join(
                            filter(
                                None,
                                (
                                    (element.text or "").strip(),
                                    (element.get_attribute("title") or "").strip(),
                                    (element.get_attribute("aria-label") or "").strip(),
                                    (element.get_attribute("translate") or "").strip(),
                                ),
                            )
                        ).lower()
                        if any(value in label for value in ("部署新版本", "发布新版本")):
                            preferred.append(element)
                        elif any(value in label for value in ("部署", "发布", "deploy", "release")):
                            if not any(value in label for value in ("记录", "历史", "日志", "回滚")):
                                fallback.append(element)
                        elif "click" in (element.get_attribute("class") or "").split():
                            fallback.append(element)
                    target = (preferred or fallback or [False])[0]
                    if not target:
                        return False
                    browser.execute_script("arguments[0].click()", target)
                    return True
                except Exception:
                    return False

            wait_for(wait, deploy_action, "打开部署新版本窗口")
            self.emit("cloud_log", "部署窗口已打开，正在匹配刚构建的包号...")

            def open_version_select(browser):
                try:
                    selectors = (
                        ".ant-modal nz-select[formcontrolname='version']",
                        ".ant-modal nz-select[formcontrolname*='version']",
                        ".ant-modal nz-select",
                    )
                    for selector in selectors:
                        element = self._first_visible(
                            browser.find_elements(By.CSS_SELECTOR, selector)
                        )
                        if element:
                            browser.execute_script("arguments[0].click()", element)
                            return True
                except Exception:
                    return False
                return False

            wait_for(wait, open_version_select, "打开部署包号下拉框")

            def select_package(browser):
                try:
                    for element in browser.find_elements(
                        By.CSS_SELECTOR,
                        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
                        ".ant-select-item-option",
                    ):
                        text = (element.text or "").strip()
                        title = (element.get_attribute("title") or "").strip()
                        if element.is_displayed() and (
                            settings["package"] in text or settings["package"] in title
                        ):
                            browser.execute_script("arguments[0].click()", element)
                            return True
                except Exception:
                    return False
                return False

            wait_for(
                wait,
                select_package,
                f'匹配部署包号“{settings["package"]}”',
            )
            if cancel_event and cancel_event.is_set():
                raise PipelineCancelled("云端部署提交前已取消")

            def start_button(browser):
                try:
                    for modal in browser.find_elements(By.CSS_SELECTOR, ".ant-modal"):
                        if not modal.is_displayed():
                            continue
                        for button in modal.find_elements(By.CSS_SELECTOR, "button"):
                            text = (button.text or "").strip()
                            if button.is_displayed() and button.is_enabled() and text in (
                                "开始部署", "开始发布", "确定"
                            ):
                                browser.execute_script("arguments[0].click()", button)
                                return True
                except Exception:
                    return False
                return False

            wait_for(wait, start_button, "提交云端部署请求")
            self.emit("cloud_log", f'部署请求已提交：{settings["package"]}')
            self.emit("cloud_status", "部署请求已提交")
        except PipelineCancelled as exc:
            self.emit("cloud_log", str(exc))
            self.emit("cloud_status", "部署已取消")
            raise
        except Exception as exc:
            self.emit("cloud_log", f"云端部署失败：{exception_message(exc)}")
            self.emit("cloud_status", "部署失败")
            raise

    # ── Dashboard Management ───────────────────────────────────────
    def refresh_dashboards(self):
        if not hasattr(self, "dashboard_tree"):
            return
        self.dashboard_tree.clear()
        for index, item in enumerate(self.store.data.get("dashboards", [])):
            tree_item = QTreeWidgetItem()
            tree_item.setText(0, item.get("name", ""))
            tree_item.setText(1, item.get("url", ""))
            tree_item.setToolTip(0, "双击用系统默认浏览器打开环境页面并复制密码")
            dashboard_tip = "双击自动填写 Dashboard Token 并登录，随后打开 etm Pods"
            tree_item.setToolTip(1, dashboard_tip)
            tree_item.setData(0, Qt.UserRole, index)
            self.dashboard_tree.addTopLevelItem(tree_item)
        # Update dashboard combo in env entries
        for entries in (self.env_entries, self.build_env_entries):
            dashboard_widget = entries.get("dashboard")
            if isinstance(dashboard_widget, SearchableComboBox):
                dashboard_widget.set_values(self._dashboard_names())

    def _dashboard_dialog(self, existing=None):
        dialog = DashboardDialog(self, "Dashboard", existing)
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.result

    def add_dashboard(self):
        item = self._dashboard_dialog()
        if item:
            if item["name"] in self._dashboard_names():
                QMessageBox.warning(self, "名称重复", f'Dashboard“{item["name"]}”已存在')
                return
            self.store.data["dashboards"].append(item)
            ensure_dashboard_bindings(self.store.data)
            self.store.save()
            self.refresh_dashboards()

    def edit_dashboard(self):
        selected = self.dashboard_tree.selectedItems()
        if not selected:
            return
        index = selected[0].data(0, Qt.UserRole)
        existing = self.store.data["dashboards"][index]
        item = self._dashboard_dialog(existing)
        if item:
            if any(
                i != index and dashboard.get("name") == item["name"]
                for i, dashboard in enumerate(self.store.data["dashboards"])
            ):
                QMessageBox.warning(self, "名称重复", f'Dashboard“{item["name"]}”已存在')
                return
            item["id"] = existing["id"]
            rename_bound_dashboard(self.store.data, item["id"], item["name"])
            self.store.data["dashboards"][index] = item
            self.store.save()
            self.refresh_dashboards()
            self.refresh_environment()

    def delete_dashboard(self):
        selected = self.dashboard_tree.selectedItems()
        if not selected:
            return
        reply = QMessageBox.question(self, "确认删除", "删除选中的 Dashboard？")
        if reply == QMessageBox.Yes:
            index = selected[0].data(0, Qt.UserRole)
            dashboard = self.store.data["dashboards"][index]
            dashboard_id = dashboard.get("id", "")
            dashboard_name = dashboard.get("name", "")
            del self.store.data["dashboards"][index]
            for environment in self.store.data.get("environments", []):
                if (
                    environment.get("dashboard_id") == dashboard_id
                    or environment.get("dashboard") == dashboard_name
                ):
                    environment["dashboard_id"] = ""
                    environment["dashboard"] = ""
            self.store.save()
            self.refresh_dashboards()
            self.refresh_environment()

    def open_dashboard(self):
        selected = self.dashboard_tree.selectedItems()
        if not selected:
            QMessageBox.warning(self, "未选择", "请选择一个 Dashboard")
            return
        index = selected[0].data(0, Qt.UserRole)
        item = self.store.data["dashboards"][index]
        self._on_open_dashboard_item(item)

    def _on_dashboard_item_double_clicked(self, tree_item, column):
        index = tree_item.data(0, Qt.UserRole)
        dashboard = self.store.data["dashboards"][index]
        if column == 0:
            self._open_dashboard_environment(dashboard)
        else:
            self._on_open_dashboard_item(dashboard)

    def _open_dashboard_environment(self, item):
        url = item.get("env_url", "").strip()
        if not url:
            QMessageBox.warning(self, "环境链接为空", "当前 Dashboard 未配置环境 URL")
            return
        username = item.get("env_username", "").strip()
        password = unprotect_secret(item.get("env_password", ""))
        if password:
            QApplication.clipboard().setText(password)
        webbrowser.open(url)
        credential_hint = (
            "环境密码已复制到剪贴板，请在浏览器中粘贴。"
            if password else "当前环境未保存密码，请手工输入。"
        )
        QMessageBox.information(
            self,
            "环境页面已打开",
            f"账号：{username or '未配置'}\n{credential_hint}",
        )

    def _open_dashboard_url(self, item):
        """Open the Dashboard address with the system default browser."""
        url = item.get("url", "").strip()
        if not url:
            QMessageBox.warning(self, "Dashboard 地址为空", "当前 Dashboard 未配置地址")
            return
        url = dashboard_pods_url(url, item.get("namespace"))
        webbrowser.open(url)

    def _open_dashboard_item(self, item):
        self.emit("loading_start", "正在打开 Dashboard...")
        url = item.get("url", "").strip()
        namespace = str(item.get("namespace") or "").strip() or "etm"
        if not url:
            self.emit("dashboard_fallback", (item, "Dashboard 地址为空"))
            self.emit("loading_stop", None)
            return
        token = unprotect_secret(item.get("token", ""))
        if not token:
            self.emit("dashboard_fallback", (item, "Dashboard Token 为空"))
            self.emit("loading_stop", None)
            return
        driver = None
        try:
            By = importlib.import_module("selenium.webdriver.common.by").By
            WebDriverWait = importlib.import_module("selenium.webdriver.support.ui").WebDriverWait
            StaleElementReferenceException = importlib.import_module(
                "selenium.common.exceptions"
            ).StaleElementReferenceException
            driver = create_browser_driver()
            driver.get(url)
            wait = WebDriverWait(driver, 25)

            def find_token_input(browser):
                selectors = [
                    "textarea[name='token']",
                    "textarea",
                    "input[name='token']",
                    "input[type='password']",
                ]
                for selector in selectors:
                    for element in browser.find_elements(By.CSS_SELECTOR, selector):
                        try:
                            if element.is_displayed() and element.is_enabled():
                                return element
                        except StaleElementReferenceException:
                            continue
                return False

            def enter_token(browser):
                token_input = find_token_input(browser)
                if not token_input:
                    return False
                try:
                    token_input.clear()
                    token_input.send_keys(token)
                    return True
                except StaleElementReferenceException:
                    return False

            wait.until(enter_token)

            def find_login_button(browser):
                selectors = ["button[type='submit']", "button"]
                words = ("登录", "sign in", "login")
                for selector in selectors:
                    for element in browser.find_elements(By.CSS_SELECTOR, selector):
                        try:
                            text = (element.text or "").strip().lower()
                            if element.is_displayed() and element.is_enabled() and (
                                selector == "button[type='submit']"
                                or any(word in text for word in words)
                            ):
                                return element
                        except StaleElementReferenceException:
                            continue
                return False

            def click_login_button(browser):
                login_button = find_login_button(browser)
                if not login_button:
                    return False
                try:
                    login_button.click()
                    return True
                except StaleElementReferenceException:
                    return False

            wait.until(click_login_button)
            wait.until(lambda browser: not find_token_input(browser))
            pods_url = dashboard_pods_url(url, namespace)
            driver.get(pods_url)
            wait.until(
                lambda browser: urlsplit(browser.current_url or "").fragment
                == urlsplit(pods_url).fragment
            )
            self.dashboard_drivers.append(driver)
            self.emit("dashboard_login_ok", (item["name"], namespace))
        except Exception as exc:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            message = exception_message(exc)
            if "stale element reference" in message.lower():
                message = "Dashboard 页面持续刷新，自动登录重试超时"
            elif "decompressing data" in message.lower():
                message = "浏览器组件读取失败，程序文件可能在运行中被替换。请退出并重新打开应用，再重试 Dashboard。"
            self.emit("deploy_log", f"Dashboard 自动登录失败：{message}")
            self.emit("dashboard_fallback", (item, f"浏览器自动化失败：{message}"))
        finally:
            self.emit("loading_stop", None)

    def _copy_dashboard_token(self, item):
        token = unprotect_secret(item.get("token", ""))
        clipboard = QApplication.clipboard()
        clipboard.setText(token)


class TaskCenterPanel(GlassBackground):
    """Compact task switcher shown in the main window's bottom dock."""

    expanded_changed = Signal(bool)

    _STATE_COLORS = {
        "idle": COLORS["text_muted"],
        "running": COLORS["accent_hover"],
        "succeeded": COLORS["success"],
        "failed": COLORS["danger"],
        "cancelled": COLORS["warning"],
    }

    def __init__(self, manager):
        super().__init__(manager)
        self.manager = manager
        self._items = {}
        self._actions = {}
        self._states = {}
        self._expanded = False
        self.setObjectName("TaskCenterPanel")
        self.setStyleSheet(f"""
            QWidget#TaskCenterPanel {{
                background: transparent;
                border-top: 1px solid {COLORS['border']};
            }}
            QTreeWidget {{
                background: {COLORS['log_bg']};
                alternate-background-color: {COLORS['input_bg']};
                border: 1px solid {COLORS['border']};
                border-radius: 4px;
            }}
            QHeaderView::section {{
                background: {COLORS['card']};
                color: {COLORS['text_secondary']};
                border: none;
                border-right: 1px solid {COLORS['border']};
                border-bottom: 1px solid {COLORS['border']};
                padding: 6px 8px;
                font-weight: 600;
            }}
            QWidget#TaskCenterPanel QToolButton {{
                background: {COLORS['input_bg']};
                border: 1px solid {COLORS['input_border']};
                border-radius: 4px;
                padding: 3px;
            }}
            QWidget#TaskCenterPanel QToolButton:hover {{
                background: {COLORS['accent_light']};
                border-color: {COLORS['border_focus']};
            }}
            QWidget#TaskCenterPanel QToolButton:disabled {{
                background: {COLORS['sidebar_bg']};
                border-color: {COLORS['border']};
            }}
            QWidget#TaskCenterActions QPushButton {{
                min-width: 50px;
                max-width: 50px;
                min-height: 20px;
                max-height: 20px;
                padding: 4px 8px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setFixedHeight(46)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 6, 12, 6)
        header_layout.setSpacing(10)

        title = QLabel("任务中心")
        title.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 14px; font-weight: 600;"
        )
        header_layout.addWidget(title)

        self.counter_label = QLabel("运行 0  /  失败 0  /  共 0")
        self.counter_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 12px;"
        )
        header_layout.addWidget(self.counter_label)
        self.copy_feedback = QLabel("")
        self.copy_feedback.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 12px;"
        )
        header_layout.addWidget(self.copy_feedback)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.timeout.connect(self.copy_feedback.clear)
        header_layout.addStretch()

        self.toggle_button = QToolButton()
        self.toggle_button.setObjectName("GhostBtn")
        self.toggle_button.setFixedSize(36, 34)
        self.toggle_button.setIcon(ui_icon("chevron-up", COLORS["text_primary"]))
        self.toggle_button.setIconSize(QSize(22, 22))
        self.toggle_button.setCursor(Qt.PointingHandCursor)
        self.toggle_button.setToolTip("展开任务中心")
        self.toggle_button.setAccessibleName("展开或收起任务中心")
        self.toggle_button.clicked.connect(self.toggle_expanded)
        header_layout.addWidget(self.toggle_button)
        layout.addWidget(header)

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(12, 0, 12, 10)
        body_layout.setSpacing(0)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(8)
        self.tree.setHeaderLabels(
            ("任务", "包号 / Job", "目标", "当前阶段", "被替换包号", "进度", "用时", "操作")
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        header_view = self.tree.header()
        header_view.setStretchLastSection(False)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.Stretch)
        header_view.setSectionResizeMode(4, QHeaderView.Stretch)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(7, QHeaderView.Fixed)
        self.tree.setColumnWidth(7, 184)
        self.tree.itemClicked.connect(self._activate_item)
        self.tree.itemDoubleClicked.connect(self._activate_item)
        copy_action = QAction("复制包号", self.tree)
        copy_action.setShortcut(QKeySequence.Copy)
        copy_action.setShortcutContext(Qt.WidgetShortcut)
        copy_action.triggered.connect(
            lambda: self._copy_package(self.tree.currentItem(), self.tree.currentColumn())
        )
        self.tree.addAction(copy_action)
        body_layout.addWidget(self.tree)
        layout.addWidget(self.body, 1)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self.refresh_elapsed)
        self._elapsed_timer.start()
        self.set_expanded(False)

    def add_task(self, task):
        item = QTreeWidgetItem(self.tree)
        item.setTextAlignment(5, Qt.AlignCenter)
        item.setTextAlignment(6, Qt.AlignCenter)
        item.setSizeHint(7, QSize(160, 50))
        self._items[task] = item

        actions = QWidget()
        actions.setObjectName("TaskCenterActions")
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(6, 2, 6, 2)
        actions_layout.setSpacing(8)
        actions_layout.setAlignment(Qt.AlignCenter)
        cancel_button = QPushButton("停止")
        cancel_button.setObjectName("DangerGhostBtn")
        cancel_button.setToolTip("停止当前正在运行的 Jenkins 任务")
        cancel_button.setAccessibleName(f"取消任务 {task.task_number}")
        cancel_button.setFixedSize(68, 30)
        cancel_button.clicked.connect(lambda: self.manager.toggle_task_execution(task))
        actions_layout.addWidget(cancel_button)
        remove_button = QPushButton("移除")
        remove_button.setObjectName("GhostBtn")
        remove_button.setToolTip("从任务中心移除已结束或未开始的任务")
        remove_button.setAccessibleName(f"移除任务 {task.task_number}")
        remove_button.setFixedSize(68, 30)
        remove_button.clicked.connect(lambda: self.manager.remove_task(task))
        actions_layout.addWidget(remove_button)
        self.tree.setItemWidget(item, 7, actions)
        self._actions[task] = (cancel_button, remove_button)
        apply_ui_icons(actions)
        self.update_task(task)
        self.select_task(task)
        if len(self._items) > 1:
            self.set_expanded(True)

    def remove_task(self, task):
        item = self._items.pop(task, None)
        self._actions.pop(task, None)
        self._states.pop(task, None)
        if item is not None:
            index = self.tree.indexOfTopLevelItem(item)
            if index >= 0:
                self.tree.takeTopLevelItem(index)
        self._update_counter()

    def update_task(self, task):
        item = self._items.get(task)
        if item is None:
            return
        summary = task.task_summary()
        previous_state = self._states.get(task)
        self._states[task] = summary["state"]
        item.setText(0, f"任务 {summary['number']}")
        descriptor = summary["package"] or summary["title"] or "等待配置"
        item.setText(1, descriptor)
        item.setData(1, Qt.UserRole, summary["package"])
        item.setToolTip(
            1, f"{descriptor}\n点击复制包号，或选中任务后按 {QKeySequence(QKeySequence.Copy).toString(QKeySequence.NativeText)}"
            if summary["package"] else descriptor
        )
        item.setText(2, summary["target"] or "-")
        item.setToolTip(2, summary["target"] or "-")
        item.setText(3, summary["phase"] or "未开始")
        item.setToolTip(3, summary["phase"] or "未开始")
        item.setText(4, summary["replaced_text"])
        item.setData(4, Qt.UserRole, "\n".join(summary["replaced_packages"]))
        item.setToolTip(4, summary["replaced_tooltip"] + (
            "\n点击复制被替换包号" if summary["replaced_packages"] else ""
        ))
        item.setText(
            5,
            f"{summary['progress']}%" if summary["state"] != "idle" else "-",
        )
        item.setText(6, self._format_elapsed(summary["elapsed"]))
        color = QColor(self._STATE_COLORS.get(summary["state"], COLORS["text_muted"]))
        item.setForeground(0, QBrush(color))
        item.setForeground(3, QBrush(color))
        cancel_button, remove_button = self._actions[task]
        restartable = summary["restartable"]
        text = "继续" if restartable else ("停止中" if summary["is_cancelling"] else "停止")
        button_style = "SuccessBtn" if restartable else "DangerGhostBtn"
        if cancel_button.text() != text or cancel_button.objectName() != button_style:
            cancel_button.setText(text)
            cancel_button.setObjectName(button_style)
            cancel_button.setIcon(
                QIcon() if summary["is_cancelling"] else
                ui_icon("play" if restartable else "square", COLORS["text_primary"])
            )
            cancel_button.style().unpolish(cancel_button)
            cancel_button.style().polish(cancel_button)
        cancel_button.setToolTip(
            "使用上次任务的 Job、分支、参数和部署配置重新打包"
            if restartable else "等待任务确认停止" if summary["is_cancelling"]
            else "停止当前正在运行的 Jenkins 任务"
        )
        cancel_button.setAccessibleName(f"{'继续' if restartable else '停止'}任务 {task.task_number}")
        cancel_button.setEnabled(summary["can_resume"] if restartable else summary["can_cancel"])
        remove_button.setEnabled(summary["can_remove"] and len(self._items) > 1)
        self._update_counter()
        if summary["state"] == "failed" and previous_state != "failed":
            self.set_expanded(True)

    def refresh_elapsed(self):
        for task in tuple(self._items):
            self.update_task(task)

    def select_task(self, task):
        item = self._items.get(task)
        if item is None or self.tree.currentItem() is item:
            return
        self.tree.blockSignals(True)
        self.tree.setCurrentItem(item)
        self.tree.blockSignals(False)

    def _copy_package(self, item, column=1):
        column = 4 if column == 4 else 1
        package = item.data(column, Qt.UserRole) if item is not None else ""
        if not package:
            return
        QApplication.clipboard().setText(package)
        self.copy_feedback.setText("已复制被替换包号" if column == 4 else "已复制包号")
        self._copy_feedback_timer.start(2500)

    def _activate_item(self, item, column=0):
        for task, candidate in self._items.items():
            if candidate is item:
                self.manager.activate_task(task)
                if column in (1, 4):
                    self._copy_package(item, column)
                return

    def _update_counter(self):
        summaries = [task.task_summary() for task in self._items]
        running = sum(summary["state"] == "running" for summary in summaries)
        failed = sum(summary["state"] == "failed" for summary in summaries)
        self.counter_label.setText(
            f"运行 {running}  /  失败 {failed}  /  共 {len(summaries)}"
        )
        for task, (_cancel, remove) in self._actions.items():
            remove.setEnabled(task.task_summary()["can_remove"] and len(self._items) > 1)

    @staticmethod
    def _format_elapsed(seconds):
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def toggle_expanded(self):
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded):
        self._expanded = bool(expanded)
        self.body.setVisible(self._expanded)
        self.toggle_button.setIcon(ui_icon(
            "chevron-down" if self._expanded else "chevron-up", COLORS["text_primary"]
        ))
        self.toggle_button.setToolTip("收起任务中心" if self._expanded else "展开任务中心")
        self.expanded_changed.emit(self._expanded)

    @property
    def expanded(self):
        return self._expanded


class SingleApplicationInstance(QObject):
    """Share one application per configuration directory, including EXE copies."""

    activation_requested = Signal()

    def __init__(self, config_dir, parent=None):
        super().__init__(parent)
        self.config_dir = Path(config_dir).resolve()
        identity = os.path.normcase(str(self.config_dir)).encode("utf-8")
        self.server_name = "DeployConsole-" + hashlib.sha256(identity).hexdigest()[:32]
        self.lock = QLockFile(str(self.config_dir / "application.lock"))
        # Long-running deployments must not lose the lock just because it is old.
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.newConnection.connect(self._activate_existing_window)
        self._owns_lock = False

    def acquire(self, timeout_ms=5000):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if self.lock.tryLock(0):
                self._owns_lock = True
                # Only the lock owner may remove a socket left by a crashed process.
                QLocalServer.removeServer(self.server_name)
                if not self.server.listen(self.server_name):
                    error = self.server.errorString()
                    self.close()
                    raise RuntimeError(f"无法创建应用启动通道：{error}")
                return True
            if self.lock.error() != QLockFile.LockFailedError:
                raise RuntimeError("无法锁定应用配置目录，请检查目录权限。")

            socket = QLocalSocket()
            socket.connectToServer(self.server_name)
            connected = socket.waitForConnected(100)
            socket.abort()
            if connected:
                return False
            if time.monotonic() >= deadline:
                raise RuntimeError("应用已在运行或正在启动，请稍后从系统托盘打开。")
            time.sleep(0.05)

    def _activate_existing_window(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.close()
            socket.deleteLater()
            self.activation_requested.emit()

    def close(self):
        if self._owns_lock:
            self.server.close()
            self.lock.unlock()
            self._owns_lock = False


class ApplicationWindowManager(QMainWindow):
    """Single main window that owns task workspaces and shared coordinators."""

    def __init__(self, app, store=None):
        super().__init__()
        self.app = app
        self.store = store or ConfigStore()
        self.target_coordinator = DeploymentTargetCoordinator()
        self.batch_coordinator = BatchDeploymentCoordinator()
        self._windows = []
        self._next_task_number = 1
        self._closing = False
        self._hidden_to_tray = False
        self._restore_maximized = False

        self.setWindowTitle("Deploy Console 并行任务中心版")
        self.setWindowIcon(QIcon(str(resource_path("assets/deploy-console-parallel.png"))))
        self.resize(1440, 960)
        self.setMinimumSize(1080, 760)

        self.task_stack = QStackedWidget()
        self.task_stack.setObjectName("TaskWorkspaceStack")
        self.setCentralWidget(self.task_stack)

        self.task_dock = QDockWidget(self)
        self.task_dock.setObjectName("TaskCenterDock")
        self.task_dock.setAllowedAreas(Qt.BottomDockWidgetArea)
        self.task_dock.setFeatures(QDockWidget.NoDockWidgetFeatures)
        hidden_title = QWidget()
        hidden_title.setFixedHeight(0)
        self.task_dock.setTitleBarWidget(hidden_title)
        self.task_center = TaskCenterPanel(self)
        self.task_center.expanded_changed.connect(self._resize_task_center)
        self.task_dock.setWidget(self.task_center)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.task_dock)
        self._resize_task_center(False)
        self._build_system_tray()

    @property
    def windows(self):
        return tuple(self._windows)

    @property
    def current_task(self):
        widget = self.task_stack.currentWidget()
        return widget if widget in self._windows else None

    def create_window(self, source_window=None):
        """Create an embedded task workspace; kept for call-site compatibility."""
        task_number = self._next_task_number
        self._next_task_number += 1
        window = DeployTool(
            store=self.store,
            window_manager=self,
            task_number=task_number,
            embedded=True,
        )
        self._windows.append(window)
        self.task_stack.addWidget(window)
        window.destroyed.connect(
            lambda *_args, task=window: self._forget_window(task)
        )
        self.task_center.add_task(window)
        self.activate_task(window)
        if not self.isVisible():
            self.show()
        return window

    def request_new_task(self):
        source = self.current_task
        if source is None:
            self.create_window()
        else:
            source.request_new_task_window()

    def activate_task(self, task):
        if task not in self._windows:
            return
        self.task_stack.setCurrentWidget(task)
        self.task_center.select_task(task)

    def update_task(self, task):
        self.task_center.update_task(task)

    def cancel_task(self, task):
        if task not in self._windows:
            return
        self.activate_task(task)
        task.cancel_pipeline()

    def toggle_task_execution(self, task):
        if task not in self._windows:
            return
        if task.task_summary()["restartable"]:
            self.activate_task(task)
            task.resume_build()
        else:
            self.cancel_task(task)

    def remove_task(self, task):
        if task not in self._windows:
            return
        if len(self._windows) == 1:
            task.message_overlay.show_message(
                "保留当前任务", "任务中心至少保留一个工作区。"
            )
            return
        if task.has_active_work():
            self.activate_task(task)
            task.message_overlay.show_message(
                "任务仍在运行",
                "请先取消当前任务，并等待打包、部署或数据加载结束后再移除。",
                kind="error",
            )
            return
        index = self._windows.index(task)
        fallback = self._windows[index - 1] if index else self._windows[1]
        task.shutdown_resources()
        self.target_coordinator.release_all(task)
        self.batch_coordinator.release_all(task)
        self._windows.remove(task)
        self.task_center.remove_task(task)
        self.task_stack.removeWidget(task)
        task.deleteLater()
        self.activate_task(fallback)

    def _resize_task_center(self, expanded):
        if expanded:
            self.task_dock.setMinimumHeight(210)
            self.task_dock.setMaximumHeight(360)
            QTimer.singleShot(
                0,
                lambda: self.resizeDocks(
                    [self.task_dock], [270], Qt.Vertical
                ),
            )
        else:
            self.task_dock.setMinimumHeight(46)
            self.task_dock.setMaximumHeight(46)

    def _forget_window(self, window):
        try:
            self._windows.remove(window)
        except ValueError:
            pass
        self.task_center.remove_task(window)
        self.target_coordinator.release_all(window)
        self.batch_coordinator.release_all(window)

    def _build_system_tray(self):
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_icon.setToolTip("Deploy Console 并行任务中心版")

        tray_menu = QMenu(self)
        show_action = QAction("打开主窗口", self)
        show_action.triggered.connect(self.restore_from_tray)
        tray_menu.addAction(show_action)
        tray_menu.setDefaultAction(show_action)
        tray_menu.addSeparator()
        quit_action = QAction("退出应用", self)
        quit_action.triggered.connect(self.request_exit)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_icon_activated)
        self.app.aboutToQuit.connect(self.tray_icon.hide)
        if self.tray_available:
            self.tray_icon.show()

    def hide_to_tray(self):
        """Hide the window while keeping builds and deployments running."""
        if self._closing:
            return
        self._restore_maximized = self.isMaximized()
        self._hidden_to_tray = self.tray_available
        if self.tray_available:
            self.hide()
        else:
            self.showMinimized()

    def restore_from_tray(self):
        """Restore and focus the main window from the notification area."""
        if self._closing:
            return
        self._hidden_to_tray = False
        if self._restore_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_icon_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_from_tray()

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange and not self.isMinimized():
            self._hidden_to_tray = False
        super().changeEvent(event)

    def request_exit(self):
        """Exit only when all task work has finished.

        The native title-bar X is reserved for minimizing.  This explicit
        action keeps a recoverable exit path for users who really want to
        terminate the process.
        """
        active = [task for task in self._windows if task.has_active_work()]
        if active:
            task = active[0]
            self.restore_from_tray()
            self.activate_task(task)
            self.task_center.set_expanded(True)
            task.message_overlay.show_message(
                "仍有任务运行",
                f"任务 {task.task_number} 仍在打包、部署或加载数据。\n"
                "请先取消并等待状态结束后再退出软件。",
                kind="error",
            )
            return False
        self._closing = True
        self.tray_icon.hide()
        self.close()
        self.app.quit()
        return True

    def closeEvent(self, event):
        if not self._closing:
            event.ignore()
            self.hide_to_tray()
            return
        for task in tuple(self._windows):
            task.shutdown_resources()
        event.accept()


def run_application():
    app = QApplication(sys.argv)
    app.setApplicationName("Deploy Console 并行任务中心版")
    instance = SingleApplicationInstance(CONFIG_DIR, app)
    try:
        try:
            if not instance.acquire():
                return 0
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(None, "无法启动 Deploy Console", str(exc))
            return 1
        configure_diagnostics()
        app.setQuitOnLastWindowClosed(False)
        app.setWindowIcon(QIcon(str(resource_path("assets/deploy-console-parallel.png"))))
        app.setStyleSheet(GLOBAL_QSS)
        app.setFont(QFont(FONT_FAMILY, 10))
        manager = ApplicationWindowManager(app)
        instance.activation_requested.connect(manager.restore_from_tray)
        first_task = manager.create_window()
        log_path, log_error = diagnostics_status()
        if log_error:
            first_task.emit("build_log", f"耗时日志不可用（{log_error}），请检查 log 目录权限。")
        elif log_path:
            first_task.emit("build_log", f"耗时日志：{log_path}")
        return app.exec()
    finally:
        shutdown_diagnostics()
        instance.close()


if __name__ == "__main__":
    if "--self-test-diagnostics" in sys.argv:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="deploy-console-diagnostics-") as test_dir:
            sink = configure_diagnostics(test_dir)
            if sink is None:
                raise RuntimeError("diagnostics initialization failed")
            trace = new_task_trace(0, kind="self-test", package="synthetic:1")
            with trace.stage("synthetic_stage"):
                trace.event("synthetic_event", password="FAKE_SECRET")
            trace.finish("succeeded")
            shutdown_diagnostics()
            content = sink.path.read_text(encoding="utf-8")
            records = [json.loads(line) for line in content.splitlines()]
            if "FAKE_SECRET" in content or not any(r["event"] == "task_end" for r in records):
                raise RuntimeError("diagnostics self-test failed")
        print("deployment diagnostics: ok")
    elif "--self-test-credentials" in sys.argv:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="deploy-console-credentials-") as test_dir:
            CONFIG_DIR = Path(test_dir)
            test_secret = protect_secret("credential-self-test")
            if unprotect_secret(test_secret) != "credential-self-test":
                raise RuntimeError("credential encryption self-test failed")
        print("credential encryption: ok")
    elif "--self-test-crypto" in sys.argv:
        test_key = Fernet.generate_key()
        test_value = Fernet(test_key).decrypt(Fernet(test_key).encrypt(b"ok"))
        if test_value != b"ok":
            raise RuntimeError("cryptography.fernet self-test failed")
        print("cryptography.fernet: ok")
    elif "--self-test-selenium" in sys.argv:
        for module in ("selenium.webdriver.common.by", "selenium.webdriver.support.ui", "selenium.common.exceptions"):
            importlib.import_module(module)
        backend = "edge" if sys.platform == "win32" else "chrome"
        print(f"selenium backend: {backend}")
    else:
        sys.exit(run_application())
