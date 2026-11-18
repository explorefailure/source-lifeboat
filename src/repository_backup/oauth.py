"""GitHub device authorization; fixed HTTPS endpoints, no client secret."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .github import GitHubError

DEFAULT_CLIENT_ID = "Ov23lid8jIzZrxu2Ye6d"
VERIFY_URL = "https://github.com/login/device"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(endpoint: str, fields: dict[str, str]) -> dict[str, Any]:
    if endpoint not in ("/login/device/code", "/login/oauth/access_token"):
        raise ValueError("invalid OAuth endpoint")
    request = urllib.request.Request("https://github.com" + endpoint,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Accept": "application/json", "User-Agent": "Source-Lifeboat", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
            body = response.read(65537)
        if len(body) > 65536:
            raise ValueError("response too large")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("invalid response")
        return value
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise GitHubError("Could not reach GitHub authorization. Try again.") from exc


def client_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{10,100}", value):
        raise ValueError("Enter the public Client ID from your GitHub OAuth app.")
    return value


def token_record(value: dict[str, Any], app_id: str) -> dict[str, Any]:
    token = value.get("access_token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]{20,255}", token) or value.get("token_type") not in ("bearer", "Bearer"):
        raise GitHubError("GitHub did not return a valid access token.")
    scopes = set(str(value.get("scope", "")).replace(",", " ").split())
    if scopes - {"offline_access"}:
        raise GitHubError("GitHub returned broader access than requested. Revoke this app on GitHub and reconnect.")
    record = {"token": token, "kind": "oauth", "client_id": app_id}
    if "expires_in" in value:
        seconds = value["expires_in"]
        refresh = value.get("refresh_token")
        refresh_seconds = value.get("refresh_token_expires_in")
        if (type(seconds) is not int or seconds <= 0 or not isinstance(refresh, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{20,255}", refresh)
            or type(refresh_seconds) is not int or refresh_seconds <= 0):
            raise GitHubError("GitHub returned incomplete token-expiry information.")
        record.update(expires_at=time.time() + seconds, refresh_token=refresh,
                      refresh_expires_at=time.time() + refresh_seconds)
    return record


class DeviceFlow:
    """One installation-wide pending flow, bound to the owner's session.

    Polling is driven by authenticated POST requests. Codes disappear on restart.
    All operations share the credential lock so cancel/remove/save cannot race a poll.
    """
    def __init__(self, settings):
        self.settings = settings
        self.flow: dict[str, Any] | None = None
        self.last_start = float("-inf")

    def view(self, session: str) -> dict[str, Any]:
        flow = self.flow
        if not flow or flow["session"] != session:
            return {"state": "idle"}
        if flow["state"] == "pending" and time.monotonic() >= flow["deadline"]:
            flow["state"] = "expired"
            flow.pop("device_code", None)
        return {key: flow[key] for key in ("state", "user_code", "verification_uri", "expires_at", "interval") if key in flow}

    def action(self, action: str, value: dict[str, Any], session: str) -> dict[str, Any]:
        with self.settings.lock:
            if action == "client":
                app_id = client_id(value.get("client_id"))
                self.settings.write_private(self.settings.client_path, {"client_id": app_id})
                self.flow = None
                return {"state": "idle", "client_id": app_id}
            if action == "start":
                if self.flow and self.flow["session"] == session and self.view(session)["state"] == "pending":
                    return self.view(session)
                if time.monotonic() - self.last_start < 10:
                    raise ValueError("Wait a few seconds before requesting another sign-in code.")
                self.last_start = time.monotonic()
                app_id = self.settings.client_id()
                result = post("/login/device/code", {"client_id": app_id})
                if result.get("error"):
                    raise GitHubError("GitHub could not start sign-in. Check the Client ID and enable Device Flow in the OAuth app settings.")
                if (result.get("verification_uri") != VERIFY_URL
                    or not re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", str(result.get("user_code", "")))
                    or not re.fullmatch(r"[A-Za-z0-9_\-]{20,255}", str(result.get("device_code", "")))
                    or type(result.get("expires_in")) is not int or not 1 <= result["expires_in"] <= 3600
                    or type(result.get("interval", 5)) is not int or not 1 <= result.get("interval", 5) <= 3600):
                    raise GitHubError("GitHub returned invalid device authorization details.")
                interval = result.get("interval", 5)
                self.flow = {"session": session, "state": "pending", "client_id": app_id,
                    "device_code": result["device_code"], "user_code": result["user_code"],
                    "verification_uri": VERIFY_URL, "expires_at": time.time() + result["expires_in"],
                    "deadline": time.monotonic() + result["expires_in"], "interval": interval,
                    "next_poll": time.monotonic() + interval}
                return self.view(session)
            if action == "cancel":
                if self.flow and self.flow["session"] == session:
                    self.flow = None
                return {"state": "cancelled"}
            if action != "poll":
                raise ValueError("Choose start, poll, cancel or client.")
            view = self.view(session)
            flow = self.flow
            if view["state"] != "pending" or not flow:
                return view
            if time.monotonic() < flow["next_poll"]:
                return view
            # Reserve the interval before making the request, including on network failure.
            flow["next_poll"] = time.monotonic() + flow["interval"]
            result = post("/login/oauth/access_token", {"client_id": flow["client_id"],
                "device_code": flow["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            error = result.get("error")
            if error == "authorization_pending":
                pass
            elif error == "slow_down":
                supplied = result.get("interval", 0)
                flow["interval"] = max(flow["interval"] + 5, supplied if type(supplied) is int else 0)
            elif error:
                flow["state"] = {"access_denied": "denied", "expired_token": "expired", "token_expired": "expired"}.get(error, "failed")
                flow.pop("device_code", None)
            else:
                # Never overwrite the current credential until identity validation succeeds.
                try:
                    record = token_record(result, flow["client_id"])
                    connection = self.settings.test(record["token"])
                    record["account"] = connection["account"]
                    self.settings.write_private(self.settings.path, record)
                except Exception:
                    flow["state"] = "failed"
                    flow.pop("device_code", None)
                    raise
                flow["state"] = "connected"
                flow.pop("device_code", None)
            flow["next_poll"] = time.monotonic() + flow["interval"]
            return self.view(session)
