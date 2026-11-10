#!/usr/bin/env python3
"""Standalone ARAS access probe: is the tunnel up, who answers, do my tokens work?

One file, standard library only, no project imports -- copy it anywhere, put a
.env next to it (or point --env at one) and run:

    python scripts/aras_smoke.py                # from the repo root
    python aras_smoke.py --env C:\\path\\to\\.env  # the copied-anywhere form

Three layers per service, because each failure means a different fix:

  1. TCP connect to the port.      Closed  -> the SSH tunnel is not forwarding.
  2. GET the unauthenticated       Non-200 -> something answered that is not
     health route.                            the service (a proxy?); the body
                                              snippet shows who.
  3. One read-only authenticated   401/403 -> the token is wrong or inactive.
     call with the token from      400/422 -> the token PASSED; the service
     .env.                                    rejected the empty probe body,
                                              which is exactly what we sent.

Proxies from the environment (HTTP_PROXY/HTTPS_PROXY) are explicitly ignored:
loopback traffic through a VPN proxy is precisely the failure this script
exists to expose. Tokens are read from .env and never printed. Port 8106 (the
remote orchestrator) is never called, as the access rules require.

A service is *configured* when .env holds a token for it (its own
ARAS_<SERVICE>_TOKEN or the shared ARAS_TOKEN). One without a token is listed
as SKIP -- its port and health are still shown -- and does not decide the
result, so leaving an optional service out is not a failure.

Exit code 0 only when at least one service is configured and every configured
service passes all three layers.

The probe goes to the standard port of each service on the host of
ARAS_BASE_URL. A per-service ARAS_<SERVICE>_URL override is not followed.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from urllib import error, request

CONNECT_TIMEOUT = 3.0
HTTP_TIMEOUT = 15.0
SNIPPET = 140

#: service -> (port, health path, auth method, auth path, probe body or None)
#: Every authenticated probe is read-only or an empty body the service will
#: reject with a 4xx -- nothing here writes, ingests or spends a model call.
SERVICES: list[dict] = [
    {"name": "router", "port": 8101, "health": "/api/v1/health/",
     "method": "GET", "path": "/api/v1/models/", "body": None},
    {"name": "ocr", "port": 8102, "health": "/api/v1/health/",
     "method": "POST", "path": "/api/v1/ocr/", "body": {}},
    {"name": "vector", "port": 8103, "health": "/health/",
     "method": "GET", "path": "/v1/collections/", "body": None},
    {"name": "ingestion", "port": 8104, "health": "/health/",
     "method": "POST", "path": "/v1/ingest/", "body": {}},
    {"name": "retrieval", "port": 8105, "health": "/health/",
     "method": "POST", "path": "/v1/search/", "body": {}},
    {"name": "data", "port": 8107, "health": "/v1/health/",
     "method": "GET", "path": "/v1/data-sources/", "body": None},
    {"name": "web", "port": 8108, "health": "/v1/health/",
     "method": "POST", "path": "/v1/search/", "body": {}},
]


def parse_env(text: str) -> dict[str, str]:
    """A tiny .env reader: KEY=VALUE lines, quotes stripped, comments skipped."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value and value[0] in "\"'" and value.endswith(value[0]) and len(value) >= 2:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key.upper()] = value
    return values


def token_for(env: dict[str, str], service: str) -> str:
    """Per-service token, falling back to the shared ARAS_TOKEN."""
    return env.get(f"ARAS_{service.upper()}_TOKEN", "") or env.get("ARAS_TOKEN", "")


def base_host(env: dict[str, str]) -> str:
    """The tunnel host from ARAS_BASE_URL (default 127.0.0.1), no port."""
    base = env.get("ARAS_BASE_URL", "").strip() or "http://127.0.0.1"
    host = base.split("://", 1)[-1].split("/", 1)[0]
    return host.rsplit(":", 1)[0] if ":" in host and not host.startswith("[") else host


