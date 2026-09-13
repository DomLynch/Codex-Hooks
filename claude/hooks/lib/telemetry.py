"""Optional operational telemetry; importing this module needs only the stdlib."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import uuid


AGENT = "claude"
COMPONENTS = frozenset({
    "session_start", "user_prompt_submit", "post_tool", "stop_orchestrator",
    "mimo_audit", "autonomy_daemon", "smoke_test",
})
CATEGORIES = frozenset({"audit_unavailable", "hook_exception", "smoke_test"})
CONFIG_PATH = Path.home() / ".config/agent-hooks/sentry.json"
WORKER_PYTHON = Path.home() / ".local/share/agent-hooks/venv/bin/python"
WORKER_TIMEOUT = 1.8
MESSAGE = "Agent hook operational failure"
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _label(value: object) -> str:
    if not isinstance(value, str) or not LABEL_RE.fullmatch(value):
        raise ValueError("invalid telemetry label")
    return value


def _settings(data: dict) -> dict:
    dsn = data.get("dsn")
    if not isinstance(dsn, str) or len(dsn) > 2048:
        raise ValueError("invalid telemetry configuration")
    url = urlsplit(dsn)
    if (url.scheme != "https" or not url.hostname or not url.username
            or url.password or url.query or url.fragment
            or not url.path.rstrip("/").rsplit("/", 1)[-1].isdigit()):
        raise ValueError("invalid telemetry configuration")
    return {
        "dsn": dsn,
        "environment": _label(data.get("environment", "production")),
        "release": _label(data.get("release", "agent-hooks")),
    }


def _load_settings() -> dict:
    # Configuration failures disable telemetry, including when there is no DSN.
    try:
        data = {}
        if CONFIG_PATH.is_file():
            with CONFIG_PATH.open() as handle:
                data = json.loads(handle.read(4097))
        if not isinstance(data, dict):
            return {}
        dsn = os.environ.get("HOOK_SENTRY_DSN", data.get("dsn"))
        if not dsn:
            return {}
        return _settings({**data, "dsn": dsn})
    except Exception:
        return {}


def _warn() -> None:
    try:
        print("[agent-hooks telemetry] report unavailable", file=sys.stderr)
    except Exception:
        pass


def report_failure(component: str, category: str) -> None:
    """Best effort only: constants in, no exception text, no verdict changes."""
    _report(component, category)


def _report(component: str, category: str, *, smoke: bool = False) -> str | None:
    try:
        if component not in COMPONENTS or category not in CATEGORIES:
            return
        settings = _load_settings()
        if not settings:
            return
        payload = {**settings, "component": component, "category": category}
        result = subprocess.run(
            [str(WORKER_PYTHON), "-I", "-B", str(Path(__file__).resolve()), "--worker"],
            input=json.dumps(payload), text=True,
            stdout=subprocess.PIPE if smoke else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=WORKER_TIMEOUT, check=False,
            cwd="/", env={"PATH": os.defpath},
        )
        if result.returncode:
            _warn()
        elif smoke:
            event_id = result.stdout.strip()
            if re.fullmatch(r"[0-9a-f]{32}", event_id):
                return event_id
            _warn()
    except Exception:
        _warn()


def _before_send(event: dict, hint: dict) -> dict | None:
    # Rebuild, never redact in place: SDK metadata and unknown fields disappear.
    try:
        tags = event.get("tags", {})
        component, category = tags.get("component"), tags.get("category")
        if component not in COMPONENTS or category not in CATEGORIES:
            return None
        event_id = event.get("event_id", "")
        if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", event_id):
            event_id = uuid.uuid4().hex
        return {
            "event_id": event_id, "level": "error", "message": MESSAGE,
            "tags": {"component": component, "category": category, "agent": AGENT,
                     "synthetic_test": str(category == "smoke_test").lower()},
            "fingerprint": ["agent-hooks", AGENT, component, category],
            "environment": _label(event.get("environment", "production")),
            "release": _label(event.get("release", "agent-hooks")),
        }
    except Exception:
        return None


def _client(settings: dict, transport=None):
    from sentry_sdk import Client

    return Client(
        **settings, transport=transport, before_send=_before_send,
        default_integrations=False, auto_enabling_integrations=False,
        traces_sample_rate=0, send_default_pii=False, include_local_variables=False,
        attach_stacktrace=False, server_name="", debug=False, spotlight=False,
        send_client_reports=False, auto_session_tracking=False,
        enable_logs=False, enable_metrics=False, shutdown_timeout=0.6,
    )


def _send(payload: dict, transport=None) -> str | None:
    component, category = payload["component"], payload["category"]
    if component not in COMPONENTS or category not in CATEGORIES:
        raise ValueError("invalid telemetry labels")
    settings = _settings(payload)
    client = _client(settings, transport)
    try:
        return client.capture_event({
            "message": MESSAGE, "level": "error",
            "tags": {"component": component, "category": category, "agent": AGENT},
            "environment": settings["environment"], "release": settings["release"],
        })
    finally:
        client.close(timeout=0.6)


def _worker() -> int:
    try:
        if sys.argv[1:] == ["--smoke"]:
            event_id = _report("smoke_test", "smoke_test", smoke=True)
            if event_id:
                print(event_id)
                return 0
            print("[agent-hooks telemetry] smoke unavailable or not configured", file=sys.stderr)
            return 1
        if sys.argv[1:] != ["--worker"]:
            return 1
        event_id = _send(json.loads(sys.stdin.read(4097)))
        if not event_id:
            return 1
        print(event_id)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(_worker())
