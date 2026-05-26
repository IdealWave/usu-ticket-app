import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


DEFAULT_USM_EMAIL = "norbert.nutzer@usu.com"
SUCCESS_RETURN_CODES = {"0", "00"}
PRIORITY_USM_IMPACT_VALUES = {
    "critical": "1 Severe",
    "high": "2 High",
    "medium": "3 Medium",
    "low": "4 Low",
}
PRIORITY_USM_URGENCY_VALUES = {
    "critical": "1 Critical",
    "high": "2 High",
    "medium": "3 Medium",
    "low": "4 Low",
}


WINDOWS_POST_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$body = [Console]::In.ReadToEnd()

try {
    $response = Invoke-WebRequest `
        -Uri $env:USM_EXECWF_URL `
        -Method Post `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 30 `
        -UseBasicParsing

    if ($null -ne $response.Content) {
        [Console]::Out.Write($response.Content)
    }

    exit 0
} catch {
    [Console]::Error.Write($_.Exception.Message)
    exit 1
}
"""


class USUSMServiceError(Exception):
    """Raised when the USU SM service cannot complete a requested operation."""


@dataclass
class ServiceResult:
    action: str
    provider: str
    status: str
    message: str
    external_id: str | None = None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def setting(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def required_setting(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise USUSMServiceError(f"Missing required USU SM setting: {name}")
    return value


def usm_value_for_priority(
    priority: str | None,
    values: dict[str, str],
    fallback_setting: str,
) -> str:
    normalized = (priority or "").strip().lower()
    return values.get(normalized, setting(fallback_setting, "3 Medium"))


def service_mode() -> str:
    return setting("USM_SERVICE_MODE", "mock").strip().lower() or "mock"


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def is_loopback_url(url: str) -> bool:
    hostname = urlparse(url).hostname
    return hostname in {"localhost", "127.0.0.1", "::1"}


def parse_response_body(content: bytes | str) -> dict:
    if not content:
        return {}

    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    else:
        text = content

    try:
        parsed = json.loads(text)
    except ValueError:
        return {"message": text}

    if isinstance(parsed, dict):
        return parsed

    return {"message": parsed}


def has_api_credentials() -> bool:
    required = ("USM_ACCESS_TOKEN", "USM_USERNAME", "USM_PASSWORD")
    return all(os.getenv(name) for name in required)


def usm_error_message(response: dict, action: str) -> str | None:
    return_code = response.get("returnCode")
    if return_code is None:
        return None

    normalized = str(return_code).strip()
    if normalized in SUCCESS_RETURN_CODES:
        return None

    message = response.get("message") or response.get("returnMessage") or response.get("error")
    if message:
        return f"USU SM rejected {action} with returnCode {normalized}: {message}"

    return f"USU SM rejected {action} with returnCode {normalized}."


class MockUSUSMService:
    provider = "mock"

    def _result(self, action: str, data: dict | None = None) -> ServiceResult:
        external_id = f"MOCK-{action.upper()}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        return ServiceResult(
            action=action,
            provider=self.provider,
            status="mocked",
            external_id=external_id,
            message=f"Mock USU SM service accepted {action}.",
            data=data or {},
        )

    def create_ticket(self, ticket: dict) -> ServiceResult:
        return self._result("create_ticket", {"ticket": ticket})

    def update_ticket(self, ticket: dict, changes: list[tuple[str, str, str]]) -> ServiceResult:
        return self._result("update_ticket", {"ticket": ticket, "changes": changes})

    def delete_ticket(self, ticket: dict, actor: str) -> ServiceResult:
        return self._result("delete_ticket", {"ticket": ticket, "actor": actor})

    def change_status(self, ticket: dict, new_status: str, actor: str, reason: str = "") -> ServiceResult:
        return self._result(
            "change_status",
            {
                "ticketId": ticket["id"],
                "from": ticket["status"],
                "to": new_status,
                "actor": actor,
                "reason": reason,
            },
        )

    def add_comment(self, ticket: dict, author: str, body: str) -> ServiceResult:
        return self._result(
            "add_comment",
            {"ticketId": ticket["id"], "author": author, "body": body},
        )

    def read_ticket_status(self, ticket: dict) -> ServiceResult:
        return self._result(
            "read_ticket_status",
            {"ticketId": ticket["id"], "status": ticket["status"]},
        )


class HttpUSUSMService:
    provider = "usu_sm_api"

    def create_ticket(self, ticket: dict) -> ServiceResult:
        response = self._post_json(self._build_create_payload(ticket))
        error_message = usm_error_message(response, "create_ticket")
        if error_message:
            raise USUSMServiceError(error_message)

        external_id = str(response.get("id") or response.get("ticketId") or "")
        return ServiceResult(
            action="create_ticket",
            provider=self.provider,
            status="accepted",
            external_id=external_id or None,
            message="USU SM service accepted create_ticket.",
            data=response,
        )

    def update_ticket(self, ticket: dict, changes: list[tuple[str, str, str]]) -> ServiceResult:
        return self._not_configured("update_ticket", {"ticket": ticket, "changes": changes})

    def delete_ticket(self, ticket: dict, actor: str) -> ServiceResult:
        return self._not_configured("delete_ticket", {"ticket": ticket, "actor": actor})

    def change_status(self, ticket: dict, new_status: str, actor: str, reason: str = "") -> ServiceResult:
        return self._not_configured(
            "change_status",
            {
                "ticketId": ticket["id"],
                "from": ticket["status"],
                "to": new_status,
                "actor": actor,
                "reason": reason,
            },
        )

    def add_comment(self, ticket: dict, author: str, body: str) -> ServiceResult:
        return self._not_configured(
            "add_comment",
            {"ticketId": ticket["id"], "author": author, "body": body},
        )

    def read_ticket_status(self, ticket: dict) -> ServiceResult:
        return self._not_configured(
            "read_ticket_status",
            {"ticketId": ticket["id"], "status": ticket["status"]},
        )

    def _not_configured(self, action: str, data: dict) -> ServiceResult:
        raise USUSMServiceError(
            f"USU SM API operation is not configured yet: {action}."
        )

    def _build_create_payload(self, ticket: dict) -> dict:
        requested_by_email = setting("USM_REQUESTED_BY_EMAIL", DEFAULT_USM_EMAIL)
        impact = usm_value_for_priority(
            ticket.get("priority"),
            PRIORITY_USM_IMPACT_VALUES,
            "USM_IMPACT",
        )
        urgency = usm_value_for_priority(
            ticket.get("priority"),
            PRIORITY_USM_URGENCY_VALUES,
            "USM_URGENCY",
        )

        payload = {
            "accessToken": required_setting("USM_ACCESS_TOKEN"),
            "service": setting("USM_SERVICE", "InterfaceTransactionStart"),
            "username": required_setting("USM_USERNAME"),
            "password": required_setting("USM_PASSWORD"),
            "encrypted": setting("USM_ENCRYPTED", "N"),
            "client": setting("USM_CLIENT", "01"),
            "params": {
                "interfaceActionName": setting(
                    "USM_INTERFACE_ACTION",
                    "VMEx_VM_CreateTicket_complex",
                ),
                "fields": {
                    "description": ticket["description"],
                    "summary": ticket["title"],
                },
                "impact": impact,
                "urgency": urgency,
                "priority": urgency,
                "persEmailReqBy": requested_by_email,
                "persEmailAffected": setting("USM_AFFECTED_EMAIL", requested_by_email),
                "status": setting("USM_STATUS", "IN_CRE"),
            },
        }

        self._require_payload_values(payload)
        return payload

    def _require_payload_values(self, payload: dict) -> None:
        params = payload["params"]
        required_values = {
            "params.fields.description": params["fields"]["description"],
            "params.fields.summary": params["fields"]["summary"],
            "params.impact": params["impact"],
            "params.urgency": params["urgency"],
            "params.priority": params["priority"],
            "params.persEmailReqBy": params["persEmailReqBy"],
            "params.persEmailAffected": params["persEmailAffected"],
            "params.status": params["status"],
        }
        missing = [field for field, value in required_values.items() if not value]

        if missing:
            raise USUSMServiceError(
                f"Missing required USU SM payload values: {', '.join(missing)}"
            )

    def _post_json(self, payload: dict) -> dict:
        url = setting(
            "USM_EXECWF_URL",
            "http://localhost:8087/vmwebjetty/services/api/execwf",
        )
        timeout = float(setting("USM_TIMEOUT_SECONDS", "30"))

        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise USUSMServiceError(
                f"USU SM returned HTTP {response.status_code}: {response.text}"
            ) from exc
        except requests.RequestException as exc:
            if is_wsl() and is_loopback_url(url):
                return self._post_json_via_windows(payload, exc)

            raise USUSMServiceError(f"Could not reach USU SM: {exc}") from exc

        return parse_response_body(response.content)

    def _post_json_via_windows(self, payload: dict, original_error: Exception) -> dict:
        if shutil.which("powershell.exe") is None:
            raise USUSMServiceError(
                "Could not reach USU SM from Ubuntu/WSL, and powershell.exe is not "
                f"available for the Windows localhost retry. Original error: {original_error}"
            ) from original_error

        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", WINDOWS_POST_SCRIPT],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=35,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise USUSMServiceError(
                "Could not reach USU SM from Ubuntu/WSL, and the Windows localhost "
                f"retry failed to run. Original error: {original_error}"
            ) from exc

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Unknown PowerShell error."
            raise USUSMServiceError(
                "Could not reach USU SM directly from Ubuntu/WSL. The Windows localhost "
                f"retry also failed: {detail}"
            ) from original_error

        return parse_response_body(result.stdout)


class FallbackUSUSMService:
    provider = "fallback"

    def __init__(self, primary=None, fallback=None):
        self.primary = primary or HttpUSUSMService()
        self.fallback = fallback or MockUSUSMService()

    def create_ticket(self, ticket: dict) -> ServiceResult:
        return self._run("create_ticket", ticket)

    def update_ticket(self, ticket: dict, changes: list[tuple[str, str, str]]) -> ServiceResult:
        return self._run("update_ticket", ticket, changes)

    def delete_ticket(self, ticket: dict, actor: str) -> ServiceResult:
        return self._run("delete_ticket", ticket, actor)

    def change_status(self, ticket: dict, new_status: str, actor: str, reason: str = "") -> ServiceResult:
        return self._run("change_status", ticket, new_status, actor, reason)

    def add_comment(self, ticket: dict, author: str, body: str) -> ServiceResult:
        return self._run("add_comment", ticket, author, body)

    def read_ticket_status(self, ticket: dict) -> ServiceResult:
        return self._run("read_ticket_status", ticket)

    def _run(self, method_name: str, *args) -> ServiceResult:
        try:
            return getattr(self.primary, method_name)(*args)
        except USUSMServiceError as exc:
            result = getattr(self.fallback, method_name)(*args)
            result.message = f"{result.message} Fallback reason: {exc}"
            return result


def get_usu_sm_service():
    mode = service_mode()

    if mode == "mock":
        return MockUSUSMService()

    if mode == "api":
        return HttpUSUSMService()

    if mode == "auto" and has_api_credentials():
        return FallbackUSUSMService()

    return MockUSUSMService()