def classify_auth(status: int) -> tuple[bool, str]:
    """Verdict for the authenticated probe: did the token pass the door?"""
    if status in (401, 403):
        return False, "TOKEN REJECTED (check the token in .env; key inactive?)"
    if 200 <= status < 300:
        return True, "token OK (request accepted)"
    if status in (400, 404, 405, 422):
        return True, "token OK (empty probe body rejected, as expected)"
    if status >= 500:
        return False, f"service error HTTP {status} (token not the problem)"
    return False, f"unexpected HTTP {status}"


def _opener() -> request.OpenerDirector:
    # The empty ProxyHandler is the point: never route loopback through a
    # VPN/system proxy, no matter what HTTP_PROXY says.
    return request.build_opener(request.ProxyHandler({}))


def http_call(method: str, url: str, *, token: str = "", body: dict | None = None,
              timeout: float = HTTP_TIMEOUT) -> tuple[int, str]:
    """(status, body snippet); status 0 means no HTTP answer at all."""
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _opener().open(req, timeout=timeout) as response:
            snippet = response.read(4096).decode("utf-8", "replace")
            return response.status, " ".join(snippet.split())[:SNIPPET]
    except error.HTTPError as exc:
        snippet = exc.read(4096).decode("utf-8", "replace")
        return exc.code, " ".join(snippet.split())[:SNIPPET]
    except (error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return 0, f"{type(reason).__name__ if not isinstance(reason, str) else ''} {reason}".strip()


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def probe(service: dict, host: str, env: dict[str, str]) -> dict:
    """All three layers for one service; never prints, just reports."""
    name, port = service["name"], service["port"]
    token = token_for(env, name)
    out = {"service": name, "port": port, "configured": bool(token), "port_open": False,
           "health": None, "auth": None, "notes": []}
    not_configured = (f"not configured: no token in .env (ARAS_{name.upper()}_TOKEN or "
                      "ARAS_TOKEN) - skipped")

    if not port_open(host, port):
        out["notes"].append("PORT CLOSED - nothing is listening; the SSH tunnel "
                            "is not forwarding this port")
        if not token:
            out["notes"].append(not_configured)
        return out
    out["port_open"] = True

    base = f"http://{host}:{port}"
    status, snippet = http_call("GET", base + service["health"])
    out["health"] = status
    if status != 200:
        who = f" body: {snippet}" if snippet else ""
        out["notes"].append(f"health answered HTTP {status or 'nothing'} - "
                            f"something replied that is not a healthy service.{who}")

    if not token:
        out["notes"].append(not_configured)
        return out
    headers_note = ""
    req_url = base + service["path"]
    extra = {}
    if name == "data":
        extra = {"X-Tenant": env.get("ARAS_TENANT", ""),
                 "X-Owner-External-Id": env.get("ARAS_OWNER", "")}
        if not all(extra.values()):
            headers_note = " (ARAS_TENANT/ARAS_OWNER missing - the service may 400)"
    status, snippet = authenticated_call(service, req_url, token, extra)
    out["auth"] = status
    ok, verdict = classify_auth(status) if status else (False, f"no answer: {snippet}")
    out["auth_ok"] = ok
    out["notes"].append(verdict + headers_note)
    return out


def authenticated_call(service: dict, url: str, token: str,
                       extra_headers: dict[str, str]) -> tuple[int, str]:
    data = json.dumps(service["body"]).encode() if service["body"] is not None else None
    req = request.Request(url, data=data, method=service["method"])
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in extra_headers.items():
        if value:
            req.add_header(key, value)
    try:
        with _opener().open(req, timeout=HTTP_TIMEOUT) as response:
            return response.status, ""
    except error.HTTPError as exc:
        snippet = exc.read(2048).decode("utf-8", "replace")
        return exc.code, " ".join(snippet.split())[:SNIPPET]
    except (error.URLError, OSError) as exc:
        return 0, str(getattr(exc, "reason", exc))


def is_configured(result: dict) -> bool:
    """Does .env hold a token for this service? (Results without the key count as yes.)"""
    return bool(result.get("configured", True))


def passed(result: dict) -> bool:
    """All three layers: port open, health 200, token accepted."""
    return bool(result["port_open"] and result["health"] == 200 and result.get("auth_ok", False))


def verdict(result: dict) -> str:
    """``OK``, ``FAIL`` or ``SKIP`` (no token, so the service does not decide the result)."""
    if not is_configured(result):
        return "SKIP"
    return "OK" if passed(result) else "FAIL"


def all_good(results: list[dict]) -> bool:
    """The exit-code rule: something is configured, and all of it passes."""
    configured = [r for r in results if is_configured(r)]
    return bool(configured) and all(passed(r) for r in configured)


def diagnose(results: list[dict]) -> list[str]:
    """The aggregate story: one sentence that names the single likely cause."""
    lines: list[str] = []
    configured = [r for r in results if is_configured(r)]
    skipped = [r for r in results if not is_configured(r)]
    every_closed = bool(results) and all(not r["port_open"] for r in results)
    closed = [r for r in configured if not r["port_open"]]
    sick_health = [r for r in results if r["port_open"] and r["health"] != 200]
    rejected = [r for r in configured if r.get("auth") in (401, 403)]
    if every_closed:
        lines.append("EVERY port is closed: the SSH tunnel is not open. Start it, "
                     "then re-run this script before touching anything else.")
    elif closed:
        lines.append("Closed ports: " + ", ".join(f"{r['service']}:{r['port']}" for r in closed)
                     + " - add the missing -L forwards to the tunnel.")
    if len(sick_health) == len(results) and results:
        lines.append("Every service answers but none is healthy: if the bodies look like "
                     "one error page, a proxy/VPN is intercepting loopback traffic.")
    if rejected:
        lines.append("Rejected tokens: " + ", ".join(r["service"] for r in rejected)
                     + " - compare .env with the samples; a key may be set inactive.")
    # whatever failed without one of the causes above still has to be named:
    # a line that says "all good" above a FAIL row helps nobody
    named = {id(r) for r in closed} | {id(r) for r in rejected}
    other = [r for r in configured if not passed(r) and id(r) not in named]
    if other and not every_closed:
        lines.append("Failing: " + ", ".join(f"{r['service']} ({_why(r)})" for r in other)
                     + " - see the note under each service.")
    if not configured:
        lines.append("No ARAS token in .env: nothing could be verified beyond ports and "
                     "health. Set ARAS_TOKEN or the ARAS_<SERVICE>_TOKEN lines.")
    elif not lines:
        lines.append("All good: tunnel up, services healthy, tokens accepted.")
    if skipped and configured:
        lines.append("Skipped, no token in .env: " + ", ".join(r["service"] for r in skipped)
                     + " - fine when the app does not use them.")
    return lines


def _why(result: dict) -> str:
    """Three words for a failure that is neither a closed port nor a rejected token."""
    if result["health"] != 200:
        return f"health {result['health'] or 'no answer'}"
    if result.get("auth"):
        return f"authenticated call HTTP {result['auth']}"
    return "no answer to the authenticated call"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", default=".env", help="path to the .env with the tokens")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    env_path = Path(args.env)
    env = parse_env(env_path.read_text(encoding="utf-8")) if env_path.is_file() else {}
    if not env_path.is_file():
        # stderr, so --json keeps stdout as one JSON document
        print(f"note: {env_path} not found - probing without tokens (layers 1+2 only)",
              file=sys.stderr)

    host = base_host(env)
    results = [probe(service, host, env) for service in SERVICES]

    for result in results:
        result["verdict"] = verdict(result)
    good = all_good(results)

    if args.json:
        print(json.dumps({"host": host, "ok": good, "results": results,
                          "diagnosis": diagnose(results)}, indent=2))
    else:
        print(f"ARAS access probe - host {host} (proxy env ignored on purpose)")
        print()
        for r in results:
            print(f"{r['verdict']:<4} {r['service']:<10} :{r['port']}  "
                  f"port={'open' if r['port_open'] else 'CLOSED'}  "
                  f"health={r['health'] if r['health'] is not None else '-'}  "
                  f"auth={r['auth'] if r['auth'] is not None else '-'}")
            for note in r["notes"]:
                print(f"     {note}")
        print()
        for line in diagnose(results):
            print("DIAGNOSIS:", line)

    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
