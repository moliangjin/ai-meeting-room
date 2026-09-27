"""Phase 2 application service: UI-facing orchestration over MeetingCore."""

from __future__ import annotations

import threading
import time
import json
import hashlib
import sqlite3
import marshal
import os
import platform
import re
import secrets
import sys
import urllib.request
import logging
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..brain.adapter import BrainAdapter
from ..brain.api import API_BRAIN_POC_TASK_ID, ApiBrainBridge
from ..brain.chatgpt_web import BrainRequest, ChatGPTWebBrainBridge, PromptComposer, BrowserController
from ..brain.playwright_attached_brain import PlaywrightAttachedBrainError, PlaywrightAttachedBrainHost, PlaywrightAttachedBrainState
from ..brain.runtime_registry import FormalBrainRuntimeRegistry, FormalBrainRuntimeService
from ..brain.dedicated_cdp import DEDICATED_CHROME_USER_DATA_DIR, DedicatedChromeEndpointResolver
from ..brain.config import BrainProviderConfigError, BrainProviderConfigValidator
from ..brain.desktop import DESKTOP_BRAIN_POC_PROMPT, validate_desktop_brain_poc
from ..brain.extension_transport import BoundExtensionTransport
from ..brain.inbox import BrainInbox, ManualBrainBridge
from ..brain.local_bridge import LocalBrainBridge
from ..brain.manual_gpt import (
    BRAIN_DECISION_PACKET_SCHEMA,
    FORMAL_DECISION_TYPES,
    MANUAL_GPT_TRANSPORT,
    WAITING_FOR_HUMAN_HANDOFF,
    ManualGPTBrainTransport,
    ManualGPTHandoffError,
)
from ..brain.protocol import BrainDecisionInvalid
from ..brain.provider import BrainProviderConfig, OpenAICompatibleProvider
from ..brain.provider import BrainProviderError
from ..brain.secrets import SecretStoreError, default_brain_secret_store
from ..core.recovery import RecoveryManager
from ..core.errors import MeetingLifecycleConflict
from ..core.safety import DispatchBlockedError, SafetyEngine
from ..core.service import MeetingCore
from ..events.bus import EventBus
from ..integrations.cao.adapter import CaoAgentAdapter
from ..integrations.cao.model_config import CodexModelConfigurationError, resolve_codex_model
from ..integrations.cao.preflight import CaoPreflightResult, CaoRuntimePreflight
from ..runtime.cao_service_manager import CaoServiceError, CaoServiceManager
from phase0_poc.cao_bridge import CaoRuntimeAgent, is_unsettled_terminal_output
from ..models import AgentHealth, AgentRecord, AgentRole, AgentStatus, DomainEvent, MeetingStatus, TaskStatus, model_dict, utc_now
from ..persistence.sqlite_store import ManualGPTHandoffStoreError, SQLiteStore
from .meeting_summary import MeetingSummaryExporter
from ..providers.catalog import ProviderAvailability, ProviderCatalog
from ..runtime.identity import RuntimeIdentity
from ..runtime.tools import RuntimeToolResolver
from ..workspace.manager import WorkspaceManager, WorkspaceError
from .. import __version__
from .operations import BackupValidationError, ProductDataPaths, V1Operations, configure_product_logging


FORMAL_RUNTIME_OWNER = "PRODUCT_SHELL"

_PRODUCT_ERROR_MESSAGES_ZH = {
    "MEETING_NAME_REQUIRED": "请填写会议名称。",
    "WORKSPACE_DIRECTORY_INVALID": "工作区路径无效或文件夹不存在，请选择本机已有的项目目录。",
    "DUPLICATE_DECISION_REJECTED": "该 Brain 决策已处理，不能重复导入或应用。",
    "INVALID_BRAIN_DECISION": "Brain 决策格式或状态无效；会议和任务状态未被修改。",
    "STALE_DECISION_REJECTED": "该 Brain 决策已过期，会议或任务状态未被修改。",
    "CROSS_MEETING_DECISION_REJECTED": "该 Brain 决策不属于当前会议。",
    "BRAIN_PACKET_NOT_COPIED": "请先复制当前 Brain Packet，再导入决策。",
    "MEETING_NOT_STARTABLE": "会议当前状态不允许开始。",
    "MEETING_NOT_PAUSED": "会议当前未处于暂停状态，不能恢复。",
    "MEETING_TERMINAL": "会议已结束，不能再创建任务。",
}


def localized_product_error_message(code: str, fallback: str) -> str:
    """Return a Chinese user-facing message while retaining the stable error code."""
    return _PRODUCT_ERROR_MESSAGES_ZH.get(code, fallback)


class ProductError(RuntimeError):
    def __init__(self, message: str, *, code: str = "PRODUCT_ERROR", stage: str = "UNKNOWN", diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.diagnostics = diagnostics or {}

    def as_error(self) -> dict[str, Any]:
        safe = {
            "code": self.code,
            "stage": self.stage,
            "message": localized_product_error_message(self.code, str(self)),
        }
        safe.update({key: value for key, value in self.diagnostics.items() if key in {
            "formalRuntimeType", "launchSessionId", "registryInstanceId", "hostInstanceId", "processPid", "parentPid", "runtimeOwner", "boundPageId", "browserConnected",
            "pageAlive", "hostname", "authState", "composerReady", "precheckResult", "failureCode", "invariantViolation", "invariantDetails",
            "ownerThreadId", "threadMatrix", "threadAffinityError", "expectedThreadId", "actualThreadId", "operationName",
            "runtimeLossEvidence", "lifecycleBefore", "lifecycleAfter", "connectIdentity", "probeIdentity",
            "sameRegistry", "sameHost", "sameBoundPage", "sameOwnerThread", "atomicConnect",
            "pocState", "pocFailure", "pocFailureCode", "pocFailureStage", "pocFailureName", "pocFailureMessage", "pocFailureStackId",
            "requestId", "httpStatus",
        }})
        return safe


class ProviderBlockedError(ProductError):
    pass


@dataclass
class BrainParticipantBinding:
    """One Meeting's joined Brain identity and the runtime it must recover through."""

    meeting_id: str
    participant_id: str
    provider: str
    runtime_identity: dict[str, Any]
    bridge: BrainAdapter
    controller: Any | None
    health_state: str
    joined_at: str


def _connect_result_dto(status: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    """Return the only connection shape intended for a desktop boundary."""
    selected_page = None
    if status.get("selectedTargetHostname") and status.get("selectedTargetPathname"):
        selected_page = {
            "hostname": str(status["selectedTargetHostname"]),
            "pathname": str(status["selectedTargetPathname"]),
        }
    return {
        "attemptId": str(attempt_id),
        "brainConnectionId": str(status.get("brainConnectionId") or ""),
        "registryInstanceId": str(status.get("registryInstanceId") or "UNKNOWN"),
        "hostInstanceId": str(status.get("hostInstanceId") or "UNKNOWN"),
        "processPid": int(status.get("processPid") or 0),
        "parentPid": int(status.get("parentPid") or 0),
        "runtimeOwner": str(status.get("runtimeOwner") or FORMAL_RUNTIME_OWNER),
        "boundPageId": status.get("boundPageId"),
        "connectionMode": str(status.get("formalBrowserConnection") or "UNKNOWN"),
        "channel": str(status.get("channel") or ""),
        "browserConnected": bool(status.get("browserConnected")),
        "contextCount": int(status.get("contextCount") or 0),
        "pageCount": int(status.get("pageTargetCount") or 0),
        "chatgptPageCount": int(status.get("chatgptTargetCount") or 0),
        "selectedPage": selected_page,
        "composerDetected": bool(status.get("composerFound")),
        "state": str(status.get("state") or "UNKNOWN"),
    }


class Phase2Application:
    """Keeps UI concerns outside the Core and injects provider adapters at the edge."""

    def __init__(self, store: SQLiteStore, *, cao_base_url: str = "http://127.0.0.1:9889", adapter_factory: Callable[..., Any] | None = None, heartbeat_timeout_seconds: float = 60.0, monitor_poll_interval_seconds: float = 1.0, cao_service_manager: CaoServiceManager | None = None) -> None:
        if heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be positive")
        if monitor_poll_interval_seconds <= 0:
            raise ValueError("monitor_poll_interval_seconds must be positive")
        self.store = store
        self.product_paths = ProductDataPaths.from_database(self.store.path)
        self.product_paths.ensure()
        self.operations = V1Operations(self.product_paths, app_version=__version__)
        self.logger = configure_product_logging(self.product_paths)
        self.cao_base_url = cao_base_url.rstrip("/")
        self._acceptance_agent_session_prefix: str | None = None
        configured_session_prefix = os.environ.get("AI_MEETING_ROOM_ACCEPTANCE_SESSION_PREFIX")
        if configured_session_prefix is not None and os.environ.get("AI_MEETING_ROOM_ACCEPTANCE_MODE") == "1":
            if re.fullmatch(r"aimr-v[0-9]+-r[0-9]+-[a-f0-9]{10}-", configured_session_prefix) is None:
                raise ValueError("AI_MEETING_ROOM_ACCEPTANCE_SESSION_PREFIX_INVALID")
            self._acceptance_agent_session_prefix = configured_session_prefix
        self.runtime_tool_resolver = RuntimeToolResolver()
        self.cao_service_manager = cao_service_manager or CaoServiceManager(
            self.cao_base_url, self.product_paths, tool_resolver=self.runtime_tool_resolver
        )
        self.heartbeat_timeout_seconds = float(heartbeat_timeout_seconds)
        self.monitor_poll_interval_seconds = float(monitor_poll_interval_seconds)
        self.catalog = ProviderCatalog()
        self.brain_secret_store = default_brain_secret_store()
        self.local_brain_bridge = LocalBrainBridge()
        self.manual_gpt_transport = ManualGPTBrainTransport()
        # Formal GPT Web Brain runtime: attach to the already-running,
        # user-owned dedicated Chrome over Playwright CDP. This host never
        # launches Chrome or uses the historical MCP route.
        formal_host = PlaywrightAttachedBrainHost(
            on_failure=None,
            dedicated_user_data_dir=DEDICATED_CHROME_USER_DATA_DIR,
            endpoint_resolver=DedicatedChromeEndpointResolver(DEDICATED_CHROME_USER_DATA_DIR).resolve,
            attach_mode="EXACT_DEDICATED_CDP",
            endpoint_source="DEVTOOLS_ACTIVE_PORT",
        )
        formal_host.on_failure = lambda state, reason: self._on_real_chrome_brain_failure(
            state, reason, source_host=formal_host
        )
        self.formal_brain_registry = FormalBrainRuntimeRegistry(
            formal_host,
            runtime_owner=FORMAL_RUNTIME_OWNER,
            bridge_factory=lambda host: ChatGPTWebBrainBridge(host, response_timeout_seconds=180.0),
        )
        self.formal_brain_service = FormalBrainRuntimeService(self.formal_brain_registry)
        self._runtime_identity = RuntimeIdentity.create(Path(__file__).resolve().parents[2])
        # Compatibility alias; all formal entry points below obtain this same
        # object through formal_brain_registry.
        self.real_chrome_brain = self.formal_brain_registry.get()
        self._desktop_poc_active = False
        self._adapter_factory = adapter_factory or self._create_cao_adapter
        self._uses_real_cao = adapter_factory is None
        self._cores: dict[str, MeetingCore] = {}
        self._brains: dict[str, BrainAdapter] = {}
        self._brain_participants: dict[str, BrainParticipantBinding] = {}
        self._manual_gpt_restore_errors: dict[str, str] = {}
        self._brain_inboxes: dict[str, BrainInbox] = {}
        self._monitor_threads: dict[str, threading.Thread] = {}
        self._monitor_stop: dict[str, threading.Event] = {}
        self._task_threads: dict[str, threading.Thread] = {}
        self._task_stop: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._join_locks: dict[tuple[str, str], threading.Lock] = {}
        self._cao_startup_recovery: dict[str, Any] = {
            "status": "NOT_RUN", "action": "NOT_RUN", "reasonCode": None,
            "caoServerPath": None, "httpHealthStatus": "UNKNOWN", "requestId": None,
        }
        self._safety_state_hydrated = False
        self._close_lock = threading.Lock()
        self._closed = False

    def runtime_preflight(self) -> CaoPreflightResult:
        """Return the current real CAO/Codex dependency status."""
        return CaoRuntimePreflight(
            self.cao_base_url,
            resolver=self.runtime_tool_resolver,
        ).check()

    def _codex_authentication_status(self, codex_path: str | None) -> tuple[str, str]:
        """Check only the selected Codex executable's login state; never return its output."""
        if not codex_path:
            return "UNKNOWN", "CODEX_AUTH_UNKNOWN"
        try:
            probe = subprocess.run(
                [codex_path, "login", "status"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
                env=self.runtime_tool_resolver.controlled_environment({"NO_COLOR": "1"}),
            )
        except (OSError, subprocess.SubprocessError):
            return "UNKNOWN", "CODEX_AUTH_UNKNOWN"
        response = f"{probe.stdout}\n{probe.stderr}".lower()
        if probe.returncode == 0 and "logged in" in response:
            return "READY", "CODEX_AUTHENTICATED"
        if "not logged in" in response or "login required" in response:
            return "BLOCKED", "CODEX_AUTH_REQUIRED"
        return "UNKNOWN", "CODEX_AUTH_UNKNOWN"

    @staticmethod
    def _codex_readiness_message(code: str) -> str:
        return {
            "CAO_UNAVAILABLE": "Codex 加入会议失败：CAO 服务不可用。",
            "CAO_START_FAILED": "Codex 暂不可用：CAO 服务启动失败。",
            "CAO_START_TIMEOUT": "Codex 暂不可用：CAO 服务未能及时就绪。",
            "CAO_PORT_CONFLICT": "Codex 暂不可用：CAO 端口被其他服务占用，产品未终止该进程。",
            "CAO_SERVER_EXECUTABLE_UNAVAILABLE": "Codex 暂不可用：未找到可运行的 CAO 服务程序。",
            "CAO_TERMINAL_BACKEND_MISMATCH": "Codex 暂不可用：CAO 未使用受支持的 tmux 后端。",
            "CAO_TERMINAL_BACKEND_UNKNOWN": "Codex 暂不可用：无法确认 CAO 的 tmux 后端。",
            "CAO_LOCAL_ONLY_REQUIRED": "Codex 暂不可用：CAO 地址不符合本机安全策略。",
            "CAO_STARTUP_RECOVERY_FAILED": "Codex 暂不可用：CAO 自动恢复失败。",
            "CAO_EXECUTABLE_UNAVAILABLE": "Codex 加入会议失败：未找到 CAO 服务程序。",
            "TMUX_UNAVAILABLE": "Codex 加入会议失败：tmux 当前不可用。",
            "CODEX_CLI_UNAVAILABLE": "Codex 加入会议失败：Codex 命令行工具不可用。",
            "CODEX_AUTH_REQUIRED": "Codex 加入会议失败：Codex 登录状态无效。",
            "CODEX_AUTH_UNKNOWN": "Codex 加入会议失败：无法确认 Codex 登录状态。",
            "CODEX_MODEL_UNSUPPORTED": "Codex 加入会议失败：当前模型配置不受支持。",
            "WORKSPACE_UNAVAILABLE": "Codex 加入会议失败：会议工作区不可用。",
            "MEETING_NOT_JOINABLE": "Codex 加入会议失败：会议状态不允许加入智能体。",
            "CODEX_RUNTIME_SESSION_CREATE_FAILED": "Codex 加入会议失败：无法创建运行时会话。",
            "CODEX_RUNTIME_UNHEALTHY": "Codex 加入会议失败：运行时会话未通过健康检查。",
            "CODEX_PREVIOUS_RUNTIME_NOT_STOPPED": "Codex 加入会议失败：无法安全停止先前的运行时会话。",
            "GPT_BRAIN_UNAVAILABLE": "Codex 加入会议失败：GPT 主脑尚未就绪。",
            "CODEX_READINESS_UNKNOWN": "Codex 加入会议失败：无法确认运行环境状态。",
        }.get(code, "Codex 加入会议失败：当前暂不可用。")

    def _codex_runtime_readiness(self) -> tuple[str, str | None, str | None]:
        """Return (environment state, blocking code, safe reason) from live prerequisites."""
        try:
            preflight = self.runtime_preflight()
        except Exception:
            return "UNKNOWN", "CODEX_READINESS_UNKNOWN", None
        if not preflight.cao_server_healthy:
            recovery = self._cao_startup_recovery
            recovery_code = recovery.get("reasonCode") if recovery.get("status") == "BLOCKED" else None
            code = str(recovery_code or "CAO_UNAVAILABLE")
            return "BLOCKED", code, self._codex_readiness_message(code)
        tools = preflight.tools
        for name, code in (
            ("cao", "CAO_EXECUTABLE_UNAVAILABLE"),
            ("tmux", "TMUX_UNAVAILABLE"),
            ("codex", "CODEX_CLI_UNAVAILABLE"),
        ):
            tool = tools.get(name)
            if tool is None or not tool.usable:
                return "BLOCKED", code, self._codex_readiness_message(code)
        codex = tools.get("codex")
        auth_state, auth_code = self._codex_authentication_status(getattr(codex, "path", None))
        if auth_state != "READY":
            return "BLOCKED" if auth_state == "BLOCKED" else "UNKNOWN", auth_code, self._codex_readiness_message(auth_code)
        return "READY", None, None

    def provider_join_readiness(self, meeting_id: str | None = None) -> list[dict[str, Any]]:
        """Readiness shown to the UI; a static provider catalog is not join readiness."""
        providers: list[dict[str, Any]] = []
        core = self._core(meeting_id) if meeting_id else None
        for info in self.catalog.all():
            item = info.as_dict()
            if info.provider_id != "codex" or not self._uses_real_cao:
                brain = self._brains.get(meeting_id or "")
                try:
                    brain_ready = bool(brain and brain.health_check())
                except Exception:
                    brain_ready = False
                can_join = bool(core and info.enabled and info.availability == ProviderAvailability.AVAILABLE)
                if info.provider_id == "codex" and core and not brain_ready:
                    can_join = False
                item.update({
                    "runtimeStatus": "READY" if info.availability == ProviderAvailability.AVAILABLE else "UNAVAILABLE",
                    "joinAvailable": can_join,
                    "joinStatus": "AVAILABLE" if can_join else "UNAVAILABLE",
                    "reasonCode": None if can_join else "GPT_BRAIN_UNAVAILABLE" if info.provider_id == "codex" and core and not brain_ready else "PROVIDER_UNAVAILABLE",
                    "reason": None if can_join else self._codex_readiness_message("GPT_BRAIN_UNAVAILABLE") if info.provider_id == "codex" and core and not brain_ready else info.reason,
                })
                providers.append(item)
                continue
            runtime_status, code, reason = self._codex_runtime_readiness()
            joined = bool(core and any(agent.provider.lower() == "codex" for agent in core.registry.all()))
            if joined:
                join_status = "JOINED"
                join_available = False
                code = "ALREADY_JOINED"
                reason = "Codex 已加入本次会议。"
            elif not meeting_id and code is None:
                join_status = "UNAVAILABLE"
                join_available = False
                code = "MEETING_REQUIRED"
                reason = "请先创建或选择会议。"
            elif code is None:
                brain = self._brains.get(meeting_id)
                try:
                    brain_ready = bool(brain and brain.health_check())
                except Exception:
                    brain_ready = False
                if not brain_ready:
                    join_status = "UNAVAILABLE"
                    join_available = False
                    code = "GPT_BRAIN_UNAVAILABLE"
                    reason = self._codex_readiness_message(code)
                else:
                    join_status = "CHECKING"
                    join_available = False
                    code = None
                    reason = None
            if code is None and core and core.meeting.meeting.status not in {MeetingStatus.CREATED, MeetingStatus.READY}:
                join_status = "UNAVAILABLE"
                join_available = False
                code = "MEETING_NOT_JOINABLE"
                reason = self._codex_readiness_message(code)
            elif code is None and core and not Path(core.meeting.meeting.workspace_id or "").is_dir():
                join_status = "UNAVAILABLE"
                join_available = False
                code = "WORKSPACE_UNAVAILABLE"
                reason = self._codex_readiness_message(code)
            elif code is None:
                try:
                    resolve_codex_model()
                except CodexModelConfigurationError:
                    code = "CODEX_MODEL_UNSUPPORTED"
                    reason = self._codex_readiness_message(code)
                join_available = code is None
                join_status = "AVAILABLE" if join_available else "UNAVAILABLE"
            elif not joined:
                join_status = "UNAVAILABLE"
                join_available = False
            item.update({
                "runtimeStatus": runtime_status,
                "joinAvailable": join_available,
                "joinStatus": join_status,
                "reasonCode": code,
                "reason": reason,
            })
            recovery = self._cao_startup_recovery
            if runtime_status != "READY":
                if recovery.get("status") == "BLOCKED" and not joined:
                    item.update({
                        "caoServerPath": recovery.get("caoServerPath"),
                        "httpHealthStatus": recovery.get("httpHealthStatus", "UNAVAILABLE"),
                        "requestId": recovery.get("requestId"),
                    })
            providers.append(item)
        return providers

    def recover_cao_runtime(self) -> dict[str, Any]:
        """Safely reuse or start the canonical loopback CAO Product Runtime."""
        request_id = str(uuid4())
        resolution = self.runtime_tool_resolver.resolve_path("cao-server")
        cao_server_path = resolution.path if resolution.usable else None
        try:
            recovered = self.cao_service_manager.recover()
        except CaoServiceError as exc:
            self._cao_startup_recovery = {
                "status": "BLOCKED", "action": "FAILED", "reasonCode": exc.code,
                "caoServerPath": cao_server_path, "httpHealthStatus": "UNAVAILABLE",
                "requestId": request_id,
            }
            self.logger.warning(
                "cao recovery status=BLOCKED reason=%s path=%s request_id=%s",
                exc.code, cao_server_path or "UNKNOWN", request_id,
            )
            raise
        result = {**recovered, "requestId": request_id, "caoServerPath": cao_server_path, "httpHealthStatus": "HTTP_200_HEALTHY"}
        self._cao_startup_recovery = {
            "status": "READY", "action": result.get("action"), "reasonCode": None,
            "caoServerPath": cao_server_path, "httpHealthStatus": "HTTP_200_HEALTHY",
            "requestId": request_id,
        }
        self.logger.info(
            "cao recovery action=%s status=%s backend=%s pid=%s request_id=%s",
            result.get("action"), result.get("status"), result.get("terminalBackend"), result.get("processPid"), request_id,
        )
        return result

    def initialize_startup_runtime(self) -> dict[str, Any]:
        """Attempt fail-closed Product-owned CAO recovery before serving the UI."""
        try:
            return self.recover_cao_runtime()
        except CaoServiceError:
            return dict(self._cao_startup_recovery)
        except Exception as exc:
            request_id = str(uuid4())
            resolution = self.runtime_tool_resolver.resolve_path("cao-server")
            self._cao_startup_recovery = {
                "status": "BLOCKED", "action": "FAILED", "reasonCode": "CAO_STARTUP_RECOVERY_FAILED",
                "caoServerPath": resolution.path if resolution.usable else None,
                "httpHealthStatus": "UNAVAILABLE", "requestId": request_id,
            }
            self.logger.warning(
                "cao recovery status=BLOCKED reason=CAO_STARTUP_RECOVERY_FAILED error=%s request_id=%s",
                type(exc).__name__, request_id,
            )
            return dict(self._cao_startup_recovery)

    def product_runtime_preflight(self) -> dict[str, Any]:
        """Return a redacted, user-facing startup readiness summary.

        This is a redacted readiness surface. The Codex login-status command
        is read-only and does not inspect credential files; successful real
        provider admission remains the authoritative runtime check. The
        Product Shell must never attempt an automatic login.
        """
        database_ready = self.store.health_check()
        source_root = Path(self._runtime_identity.project_root)
        checks: dict[str, dict[str, Any]] = {
            "productShell": {"status": "READY", "code": "PRODUCT_SHELL_READY", "detail": "Product Shell is serving the local UI", "nextAction": "无需操作"},
            "database": {"status": "READY" if database_ready else "BLOCKED", "code": "DATABASE_READY" if database_ready else "DATABASE_UNAVAILABLE", "detail": "SQLite persistence", "nextAction": "无需操作" if database_ready else "检查产品数据目录读写权限"},
            "dataDirectory": {"status": "READY" if self.product_paths.root.is_dir() else "BLOCKED", "code": "DATA_DIRECTORY_READY" if self.product_paths.root.is_dir() else "DATA_DIRECTORY_UNAVAILABLE", "detail": str(self.product_paths.root), "nextAction": "无需操作" if self.product_paths.root.is_dir() else "检查产品数据目录权限"},
            "workspace": {"status": "READY" if source_root.is_dir() else "BLOCKED", "code": "WORKSPACE_READY" if source_root.is_dir() else "WORKSPACE_UNAVAILABLE", "detail": str(source_root), "nextAction": "无需操作" if source_root.is_dir() else "重新安装或修复应用包"},
            "productPort": {"status": "READY", "code": "PRODUCT_PORT_BOUND", "detail": "127.0.0.1:8765", "nextAction": "无需操作"},
        }
        try:
            preflight = self.runtime_preflight()
            tools = preflight.as_dict().get("tools", {})
            for name in ("cao", "tmux", "codex"):
                item = tools.get(name, {}) if isinstance(tools, dict) else {}
                checks[name] = {
                    "status": "READY" if item.get("status") == "AVAILABLE" else "BLOCKED",
                    "code": f"{name.upper()}_READY" if item.get("status") == "AVAILABLE" else f"{name.upper()}_UNAVAILABLE",
                    "version": item.get("version"),
                    "path": item.get("path"),
                    "detail": item.get("reason"),
                    "nextAction": "无需操作" if item.get("status") == "AVAILABLE" else f"请安装或修复 {name}",
                }
            checks["cao"]["serverHealthy"] = bool(preflight.cao_server_healthy)
            checks["cao"]["status"] = "READY" if preflight.cao_server_healthy else "BLOCKED"
            checks["cao"]["code"] = "CAO_SERVER_READY" if preflight.cao_server_healthy else "CAO_SERVER_UNAVAILABLE"
            checks["cao"]["nextAction"] = "无需操作" if preflight.cao_server_healthy else "点击重试，或启动本机 CAO 服务"
            checks["cao"].update({
                "httpHealthStatus": "HTTP_200_HEALTHY" if preflight.cao_server_healthy else "UNAVAILABLE",
                "startupRecovery": dict(self._cao_startup_recovery),
            })
        except Exception as exc:
            checks.update({
                "cao": {"status": "BLOCKED", "code": "CAO_PREFLIGHT_FAILED", "detail": "CAO preflight failed", "nextAction": "点击重试，或检查本机 CAO 服务"},
                "tmux": {"status": "BLOCKED", "code": "TMUX_STATUS_UNKNOWN", "detail": "CAO preflight unavailable", "nextAction": "确认 tmux 已安装"},
                "codex": {"status": "BLOCKED", "code": "CODEX_STATUS_UNKNOWN", "detail": "CAO preflight unavailable", "nextAction": "确认 Codex CLI 已安装"},
                "preflight": {"status": "DEGRADED", "code": "PREFLIGHT_EXCEPTION", "detail": type(exc).__name__, "nextAction": "重试预检并查看诊断报告"},
            })
        # ``codex login status`` returns only login state. Never inspect or
        # persist credential material, and keep real Agent admission as the
        # authoritative runtime check.
        # The authenticated CLI must be the exact binary selected by the
        # formal preflight/runtime resolver. No raw-PATH fallback is allowed.
        codex_path = str(checks.get("codex", {}).get("path") or "")
        auth_status = "UNKNOWN"
        auth_code = "CODEX_AUTH_UNKNOWN"
        auth_detail = "无法确认 Codex 登录状态"
        auth_action = "在终端中人工执行 codex login 后重试预检"
        if codex_path:
            try:
                probe = subprocess.run(
                    [codex_path, "login", "status"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                    check=False,
                    env=self.runtime_tool_resolver.controlled_environment({"NO_COLOR": "1"}),
                )
                combined = f"{probe.stdout}\n{probe.stderr}".lower()
                if probe.returncode == 0 and "logged in" in combined:
                    auth_status, auth_code = "READY", "CODEX_AUTHENTICATED"
                    auth_detail, auth_action = "Codex CLI 已登录", "无需操作"
                elif "not logged in" in combined or "login required" in combined:
                    auth_status, auth_code = "BLOCKED", "CODEX_AUTH_REQUIRED"
                    auth_detail = "Codex CLI 需要人工登录"
            except (OSError, subprocess.SubprocessError):
                pass
        checks["codexAuth"] = {
            "status": auth_status,
            "code": auth_code,
            "detail": auth_detail,
            "nextAction": auth_action,
        }
        statuses = [str(item.get("status")) for item in checks.values()]
        overall = "BLOCKED" if "BLOCKED" in statuses else ("DEGRADED" if "UNKNOWN" in statuses or "DEGRADED" in statuses else "READY")
        return {
            "overallStatus": overall,
            "checks": checks,
            "caoStartupRecovery": dict(self._cao_startup_recovery),
            "automaticLogin": False,
            "appVersion": __version__,
            "optionalDependencies": {
                "gptWeb": {"status": "EXPERIMENTAL", "blocksV1": False},
                "minimax": {"status": "BLOCKED_UPSTREAM", "blocksV1": False},
                "openaiApi": {"status": "NOT_CONFIGURED", "blocksV1": False},
            },
        }

    def list_backups(self) -> dict[str, Any]:
        return {"backups": self.operations.list_backups(), "dataDirectory": str(self.product_paths.root)}

    def create_backup(self) -> dict[str, Any]:
        result = self.operations.create_backup()
        self.logger.info("backup created backupId=%s", result["backupId"])
        return result

    def validate_backup(self, backup_id: str) -> dict[str, Any]:
        return self.operations.validate_backup(backup_id)

    def restore_backup(self, backup_id: str, *, confirm_overwrite: bool) -> dict[str, Any]:
        if any(str(item.get("status")) in {"RUNNING", "RECOVERING"} for item in self.store.list_meetings()):
            raise ProductError("停止或暂停所有运行中的会议后再恢复备份", code="RESTORE_ACTIVE_MEETING_BLOCKED")
        try:
            result = self.operations.restore_backup(backup_id, confirm_overwrite=confirm_overwrite)
        except BackupValidationError as exc:
            raise ProductError(str(exc), code=exc.code) from None
        self.logger.info("backup restored backupId=%s restartRequired=true", backup_id)
        return result

    def create_diagnostic_report(self) -> dict[str, Any]:
        result = self.operations.create_diagnostic_report(
            preflight=self.product_runtime_preflight(),
            meetings=self.store.list_meetings(),
            build={
                "buildId": os.environ.get("APP_BUILD_ID", "LOCAL"),
                "commit": os.environ.get("GIT_COMMIT", "UNKNOWN"),
                "python": platform.python_version(),
            },
        )
        self.logger.info("diagnostic report created")
        return result

    def _on_real_chrome_brain_failure(self, state: str, reason: str, *, source_host: Any | None = None) -> None:
        """Pause only Meetings joined to the exact Host that emitted the fault."""
        host = source_host or self.formal_brain_registry.get()
        for binding in tuple(self._brain_participants.values()):
            if binding.provider != "GPT_WEB_BRAIN" or binding.controller is not host:
                continue
            try:
                self.observe_brain_failure(
                    reason,
                    brain_id="ChromeWebBrain",
                    state=state,
                    meeting_id=binding.meeting_id,
                    brain_participant_id=binding.participant_id,
                    runtime_identity=binding.runtime_identity,
                )
            except Exception as exc:
                # If normal event persistence fails, fail closed through the
                # existing safety-monitor path for this joined Meeting only.
                core = self._cores.get(binding.meeting_id)
                if core is not None:
                    try:
                        core.safety.observe_system_failure(
                            "brain-failure-handler",
                            f"joined Brain failure handling failed: {type(exc).__name__}",
                        )
                        core._save_meeting()
                    except Exception:
                        pass

    # Compatibility callback names for older integrations.
    def _on_playwright_brain_failure(self, state: str, reason: str) -> None:
        self._on_real_chrome_brain_failure(state, reason)

    def _on_chrome_brain_failure(self, state: str, reason: str) -> None:
        self._on_real_chrome_brain_failure(state, reason)

    def real_chrome_brain_status(self) -> dict[str, Any]:
        status = self.get_formal_brain_runtime_state()
        status.update({"MCP_PROCESS_STARTED": False, "MCP_FORMAL_ROLE": "LEGACY_EXPERIMENT_ONLY"})
        return status

    def _formal_source_paths(self) -> dict[str, Path]:
        root = self._runtime_identity.project_root
        return {
            "formalRegistry": Path(root) / "ai_meeting_room" / "brain" / "runtime_registry.py",
            "playwrightHost": Path(root) / "ai_meeting_room" / "brain" / "playwright_attached_brain.py",
            "desktopPocHandler": Path(root) / "ai_meeting_room" / "product" / "app.py",
            "connectHandler": Path(root) / "ai_meeting_room" / "product" / "app.py",
            "productEntrypoint": Path(root) / "ai_meeting_room" / "product" / "serve.py",
            "productHttpHandler": Path(root) / "ai_meeting_room" / "product" / "server.py",
        }

    def get_formal_brain_runtime_state(self) -> dict[str, Any]:
        """Return the one authoritative live state used by UI and POC."""
        host = self.formal_brain_registry.get()
        host_status = host.status()
        live = host.live_poc_health_check()
        identity = self.formal_brain_registry.describe()
        cached_ready = bool(
            host_status.get("state") == "READY"
            and host_status.get("authState") == "AUTHENTICATED"
            and host_status.get("composerFound")
        )
        live_ready = live.get("precheckResult") == "PASS"
        invariant = bool(host_status.get("browserConnected") and cached_ready != live_ready)
        state = {
            **host_status,
            **identity,
            "brainState": host_status.get("state", "UNKNOWN"),
            "browserConnected": bool(live.get("browserConnected", host_status.get("browserConnected", False))),
            "pageAlive": bool(live.get("pageAlive", False)),
            "authState": live.get("authState", host_status.get("authState", "UNKNOWN")),
            "composerReady": bool(live.get("composerReady", False)),
            "boundPageId": live.get("boundPageId", host_status.get("boundPageId")),
            "precheckResult": live.get("precheckResult", "FAIL"),
            "failureCode": live.get("failureCode"),
            "launchSessionId": self._runtime_identity.launch_session_id,
            "processPid": self._runtime_identity.process_pid,
            "parentPid": self._runtime_identity.process_ppid,
            "processStartedAt": self._runtime_identity.process_started_at,
            "projectRoot": self._runtime_identity.project_root,
            "sourceRoot": self._runtime_identity.source_root,
            "gitCommit": self._runtime_identity.git_commit,
            "buildId": self._runtime_identity.build_id,
            "runtimeOwner": identity.get("runtimeOwner", FORMAL_RUNTIME_OWNER),
            "invariantViolation": invariant,
        }
        if invariant:
            state["failureCode"] = "FORMAL_BRAIN_STATE_INVARIANT_VIOLATION"
            state["invariantDetails"] = {
                "uiBrainState": host_status.get("state", "UNKNOWN"),
                "uiAuthState": host_status.get("authState", "UNKNOWN"),
                "uiComposerReady": bool(host_status.get("composerFound")),
                "livePrecheckResult": live.get("precheckResult", "FAIL"),
                "liveAuthState": live.get("authState", "UNKNOWN"),
                "liveComposerReady": bool(live.get("composerReady", False)),
                "registryInstanceId": identity.get("registryInstanceId", "UNKNOWN"),
                "hostInstanceId": identity.get("hostInstanceId", "UNKNOWN"),
                "boundPageId": live.get("boundPageId", host_status.get("boundPageId")),
            }
        return state

    def runtime_identity(self) -> dict[str, Any]:
        """Return safe provenance plus the authoritative formal Brain state."""
        host = self.formal_brain_registry.get()
        state = self.get_formal_brain_runtime_state()
        return self._runtime_identity.as_dict(
            registry=self.formal_brain_registry,
            host=host,
            state=state,
            module_paths=self._formal_source_paths(),
        ) | {
            "sourceFiles": self._runtime_identity.module_metadata(self._formal_source_paths()),
            "invariantViolation": bool(state.get("invariantViolation")),
        }

    @staticmethod
    def _provenance_class_identity(cls: type[Any]) -> dict[str, str]:
        module_name = str(getattr(cls, "__module__", "UNKNOWN"))
        module = sys.modules.get(module_name)
        module_file = getattr(module, "__file__", None) if module is not None else None
        return {
            "class": str(getattr(cls, "__qualname__", getattr(cls, "__name__", "UNKNOWN"))),
            "module": module_name,
            "moduleFile": str(Path(module_file).resolve()) if module_file else "UNKNOWN",
        }

    @staticmethod
    def _provenance_code_fingerprint(owner: Any, attribute: str) -> str:
        candidate = getattr(owner, attribute, None)
        function = getattr(candidate, "__func__", candidate)
        code = getattr(function, "__code__", None)
        if code is None:
            return "UNKNOWN"
        return hashlib.sha256(marshal.dumps(code)).hexdigest()

    def get_runtime_provenance(self) -> dict[str, Any]:
        """Describe loaded Product Shell code without inspecting Browser health or Page state."""
        registry = self.formal_brain_registry
        host = registry.get()
        service = self.formal_brain_service
        registry_info = registry.describe()
        active_bridge = registry.wrapped_bridge

        module_paths = {
            **self._formal_source_paths(),
            "runtimeIdentity": Path(sys.modules[RuntimeIdentity.__module__].__file__).resolve(),
            "chatgptWebBrain": Path(sys.modules[ChatGPTWebBrainBridge.__module__].__file__).resolve(),
            "safetyEngine": Path(sys.modules[SafetyEngine.__module__].__file__).resolve(),
            "recoveryManager": Path(sys.modules[RecoveryManager.__module__].__file__).resolve(),
        }
        module_metadata = RuntimeIdentity.module_metadata(module_paths)
        module_hashes = {name: item["sha256"] for name, item in module_metadata.items()}
        source_fingerprint = hashlib.sha256(
            json.dumps(module_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        loaded_code = {
            "formalDomDiagnosticSurface": self._provenance_code_fingerprint(type(host), "get_chatgpt_structural_diagnostics"),
            "urlOnlyRebind": self._provenance_code_fingerprint(type(host), "connect_url_only_for_diagnostics"),
            "pb02AuthClassification": self._provenance_code_fingerprint(ChatGPTWebBrainBridge, "start"),
            "pb03ParticipantBinding": self._provenance_code_fingerprint(type(self), "join_formal_brain"),
        }

        def component(cls: type[Any]) -> dict[str, str]:
            return self._provenance_class_identity(cls)

        process = self._runtime_identity
        return {
            "schemaVersion": "P3A_RUNTIME_PROVENANCE_V1",
            "process": {
                "pid": process.process_pid,
                "parentPid": process.process_ppid,
                "startedAt": process.process_started_at,
                "pythonExecutable": str(Path(sys.executable).resolve()),
                "pythonVersion": platform.python_version(),
            },
            "projectRoot": process.project_root,
            "sourceRoot": process.source_root,
            "sourceVersion": {
                "gitCommit": process.git_commit,
                "buildId": process.build_id,
                "sourceFingerprint": source_fingerprint,
            },
            "runtime": {
                "runtimeOwner": registry_info.get("runtimeOwner", FORMAL_RUNTIME_OWNER),
                "formalRuntimeType": registry_info.get("formalRuntimeType", "UNKNOWN"),
                "registryInstanceId": registry_info.get("registryInstanceId", "UNKNOWN"),
                "hostInstanceId": registry_info.get("hostInstanceId", "UNKNOWN"),
                "runtimeClass": component(type(self)),
            },
            "components": {
                "registry": component(type(registry)),
                "host": component(type(host)),
                "service": component(type(service)),
                "brainBridge": {
                    "configuredClass": component(ChatGPTWebBrainBridge),
                    "active": active_bridge is not None,
                    "activeClass": component(type(active_bridge)) if active_bridge is not None else None,
                },
                "safetyEngine": component(SafetyEngine),
                "recoveryManager": component(RecoveryManager),
            },
            "moduleHashes": module_hashes,
            "loadedCodeFingerprints": loaded_code,
            "featureFlags": {
                "devProbesEnabled": os.environ.get("AI_MEETING_ROOM_ENABLE_DEV_PROBES") == "1",
                "legacyElectronBrainEnabled": os.environ.get("AIMR_ENABLE_ELECTRON_BRAIN_LEGACY") == "1",
                "formalAttachMode": str(getattr(host, "attach_mode", "UNKNOWN")),
                "formalEndpointSource": str(getattr(host, "endpoint_source", "UNKNOWN")),
            },
            "privacy": {
                "liveHealthCalled": False,
                "pageAccessCount": 0,
                "domAccessCount": 0,
                "userContentReturned": False,
                "authSecretReturned": False,
            },
        }

    def poc_route_consistency(self) -> dict[str, Any]:
        """Return the current no-side-effect route target without running a POC."""
        state = self.get_formal_brain_runtime_state()
        return {
            "formalRuntimeType": state.get("formalRuntimeType"),
            "runtimeOwner": state.get("runtimeOwner", FORMAL_RUNTIME_OWNER),
            "processPid": state.get("processPid"),
            "parentPid": state.get("parentPid"),
            "launchSessionId": state.get("launchSessionId"),
            "registryInstanceId": state.get("registryInstanceId"),
            "hostInstanceId": state.get("hostInstanceId"),
            "boundPageId": state.get("boundPageId"),
            "ownerThreadId": state.get("ownerThreadId"),
            "sourceFile": str(Path(__file__).resolve()),
            "brainState": state.get("brainState"),
            "authState": state.get("authState"),
            "composerReady": bool(state.get("composerReady")),
            "precheckResult": state.get("precheckResult"),
            "invariantViolation": bool(state.get("invariantViolation")),
            "threadMatrix": state.get("threadMatrix", {}),
        }

    def dev_exact_cdp_diagnostic(self, _body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the R26 exact-CDP diagnostic inside this Product Shell process.

        The diagnostic is now a view of the formal service identity; it does
        not create a second Browser, Host, Registry, or bound page.  The old
        ``run_product_shell_exact_cdp_diagnostic`` helper remains historical
        evidence only and is deliberately not called here.
        """
        return self.formal_brain_service.readonly_identity() | {
            "executionContext": FORMAL_RUNTIME_OWNER,
            "diagnosticRuntime": "FORMAL_RUNTIME_SERVICE",
            "sensitiveProfileDataRead": False,
        }

    def dev_composer_write_probe(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run the localhost-only, send-forbidden Composer diagnostic.

        This route is intentionally outside MeetingCore and BrainInbox.  It
        uses the already-registered formal Host and its already-bound page;
        it never creates a browser, context, page, or alternate runtime.
        """
        body = body or {}
        if bool(body.get("allowSend")):
            return {
                "ok": False,
                "probeState": "FAILED",
                "allowSend": True,
                "failureCode": "DEV_PROBE_SEND_FORBIDDEN",
                "failureStage": "DEV_PROBE_PRECONDITION",
            }

        state = self.get_formal_brain_runtime_state()
        precondition = {
            "brainState": state.get("brainState", "UNKNOWN"),
            "authState": state.get("authState", "UNKNOWN"),
            "composerReady": bool(state.get("composerReady")),
            "browserConnected": bool(state.get("browserConnected")),
            "pageAlive": bool(state.get("pageAlive")),
            "boundPageId": state.get("boundPageId"),
        }
        if not (
            precondition["brainState"] == PlaywrightAttachedBrainState.READY
            and precondition["authState"] == "AUTHENTICATED"
            and precondition["composerReady"]
            and precondition["browserConnected"]
            and precondition["pageAlive"]
            and precondition["boundPageId"]
        ):
            return {
                "ok": False,
                "probeState": "FAILED",
                "allowSend": False,
                "failureCode": "DEV_COMPOSER_PROBE_PRECONDITION_FAILED",
                "failureStage": "DEV_PROBE_PRECONDITION",
                "precondition": precondition,
                "runtime": self.formal_brain_registry.describe(),
            }

        # The unique value is kept inside the Host and is never returned or
        # logged as request input.  It can only be written, verified, and
        # cleared by the send-forbidden Host method.
        host = self.formal_brain_registry.get()
        result = host.run_dev_composer_write_probe(
            f"AI_MEETING_ROOM_WRITE_PROBE_{uuid4()}",
            allow_send=False,
        )
        result["precondition"] = precondition
        result["runtime"] = self.formal_brain_registry.describe()
        return result

    def dev_connect_and_composer_probe(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Atomically reuse/connect the formal runtime and run one no-send probe."""
        body = body or {}
        host = self.formal_brain_registry.get()
        result = self.formal_brain_service.connect_and_composer_probe(
            f"AI_MEETING_ROOM_WRITE_PROBE_{uuid4()}",
            allow_send=bool(body.get("allowSend")),
            connect_attempt_id=str(body.get("connectAttemptId") or "") or None,
        )
        result["runtime"] = self.formal_brain_registry.describe()
        result["lifecycle"] = host.lifecycle_snapshot()
        return result

    def ensure_chatgpt_page(self) -> dict[str, Any]:
        """Ensure a ChatGPT target only through the formal Registry-owned Host."""
        return self.formal_brain_service.ensure_chatgpt_page()

    def bring_bound_brain_page_to_front(self) -> dict[str, Any]:
        """Foreground only the already-bound page of the formal Brain Host."""
        return self.formal_brain_service.bring_bound_page_to_front()

    def get_chatgpt_structural_diagnostics(self) -> dict[str, Any]:
        """Expose fixed, read-only diagnostics through the formal runtime only."""
        return self.formal_brain_service.get_chatgpt_structural_diagnostics()

    def connect_formal_brain_url_only_for_diagnostics(self) -> dict[str, Any]:
        """Reconnect the existing formal Host and bind only by allowlisted URL metadata."""
        return self.formal_brain_service.connect_url_only_for_diagnostics()

    def open_real_chrome_brain(self, connect_attempt_id: str | None = None) -> dict[str, Any]:
        host = self.formal_brain_registry.get()
        try:
            self.formal_brain_service.connect(connect_attempt_id=connect_attempt_id)
            status = self.get_formal_brain_runtime_state()
            if status.get("failureCode") == "FORMAL_BRAIN_STATE_INVARIANT_VIOLATION":
                raise ProductError(
                    "Formal Brain state invariant violation",
                    code="FORMAL_BRAIN_STATE_INVARIANT_VIOLATION",
                    stage="FORMAL_STATE_CHECK",
                    diagnostics=status,
                )
            attempt_id = connect_attempt_id or str(status.get("connectAttemptId") or "UNKNOWN")
            connection = _connect_result_dto(status, attempt_id)
            return {"ok": True, "connection": connection, "data": status}
        except PlaywrightAttachedBrainError as exc:
            attempt_id = connect_attempt_id or host.connect_attempt_id or "UNKNOWN"
            error = exc.envelope(attempt_id)
            # R30 formal-connect evidence is safe metadata only: current
            # DevTools port, effective non-secret call options, and the first
            # failure classification.  Never include the WS endpoint.
            status = host.status()
            for key in (
                "formalEffectiveConnectOptions",
                "formalCdpPort",
                "formalEndpointFreshness",
                "formalTcpProbe",
                "formalConnectFailure",
                "registryInstanceId",
                "hostInstanceId",
                "boundPageId",
                "ownerThreadId",
                "browserConnected",
                "state",
                "authState",
                "composerFound",
            ):
                if key in status:
                    error[key] = status[key]
            return {"ok": False, "error": error}

    def run_real_chrome_brain_poc(self) -> dict[str, Any]:
        """Run the existing Desktop Brain POC through the attached Chrome transport."""
        started_at = utc_now()
        request = BrainRequest(
            brain_request_id=str(uuid4()),
            meeting_id="phase3w-chrome-no-meeting-context",
            task_id="desktop-brain-poc",
            prompt=DESKTOP_BRAIN_POC_PROMPT,
            allowed_actions=("ACCEPT",),
            created_at=started_at,
        )
        host = self.formal_brain_registry.get()
        precheck = self.get_formal_brain_runtime_state()
        if precheck.get("failureCode") == "FORMAL_BRAIN_STATE_INVARIANT_VIOLATION":
            raise ProductError(
                "Formal Brain state invariant violation",
                code="FORMAL_BRAIN_STATE_INVARIANT_VIOLATION",
                stage="POC_PRECHECK",
                diagnostics=precheck,
            )
        if precheck.get("precheckResult") != "PASS":
            raise ProductError(
                "GPT Web Brain is not ready",
                code="GPT_WEB_BRAIN_NOT_READY",
                stage="POC_PRECHECK",
                diagnostics=precheck,
            )
        bridge = ChatGPTWebBrainBridge(host, response_timeout_seconds=180.0)
        self._desktop_poc_active = True
        try:
            try:
                bridge.start()
                if not bridge.health_check():
                    current_state = self.get_formal_brain_runtime_state()
                    failure_code = str(current_state.get("failureCode") or "GPT_WEB_BRAIN_NOT_READY")
                    raise ProductError(
                        "GPT Web Brain is not ready" if failure_code != "FORMAL_BRAIN_STATE_INVARIANT_VIOLATION" else "Formal Brain state invariant violation",
                        code=failure_code,
                        stage="POC_PRECHECK",
                        diagnostics=current_state,
                    )
                bridge.dispatch_instruction({"request": request, "reuseCurrentPage": True})
                decision = bridge.decide()
                if not decision:
                    raise ProductError("GPT Web Brain returned no validated decision", code="RESPONSE_PARSE_FAILURE", stage="PARSING")
            except ProductError:
                raise
            except Exception as exc:
                metrics = host.poc_metrics()
                failure = exc.envelope(host.connect_attempt_id or "UNKNOWN") if isinstance(exc, PlaywrightAttachedBrainError) else None
                raise ProductError(
                    "GPT Web Brain POC failed",
                    code=str(getattr(exc, "code", None) or ("BRAIN_DECISION_INVALID" if isinstance(exc, BrainDecisionInvalid) else "POC_FAILED")),
                    stage=str(getattr(exc, "stage", None) or "POC_RUN"),
                    diagnostics={
                        # Do not run a second live precheck after a terminal
                        # POC failure.  The original precheck is the
                        # authoritative attempt boundary.
                        **precheck,
                        **metrics,
                        "pocState": "FAILED",
                        **({
                            "pocFailure": failure,
                            "pocFailureCode": failure.get("code"),
                            "pocFailureStage": failure.get("stage"),
                            "pocFailureName": failure.get("name"),
                            "pocFailureMessage": failure.get("message"),
                            "pocFailureStackId": failure.get("stackId"),
                        } if failure else {}),
                    },
                ) from exc
        finally:
            self._desktop_poc_active = False
        metrics = host.poc_metrics()
        poc_decision = {
            "decision": str(decision.get("type") or ""),
            "taskId": str(decision.get("relatedTaskId") or ""),
            "reason": str(decision.get("reason") or ""),
            "instruction": str(decision.get("instruction") or ""),
            "confidence": float(decision.get("confidence")),
        }
        return {
            "phase": "PHASE3W_CHROME_BRAIN_POC",
            "brainRequestId": request.brain_request_id,
            "provider": "ChatGPTWeb",
            "browserRuntime": "Google Chrome",
            "transport": "ProductShell->PlaywrightAttachedBrainHost->PlaywrightAttachedBrainTransport->ChatGPTWeb",
            "formalRuntimeOwner": FORMAL_RUNTIME_OWNER,
            "registryInstanceId": str(precheck.get("registryInstanceId") or self.formal_brain_registry.registry_instance_id),
            "processPid": int(precheck.get("processPid") or self.formal_brain_registry.process_pid),
            "parentPid": int(precheck.get("parentPid") or self.formal_brain_registry.parent_pid),
            "requestSent": bool(metrics.get("chatgptSend")),
            "responseCaptured": bool(metrics.get("chatgptResponseCapture")),
            "decisionParsed": True,
            "composerWrite": bool(metrics.get("composerWrite")),
            "chatgptSend": bool(metrics.get("chatgptSend")),
            "chatgptSendConfirmed": bool(metrics.get("chatgptSendConfirmed")),
            "responseCompletion": bool(metrics.get("responseCompletion")),
            "responseBoundary": bool(metrics.get("responseBoundary")),
            "chatgptResponseCapture": bool(metrics.get("chatgptResponseCapture")),
            "brainDecisionParseLive": True,
            "meetingCoreMutation": False,
            "codexDispatch": False,
            "pocPrecheck": precheck,
            "formalRuntimeType": str(precheck.get("formalRuntimeType") or "PLAYWRIGHT_ATTACHED_REAL_CHROME"),
            "hostInstanceId": str(precheck.get("hostInstanceId") or "UNKNOWN"),
            "boundPageId": precheck.get("boundPageId"),
            "failureStage": None,
            "decision": poc_decision,
        }

    # Historical method names remain API compatibility shims only.
    def playwright_brain_status(self) -> dict[str, Any]:
        return self.real_chrome_brain_status()

    def open_playwright_brain(self) -> dict[str, Any]:
        return self.open_real_chrome_brain()

    def run_playwright_brain_poc(self) -> dict[str, Any]:
        return self.run_real_chrome_brain_poc()

    def chrome_brain_status(self) -> dict[str, Any]:
        return self.real_chrome_brain_status()

    def open_chrome_brain(self) -> dict[str, Any]:
        return self.open_real_chrome_brain()

    def run_chrome_brain_poc(self) -> dict[str, Any]:
        return self.run_real_chrome_brain_poc()

    @staticmethod
    def validate_desktop_brain_poc(raw_response: str, brain_request_id: str) -> dict[str, Any]:
        """Validate one Electron POC response without touching a Meeting."""
        return validate_desktop_brain_poc(raw_response, brain_request_id=brain_request_id)

    def _stored_brain_config(self) -> dict[str, Any]:
        return self.store.get_brain_provider_config() or {
            "provider": "openai-compatible",
            "model": "",
            "baseUrl": "",
            "timeout": 60.0,
            "enabled": False,
            "secretReference": "brain-provider",
        }

    def brain_provider_status(self) -> dict[str, Any]:
        """Return configuration-only status; never return a credential value."""
        raw = self._stored_brain_config()
        reference = str(raw.get("secretReference") or "brain-provider")
        configured = self.brain_secret_store.exists(reference)
        try:
            candidate = BrainProviderConfigValidator.validate(raw, credential_configured=configured)
            # A sentinel is used only to evaluate configuration health; the
            # real credential is not resolved by a status read.
            provider = OpenAICompatibleProvider(
                replace(candidate.config, authentication_reference=reference),
                environ={reference: "configured"} if configured else {},
            )
            health = provider.health_check().as_dict()
            public = candidate.as_public_dict(configured)
        except BrainProviderConfigError as exc:
            health = {"healthy": False, "status": exc.code, "reason": str(exc)}
            public = {
                "provider": raw.get("provider", "openai-compatible"),
                "model": raw.get("model", ""),
                "baseUrl": raw.get("baseUrl", ""),
                "timeout": raw.get("timeout", 60.0),
                "enabled": bool(raw.get("enabled", True)),
                "secretReference": reference,
                "credentialConfigured": configured,
            }
        return {"config": public, "health": health}

    def save_brain_provider_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and save metadata plus an optional new Keychain secret."""
        current = self._stored_brain_config()
        reference = str(current.get("secretReference") or "brain-provider")
        candidate = {**payload, "secretReference": reference}
        new_secret = payload.get("apiCredential")
        has_new_secret = isinstance(new_secret, str) and bool(new_secret)
        existing = self.brain_secret_store.exists(reference)
        validated = BrainProviderConfigValidator.validate(candidate, credential_configured=has_new_secret or existing)
        if has_new_secret:
            self.brain_secret_store.save(new_secret)
        metadata = {
            "provider": validated.config.provider,
            "model": validated.config.model,
            "baseUrl": validated.config.endpoint,
            "timeout": validated.config.timeout,
            "enabled": validated.config.enabled,
            "secretReference": reference,
        }
        self.store.save_brain_provider_config(metadata)
        return self.brain_provider_status()

    def remove_brain_provider_credential(self) -> dict[str, Any]:
        reference = str(self._stored_brain_config().get("secretReference") or "brain-provider")
        self.brain_secret_store.delete(reference)
        return self.brain_provider_status()

    def test_brain_provider(self) -> dict[str, Any]:
        """Make one minimal real provider request; never creates a decision."""
        raw = self._stored_brain_config()
        reference = str(raw.get("secretReference") or "brain-provider")
        configured = self.brain_secret_store.exists(reference)
        try:
            validated = BrainProviderConfigValidator.validate(raw, credential_configured=configured)
            secret = self.brain_secret_store.resolve(reference)
            provider = OpenAICompatibleProvider(
                replace(validated.config, authentication_reference=reference),
                environ={reference: secret},
            )
            provider.complete("Respond with exactly OK and no other text.")
            return {"status": "READY", "code": "READY", "credentialConfigured": True}
        except BrainProviderConfigError as exc:
            return {"status": exc.code, "code": exc.code, "reason": str(exc), "credentialConfigured": configured}
        except SecretStoreError:
            return {"status": "AUTH_REQUIRED", "code": "AUTH_REQUIRED", "credentialConfigured": False}
        except BrainProviderError as exc:
            status = {"AUTH_REQUIRED": "AUTH_ERROR", "RATE_LIMITED": "RATE_LIMITED", "TIMEOUT": "NETWORK_ERROR", "NETWORK_ERROR": "NETWORK_ERROR", "PROVIDER_ERROR": "ERROR"}.get(exc.code, "ERROR")
            return {"status": status, "code": exc.code, "credentialConfigured": configured}

    def run_api_brain_poc(self) -> dict[str, Any]:
        """Run one real, context-free API Brain transport/parser POC.

        This method intentionally has no Meeting ID and does not call Core,
        TaskEngine, BrainInbox, or persistence.  A missing configuration or a
        real provider failure is returned as a failure record, never replaced
        with a canned response.
        """
        raw = self._stored_brain_config()
        reference = str(raw.get("secretReference") or "brain-provider")
        configured = self.brain_secret_store.exists(reference)
        try:
            validated = BrainProviderConfigValidator.validate(raw, credential_configured=configured)
            secret = self.brain_secret_store.resolve(reference)
            config = replace(validated.config, authentication_reference=reference)
            provider = OpenAICompatibleProvider(config, environ={reference: secret})
        except (BrainProviderConfigError, SecretStoreError) as exc:
            code = getattr(exc, "code", None) or "AUTH_REQUIRED"
            return {
                "phase": "PHASE3P-API-BRAIN-POC", "provider": raw.get("provider", "UNKNOWN"),
                "model": raw.get("model", "UNKNOWN"), "endpointConfigured": bool(raw.get("baseUrl")),
                "brainRequestId": None, "requestSent": False, "responseCaptured": False,
                "decisionParsed": False, "status": "FAILED", "failureCode": code,
                "failureReason": str(exc), "startTime": utc_now(), "endTime": utc_now(),
            }
        bridge = ApiBrainBridge(provider)
        started_at = utc_now()
        bridge.start()
        health = provider.health_check()
        result: dict[str, Any] = {
            "phase": "PHASE3P-API-BRAIN-POC",
            "provider": config.provider,
            "model": config.model or "UNKNOWN",
            "endpointConfigured": bool(config.endpoint),
            "brainRequestId": None,
            "requestSent": False,
            "responseCaptured": False,
            "decisionParsed": False,
            "status": "FAILED",
            "failureCode": health.status if not health.healthy else None,
            "failureReason": health.reason,
            "startTime": started_at,
            "endTime": None,
        }
        if not health.healthy:
            result["endTime"] = utc_now()
            return result

        request = BrainRequest(
            brain_request_id=str(uuid4()),
            meeting_id="phase3p-no-meeting-context",
            task_id=API_BRAIN_POC_TASK_ID,
            prompt=(
                "Return exactly one JSON object and no Markdown or extra prose. "
                'Use exactly this schema and values: '
                '{"decision":"ACCEPT","taskId":"api-brain-poc",'
                '"reason":"api brain poc","instruction":"","confidence":1.0}. '
                "Do not discuss this request."
            ),
            allowed_actions=("ACCEPT",),
            created_at=started_at,
        )
        result["brainRequestId"] = request.brain_request_id
        try:
            bridge.dispatch_instruction({"request": request})
            result["requestSent"] = True
            result["responseCaptured"] = True
            result["decisionParsed"] = True
            result["decision"] = bridge.decide()
            result["status"] = "PASS"
        except Exception as exc:
            result["failureCode"] = getattr(exc, "code", None) or ("BRAIN_DECISION_INVALID" if isinstance(exc, BrainDecisionInvalid) else type(exc).__name__)
            result["failureReason"] = str(exc)
        finally:
            result["endTime"] = utc_now()
        return result

    def create_brain_pairing(self, meeting_id: str) -> dict[str, Any]:
        """Create a one-time local Extension pairing code for a Meeting."""
        self._core(meeting_id)
        return self.local_brain_bridge.create_pairing(meeting_id)

    def observe_brain_failure(
        self,
        reason: str,
        *,
        brain_id: str = "ChatGPTWebBrain",
        state: str = "UNKNOWN",
        meeting_id: str | None = None,
        brain_participant_id: str | None = None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open the fail-closed circuit only for a matching joined participant."""
        state = str(state or "UNKNOWN").upper()
        if state not in {"ERROR", "LOST", "UNKNOWN", "AUTH_REQUIRED", "CHALLENGE_REQUIRED"}:
            state = "UNKNOWN"
        if not meeting_id or not brain_participant_id:
            return {"state": state, "observedMeetings": [], "failures": [], "ignored": "BRAIN_PARTICIPANT_IDENTITY_REQUIRED"}
        binding = self._brain_participants.get(meeting_id)
        if (
            binding is None
            or binding.participant_id != brain_participant_id
            or self._brains.get(meeting_id) is not binding.bridge
            or (runtime_identity is not None and runtime_identity != binding.runtime_identity)
        ):
            return {"state": state, "observedMeetings": [], "failures": [], "ignored": "BRAIN_PARTICIPANT_IDENTITY_MISMATCH"}
        core = self._core(meeting_id)
        if core.meeting.meeting.status in {MeetingStatus.COMPLETED, MeetingStatus.FAILED}:
            return {"state": state, "observedMeetings": [], "failures": [], "ignored": "MEETING_TERMINAL"}
        binding.health_state = state
        core.safety.observe_brain_failure(
            brain_id,
            f"{state}: {reason}",
            brain_participant_id=binding.participant_id,
            runtime_identity=binding.runtime_identity,
        )
        core._save_meeting()
        return {"state": state, "observedMeetings": [meeting_id], "failures": [], "brainParticipantId": binding.participant_id}

    def run_extension_brain_poc(self, meeting_id: str) -> dict[str, Any]:
        """Run the single Phase 3A-X transport/parser POC request.

        This deliberately stops at validated Brain output. It does not submit
        the ACCEPT decision to MeetingCore or create a Meeting task.
        """
        self._core(meeting_id)
        request = BrainRequest(
            brain_request_id=str(uuid4()),
            meeting_id=meeting_id,
            task_id="phase3a-extension-poc",
            prompt=(
                'Return exactly one JSON object and no Markdown or extra prose. '
                'Use exactly this schema and values: '
                '{"decision":"ACCEPT","taskId":"phase3a-extension-poc",'
                '"reason":"extension transport poc","instruction":"","confidence":1.0}. '
                'Do not discuss this request and do not modify any files.'
            ),
            allowed_actions=("ACCEPT",),
            created_at=utc_now(),
        )
        transport = BoundExtensionTransport(self.local_brain_bridge, meeting_id)
        brain = ChatGPTWebBrainBridge(transport, response_timeout_seconds=180.0)
        brain.start()
        if not brain.health_check():
            raise ProductError(f"extension Brain is not ready: {brain.state.value}")
        brain.dispatch_instruction({"request": request})
        body = brain.decide()
        if not body:
            raise ProductError("extension Brain returned no validated decision")
        return {
            "phase": "PHASE3A-X-LIVE-POC1",
            "brainRequestId": request.brain_request_id,
            "provider": "ChatGPTWeb",
            "transport": "ProductShell->BrainRequestDispatcher->LocalBrainBridge->BrowserExtension->ChatGPTTab",
            "requestSent": True,
            "responseCaptured": True,
            "decisionParsed": True,
            "decision": body,
            "extensionPairingLive": True,
            "chatgptTabBindingLive": True,
        }

    def _poll_agent(self, core: MeetingCore, agent_id: str) -> AgentHealth:
        health = core.poll_agent(agent_id)
        if health in {AgentHealth.ERROR, AgentHealth.LOST, AgentHealth.UNKNOWN}:
            agent = core.registry.get(agent_id)
            if agent.provider == "codex":
                runtime = getattr(core.runtime.adapters.get(agent_id), "runtime", None)
                reason = getattr(runtime, "error", None) or f"runtime health={health.value}"
                self.catalog.mark_blocked("codex", reason)
        return health

    def _create_cao_adapter(self, *, provider: str, session_id: str, working_directory: str) -> CaoAgentAdapter:
        adapter_options: dict[str, Any] = {}
        if provider.lower() == "codex":
            try:
                adapter_options["model"] = resolve_codex_model()
            except CodexModelConfigurationError as exc:
                raise ProviderBlockedError(
                    str(exc),
                    code=exc.code,
                    stage="PROVIDER_CONFIGURATION",
                ) from exc
        return CaoAgentAdapter.create(
            self.cao_base_url,
            agent_profile="developer",
            provider=provider,
            session_id=session_id,
            working_directory=working_directory,
            initial_message=None,
            **adapter_options,
        )

    def _new_provider_session_id(self, meeting_id: str, provider: str, *, recovery: bool = False) -> str:
        suffix = uuid4().hex[:8]
        if self._acceptance_agent_session_prefix:
            return f"{self._acceptance_agent_session_prefix}{provider}-{suffix}"
        kind = "phase2-recovery" if recovery else "phase2"
        return f"{kind}-{meeting_id[:8]}-{provider}-{suffix}"

    def _attach_brain(self, core: MeetingCore, *, persist_participant: bool = False) -> ManualBrainBridge:
        meeting_id = core.meeting.meeting.meeting_id
        inbox = BrainInbox(meeting_id, self.store.save_brain_event, self.store.save_brain_decision)
        brain = ManualBrainBridge(inbox)
        brain.start()
        core.events.subscribe("*", brain.receive_event)
        core.events.subscribe("*", lambda event: self.logger.info(
            "domain event meetingId=%s event=%s source=%s",
            event.meeting_id,
            event.event_type,
            event.source,
        ))
        self._brains[meeting_id] = brain
        self._brain_inboxes[meeting_id] = inbox
        persisted = self.store.get_brain_participant(meeting_id)
        if persisted is not None:
            if (
                persisted.get("meetingId") != meeting_id
                or persisted.get("role") != "BRAIN"
                or persisted.get("transport") != MANUAL_GPT_TRANSPORT
            ):
                raise ProductError("persisted Manual GPT participant identity is invalid", code="BRAIN_PARTICIPANT_PERSISTENCE_INVALID", stage="BRAIN_PARTICIPANT_RESTORE")
            participant_id = str(persisted["participantId"])
            runtime_identity = dict(persisted.get("runtimeIdentity") or {})
            joined_at = str(persisted["joinedAt"])
        else:
            participant_id = f"gpt-brain-{uuid5(NAMESPACE_URL, 'ai-meeting-room:brain:' + meeting_id)}"
            runtime_identity = {"runtimeType": MANUAL_GPT_TRANSPORT, "runtimeInstanceId": participant_id}
            joined_at = utc_now()
            if persist_participant:
                self.store.save_brain_participant(meeting_id, {
                    "meetingId": meeting_id,
                    "participantId": participant_id,
                    "role": "BRAIN",
                    "transport": MANUAL_GPT_TRANSPORT,
                    "state": "READY",
                    "health": "HEALTHY" if brain.health_check() else "UNKNOWN",
                    "runtimeIdentity": runtime_identity,
                    "joinedAt": joined_at,
                })
        self._brain_participants[meeting_id] = BrainParticipantBinding(
            meeting_id=meeting_id,
            participant_id=participant_id,
            provider=MANUAL_GPT_TRANSPORT,
            runtime_identity=runtime_identity,
            bridge=brain,
            controller=None,
            health_state="HEALTHY" if brain.health_check() else "UNKNOWN",
            joined_at=joined_at,
        )
        return brain

    def _capture_brain_runtime_identity(self, controller: Any, provider: str) -> dict[str, Any]:
        if controller is self.formal_brain_registry.get():
            raw = self.formal_brain_service.readonly_identity()
        else:
            snapshot = getattr(controller, "runtime_identity_snapshot", None)
            raw = snapshot() if callable(snapshot) else {}
        allowed = {
            "registryInstanceId", "hostInstanceId", "boundPageId", "ownerThreadId",
            "processPid", "formalRuntimeType", "runtimeType", "runtimeInstanceId",
        }
        identity = {
            str(key): value
            for key, value in dict(raw or {}).items()
            if key in allowed and (isinstance(value, (str, int, bool)) or value is None)
        }
        identity.setdefault("runtimeType", provider)
        if not any(identity.get(key) for key in ("hostInstanceId", "runtimeInstanceId")):
            identity["runtimeInstanceId"] = f"runtime-{uuid4()}"
        return identity

    @staticmethod
    def _same_formal_runtime_identity(expected: dict[str, Any], current: dict[str, Any]) -> bool:
        required = ("registryInstanceId", "hostInstanceId", "boundPageId", "ownerThreadId", "processPid")
        return all(
            expected.get(key) not in (None, "", "UNKNOWN")
            and current.get(key) == expected.get(key)
            for key in required
        )

    def _persisted_brain_failure_identity(self, meeting_id: str) -> tuple[str | None, dict[str, Any] | None]:
        """Resolve additive participant metadata from the persisted Brain breaker event."""
        try:
            rows = self.store.list_events(meeting_id)
        except Exception:
            return None, None
        for row in reversed(rows):
            if row.get("event_type") != "BrainCircuitBreakerOpened":
                continue
            payload = row.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    continue
            if not isinstance(payload, dict):
                continue
            participant_id = payload.get("brainParticipantId")
            runtime_identity = payload.get("brainRuntimeIdentity")
            if isinstance(participant_id, str) and isinstance(runtime_identity, dict):
                return participant_id, {
                    str(key): value for key, value in runtime_identity.items()
                    if isinstance(value, (str, int, bool)) or value is None
                }
        return None, None

    def _brain_participant_dto(self, meeting_id: str) -> dict[str, Any] | None:
        binding = self._brain_participants.get(meeting_id)
        if binding is None:
            return None
        try:
            healthy = binding.bridge.health_check()
        except Exception:
            healthy = False
        if healthy:
            binding.health_state = "HEALTHY"
        return {
            "meetingId": binding.meeting_id,
            "participantId": binding.participant_id,
            "provider": binding.provider,
            "health": binding.health_state,
            "state": getattr(getattr(binding.bridge, "state", None), "value", "READY" if healthy else "UNKNOWN"),
            "runtimeIdentity": dict(binding.runtime_identity),
            "joinedAt": binding.joined_at,
        }

    def join_formal_brain(self, meeting_id: str) -> dict[str, Any]:
        """Join a Meeting to the already-connected formal GPT runtime; never attach or send here."""
        core = self._core(meeting_id)
        existing = self._brain_participants.get(meeting_id)
        if core.meeting.meeting.status == MeetingStatus.RUNNING and existing is not None and existing.controller is self.formal_brain_registry.get():
            if self._brain_binding_is_healthy(existing, existing.participant_id):
                return self.snapshot(meeting_id)
        if core.meeting.meeting.status not in {MeetingStatus.CREATED, MeetingStatus.READY, MeetingStatus.PAUSED}:
            raise ProductError("formal Brain can join only a CREATED, READY, or PAUSED Meeting", code="BRAIN_JOIN_STATE_INVALID", stage="BRAIN_PARTICIPANT_JOIN")
        host = self.formal_brain_registry.get()
        identity = self.formal_brain_service.readonly_identity()
        live = host.live_poc_health_check()
        if (
            host.status().get("state") != "READY"
            or not live.get("browserConnected")
            or not live.get("pageAlive")
            or live.get("hostname") != "chatgpt.com"
            or live.get("authState") != "AUTHENTICATED"
            or not live.get("composerReady")
            or live.get("precheckResult") != "PASS"
            or not identity.get("boundPageId")
        ):
            raise ProductError("formal GPT Web Brain runtime is not fully READY", code="GPT_WEB_BRAIN_NOT_READY", stage="BRAIN_PARTICIPANT_JOIN")
        bridge = ChatGPTWebBrainBridge(host, response_timeout_seconds=180.0)
        bridge.start()
        if not bridge.health_check() or bridge.auth_state != "AUTHENTICATED":
            raise ProductError("formal GPT Web Brain participant health check failed", code="GPT_WEB_BRAIN_NOT_READY", stage="BRAIN_PARTICIPANT_JOIN")
        identity = self._capture_brain_runtime_identity(host, "GPT_WEB_BRAIN")
        participant_id: str | None = None
        if existing is not None and existing.provider == "GPT_WEB_BRAIN" and existing.controller is host:
            participant_id = existing.participant_id
        elif core.safety.breaker.participant_type == "BRAIN":
            persisted_participant_id = core.safety.breaker.brain_participant_id
            persisted_runtime_identity = core.safety.breaker.brain_runtime_identity
            if not persisted_participant_id:
                persisted_participant_id, persisted_runtime_identity = self._persisted_brain_failure_identity(meeting_id)
            # A deliberate rejoin through this same formal provider may bind
            # the stable Meeting participant to a newly attached runtime.
            if persisted_participant_id and persisted_runtime_identity and persisted_runtime_identity.get("formalRuntimeType") == identity.get("formalRuntimeType"):
                participant_id = persisted_participant_id
        participant_id = participant_id or f"brain-participant-{uuid4()}"
        core.events.subscribe("*", bridge.receive_event)
        self._brains[meeting_id] = bridge
        self._brain_participants[meeting_id] = BrainParticipantBinding(
            meeting_id=meeting_id,
            participant_id=participant_id,
            provider="GPT_WEB_BRAIN",
            runtime_identity=identity,
            bridge=bridge,
            controller=host,
            health_state="HEALTHY",
            joined_at=utc_now(),
        )
        return self.snapshot(meeting_id)

    def _brain_binding_is_healthy(self, binding: BrainParticipantBinding, expected_participant_id: str) -> bool:
        current = self._brain_participants.get(binding.meeting_id)
        if (
            current is not binding
            or binding.participant_id != expected_participant_id
            or self._brains.get(binding.meeting_id) is not binding.bridge
        ):
            return False
        if binding.provider == "GPT_WEB_BRAIN":
            host = self.formal_brain_registry.get()
            if binding.controller is not host:
                return False
            try:
                current_identity = self.formal_brain_service.readonly_identity()
                live = host.live_poc_health_check()
            except Exception:
                return False
            if not self._same_formal_runtime_identity(binding.runtime_identity, current_identity):
                return False
            if (
                not live.get("browserConnected")
                or not live.get("pageAlive")
                or live.get("hostname") != "chatgpt.com"
                or live.get("authState") != "AUTHENTICATED"
                or not live.get("composerReady")
                or live.get("precheckResult") != "PASS"
            ):
                return False
        try:
            healthy = binding.bridge.health_check()
        except Exception:
            healthy = False
        binding.health_state = "HEALTHY" if healthy else "UNKNOWN"
        return healthy

    def attach_chatgpt_web_bridge(self, meeting_id: str, controller: BrowserController, *, response_timeout_seconds: float = 120.0) -> dict[str, Any]:
        """Explicitly replace the default ManualBrain at the application edge.

        ``controller`` is injected by the desktop/browser integration.  The
        Core only sees the validated decision submitted by
        :meth:`run_chatgpt_review`; it never receives DOM selectors or browser
        objects.
        """
        core = self._core(meeting_id)
        brain = ChatGPTWebBrainBridge(controller, response_timeout_seconds=response_timeout_seconds)
        inbox = self._brain_inboxes[meeting_id]
        brain.start()
        core.events.subscribe("*", brain.receive_event)
        self._brains[meeting_id] = brain
        previous = self._brain_participants.get(meeting_id)
        identity = self._capture_brain_runtime_identity(controller, "GPT_WEB_BRAIN")
        participant_id = (
            previous.participant_id
            if previous is not None and previous.provider == "GPT_WEB_BRAIN" and previous.controller is controller
            else f"brain-participant-{uuid4()}"
        )
        self._brain_participants[meeting_id] = BrainParticipantBinding(
            meeting_id=meeting_id,
            participant_id=participant_id,
            provider="GPT_WEB_BRAIN",
            runtime_identity=identity,
            bridge=brain,
            controller=controller,
            health_state="HEALTHY" if brain.health_check() else brain.state.value,
            joined_at=utc_now(),
        )
        return self.snapshot(meeting_id)

    def run_chatgpt_review(self, meeting_id: str, task_id: str, *, allowed_actions: tuple[str, ...] = ("ACCEPT", "REWORK")) -> dict[str, Any]:
        """Send one bounded completed-task review through the Web Brain.

        This is intentionally explicit in Phase 3A: automatic event-driven
        scheduling is not introduced until the live browser controller is
        stable. Any bridge failure is a Brain participant fault and pauses the
        Meeting through the existing fail-closed SafetyEngine.
        """
        core = self._core(meeting_id)
        brain = self._brains.get(meeting_id)
        binding = self._brain_participants.get(meeting_id)
        if not isinstance(brain, ChatGPTWebBrainBridge) or binding is None or binding.bridge is not brain:
            raise ProductError("ChatGPT Web Brain Bridge is not attached")
        task = core.tasks.get(task_id)
        if task.status != TaskStatus.COMPLETED:
            raise ProductError(f"Brain review requires COMPLETED task, got {task.status.value}")
        agent = core.registry.get(task.assigned_agent_id) if task.assigned_agent_id else None
        composer = PromptComposer()
        request = composer.compose(
            meeting_id=meeting_id,
            meeting_goal=core.meeting.meeting.name,
            task_id=task.task_id,
            task_instruction=task.instruction,
            agent_name=agent.display_name if agent else "unknown",
            agent_result=task.result or "",
            recent_events=[{"eventType": event.event_type, "source": event.source, "payload": event.payload} for event in core.events.history(meeting_id)[-10:]],
            meeting_state=core.meeting.meeting.status.value,
            allowed_actions=allowed_actions,
        )
        try:
            brain.dispatch_instruction({"request": request})
            body = brain.decide()
            if not body:
                raise ProductError("ChatGPT Web Brain returned no validated decision")
        except Exception as exc:
            if core.meeting.meeting.status == MeetingStatus.RUNNING:
                reason_code = "BRAIN_DECISION_INVALID" if isinstance(exc, BrainDecisionInvalid) else "BRAIN_UNAVAILABLE"
                self.observe_brain_failure(
                    f"{reason_code}: {type(exc).__name__}",
                    brain_id="ChatGPTWebBrain",
                    state="UNKNOWN" if reason_code == "BRAIN_DECISION_INVALID" else "ERROR",
                    meeting_id=meeting_id,
                    brain_participant_id=binding.participant_id,
                    runtime_identity=binding.runtime_identity,
                )
            raise
        # Business validation remains outside the Bridge. A valid Brain
        # response that conflicts with the Core's task state is a Core-level
        # rejection, not evidence that the browser itself is unavailable.
        return self.submit_brain_decision(meeting_id, body)

    def _core(self, meeting_id: str) -> MeetingCore:
        with self._lock:
            if meeting_id not in self._cores:
                core = MeetingCore.restore(meeting_id, self.store, heartbeat_timeout_seconds=self.heartbeat_timeout_seconds)
                self._cores[meeting_id] = core
                self._attach_brain(core)
                try:
                    record = self.store.get_current_manual_gpt_handoff(meeting_id)
                    if record is not None:
                        self._validate_restored_manual_gpt_handoff(core, record)
                        core.set_waiting_for_human_handoff(True)
                except Exception as exc:
                    failure_class = type(exc).__name__
                    self._manual_gpt_restore_errors[meeting_id] = failure_class
                    core.set_waiting_for_human_handoff(True)
                    core.safety.observe_system_failure(
                        "manual-gpt-handoff-restore",
                        f"persisted Brain handoff identity/state could not be verified ({failure_class})",
                    )
            return self._cores[meeting_id]

    def _validate_restored_manual_gpt_handoff(self, core: MeetingCore, record: dict[str, Any]) -> None:
        packet = record.get("packet")
        active_statuses = {
            WAITING_FOR_HUMAN_HANDOFF, "VALIDATED", "APPLYING", "APPLY_FAILED",
            "BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED",
        }
        required = ("requestId", "packetId", "meetingId", "taskId", "resultId", "brainParticipantId", "createdAt", "nonce")
        if not isinstance(packet, dict) or any(not isinstance(packet.get(key), str) or not packet[key] for key in required):
            raise ValueError("persisted handoff identity is incomplete")
        meeting_id = core.meeting.meeting.meeting_id
        binding = self._brain_participants[meeting_id]
        if (
            packet["meetingId"] != meeting_id
            or packet["brainParticipantId"] != binding.participant_id
            or record.get("status") not in active_statuses
        ):
            raise ValueError("persisted handoff ownership or state is inconsistent")
        task = core.tasks.get(packet["taskId"])
        if task.meeting_id != meeting_id or task.status != TaskStatus.COMPLETED:
            raise ValueError("persisted handoff task is not the completed result")
        if self._manual_gpt_result_id(meeting_id, task) != packet["resultId"]:
            raise ValueError("persisted handoff result identity is stale")
        if record.get("status") == "BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED":
            if record.get("copyText") is not None:
                raise ValueError("blocked sensitive packet unexpectedly contains copy text")
            return
        if (
            packet.get("schemaVersion") != "ai-meeting-room.brain-packet.v1"
            or packet.get("transport") != MANUAL_GPT_TRANSPORT
            or not isinstance(packet.get("decisionTypesAllowed"), list)
            or not packet["decisionTypesAllowed"]
            or not isinstance(record.get("copyText"), str)
        ):
            raise ValueError("persisted Brain Packet schema is inconsistent")
        ManualGPTBrainTransport._reject_sensitive_content((
            str(packet.get("taskSummary", "")), str(packet.get("taskInstruction", "")),
            str(packet.get("agentResult", "")),
        ))
        if record["copyText"] != ManualGPTBrainTransport._render_copy_text(packet):
            raise ValueError("persisted Brain Packet copy text does not match its identity/content")
        if record.get("status") in {"VALIDATED", "APPLYING", "APPLY_FAILED"}:
            staged = record.get("stagedDecision")
            if not isinstance(staged, dict) or staged.get("decisionId") != record.get("stagedDecisionId"):
                raise ValueError("persisted staged Decision identity is inconsistent")
            self.manual_gpt_transport.validate_decision(json.dumps(staged, ensure_ascii=False), packet)

    @property
    def safety_state_hydrated(self) -> bool:
        return self._safety_state_hydrated

    def hydrate_persisted_safety_state(self) -> None:
        """Restore every persisted Meeting and safety checkpoint before API bind."""
        with self._lock:
            if self._safety_state_hydrated:
                return
            for meeting in self.store.list_meetings():
                self._core(str(meeting["meeting_id"]))
            self._safety_state_hydrated = True

    def create_meeting(self, name: str, workspace_path: str) -> dict[str, Any]:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ProductError(
                "meeting name is required",
                code="MEETING_NAME_REQUIRED",
                stage="MEETING_CREATE",
            )
        try:
            path = Path(workspace_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProductError(
                "workspace path is invalid",
                code="WORKSPACE_DIRECTORY_INVALID",
                stage="MEETING_CREATE",
            ) from exc
        if not path.is_dir() or path == Path("/"):
            raise ProductError(
                "workspace must be an existing narrow directory",
                code="WORKSPACE_DIRECTORY_INVALID",
                stage="MEETING_CREATE",
            )
        try:
            core = MeetingCore.create(
                normalized_name,
                self.store,
                workspace_id=str(path),
                heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
            )
            with self._lock:
                self._cores[core.meeting.meeting.meeting_id] = core
                self._attach_brain(core, persist_participant=True)
        except sqlite3.Error as exc:
            raise ProductError(
                "meeting persistence is unavailable",
                code="DATABASE_UNAVAILABLE",
                stage="MEETING_CREATE",
            ) from exc
        return self.snapshot(core.meeting.meeting.meeting_id)

    def list_meetings(self) -> list[dict[str, Any]]:
        return self.store.list_meetings()

    def join_provider(self, meeting_id: str, provider: str) -> dict[str, Any]:
        provider_id = provider.lower()
        with self._lock:
            join_lock = self._join_locks.setdefault((meeting_id, provider_id), threading.Lock())
        with join_lock:
            core = self._core(meeting_id)
            # A duplicate join is a read of the existing membership, not a
            # request to allocate another CAO or tmux session.
            existing = next((agent for agent in core.registry.all() if agent.provider.lower() == provider_id), None)
            if existing is not None:
                return self.snapshot(meeting_id)
            try:
                info = self.catalog.get(provider_id)
            except KeyError as exc:
                raise ProductError("所选智能体尚未集成。", code="PROVIDER_NOT_INTEGRATED", stage="PROVIDER_LOOKUP") from exc
            if core.meeting.meeting.status not in {MeetingStatus.CREATED, MeetingStatus.READY}:
                if provider_id == "codex":
                    code = "MEETING_NOT_JOINABLE"
                    raise ProviderBlockedError(self._codex_readiness_message(code), code=code, stage="MEETING_LIFECYCLE")
                raise ProductError("agents can only join before the meeting is running")

            if provider_id == "codex":
                brain = self._brains.get(meeting_id)
                try:
                    brain_ready = bool(brain and brain.health_check())
                except Exception:
                    brain_ready = False
                if not brain_ready:
                    code = "GPT_BRAIN_UNAVAILABLE"
                    raise ProviderBlockedError(self._codex_readiness_message(code), code=code, stage="BRAIN_HEALTH")

            if provider_id == "codex" and self._uses_real_cao:
                runtime_status, code, _reason = self._codex_runtime_readiness()
                if code is not None:
                    self.catalog.mark_blocked("codex", code)
                    stage = "CODEX_AUTH" if code.startswith("CODEX_AUTH") else "CAO_PREFLIGHT"
                    raise ProviderBlockedError(self._codex_readiness_message(code), code=code, stage=stage)
                try:
                    resolve_codex_model()
                except CodexModelConfigurationError as exc:
                    code = "CODEX_MODEL_UNSUPPORTED"
                    raise ProviderBlockedError(self._codex_readiness_message(code), code=code, stage="PROVIDER_CONFIGURATION") from exc
                workspace_candidate = core.meeting.meeting.workspace_id
                if not workspace_candidate or not Path(workspace_candidate).is_dir():
                    code = "WORKSPACE_UNAVAILABLE"
                    raise ProviderBlockedError(self._codex_readiness_message(code), code=code, stage="WORKSPACE_PREFLIGHT")
                self.catalog.mark_available("codex")
            elif not self.catalog.can_join(provider_id):
                raise ProviderBlockedError(f"provider {provider_id} is {info.availability.value}: {info.reason or 'not available'}")

            workspace = core.meeting.meeting.workspace_id or str(Path.cwd())
            session_id = self._new_provider_session_id(meeting_id, provider_id)
            try:
                adapter = self._adapter_factory(provider=provider_id, session_id=session_id, working_directory=workspace)
            except Exception as exc:
                if provider_id == "codex":
                    raw_code = str(getattr(exc, "code", ""))
                    code = "CODEX_AUTH_REQUIRED" if raw_code in {"CODEX_AUTH_REQUIRED", "CODEX_AUTH_EXPIRED"} else "CODEX_RUNTIME_SESSION_CREATE_FAILED"
                    stage = "CODEX_AUTH" if code == "CODEX_AUTH_REQUIRED" else "CAO_SESSION_CREATE"
                    raise ProviderBlockedError(self._codex_readiness_message(code), code=code, stage=stage) from exc
                raise
            # Provider health is checked before the AgentRecord is admitted to the Meeting.
            try:
                healthy = bool(adapter.health_check())
            except Exception as exc:
                try:
                    adapter.stop()
                except Exception:
                    pass
                if provider_id == "codex":
                    code = "CODEX_RUNTIME_UNHEALTHY"
                    raise ProviderBlockedError(self._codex_readiness_message(code), code=code, stage="CAO_RUNTIME_HEALTH") from exc
                raise ProductError(f"provider {provider_id} failed healthCheck") from exc
            if not healthy:
                try:
                    adapter.stop()
                except Exception:
                    pass
                if provider_id == "codex":
                    code = "CODEX_RUNTIME_UNHEALTHY"
                    raise ProviderBlockedError(self._codex_readiness_message(code), code=code, stage="CAO_RUNTIME_HEALTH")
                raise ProductError(f"provider {provider_id} failed healthCheck")
            agent_id = f"{provider_id}-{uuid4().hex[:8]}"
            record = AgentRecord(
                agent_id, info.display_name, provider_id, AgentRole.WORKER,
                AgentStatus.IDLE, AgentHealth.HEALTHY,
                session_id=session_id, terminal_id=getattr(adapter, "agent_id", None), workspace_path=workspace,
            )
            try:
                core.add_agent(record, adapter)
                binding = core.runtime.current_binding(agent_id)
                core.registry.update(agent_id, status=AgentStatus.IDLE, health=AgentHealth.HEALTHY, session_id=binding.session_id, terminal_id=binding.terminal_id, runtime_id=binding.runtime_id, runtime_generation=binding.generation, runtime_bound_at=binding.bound_at, runtime_state="ACTIVE")
                core.beat_agent(agent_id, AgentHealth.IDLE, runtime_generation=binding.generation, terminal_id=binding.terminal_id)
                observation = getattr(getattr(adapter, "runtime", None), "create_observation", None)
                if observation is not None:
                    core.events.publish(DomainEvent.create("SessionCreateObserved", meeting_id, "CaoAgentAdapter", observation.as_dict()))
            except Exception as exc:
                try:
                    adapter.stop()
                except Exception:
                    pass
                if provider_id == "codex":
                    raise ProviderBlockedError(
                        "Codex 加入会议失败：无法保存智能体加入状态。",
                        code="CODEX_PARTICIPANT_PERSISTENCE_FAILED",
                        stage="AGENT_REGISTRY_PERSIST",
                    ) from exc
                raise
            return self.snapshot(meeting_id)

    def restart_agent(self, meeting_id: str, agent_id: str) -> dict[str, Any]:
        """Create a replacement real runtime while preserving the domain Agent ID."""
        core = self._core(meeting_id)
        agent = core.registry.get(agent_id)
        if core.meeting.meeting.status != MeetingStatus.PAUSED:
            raise ProductError("an Agent can only be restarted while the Meeting is PAUSED")
        if agent.provider == "codex" and self._uses_real_cao:
            preflight = self.runtime_preflight()
            if not preflight.usable:
                raise ProviderBlockedError(f"provider codex is BLOCKED: {preflight.reason or 'runtime preflight failed'}")
        workspace = agent.workspace_path or core.meeting.meeting.workspace_id or str(Path.cwd())
        session_id = self._new_provider_session_id(meeting_id, agent.provider, recovery=True)
        adapter = self._adapter_factory(provider=agent.provider, session_id=session_id, working_directory=workspace)
        if not adapter.health_check():
            try:
                adapter.stop()
            except Exception:
                pass
            raise ProductError(f"replacement Agent {agent_id} failed healthCheck")
        if agent_id in core.runtime.adapters:
            core.replace_runtime(agent_id, adapter)
        else:
            # A restored Product Shell has domain Agent records but no live
            # adapter instances; controlled restart re-attaches the runtime.
            core.runtime.register(agent_id, adapter)
            binding = core.runtime.current_binding(agent_id)
            core.tasks.register_adapter(agent_id, adapter, generation=binding.generation)
            core.registry.update(agent_id, session_id=binding.session_id, terminal_id=binding.terminal_id, runtime_id=binding.runtime_id, runtime_generation=binding.generation, runtime_bound_at=binding.bound_at, runtime_state="ACTIVE", status=AgentStatus.IDLE, health=AgentHealth.HEALTHY, current_task_id=None)
            core.beat_agent(agent_id, AgentHealth.IDLE, runtime_generation=binding.generation, terminal_id=binding.terminal_id)
        observation = getattr(getattr(adapter, "runtime", None), "create_observation", None)
        if observation is not None:
            core.events.publish(DomainEvent.create("SessionCreateObserved", meeting_id, "CaoAgentAdapter", observation.as_dict()))
        return self.snapshot(meeting_id)

    def start_meeting(self, meeting_id: str) -> dict[str, Any]:
        core = self._core(meeting_id)
        if core.meeting.meeting.status not in {MeetingStatus.CREATED, MeetingStatus.READY}:
            raise MeetingLifecycleConflict(
                "MEETING_NOT_STARTABLE",
                f"start requires CREATED or READY meeting, got {core.meeting.meeting.status.value}",
            )
        brain = self._brains.get(meeting_id)
        if brain is None or not brain.health_check():
            raise ProductError("Brain is not healthy")
        for agent in core.registry.all():
            if agent.agent_id not in core.runtime.adapters:
                self._restore_agent_runtime_for_start(core, agent)
            health = self._poll_agent(core, agent.agent_id)
            if health != AgentHealth.HEALTHY:
                if agent.provider.lower() == "codex":
                    raise ProductError(
                        self._codex_readiness_message("CODEX_RUNTIME_UNHEALTHY"),
                        code="CODEX_RUNTIME_UNHEALTHY",
                        stage="START_HEALTH_CHECK",
                    )
                raise ProductError(f"agent {agent.agent_id} is not healthy")
        core.mark_ready()
        core.start()
        self._start_monitor(meeting_id)
        return self.snapshot(meeting_id)

    def _register_restored_runtime(self, core: MeetingCore, agent: AgentRecord, adapter: Any) -> None:
        """Bind a healthy runtime to a persisted participant after an explicit Start action."""
        core.runtime.register(agent.agent_id, adapter)
        binding = core.runtime.current_binding(agent.agent_id)
        core.tasks.register_adapter(agent.agent_id, adapter, generation=binding.generation)
        core.registry.update(
            agent.agent_id,
            status=AgentStatus.IDLE,
            health=AgentHealth.HEALTHY,
            session_id=binding.session_id,
            terminal_id=binding.terminal_id,
            runtime_id=binding.runtime_id,
            runtime_generation=binding.generation,
            runtime_bound_at=binding.bound_at,
            runtime_state="ACTIVE",
            current_task_id=None,
        )
        core.beat_agent(
            agent.agent_id,
            AgentHealth.IDLE,
            runtime_generation=binding.generation,
            terminal_id=binding.terminal_id,
        )
        observation = getattr(getattr(adapter, "runtime", None), "create_observation", None)
        if observation is not None:
            core.events.publish(DomainEvent.create("SessionCreateObserved", core.meeting.meeting.meeting_id, "CaoAgentAdapter", observation.as_dict()))

    def _restore_agent_runtime_for_start(self, core: MeetingCore, agent: AgentRecord) -> None:
        """Reattach or recreate only on the operator's explicit Start action.

        A restart never resumes dispatch. This path applies only while the
        Meeting is still CREATED/READY and Start is explicitly requested.
        """
        if self._uses_real_cao and agent.provider.lower() == "codex" and agent.session_id and agent.terminal_id:
            previous = CaoAgentAdapter(CaoRuntimeAgent(
                self.cao_base_url,
                agent.terminal_id,
                agent.session_id,
                agent_id=agent.agent_id,
            ))
            try:
                previous_healthy = previous.health_check()
            except Exception as exc:
                raise ProductError(
                    "Codex 加入会议失败：无法确认先前运行时会话状态。",
                    code="CODEX_PREVIOUS_RUNTIME_NOT_STOPPED",
                    stage="START_RUNTIME_RESTORE",
                ) from exc
            if previous_healthy:
                try:
                    self._register_restored_runtime(core, agent, previous)
                    return
                except Exception as exc:
                    raise ProductError(
                        "Codex 加入会议失败：无法恢复先前运行时会话。",
                        code="CODEX_PREVIOUS_RUNTIME_NOT_STOPPED",
                        stage="START_RUNTIME_RESTORE",
                    ) from exc
            # This exact persisted terminal was checked and found unhealthy;
            # best-effort graceful exit prevents an abandoned terminal from
            # continuing alongside the new, operator-requested runtime.
            try:
                previous.stop()
            except Exception as exc:
                raise ProductError(
                    "Codex 加入会议失败：无法安全停止先前的运行时会话。",
                    code="CODEX_PREVIOUS_RUNTIME_NOT_STOPPED",
                    stage="START_RUNTIME_RESTORE",
                ) from exc

        if self._uses_real_cao and agent.provider.lower() == "codex":
            _runtime_status, code, _reason = self._codex_runtime_readiness()
            if code is not None:
                raise ProductError(self._codex_readiness_message(code), code=code, stage="START_RUNTIME_PREFLIGHT")
            try:
                resolve_codex_model()
            except CodexModelConfigurationError as exc:
                raise ProductError(self._codex_readiness_message("CODEX_MODEL_UNSUPPORTED"), code="CODEX_MODEL_UNSUPPORTED", stage="PROVIDER_CONFIGURATION") from exc
        elif self._uses_real_cao:
            raise ProductError("恢复该智能体运行时需要单独验证对应提供商。", code="PROVIDER_RUNTIME_RESTORE_UNSUPPORTED", stage="START_RUNTIME_RESTORE")

        workspace = agent.workspace_path or core.meeting.meeting.workspace_id or str(Path.cwd())
        if not Path(workspace).is_dir():
            code = "WORKSPACE_UNAVAILABLE"
            message = self._codex_readiness_message(code) if agent.provider.lower() == "codex" else "agent workspace is unavailable"
            raise ProductError(message, code=code, stage="WORKSPACE_PREFLIGHT")
        session_id = self._new_provider_session_id(core.meeting.meeting.meeting_id, agent.provider, recovery=True)
        try:
            adapter = self._adapter_factory(provider=agent.provider.lower(), session_id=session_id, working_directory=workspace)
        except Exception as exc:
            if agent.provider.lower() == "codex":
                code = "CODEX_RUNTIME_SESSION_CREATE_FAILED"
                raise ProductError(self._codex_readiness_message(code), code=code, stage="CAO_SESSION_CREATE") from exc
            raise ProductError("无法恢复智能体运行时。", code="PROVIDER_RUNTIME_RESTORE_FAILED", stage="START_RUNTIME_RESTORE") from exc
        try:
            healthy = bool(adapter.health_check())
        except Exception as exc:
            healthy = False
            health_error = exc
        else:
            health_error = None
        if not healthy:
            try:
                adapter.stop()
            except Exception:
                pass
            code = "CODEX_RUNTIME_UNHEALTHY" if agent.provider.lower() == "codex" else "PROVIDER_RUNTIME_UNHEALTHY"
            message = self._codex_readiness_message(code) if agent.provider.lower() == "codex" else "恢复的智能体运行时未通过健康检查。"
            raise ProductError(message, code=code, stage="START_RUNTIME_HEALTH") from health_error
        self._register_restored_runtime(core, agent, adapter)

    def create_task(self, meeting_id: str, title: str, instruction: str, agent_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            core = self._core(meeting_id)
            if core.meeting.meeting.status in {MeetingStatus.COMPLETED, MeetingStatus.FAILED}:
                raise MeetingLifecycleConflict("MEETING_TERMINAL", "会议已结束，不能再创建任务。")
            task = core.create_task(title, instruction)
            if agent_id:
                core.tasks.assign_task(task.task_id, agent_id)
            return self.snapshot(meeting_id)

    def dispatch_task(self, meeting_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            core = self._core(meeting_id)
            # Preserve the safety-engine path for PAUSED/RECOVERING meetings:
            # it must record a blocked dispatch and retain the global breaker
            # evidence.  CREATED/READY are pre-run lifecycle states and must
            # fail before any provider output is read or task is sent.
            if core.meeting.meeting.status not in {
                MeetingStatus.RUNNING, MeetingStatus.PAUSED, MeetingStatus.RECOVERING,
            }:
                raise MeetingLifecycleConflict(
                    "MEETING_NOT_RUNNING",
                    f"dispatch requires RUNNING meeting, got {core.meeting.meeting.status.value}",
                )
            task = core.tasks.get(task_id)
            if task.assigned_agent_id is None:
                raise ProductError("task must be assigned to an Agent before dispatch")
            adapter = core.runtime.adapters.get(task.assigned_agent_id)
            if adapter is None:
                raise ProductError("assigned Agent runtime is not available")
            try:
                baseline = adapter.get_output()
            except Exception:
                baseline = ""
            # A CAO terminal can report a quota banner in output before its status
            # endpoint changes. Poll again after reading output so QUOTA_EXHAUSTED
            # opens the global breaker before TaskEngine can dispatch.
            self._poll_agent(core, task.assigned_agent_id)
            result = core.tasks.dispatch_task(task_id)
            self._start_task_watcher(meeting_id, result.task_id, baseline)
            return self.snapshot(meeting_id)

    @staticmethod
    def _manual_gpt_result_id(meeting_id: str, task: Any) -> str:
        identity = "\0".join((meeting_id, task.task_id, task.completed_at or "", task.result or ""))
        return hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()

    def _manual_gpt_allowed_actions(self, core: MeetingCore, task: Any) -> tuple[str, ...]:
        if core.meeting.meeting.status != MeetingStatus.RUNNING or core.safety.breaker.stop_dispatch:
            return ()
        allowed: list[str] = []
        if task.status == TaskStatus.COMPLETED and not task.accepted:
            allowed.append("ACCEPT")
        if task.status == TaskStatus.COMPLETED and task.assigned_agent_id:
            try:
                if core.registry.get(task.assigned_agent_id).health == AgentHealth.HEALTHY:
                    allowed.append("REWORK")
            except KeyError:
                pass
        allowed.append("PAUSE")
        other_unfinished = any(
            item.task_id != task.task_id and item.status in {
                TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.DISPATCHED,
                TaskStatus.WORKING, TaskStatus.WAITING, TaskStatus.FAILED, TaskStatus.BLOCKED,
            }
            for item in core.tasks.all()
        )
        if not other_unfinished:
            allowed.append("COMPLETE_MEETING")
        return tuple(action for action in ("ACCEPT", "REWORK", "REJECT", "REVIEW", "PAUSE", "RESUME", "COMPLETE_MEETING", "DISPATCH") if action in allowed)

    @staticmethod
    def _manual_gpt_handoff_dto(record: dict[str, Any], *, include_copy_text: bool = False) -> dict[str, Any]:
        packet = record.get("packet") if isinstance(record.get("packet"), dict) else {}
        preview = record.get("stagedDecision") if isinstance(record.get("stagedDecision"), dict) else None
        result = {
            "requestId": packet.get("requestId"),
            "packetId": packet.get("packetId"),
            "meetingId": packet.get("meetingId"),
            "taskId": packet.get("taskId"),
            "resultId": packet.get("resultId"),
            "brainParticipantId": packet.get("brainParticipantId"),
            "status": record.get("status"),
            "createdAt": packet.get("createdAt"),
            "decisionTypesAllowed": list(packet.get("decisionTypesAllowed", [])),
            "validationStatus": record.get("validationStatus", "NOT_IMPORTED"),
            "validationErrorCode": record.get("validationErrorCode"),
            "copyCount": int(record.get("copyCount", 0)),
            "preview": ({
                "decisionId": preview.get("decisionId"),
                "decision": preview.get("decision"),
                "meetingId": preview.get("meetingId"),
                "taskId": preview.get("taskId"),
                "resultId": preview.get("resultId"),
                "reason": preview.get("reason"),
                "reworkInstruction": preview.get("reworkInstruction"),
            } if preview else None),
        }
        if include_copy_text and record.get("status") != "BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED":
            result["copyText"] = record.get("copyText")
        return result

    def _publish_manual_gpt_audit(self, core: MeetingCore, event_type: str, record: dict[str, Any], **extra: Any) -> None:
        packet = record.get("packet") if isinstance(record.get("packet"), dict) else {}
        payload = {
            "requestId": packet.get("requestId"),
            "packetId": packet.get("packetId"),
            "meetingId": core.meeting.meeting.meeting_id,
            "taskId": packet.get("taskId"),
            "resultId": packet.get("resultId"),
            "transport": MANUAL_GPT_TRANSPORT,
            "humanTransferred": True,
        }
        payload.update({key: value for key, value in extra.items() if key in {
            "decisionId", "decision", "status", "validationErrorCode", "validationStatus",
        }})
        core.events.publish(DomainEvent.create(event_type, core.meeting.meeting.meeting_id, "ManualGPTBrainTransport", payload))

    def create_manual_gpt_handoff(self, meeting_id: str, task_id: str) -> dict[str, Any]:
        """Create once per completed result, then return that same identity on retries."""
        core = self._core(meeting_id)
        with self._lock:
            if meeting_id in self._manual_gpt_restore_errors:
                raise ProductError("persisted Brain handoff state is unverified; Meeting remains fail-closed", code="BRAIN_HANDOFF_PERSISTENCE_UNKNOWN")
            task = core.tasks.get(task_id)
            if task.meeting_id != meeting_id:
                raise ProductError("task belongs to another Meeting", code="CROSS_MEETING_DECISION_REJECTED")
            if task.status != TaskStatus.COMPLETED or task.result is None:
                raise ProductError("Brain handoff requires a completed Task result", code="BRAIN_REQUEST_NOT_READY")
            result_id = self._manual_gpt_result_id(meeting_id, task)
            existing = self.store.find_manual_gpt_handoff_for_result(meeting_id, task_id, result_id)
            if existing is not None:
                if existing.get("status") != "CONSUMED":
                    core.set_waiting_for_human_handoff(True)
                return self._manual_gpt_handoff_dto(existing, include_copy_text=True)
            current = self.store.get_current_manual_gpt_handoff(meeting_id)
            if current is not None:
                raise ProductError("another Brain handoff is still open", code="BRAIN_HANDOFF_ALREADY_OPEN")
            binding = self._brain_participants[meeting_id]
            request_id, packet_id = str(uuid4()), str(uuid4())
            nonce = secrets.token_urlsafe(24)
            created_at = utc_now()
            actions = self._manual_gpt_allowed_actions(core, task)
            if not actions:
                raise ProductError("Meeting is not in a state that can request a Brain decision", code="BRAIN_REQUEST_STATE_INVALID")
            core.set_waiting_for_human_handoff(True)
            try:
                packet = self.manual_gpt_transport.create_packet(
                    meeting_id=meeting_id,
                    task_id=task_id,
                    result_id=result_id,
                    brain_participant_id=binding.participant_id,
                    decision_types_allowed=actions,
                    task_summary=task.title,
                    task_instruction=task.instruction,
                    agent_result=task.result,
                    current_task_state=task.status.value,
                    current_meeting_state=core.meeting.meeting.status.value,
                    validation_context={"taskStatus": task.status.value, "taskAccepted": task.accepted},
                    safety_context={"circuitState": core.safety.breaker.state.value, "stopDispatch": core.safety.breaker.stop_dispatch},
                    request_id=request_id,
                    packet_id=packet_id,
                    nonce=nonce,
                    created_at=created_at,
                )
            except ManualGPTHandoffError as exc:
                if exc.code != "BRAIN_PACKET_SENSITIVE_CONTENT_BLOCKED":
                    core.safety.observe_system_failure("manual-gpt-handoff", exc.code)
                    raise ProductError("Brain Packet could not be created safely", code=exc.code, stage="BRAIN_PACKET") from None
                record = {
                    "packet": {
                        "schemaVersion": "ai-meeting-room.brain-packet.v1",
                        "packetId": packet_id, "requestId": request_id,
                        "meetingId": meeting_id, "taskId": task_id,
                        "resultId": result_id, "brainParticipantId": binding.participant_id,
                        "createdAt": created_at, "nonce": nonce,
                        "decisionTypesAllowed": list(actions),
                    },
                    "copyText": None,
                    "status": exc.code,
                    "validationStatus": "BLOCKED",
                    "validationErrorCode": exc.code,
                    "stagedDecision": None,
                    "consumedDecisionId": None,
                }
                try:
                    self.store.save_manual_gpt_handoff_request(record)
                except Exception as persist_exc:
                    core.safety.observe_system_failure("manual-gpt-handoff-persistence", type(persist_exc).__name__)
                    raise ProductError("sensitive-content block could not be persisted; Meeting paused for safety", code="BRAIN_HANDOFF_PERSISTENCE_FAILED") from None
                self._publish_manual_gpt_audit(core, "BrainHandoffPacketBlocked", record, status=exc.code, validationErrorCode=exc.code)
                return self._manual_gpt_handoff_dto(record)

            brain_request = BrainRequest(
                brain_request_id=request_id,
                meeting_id=meeting_id,
                task_id=task_id,
                prompt=packet.copy_text,
                allowed_actions=actions,
                created_at=created_at,
            )
            record = {
                "packet": packet.data,
                "brainRequest": {
                    "brainRequestId": brain_request.brain_request_id,
                    "meetingId": brain_request.meeting_id,
                    "taskId": brain_request.task_id,
                    "prompt": brain_request.prompt,
                    "allowedActions": list(brain_request.allowed_actions),
                    "createdAt": brain_request.created_at,
                },
                "copyText": packet.copy_text,
                "status": WAITING_FOR_HUMAN_HANDOFF,
                "validationStatus": "NOT_IMPORTED",
                "validationErrorCode": None,
                "stagedDecision": None,
                "consumedDecisionId": None,
                "copyCount": 0,
            }
            try:
                self.store.save_manual_gpt_handoff_request(record)
            except Exception as exc:
                core.safety.observe_system_failure("manual-gpt-handoff-persistence", type(exc).__name__)
                raise ProductError("Brain handoff persistence failed; Meeting paused for safety", code="BRAIN_HANDOFF_PERSISTENCE_FAILED") from None
            self._publish_manual_gpt_audit(core, "BrainHandoffPacketGenerated", record, status=WAITING_FOR_HUMAN_HANDOFF)
            return self._manual_gpt_handoff_dto(record, include_copy_text=True)

    def copy_manual_gpt_handoff(self, meeting_id: str, request_id: str) -> dict[str, Any]:
        core = self._core(meeting_id)
        with self._lock:
            record = self.store.get_manual_gpt_handoff_request(request_id)
            if record is None:
                raise ProductError("Brain handoff request is no longer available", code="STALE_DECISION_REJECTED")
            if record.get("packet", {}).get("meetingId") != meeting_id:
                raise ProductError("Brain handoff belongs to another Meeting", code="CROSS_MEETING_DECISION_REJECTED")
            try:
                updated = self.store.mark_manual_gpt_handoff_copied(request_id, utc_now())
            except ManualGPTHandoffStoreError as exc:
                raise ProductError("Brain Packet is no longer open for copying", code=exc.code) from None
            self._publish_manual_gpt_audit(core, "BrainHandoffPacketCopied", updated, status=updated.get("status"))
            return self._manual_gpt_handoff_dto(updated, include_copy_text=True)

    def import_manual_gpt_decision(self, meeting_id: str, request_id: str, raw_response: str) -> dict[str, Any]:
        core = self._core(meeting_id)
        with self._lock:
            record = self.store.get_manual_gpt_handoff_request(request_id)
            if record is None:
                raise ProductError("Brain handoff request is no longer available", code="STALE_DECISION_REJECTED")
            if record.get("packet", {}).get("meetingId") != meeting_id:
                raise ProductError("decision belongs to another Meeting", code="CROSS_MEETING_DECISION_REJECTED")
            if record.get("status") != WAITING_FOR_HUMAN_HANDOFF:
                code = "DUPLICATE_DECISION_REJECTED" if record.get("status") in {"VALIDATED", "APPLYING", "CONSUMED", "APPLY_FAILED"} else "INVALID_BRAIN_DECISION"
                raise ProductError("this Brain request cannot accept another response", code=code)
            if int(record.get("copyCount", 0)) < 1:
                raise ProductError("copy the current Brain Packet before importing a response", code="BRAIN_PACKET_NOT_COPIED")
            if not isinstance(raw_response, str) or len(raw_response.encode("utf-8", errors="replace")) > 32768:
                self.store.record_manual_gpt_validation_error(request_id, "INVALID_BRAIN_DECISION")
                self._publish_manual_gpt_audit(core, "BrainHandoffDecisionValidationFailed", record, status="INVALID", validationErrorCode="INVALID_BRAIN_DECISION")
                raise ProductError("GPT response must be one JSON object under 32 KB", code="INVALID_BRAIN_DECISION")
            task = core.tasks.get(str(record["packet"].get("taskId")))
            expected_result_id = self._manual_gpt_result_id(meeting_id, task)
            if task.status != TaskStatus.COMPLETED or expected_result_id != record["packet"].get("resultId"):
                self.store.record_manual_gpt_validation_error(request_id, "STALE_DECISION_REJECTED")
                self._publish_manual_gpt_audit(core, "BrainHandoffDecisionValidationFailed", record, status="INVALID", validationErrorCode="STALE_DECISION_REJECTED")
                raise ProductError("Task result changed after this Brain Packet was created", code="STALE_DECISION_REJECTED")
            try:
                decision = self.manual_gpt_transport.validate_decision(raw_response, record["packet"])
                stored = self.store.stage_manual_gpt_decision(request_id, {
                    "schemaVersion": BRAIN_DECISION_PACKET_SCHEMA,
                    "decisionId": decision.decision_id,
                    "requestId": decision.request_id,
                    "packetId": decision.packet_id,
                    "meetingId": decision.meeting_id,
                    "taskId": decision.task_id,
                    "resultId": decision.result_id,
                    "decision": decision.decision,
                    "reason": decision.reason,
                    "createdAt": decision.created_at,
                    "nonceEcho": decision.nonce_echo,
                    **({"reworkInstruction": decision.rework_instruction} if decision.rework_instruction is not None else {}),
                })
            except (ManualGPTHandoffError, ManualGPTHandoffStoreError) as exc:
                code = getattr(exc, "code", "INVALID_BRAIN_DECISION")
                self.store.record_manual_gpt_validation_error(request_id, code)
                self._publish_manual_gpt_audit(core, "BrainHandoffDecisionValidationFailed", record, status="INVALID", validationErrorCode=code)
                raise ProductError("GPT response was rejected; no Meeting or Task state was changed", code=code) from None
            self._publish_manual_gpt_audit(core, "BrainHandoffDecisionValidated", stored, status="VALIDATED", validationStatus="VALID", decisionId=decision.decision_id, decision=decision.decision)
            return self._manual_gpt_handoff_dto(stored)

    def apply_manual_gpt_decision(self, meeting_id: str, request_id: str) -> dict[str, Any]:
        core = self._core(meeting_id)
        with self._lock:
            record = self.store.get_manual_gpt_handoff_request(request_id)
            if record is None:
                raise ProductError("Brain handoff request is no longer available", code="STALE_DECISION_REJECTED")
            packet = record.get("packet", {})
            if packet.get("meetingId") != meeting_id:
                raise ProductError("decision belongs to another Meeting", code="CROSS_MEETING_DECISION_REJECTED")
            if record.get("status") != "VALIDATED" or not isinstance(record.get("stagedDecision"), dict):
                code = "DUPLICATE_DECISION_REJECTED" if record.get("status") == "CONSUMED" else "INVALID_BRAIN_DECISION"
                raise ProductError("validate a BrainDecision before applying it", code=code)
            staged = record["stagedDecision"]
            try:
                decision = self.manual_gpt_transport.validate_decision(json.dumps(staged, ensure_ascii=False), packet)
                binding = self._brain_participants[meeting_id]
                if packet.get("brainParticipantId") != binding.participant_id:
                    raise ManualGPTHandoffError("STALE_DECISION_REJECTED")
                task = core.tasks.get(decision.task_id)
                if task.status != TaskStatus.COMPLETED or self._manual_gpt_result_id(meeting_id, task) != decision.result_id:
                    raise ManualGPTHandoffError("STALE_DECISION_REJECTED")
                body = decision.as_core_body(binding.participant_id)
                if decision.decision == "ACCEPT":
                    core.safety.require_task_advancement_allowed()
                    if task.accepted:
                        raise ManualGPTHandoffError("DUPLICATE_DECISION_REJECTED")
                elif decision.decision == "REWORK":
                    core.safety.require_task_advancement_allowed()
                    if core.meeting.meeting.status != MeetingStatus.RUNNING:
                        raise ManualGPTHandoffError("STALE_DECISION_REJECTED")
                    if not task.assigned_agent_id or not decision.rework_instruction:
                        raise ManualGPTHandoffError("INVALID_BRAIN_DECISION")
                    agent = core.registry.get(task.assigned_agent_id)
                    if agent.health != AgentHealth.HEALTHY:
                        raise ManualGPTHandoffError("INVALID_BRAIN_DECISION")
                    body["targetAgentId"] = task.assigned_agent_id
                elif decision.decision == "PAUSE":
                    if core.meeting.meeting.status != MeetingStatus.RUNNING:
                        raise ManualGPTHandoffError("STALE_DECISION_REJECTED")
                elif decision.decision == "COMPLETE_MEETING":
                    if "COMPLETE_MEETING" not in packet.get("decisionTypesAllowed", []):
                        raise ManualGPTHandoffError("INVALID_BRAIN_DECISION")
                    if core.meeting.meeting.status != MeetingStatus.RUNNING:
                        raise ManualGPTHandoffError("STALE_DECISION_REJECTED")
                    unfinished = {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.WORKING, TaskStatus.WAITING, TaskStatus.FAILED, TaskStatus.BLOCKED}
                    if any(item.status in unfinished for item in core.tasks.all()) or core.safety.breaker.state.value != "CLOSED":
                        raise ManualGPTHandoffError("INVALID_BRAIN_DECISION")
                else:
                    raise ManualGPTHandoffError("INVALID_BRAIN_DECISION")
            except (ManualGPTHandoffError, DispatchBlockedError) as exc:
                code = getattr(exc, "code", "INVALID_BRAIN_DECISION")
                raise ProductError("validated BrainDecision is stale or no longer applicable", code=code) from None

            try:
                self.store.claim_manual_gpt_decision(request_id, decision.decision_id)
            except ManualGPTHandoffStoreError as exc:
                raise ProductError("BrainDecision replay or stale request rejected", code=exc.code) from None
            core.set_waiting_for_human_handoff(False)
            try:
                self.submit_brain_decision(meeting_id, body, validated_manual_gpt=True)
                consumed = self.store.complete_manual_gpt_decision(request_id, decision.decision_id)
                # The formal decision path takes a snapshot while the request is
                # still APPLYING.  That snapshot correctly closes the handoff
                # gate fail-closed; once CONSUMED is durably persisted, reconcile
                # the in-memory TaskEngine gate with the now-closed request.
                core.set_waiting_for_human_handoff(False)
            except Exception as exc:
                self.store.fail_manual_gpt_decision(request_id, decision.decision_id, getattr(exc, "code", "BRAIN_DECISION_APPLY_FAILED"))
                core.set_waiting_for_human_handoff(True)
                raise ProductError("BrainDecision could not be applied; task advancement remains blocked", code="BRAIN_DECISION_APPLY_FAILED") from None
            self._publish_manual_gpt_audit(core, "BrainHandoffDecisionApplied", consumed, status="CONSUMED", decisionId=decision.decision_id, decision=decision.decision)
            if decision.decision == "ACCEPT":
                self._brain_inboxes[meeting_id].resolve_task(decision.task_id)
                self._create_next_manual_gpt_handoff(core)
            elif decision.decision in {"REJECT", "REVIEW"}:
                self._create_next_manual_gpt_handoff(core)
            return self.snapshot(meeting_id)

    def _create_next_manual_gpt_handoff(self, core: MeetingCore) -> None:
        meeting_id = core.meeting.meeting.meeting_id
        for task in core.tasks.all():
            if task.status != TaskStatus.COMPLETED or task.result is None:
                continue
            result_id = self._manual_gpt_result_id(meeting_id, task)
            if self.store.find_manual_gpt_handoff_for_result(meeting_id, task.task_id, result_id) is None:
                self.create_manual_gpt_handoff(meeting_id, task.task_id)
                return

    def submit_brain_decision(self, meeting_id: str, body: dict[str, Any], *, validated_manual_gpt: bool = False) -> dict[str, Any]:
        with self._lock:
            return self._submit_brain_decision_locked(
                meeting_id, body, validated_manual_gpt=validated_manual_gpt,
            )

    def _submit_brain_decision_locked(self, meeting_id: str, body: dict[str, Any], *, validated_manual_gpt: bool = False) -> dict[str, Any]:
        core = self._core(meeting_id)
        kind = str(body.get("type", "")).upper()
        if body.get("humanTransferred") and not validated_manual_gpt:
            raise ProductError("manual GPT decisions must use validate then apply", code="INVALID_BRAIN_DECISION")
        if validated_manual_gpt and (
            body.get("transport") != MANUAL_GPT_TRANSPORT
            or body.get("humanTransferred") is not True
            or not all(body.get(key) for key in ("brainRequestId", "decisionId", "packetId", "resultId", "brainParticipantId"))
        ):
            raise ProductError("validated manual GPT identity metadata is incomplete", code="INVALID_BRAIN_DECISION")
        if kind in {"ACCEPT", "REWORK"}:
            # Validate safety before BrainInbox persistence: an operator/Brain
            # decision must not look accepted or create a rework record while
            # the Meeting is paused or any participant/runtime is unsafe.
            core.safety.require_task_advancement_allowed()
        related_task_id = body.get("relatedTaskId")
        if kind == "ACCEPT":
            if not related_task_id:
                raise ProductError("ACCEPT requires relatedTaskId")
            task = core.tasks.get(str(related_task_id))
            if task.status != TaskStatus.COMPLETED:
                raise ProductError(f"ACCEPT requires COMPLETED task, got {task.status.value}")
        pending_handoff = None if validated_manual_gpt else self.store.get_current_manual_gpt_handoff(meeting_id)
        pending_task_id = str((pending_handoff or {}).get("packet", {}).get("taskId") or "")
        if pending_handoff is not None and kind in {"ACCEPT", "REWORK"} and str(related_task_id or "") != pending_task_id:
            raise ProductError("operator decision does not match the open Brain handoff", code="WAITING_FOR_HUMAN_HANDOFF")
        inbox = self._brain_inboxes[meeting_id]
        decision = inbox.submit(
            decision_type=str(body.get("type", "")),
            target_agent_id=body.get("targetAgentId"),
            instruction=body.get("instruction"),
            related_task_id=body.get("relatedTaskId"),
            reason=str(body.get("reason") or "manual operator decision"),
            brain_request_id=body.get("brainRequestId"),
            confidence=body.get("confidence"),
            decision_id=body.get("decisionId"),
            packet_id=body.get("packetId"),
            result_id=body.get("resultId"),
            brain_participant_id=body.get("brainParticipantId"),
            transport=body.get("transport"),
            human_transferred=body.get("humanTransferred") is True,
        )
        resolve_legacy_wait = pending_handoff is not None
        if resolve_legacy_wait:
            core.set_waiting_for_human_handoff(False)
        kind = decision.type
        try:
            if kind == "ACCEPT":
                core.tasks.accept_task(str(decision.related_task_id))
                inbox.resolve_task(str(decision.related_task_id))
            elif kind == "REWORK":
                if core.meeting.meeting.status != MeetingStatus.RUNNING:
                    raise ProductError("REWORK requires a RUNNING meeting")
                target_agent_id = decision.target_agent_id
                if not target_agent_id and decision.related_task_id:
                    target_agent_id = core.tasks.get(decision.related_task_id).assigned_agent_id
                if not target_agent_id or not decision.instruction:
                    raise ProductError("REWORK requires targetAgentId and instruction")
                agent = core.registry.get(target_agent_id)
                if agent.health != AgentHealth.HEALTHY:
                    raise ProductError("REWORK target Agent is not HEALTHY")
                task = core.create_task("Rework", decision.instruction, parent_task_id=decision.related_task_id)
                core.tasks.assign_task(task.task_id, target_agent_id)
                adapter = core.runtime.adapters[target_agent_id]
                try:
                    baseline = adapter.get_output()
                except Exception:
                    baseline = ""
                core.tasks.dispatch_task(task.task_id)
                self._start_task_watcher(meeting_id, task.task_id, baseline)
            elif kind == "PAUSE":
                core.pause(decision.reason, triggered_by="ManualBrain")
            elif kind == "RESUME":
                raise ProductError("RESUME must use the health-gated Recovery operation")
            elif kind == "COMPLETE_MEETING":
                core.complete_meeting(reason=decision.reason)
        except Exception:
            if resolve_legacy_wait:
                core.set_waiting_for_human_handoff(True)
            raise
        if resolve_legacy_wait:
            pending_packet = pending_handoff.get("packet", {})
            handoff_task_id = str(pending_packet.get("taskId") or decision.related_task_id or "")
            if handoff_task_id:
                resolved = self.store.close_manual_gpt_handoff_by_operator(meeting_id, handoff_task_id, decision.decision_id)
                if resolved is not None:
                    self._publish_manual_gpt_audit(core, "BrainHandoffSupersededByOperator", resolved, status="OPERATOR_DECISION_APPLIED", decisionId=decision.decision_id, decision=decision.type)
                    self._brain_inboxes[meeting_id].resolve_task(handoff_task_id)
        return self.snapshot(meeting_id)

    def recover(self, meeting_id: str) -> dict[str, Any]:
        core = self._core(meeting_id)
        workspace = core.meeting.meeting.workspace_id
        def cao_health() -> bool:
            try:
                with urllib.request.urlopen(f"{self.cao_base_url}/health", timeout=5) as response:
                    return response.status == 200
            except Exception:
                return False
        def workspace_health() -> bool:
            if not workspace:
                return False
            try:
                return WorkspaceManager(workspace).health_check()
            except WorkspaceError:
                return Path(workspace).is_dir()
        binding = self._brain_participants.get(meeting_id)
        circuit_snapshot = core.safety.breaker.snapshot()
        persisted_participant_id, _persisted_runtime_identity = self._persisted_brain_failure_identity(meeting_id)
        expected_brain_participant_id = (
            circuit_snapshot.get("brainParticipantId") or persisted_participant_id
            if circuit_snapshot.get("participantType") == "BRAIN"
            else (binding.participant_id if binding is not None else None)
        )
        def brain_health() -> bool:
            if binding is None or expected_brain_participant_id is None:
                return False
            if circuit_snapshot.get("participantType") == "BRAIN" and not circuit_snapshot.get("brainParticipantId"):
                return False
            return self._brain_binding_is_healthy(binding, str(expected_brain_participant_id))
        recovered = core.recover(cao_health_check=cao_health, workspace_health_check=workspace_health, brain_health_check=brain_health)
        if recovered:
            self._start_monitor(meeting_id)
        recovery_error = None
        if not recovered:
            for agent in core.registry.all():
                if agent.provider == "codex" and agent.health != AgentHealth.HEALTHY:
                    recovery_error = f"Codex health check failed: {getattr(core.runtime.adapters.get(agent.agent_id), 'runtime', None) and getattr(core.runtime.adapters[agent.agent_id].runtime, 'error', None) or agent.health.value}"
                    break
            recovery_error = recovery_error or "Recovery health checks failed"
        return self.snapshot(meeting_id) | {"recovered": recovered, "recoveryError": recovery_error}

    def pause(self, meeting_id: str, reason: str) -> dict[str, Any]:
        core = self._core(meeting_id)
        core.pause(reason, triggered_by="operator")
        return self.snapshot(meeting_id)

    def complete_meeting(self, meeting_id: str, *, reason: str = "operator completed meeting") -> dict[str, Any]:
        """Complete a Meeting through MeetingCore; never conflates with ACCEPT."""
        with self._lock:
            core = self._core(meeting_id)
            core.complete_meeting(reason=reason)
            return self.snapshot(meeting_id)

    def export_meeting_summary(self, meeting_id: str, *, action: str = "preview") -> dict[str, Any]:
        """Return a safe Markdown summary and audit the export metadata only.

        The document is assembled from the live MeetingCore, TaskEngine,
        SafetyEngine checkpoint, and durable event/Brain records.  The audit
        event intentionally contains counts and a content hash, never the
        Markdown body itself.
        """
        normalized_action = str(action or "preview").strip().lower()
        if normalized_action not in {"preview", "copy"}:
            raise ProductError(
                "summary export action must be preview or copy",
                code="MEETING_SUMMARY_EXPORT_ACTION_INVALID",
                stage="MEETING_SUMMARY_EXPORT",
            )
        with self._lock:
            core = self._core(meeting_id)
            binding = self._brain_participants.get(meeting_id)
            participant = self._brain_participant_dto(meeting_id) if binding is not None else self.store.get_brain_participant(meeting_id)
            document = MeetingSummaryExporter().build(
                meeting=core.meeting.meeting,
                agents=core.registry.all(),
                tasks=core.tasks.all(),
                safety=core.safety.breaker.snapshot(),
                events=self.store.list_events(meeting_id, limit=None),
                brain_events=self.store.list_brain_events(meeting_id, limit=None),
                brain_decisions=self.store.list_brain_decisions(meeting_id, limit=None),
                brain_participant=participant,
                brain_transport=(binding.provider if binding is not None else MANUAL_GPT_TRANSPORT),
            )
            generated_at = utc_now()
            metadata = {
                **document.metadata,
                "meetingId": meeting_id,
                "action": normalized_action,
                "generatedAt": generated_at,
            }
            # This is deliberately metadata-only.  In particular, neither
            # the Markdown body nor any task/decision text is written to the
            # audit event payload.
            core.events.publish(DomainEvent.create("MEETING_SUMMARY_EXPORTED", meeting_id, "ProductShell", metadata))
            return {
                "ok": True,
                "meetingId": meeting_id,
                "format": "markdown",
                "markdown": document.markdown,
                "metadata": metadata,
            }

    def close(self) -> None:
        """Stop background watchers and release runtime adapters for local shutdown."""
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                monitor_stops = list(self._monitor_stop.values())
                task_stops = list(self._task_stop.values())
                monitor_threads = list(self._monitor_threads.values())
                task_threads = list(self._task_threads.values())
                adapters = list({id(adapter): adapter for core in self._cores.values() for adapter in core.runtime.adapters.values()}.values())

            for stop in monitor_stops + task_stops:
                stop.set()

            shutdown_errors: list[str] = []
            # Stop runtimes before joining so a provider call currently in a
            # watcher can be released. The store remains valid until every
            # app-owned watcher has terminated.
            for adapter in adapters:
                try:
                    adapter.stop()
                except Exception as exc:
                    shutdown_errors.append(f"adapter.stop:{type(exc).__name__}")

            current_thread = threading.current_thread()
            for thread in monitor_threads + task_threads:
                if thread is current_thread:
                    shutdown_errors.append("close called from an owned watcher thread")
                    continue
                thread.join()

            try:
                self.real_chrome_brain.stop(
                    reason_code="PRODUCT_SHELL_SHUTDOWN",
                    caller="Phase2Application.close",
                )
            except Exception as exc:
                shutdown_errors.append(f"brain.stop:{type(exc).__name__}")

            if shutdown_errors:
                raise RuntimeError("application shutdown incomplete: " + ", ".join(shutdown_errors))

    def snapshot(self, meeting_id: str) -> dict[str, Any]:
        core = self._core(meeting_id)
        with self._lock:
            for agent in core.registry.all():
                if agent.agent_id in core.runtime.adapters and core.meeting.meeting.status not in {MeetingStatus.COMPLETED, MeetingStatus.FAILED}:
                    self._poll_agent(core, agent.agent_id)
            meeting = model_dict(core.meeting.meeting)
            agents = [model_dict(agent) for agent in core.registry.all()]
            tasks = [model_dict(task) for task in core.tasks.all()]
            events = []
            for row in self.store.list_events(meeting_id):
                item = dict(row)
                try:
                    import json
                    item["payload"] = json.loads(item["payload"])
                except Exception:
                    pass
                events.append(item)
            circuit = core.safety.breaker.snapshot()
            workspace = self._workspace_snapshot(meeting.get("workspace_id"))
            handoff_record = None
            handoff_read_error = self._manual_gpt_restore_errors.get(meeting_id)
            if not handoff_read_error:
                try:
                    handoff_record = self.store.get_current_manual_gpt_handoff(meeting_id)
                except Exception as exc:
                    handoff_read_error = type(exc).__name__
                    self._manual_gpt_restore_errors[meeting_id] = handoff_read_error
                    core.set_waiting_for_human_handoff(True)
                    core.safety.observe_system_failure(
                        "manual-gpt-handoff-read",
                        f"persisted Brain handoff state could not be read ({handoff_read_error})",
                    )
            if handoff_record is not None:
                core.set_waiting_for_human_handoff(True)
            handoff_dto = (
                {"status": "BRAIN_HANDOFF_PERSISTENCE_UNKNOWN", "meetingId": meeting_id, "errorClass": handoff_read_error}
                if handoff_read_error
                else (self._manual_gpt_handoff_dto(handoff_record) if handoff_record else None)
            )
            brain_events = self.store.list_brain_events(meeting_id)
            decisions = self.store.list_brain_decisions(meeting_id)
            manual_waiting = bool(handoff_read_error or (handoff_record and handoff_record.get("status") != "VALIDATED"))
            return {
                "meeting": meeting, "agents": agents, "tasks": tasks,
                "events": events, "circuit": circuit, "workspace": workspace,
                "brainInbox": brain_events, "brainDecisions": decisions,
                "brain": {"name": type(self._brains[meeting_id]).__name__ if meeting_id in self._brains else "UNKNOWN", "isAi": isinstance(self._brains.get(meeting_id), ChatGPTWebBrainBridge), "health": self._brains.get(meeting_id).health_check() if meeting_id in self._brains else False, "state": WAITING_FOR_HUMAN_HANDOFF if manual_waiting else ("DECISION_VALIDATED" if handoff_record else (getattr(self._brains.get(meeting_id), "state", None) and getattr(self._brains[meeting_id], "state").value))},
                "brainParticipant": self._brain_participant_dto(meeting_id),
                "brainHandoff": handoff_dto,
                "brainTransport": MANUAL_GPT_TRANSPORT,
                "gptWebAutomation": {
                    "default": False,
                    "status": "EXPERIMENTAL_BLOCKED_EXTERNAL_CHALLENGE",
                },
                "providers": [provider.as_dict() for provider in self.catalog.all()],
            }

    @staticmethod
    def _workspace_snapshot(workspace: str | None) -> dict[str, Any]:
        if not workspace:
            return {"path": None, "exists": False, "health": False, "status": "UNKNOWN", "diff": ""}
        path = Path(workspace)
        status = ""
        diff = ""
        health = path.is_dir()
        try:
            manager = WorkspaceManager(path)
            health = manager.health_check()
            status = manager.get_status(path)
            diff = manager.get_diff(path)
        except Exception:
            pass
        return {"path": str(path), "exists": path.is_dir(), "health": health, "status": status, "diff": diff}

    def _start_monitor(self, meeting_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("application is shutting down")
            if meeting_id in self._monitor_threads and self._monitor_threads[meeting_id].is_alive():
                return
            stop = threading.Event()
            self._monitor_stop[meeting_id] = stop
            thread = threading.Thread(target=self._monitor_loop, args=(meeting_id, stop), daemon=True, name=f"meeting-monitor-{meeting_id[:8]}")
            self._monitor_threads[meeting_id] = thread
            thread.start()

    def _monitor_loop(self, meeting_id: str, stop: threading.Event) -> None:
        while not stop.wait(self.monitor_poll_interval_seconds):
            try:
                self._monitor_once(meeting_id)
            except BaseException as exc:
                if stop.is_set():
                    return
                with self._lock:
                    core = self._cores.get(meeting_id)
                if core is not None:
                    core.safety.observe_system_failure(
                        "heartbeat-monitor",
                        f"monitor cycle failed: {type(exc).__name__}",
                    )
                return

    def _monitor_once(self, meeting_id: str) -> None:
        """Poll only joined participants and fail closed on stale identity/heartbeat."""
        core = self._core(meeting_id)
        if core.meeting.meeting.status != MeetingStatus.RUNNING:
            return
        participants = {agent.agent_id: agent for agent in core.registry.all()}

        for agent in participants.values():
            try:
                record = core.heartbeat.get(agent.agent_id)
                binding = core.runtime.current_binding(agent.agent_id)
            except (KeyError, RuntimeError):
                core.safety.observe_agent_failure(
                    agent.agent_id,
                    "heartbeat identity unavailable for joined participant",
                    AgentHealth.UNKNOWN,
                )
                return
            if (
                record.provider != agent.provider
                or record.runtime_generation != binding.generation
                or record.terminal_id != binding.terminal_id
            ):
                core.safety.observe_agent_failure(
                    agent.agent_id,
                    "heartbeat runtime identity mismatch",
                    AgentHealth.UNKNOWN,
                )
                return

        timed_out = core.heartbeat.timed_out(grace_seconds=self.monitor_poll_interval_seconds)
        for agent_id in timed_out:
            if agent_id not in participants:
                continue
            core.safety.observe_agent_failure(agent_id, "heartbeat timeout", AgentHealth.LOST)
            return

        for agent in participants.values():
            try:
                self._poll_agent(core, agent.agent_id)
            except Exception as exc:
                core.safety.observe_agent_failure(
                    agent.agent_id,
                    f"provider health check raised: {type(exc).__name__}",
                    AgentHealth.UNKNOWN,
                )
                return
            if core.safety.breaker.stop_dispatch:
                return

    def _start_task_watcher(self, meeting_id: str, task_id: str, baseline: str = "") -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("application is shutting down")
            if task_id in self._task_threads and self._task_threads[task_id].is_alive():
                return
            stop = threading.Event()
            self._task_stop[task_id] = stop
            thread = threading.Thread(target=self._watch_task, args=(meeting_id, task_id, baseline, stop), daemon=True, name=f"task-watcher-{task_id[:8]}")
            self._task_threads[task_id] = thread
            thread.start()

    def _watch_task(self, meeting_id: str, task_id: str, baseline: str, stop: threading.Event) -> None:
        if stop.is_set():
            return
        core = self._core(meeting_id)
        task = core.tasks.get(task_id)
        agent_id = task.assigned_agent_id
        if not agent_id:
            return
        adapter = core.runtime.adapters.get(agent_id)
        if adapter is None:
            return
        binding = core.runtime.current_binding(agent_id)
        generation = binding.generation
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline and not stop.is_set():
            try:
                if not core.runtime.is_current(agent_id, generation, adapter):
                    if stop.is_set():
                        return
                    core.events.publish(DomainEvent.create("STALE_RUNTIME_EVENT_IGNORED", meeting_id, "TaskWatcher", {"agentId": agent_id, "runtimeGeneration": generation, "terminalId": binding.terminal_id}))
                    return
                output = adapter.get_output()
                if stop.is_set():
                    return
                health = self._poll_agent(core, agent_id)
                if stop.is_set():
                    return
                runtime = getattr(adapter, "runtime", adapter)
                # A provider-owned prompt (for example, a model/rate-limit
                # choice) is not a settled Agent result.  An unattended
                # Meeting must pause immediately instead of leaving its task
                # WORKING until the long watcher timeout expires.
                if (
                    getattr(runtime, "raw_status", "") in {"waiting_user_answer", "waiting"}
                    or core.registry.get(agent_id).status == AgentStatus.WAITING
                ):
                    core.safety.observe_agent_failure(
                        agent_id, "agent requires interactive input: WAITING", AgentHealth.UNKNOWN,
                    )
                    return
                # A watcher may still be waiting for output when another
                # participant trips the global breaker. Stop observing this
                # task before publishing output or attempting any task write.
                if core.safety.breaker.stop_dispatch or core.meeting.meeting.status != MeetingStatus.RUNNING:
                    return
                raw_status = getattr(runtime, "raw_status", "")
                if output != baseline:
                    core.events.publish(DomainEvent.create("AgentOutputProduced", meeting_id, "TaskWatcher", {"agentId": agent_id, "taskId": task_id, "outputChars": len(output)}))
                    baseline = output
                # A raw CAO ``completed``/``idle`` status is not a formal
                # result boundary.  Only the CAORuntime's settled-turn
                # predicate may authorize TaskEngine completion.
                # Prefer the formal adapter contract.  The nested-runtime
                # fallback keeps older test adapters compatible while real CAO
                # traffic crosses CaoAgentAdapter explicitly.
                turn_complete = bool(getattr(adapter, "task_completion_observed", False))
                if not turn_complete:
                    turn_complete = bool(getattr(runtime, "task_completion_observed", False))
                if turn_complete and output.strip():
                    # CAO's ``mode=last`` output may be a live terminal
                    # viewport rather than a settled Agent Result.  In
                    # particular, instruction echo plus a trailing
                    # ``Working (... esc to interrupt)`` line means the
                    # provider is still executing.  Keep polling through the
                    # formal RuntimeCoordinator/CAORuntime boundary; never
                    # persist that intermediate viewport as COMPLETED.
                    instruction = str(getattr(runtime, "turn_input", "") or "")
                    if not is_unsettled_terminal_output(
                        output,
                        instruction=instruction,
                        settled_lifecycle_observed=True,
                    ):
                        with self._lock:
                            try:
                                if core.tasks.get(task_id).status != TaskStatus.WORKING:
                                    return
                                core.tasks.complete_task(task_id, output[-20000:])
                            except DispatchBlockedError:
                                return
                            core.set_waiting_for_human_handoff(True)
                            core.registry.update(agent_id, current_task_id=None, status=AgentStatus.IDLE, health=AgentHealth.HEALTHY)
                            try:
                                self.create_manual_gpt_handoff(meeting_id, task_id)
                            except Exception as exc:
                                # The packet failure is recorded by the transport/store path.
                                # Keep the normal-wait advancement gate closed if that path fails.
                                core.set_waiting_for_human_handoff(True)
                                if not isinstance(exc, ProductError):
                                    core.safety.observe_system_failure("manual-gpt-handoff", type(exc).__name__)
                        return
                if health in {AgentHealth.ERROR, AgentHealth.LOST, AgentHealth.UNKNOWN}:
                    # _poll_agent has already opened the circuit. Preserve the
                    # in-flight task record; it must not advance after failure.
                    return
            except Exception as exc:
                if stop.is_set():
                    return
                runtime = getattr(adapter, "runtime", adapter)
                record_error = getattr(runtime, "record_task_error", None)
                if callable(record_error):
                    record_error(exc)
                classification = getattr(runtime, "turn_failure_classification", None)
                reason = f"task watcher failed: {classification or type(exc).__name__} ({type(exc).__name__})"
                core.safety.observe_agent_failure(agent_id, reason, AgentHealth.UNKNOWN)
                return
            if stop.wait(1.0):
                return
        if not stop.is_set() and core.tasks.get(task_id).status in {TaskStatus.WORKING, TaskStatus.WAITING, TaskStatus.DISPATCHED}:
            runtime = getattr(adapter, "runtime", adapter)
            record_timeout = getattr(runtime, "record_task_timeout", None)
            if callable(record_timeout):
                record_timeout()
            classification = getattr(runtime, "turn_failure_classification", None)
            reason = f"task watcher timeout: {classification}" if classification else "task watcher timeout"
            core.safety.observe_agent_failure(agent_id, reason, AgentHealth.LOST)
