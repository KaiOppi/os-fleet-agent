#!/usr/local/bin/python3
"""os-fleet-agent — collects status from this OPNsense and pushes it to
the central os-fleet server.

Subcommands:
    run     gather snapshot + POST to server, write last_run/last_status
    test    POST nothing, just call /whoami to verify token works
    setup   register/unregister the cron entry based on settings

Why a Python script, not a configd Lua snippet:
- We need urllib + ssl + json + xml on FreeBSD; Python has all of it
  in the base OPNsense image without extra deps.
- Plain stdlib only — no external Python packages required.

Datasource:
- Local API on https://127.0.0.1 with the user's API key/secret stored
  in the model. This way the payload shape exactly matches what the
  pull-mode probe assembles, and the server reuses one parser.
- /conf/config.xml read directly for the config_xml field (saves an
  HTTP roundtrip, that file is local to us anyway).
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VERSION = "0.0.12"
CONFIG_PATH = "/conf/config.xml"

# Optional env-overrides loaded before any feature flag is read.
# Format: simple KEY=VALUE lines, no quoting, '#' comments allowed.
# Survives plugin upgrades (the file isn't shipped by the package).
_ENV_OVERRIDE_PATH = "/etc/os-fleet-agent.env"
try:
    if os.path.isfile(_ENV_OVERRIDE_PATH):
        with open(_ENV_OVERRIDE_PATH, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())
except Exception:
    pass

LOCAL_API_DEFAULT = "https://127.0.0.1"
LOCAL_TIMEOUT = 15
SERVER_TIMEOUT = 60
MODEL_PATH = "OPNsense/Fleet/general"

# Legacy-rule editing is opt-in per box because it modifies /conf/config.xml
# directly. Set OSF_AGENT_ALLOW_LEGACY_EDIT=1 in the agent's environment
# (or in the plugin settings later) to enable it. While off, the agent
# refuses every legacy_filter_* command with a clear error.
LEGACY_EDIT_ENABLED = os.environ.get(
    "OSF_AGENT_ALLOW_LEGACY_EDIT", ""
).strip().lower() in ("1", "true", "yes", "on")
BACKUP_DIR = "/var/db/os-fleet-agent/config-backups"
BACKUP_RETENTION = 10  # keep the N most recent

# Quick-poll loop: after the heavy status push, keep the agent process
# alive briefly and ping the server's /quick endpoint several times in
# a row to pick up newly queued commands without waiting for the next
# cron tick. Latency for cmd-dispatch drops from ~1 min to ~10 s.
QUICK_POLL_ITERATIONS = 19
QUICK_POLL_SLEEP = 3   # seconds — 19×3=57s, fits inside 1-min cron
QUICK_POLL_TIMEOUT = 15  # connect+read; we fail fast if server is down

STATUS_DIR = "/var/db/os-fleet-agent"
STATUS_FILE = STATUS_DIR + "/status.json"

# --- Helpers -----------------------------------------------------------------


def _xml_text(elem):
    return (elem.text or "").strip() if elem is not None else ""


def load_settings() -> dict:
    """Read the agent's settings out of /conf/config.xml directly so we
    don't depend on configctl for read-only access."""
    tree = ET.parse(CONFIG_PATH)
    root = tree.getroot()
    node = root.find(f"OPNsense/Fleet/general")
    if node is None:
        return {}
    return {
        "enabled": _xml_text(node.find("enabled")) == "1",
        "server_url": _xml_text(node.find("server_url")).rstrip("/"),
        "agent_token": _xml_text(node.find("agent_token")),
        "verify_tls": _xml_text(node.find("verify_tls")) == "1",
        "interval_minutes": int(_xml_text(node.find("interval_minutes")) or "5"),
        "local_api_key": _xml_text(node.find("local_api_key")),
        "local_api_secret": _xml_text(node.find("local_api_secret")),
        # Override for boxes where 443 is bound to something other than
        # the OPNsense webgui (HAProxy, reverse proxy, …). Empty = default.
        "local_api_url": _xml_text(node.find("local_api_url")) or LOCAL_API_DEFAULT,
    }


def _read_status_file() -> dict:
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_status(status: str, message: str, **extra) -> None:
    """Persist last-run state to /var/db/os-fleet-agent/status.json.

    Why a separate file instead of config.xml:
    - config.xml is rewritten by every save; pushing status updates into
      it would race with operator saves and trigger unnecessary
      `write_config()` audit entries.
    - The status file only the agent script writes and only
      SettingsController.statusAction() reads — single writer, single
      reader, no contention.

    `extra` may include any of: elapsed_seconds, rules_seen,
    plugins_seen, certs_seen, server_url, run_count, error_count.
    """
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = _read_status_file()
    data["last_run"] = iso
    data["last_status"] = status[:32]
    data["last_message"] = message[:480]
    for k, v in extra.items():
        if v is not None:
            data[k] = v
    data["run_count"] = int(data.get("run_count", 0)) + 1
    if status not in ("ok", "disabled"):
        data["error_count"] = int(data.get("error_count", 0)) + 1
        data["last_error_run"] = iso
    else:
        data["last_ok_run"] = iso

    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, STATUS_FILE)
        os.chmod(STATUS_FILE, 0o644)
    except Exception:
        # Last resort — drop a line in the log so we don't lose the trace.
        try:
            with open("/var/log/os-fleet-agent.log", "a", encoding="utf-8") as f:
                f.write(f"{iso} status={status} msg={message}\n")
        except Exception:
            pass


def _ssl_ctx(verify: bool) -> ssl.SSLContext:
    if verify:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get_json(url: str, *, basic_user: str = "", basic_pass: str = "",
                  verify: bool = True, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, method="GET")
    if basic_user:
        token = base64.b64encode(f"{basic_user}:{basic_pass}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, context=_ssl_ctx(verify), timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_get_text(url: str, *, basic_user: str = "", basic_pass: str = "",
                  verify: bool = True, timeout: int = 15) -> str:
    req = urllib.request.Request(url, method="GET")
    if basic_user:
        token = base64.b64encode(f"{basic_user}:{basic_pass}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, context=_ssl_ctx(verify), timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_post_json(url: str, payload: dict, *, verify: bool = True,
                   timeout: int = 60) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=_ssl_ctx(verify), timeout=timeout) as r:
        return json.loads(r.read().decode())


# --- Subcommands -------------------------------------------------------------


def cmd_test(s: dict) -> tuple[str, str]:
    if not s.get("server_url") or not s.get("agent_token"):
        return ("error", "server_url + agent_token required")
    url = f"{s['server_url']}/api/fleet/ingest/{s['agent_token']}/whoami"
    try:
        data = http_get_json(url, verify=s["verify_tls"], timeout=15)
    except urllib.error.HTTPError as e:
        return ("error", f"HTTP {e.code}: {e.reason}")
    except Exception as e:
        return ("error", f"{type(e).__name__}: {e}")
    if not data.get("ok"):
        return ("error", json.dumps(data))
    return ("ok", f"server says hello to box_id={data.get('box_id')!r}, name={data.get('name')!r}")


def gather_payload(s: dict) -> dict:
    """Collect the same fields the pull-mode probe collects, just from
    127.0.0.1 instead of remote."""
    user, pw = s["local_api_key"], s["local_api_secret"]

    def fetch(path: str) -> dict:
        return http_get_json(
            f"{s['local_api_url']}{path}", basic_user=user, basic_pass=pw,
            verify=False, timeout=LOCAL_TIMEOUT,
        )

    sysinfo = fetch("/api/diagnostics/system/system_information")
    firmware_status = fetch("/api/core/firmware/status")
    firmware_info = fetch("/api/core/firmware/info")
    cert_search = fetch("/api/trust/cert/search")

    # config.xml is local; cheaper to read off disk than via API.
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config_xml = f.read()
    except Exception:
        config_xml = ""

    return {
        "agent_version": VERSION,
        "system": sysinfo,
        "firmware_status": firmware_status,
        "firmware_info": firmware_info,
        "cert_search": cert_search,
        "config_xml": config_xml,
        "capabilities": {
            "legacy_edit_enabled": LEGACY_EDIT_ENABLED,
            "firmware_check_sync": True,
            "plugin_install_pkg": True,
            "force_status_push": True,
            "legacy_nat_edit": True,   # toggle/delete/update für <nat><rule>
            "legacy_outbound_edit": True,  # toggle/delete/update für <nat><outbound><rule>
            "legacy_nat_create": True,
            "legacy_outbound_create": True,
        },
    }


PENDING_RESULTS_FILE = STATUS_DIR + "/command_results.json"


def _stash_results(results: list[dict]) -> None:
    """Persist results that haven't been ack'd by the server yet, so an
    agent restart between executing a command and the next push doesn't
    lose them."""
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        tmp = PENDING_RESULTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(results, f)
        os.replace(tmp, PENDING_RESULTS_FILE)
    except Exception:
        pass


def _load_pending_results() -> list[dict]:
    try:
        with open(PENDING_RESULTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _exec_command(cmd: dict, s: dict) -> dict:
    """Dispatch one server-issued command. Returns the result dict that
    will be reported back in the next push."""
    cid = cmd.get("id")
    kind = cmd.get("kind")
    args = cmd.get("args") or {}

    if kind == "ping":
        return {
            "id": cid, "ok": True,
            "result": {
                "echo": args.get("echo", ""),
                "agent_version": VERSION,
                "agent_time": datetime.now(timezone.utc).isoformat(),
            },
        }

    if kind == "get_log":
        try:
            n = int(args.get("lines") or 50)
            n = max(1, min(n, 500))
            with open("/var/log/os-fleet-agent.log", "r", encoding="utf-8") as f:
                lines = f.readlines()[-n:]
            return {"id": cid, "ok": True, "result": {"lines": "".join(lines)}}
        except FileNotFoundError:
            return {"id": cid, "ok": True, "result": {"lines": "(no log yet)"}}
        except Exception as e:
            return {"id": cid, "ok": False, "error": str(e)[:300]}

    if kind == "api_call":
        method = (args.get("method") or "GET").upper()
        path = args.get("path") or ""
        body = args.get("body")
        if not path.startswith("/api/"):
            return {"id": cid, "ok": False, "error": "path must start with /api/"}
        url = f"{s['local_api_url']}{path}"
        try:
            if method == "GET":
                # Prefer JSON; fall back to raw text if upstream isn't JSON.
                try:
                    data = http_get_json(
                        url, basic_user=s["local_api_key"],
                        basic_pass=s["local_api_secret"], verify=False,
                        timeout=LOCAL_TIMEOUT,
                    )
                    return {"id": cid, "ok": True, "result": {"status": 200, "body": data}}
                except json.JSONDecodeError:
                    text = http_get_text(
                        url, basic_user=s["local_api_key"],
                        basic_pass=s["local_api_secret"], verify=False,
                        timeout=LOCAL_TIMEOUT,
                    )
                    return {"id": cid, "ok": True, "result": {"status": 200, "text": text[:8000]}}
            elif method == "POST":
                req = urllib.request.Request(
                    url, data=json.dumps(body or {}).encode(), method="POST"
                )
                req.add_header("Content-Type", "application/json")
                token = base64.b64encode(
                    f"{s['local_api_key']}:{s['local_api_secret']}".encode()
                ).decode()
                req.add_header("Authorization", f"Basic {token}")
                with urllib.request.urlopen(req, context=_ssl_ctx(False), timeout=LOCAL_TIMEOUT) as r:
                    raw = r.read().decode("utf-8", errors="replace")
                    try:
                        return {"id": cid, "ok": True, "result": {"status": r.status, "body": json.loads(raw)}}
                    except json.JSONDecodeError:
                        return {"id": cid, "ok": True, "result": {"status": r.status, "text": raw[:8000]}}
            else:
                return {"id": cid, "ok": False, "error": f"method {method} not allowed"}
        except urllib.error.HTTPError as e:
            return {"id": cid, "ok": False, "error": f"HTTP {e.code}: {e.reason}",
                    "result": {"status": e.code}}
        except Exception as e:
            return {"id": cid, "ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}

    if kind == "firmware_check_sync":
        # End-to-end firmware check: fire /firmware/check, poll
        # /firmware/upgradestatus until the run reports back, then
        # read fresh /firmware/status. Server gets a single result
        # with up-to-date data — UI doesn't need to wait for the next
        # 1-min full status push to see the new package list.
        s = load_settings()
        if not s.get("local_api_key"):
            return {"id": cid, "ok": False, "error": "local API not configured"}
        base = s["local_api_url"]
        usr = s["local_api_key"]; pw = s["local_api_secret"]
        try:
            # 1) trigger
            req = urllib.request.Request(
                f"{base}/api/core/firmware/check",
                data=b"{}", method="POST",
            )
            req.add_header("Content-Type", "application/json")
            tok = base64.b64encode(f"{usr}:{pw}".encode()).decode()
            req.add_header("Authorization", f"Basic {tok}")
            with urllib.request.urlopen(req, context=_ssl_ctx(False), timeout=LOCAL_TIMEOUT) as r:
                r.read()
            # 2) poll status (max ~20 s, 0.5 s steps — OPNsense's check
            # typically finishes in 5-10 s, this gives us ~2x headroom)
            done = False
            for _ in range(40):
                time.sleep(0.5)
                try:
                    body = http_get_json(
                        f"{base}/api/core/firmware/upgradestatus",
                        basic_user=usr, basic_pass=pw,
                        verify=False, timeout=LOCAL_TIMEOUT,
                    )
                except Exception:
                    continue
                st = (body.get("status") or "").lower()
                if st in ("done", "reboot", "error"):
                    done = True
                    break
            # 3) read fresh /firmware/status (also if poll timed out — at
            # worst we serve slightly-stale data, never block the user)
            try:
                fw = http_get_json(
                    f"{base}/api/core/firmware/status",
                    basic_user=usr, basic_pass=pw,
                    verify=False, timeout=LOCAL_TIMEOUT,
                )
            except Exception as e:
                return {"id": cid, "ok": False,
                        "error": f"firmware/status read failed: {str(e)[:200]}"}
            return {
                "id": cid, "ok": True,
                "result": {
                    "status": 200,
                    "body": fw,
                    "check_completed": done,
                },
            }
        except Exception as e:
            return {"id": cid, "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:300]}"}

    if kind == "force_status_push":
        # Bypass the 1-min cron tick: gather a fresh payload and POST it
        # to /api/fleet/ingest right now. Used by the UI 'Liste neu
        # laden'-button so a freshly installed plugin shows up in seconds.
        # We do NOT call cmd_run() here — that would spawn a parallel
        # quick-poll loop overlapping with the one we're inside. Just
        # the ingest is enough; future pending commands ride along on
        # this current quick-poll's next iteration as usual.
        s = load_settings()
        if not s.get("server_url") or not s.get("agent_token"):
            return {"id": cid, "ok": False, "error": "agent not configured"}
        try:
            payload = gather_payload(s)
            ingest_url = f"{s['server_url']}/api/fleet/ingest/{s['agent_token']}"
            resp = http_post_json(
                ingest_url, payload, verify=s["verify_tls"],
                timeout=SERVER_TIMEOUT,
            )
            return {
                "id": cid, "ok": True,
                "result": {
                    "rules_seen": resp.get("rules_seen") or 0,
                    "plugins_seen": resp.get("plugins_seen") or 0,
                    "certs_seen": resp.get("certs_seen") or 0,
                },
            }
        except Exception as e:
            return {"id": cid, "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:300]}"}

    if kind == "plugin_install_pkg":
        # Download a .pkg from a URL and install it via `pkg add`.
        # Used for tracked plugin sources (e.g. GitHub releases) — the
        # server already validated the URL came from an admin-registered
        # source, so we just trust the cmd at this point.
        url = (args.get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            return {"id": cid, "ok": False, "error": "url must be http(s)"}
        # Belt-and-braces: only fetch from github.com / github releases
        # CDN. Accepting any URL would let a compromised server install
        # arbitrary FreeBSD packages on every box.
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        ALLOWED_HOSTS = (
            "github.com",
            "objects.githubusercontent.com",
            "release-assets.githubusercontent.com",
        )
        if host not in ALLOWED_HOSTS and not host.endswith(".githubusercontent.com"):
            return {"id": cid, "ok": False,
                    "error": f"refusing pkg download from {host} — only github.com / githubusercontent.com allowed"}
        import tempfile
        try:
            tmpdir = tempfile.mkdtemp(prefix="osf-pkg-")
            pkg_path = os.path.join(tmpdir, "plugin.pkg")
            req = urllib.request.Request(url, method="GET",
                                         headers={"User-Agent": "os-fleet-agent"})
            with urllib.request.urlopen(req, context=_ssl_ctx(True),
                                         timeout=120) as r, open(pkg_path, "wb") as f:
                shutil.copyfileobj(r, f)
            size = os.path.getsize(pkg_path)
            if size < 1024:
                return {"id": cid, "ok": False,
                        "error": f"download too small ({size} bytes), refusing to install"}
            # `pkg add` ist non-interactive by default (kein -y nötig,
            # das gibt's nur bei `pkg install`). -f erzwingt Re-Install
            # falls die Version schon installiert ist — der typische
            # Use-Case bei Dev-Updates.
            proc = subprocess.run(
                ["/usr/local/sbin/pkg", "add", "-f", pkg_path],
                capture_output=True, text=True, timeout=180,
            )
            stdout = (proc.stdout or "")[-4000:]
            stderr = (proc.stderr or "")[-1000:]
            ok = proc.returncode == 0
            return {
                "id": cid, "ok": ok,
                "result": {
                    "size_bytes": size,
                    "exit_code": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                },
                "error": None if ok else f"pkg add exit {proc.returncode}: {stderr.strip()[:300]}",
            }
        except Exception as e:
            return {"id": cid, "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:300]}"}
        finally:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

    if kind == "legacy_filter_toggle":
        return _legacy_filter_op(cid, args, op="toggle")

    if kind == "legacy_filter_update":
        return _legacy_filter_op(cid, args, op="update")

    if kind == "legacy_nat_toggle":
        return _legacy_xml_op(cid, args, op="toggle",
                              container_path="nat",
                              hash_fn=_legacy_nat_hash, variant="nat-pf")
    if kind == "legacy_nat_delete":
        return _legacy_xml_op(cid, args, op="delete",
                              container_path="nat",
                              hash_fn=_legacy_nat_hash, variant="nat-pf")
    if kind == "legacy_nat_update":
        return _legacy_xml_op(cid, args, op="update",
                              container_path="nat",
                              hash_fn=_legacy_nat_hash, variant="nat-pf")

    if kind == "legacy_outbound_toggle":
        return _legacy_xml_op(cid, args, op="toggle",
                              container_path="nat/outbound",
                              hash_fn=_legacy_outbound_hash, variant="nat-outbound")
    if kind == "legacy_outbound_delete":
        return _legacy_xml_op(cid, args, op="delete",
                              container_path="nat/outbound",
                              hash_fn=_legacy_outbound_hash, variant="nat-outbound")
    if kind == "legacy_outbound_update":
        return _legacy_xml_op(cid, args, op="update",
                              container_path="nat/outbound",
                              hash_fn=_legacy_outbound_hash, variant="nat-outbound")

    if kind == "legacy_nat_create":
        return _legacy_xml_create(cid, args,
                                  container_path="nat",
                                  variant="nat-pf")
    if kind == "legacy_outbound_create":
        return _legacy_xml_create(cid, args,
                                  container_path="nat/outbound",
                                  variant="nat-outbound")

    if kind == "legacy_filter_delete":
        return _legacy_filter_op(cid, args, op="delete")

    return {"id": cid, "ok": False, "error": f"unsupported command kind: {kind!r}"}


# ---------------------------------------------------------------------------
# Legacy filter rule editing
# ---------------------------------------------------------------------------
import hashlib
import shutil
import subprocess


def _normalize(s: str) -> str:
    return (s or "").strip()


def _flag(elem):
    """OPNsense boolean idiom: <foo>1</foo> AND empty <foo/> both mean
    'true'. Absence or any other text means 'false'. Must match the
    server's _flag() exactly so legacy_rule_hash agrees on both ends."""
    if elem is None:
        return False
    txt = (elem.text or "").strip()
    return txt == "1" or txt == ""


def _addr_render(elem):
    """Mirror of the server's _addr_block — must produce identical
    strings for the hash to match across both ends."""
    if elem is None:
        return ("", "")
    not_flag = "!" if _flag(elem.find("not")) else ""
    if elem.find("any") is not None and _flag(elem.find("any")):
        host = "any"
    elif elem.find("address") is not None:
        host = (elem.find("address").text or "?").strip()
    elif elem.find("network") is not None:
        host = (elem.find("network").text or "?").strip()
    else:
        host = ""
    p = elem.find("port")
    port = (p.text or "").strip() if p is not None else ""
    return (f"{not_flag}{host}" if host else "", port)


def _legacy_rule_hash(rule_elem) -> str:
    """SHA-256 over the rule's identifying fields. Same recipe as the
    server, so a hash collision means 'this is the rule we mean'."""
    src_h, src_p = _addr_render(rule_elem.find("source"))
    dst_h, dst_p = _addr_render(rule_elem.find("destination"))

    def _t(name):
        el = rule_elem.find(name)
        return _normalize(el.text if el is not None and el.text else "")

    parts = [
        _t("interface"),
        _t("type"),
        _t("direction"),
        _t("ipprotocol"),
        _t("protocol"),
        src_h, src_p, dst_h, dst_p,
        _t("descr"),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _legacy_nat_hash(rule_elem) -> str:
    """Hash for <nat><rule> port-forwards — must match server's
    _legacy_nat_hash byte-for-byte."""
    src_h, src_p = _addr_render(rule_elem.find("source"))
    dst_h, dst_p = _addr_render(rule_elem.find("destination"))

    def _t(name):
        el = rule_elem.find(name)
        return _normalize(el.text if el is not None and el.text else "")

    parts = [
        _t("interface"),
        _t("ipprotocol"),
        _t("protocol"),
        src_h, src_p, dst_h, dst_p,
        _t("target"),
        _t("local-port"),
        _t("descr"),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _legacy_outbound_hash(rule_elem) -> str:
    """Hash for <nat><outbound><rule> SNAT rules — must match
    server's _legacy_outbound_hash."""
    src_h, src_p = _addr_render(rule_elem.find("source"))
    dst_h, dst_p = _addr_render(rule_elem.find("destination"))

    def _t(name):
        el = rule_elem.find(name)
        return _normalize(el.text if el is not None and el.text else "")

    parts = [
        _t("interface"),
        _t("ipprotocol"),
        _t("protocol"),
        src_h, src_p, dst_h, dst_p,
        _t("target"),
        _t("descr"),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _legacy_filter_op(cid, args: dict, *, op: str) -> dict:
    """Compatibility shim — delegates to the generic _legacy_xml_op
    so existing call-sites keep working."""
    return _legacy_xml_op(
        cid, args, op=op,
        container_path="filter", hash_fn=_legacy_rule_hash,
        variant="filter",
    )


def _legacy_xml_create(cid, args: dict, *,
                       container_path: str, variant: str) -> dict:
    """Append a new <rule> to root/<container_path> in /conf/config.xml.

    args.rule is a dict of fields; we map them to the variant's XML
    schema (see _set_text/_set_addr below) and write the result with
    the same backup/atomic-write/sanity-check/reload pattern as the
    edit op. Hash validation isn't applicable — there's no existing
    rule to drift against."""
    if not LEGACY_EDIT_ENABLED:
        return {"id": cid, "ok": False,
                "error": "legacy edit not enabled (set OSF_AGENT_ALLOW_LEGACY_EDIT=1)"}
    rule_data = args.get("rule") or {}
    if not isinstance(rule_data, dict) or not rule_data:
        return {"id": cid, "ok": False, "error": "rule payload missing"}

    # Backup
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"config-{variant}-create-{ts}.xml")
    try:
        shutil.copyfile(CONFIG_PATH, backup_path)
    except Exception as e:
        return {"id": cid, "ok": False, "error": f"backup failed: {e}"}

    # Parse + locate (creating intermediate containers if needed —
    # e.g. <nat>/<outbound> may not exist on a freshly-installed box)
    try:
        tree = ET.parse(CONFIG_PATH)
    except Exception as e:
        return {"id": cid, "ok": False, "error": f"config parse failed: {e}"}
    root = tree.getroot()
    container = root
    for part in container_path.split("/"):
        nxt = container.find(part)
        if nxt is None:
            nxt = ET.SubElement(container, part)
        container = nxt

    # Build the new <rule>. We share the helpers from the update branch
    # of _legacy_xml_op by inlining them here — yes, duplication, but
    # it keeps the create-path independent of the more delicate
    # update-path.
    target = ET.SubElement(container, "rule")

    def _set_text(parent, tag: str, value):
        v = "" if value is None else str(value).strip()
        if not v:
            return
        el = parent.find(tag)
        if el is None:
            el = ET.SubElement(parent, tag)
        el.text = v

    def _set_addr(parent, sub_tag: str, host: str, port: str):
        elem = ET.SubElement(parent, sub_tag)
        host = (host or "").strip()
        not_flag = host.startswith("!")
        if not_flag:
            host = host[1:]
            ET.SubElement(elem, "not").text = "1"
        if not host or host == "any":
            ET.SubElement(elem, "any").text = "1"
        elif "/" in host or host[:1].isdigit() or ":" in host:
            ET.SubElement(elem, "network").text = host
        else:
            ET.SubElement(elem, "address").text = host
        if port:
            ET.SubElement(elem, "port").text = str(port)

    # Field mapping per variant
    if variant == "filter":
        FIELD_MAP = {
            "action": "type", "interface": "interface",
            "ipprotocol": "ipprotocol", "protocol": "protocol",
            "description": "descr", "gateway": "gateway",
            "direction": "direction",
        }
    elif variant == "nat-pf":
        FIELD_MAP = {
            "interface": "interface", "ipprotocol": "ipprotocol",
            "protocol": "protocol", "description": "descr",
            "target": "target", "local_port": "local-port",
        }
    elif variant == "nat-outbound":
        FIELD_MAP = {
            "interface": "interface", "ipprotocol": "ipprotocol",
            "protocol": "protocol", "description": "descr",
            "target": "target",
        }
    else:
        FIELD_MAP = {}

    for src, dst in FIELD_MAP.items():
        if src in rule_data and rule_data[src]:
            _set_text(target, dst, rule_data[src])

    # Defaults: a brand-new rule should be enabled, NAT-PF defaults
    # ipprotocol to inet if caller omitted it.
    if variant == "nat-pf" and target.find("ipprotocol") is None:
        ET.SubElement(target, "ipprotocol").text = "inet"

    # source / destination
    src_h = rule_data.get("source_net", "")
    src_p = rule_data.get("source_port", "")
    dst_h = rule_data.get("destination_net", "")
    dst_p = rule_data.get("destination_port", "")
    _set_addr(target, "source", src_h, src_p)
    _set_addr(target, "destination", dst_h, dst_p)

    # quick + log presence-flags
    for flag_field in ("quick", "log"):
        if str(rule_data.get(flag_field, "")) == "1":
            ET.SubElement(target, flag_field).text = "1"

    # Atomic write
    tmp_path = CONFIG_PATH + ".osfleet-tmp"
    try:
        tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
    except Exception as e:
        return {"id": cid, "ok": False, "error": f"write failed: {e}"}
    try:
        ET.parse(tmp_path)
    except Exception as e:
        try: os.remove(tmp_path)
        except Exception: pass
        return {"id": cid, "ok": False, "error": f"validation failed: {e}"}
    try:
        os.rename(tmp_path, CONFIG_PATH)
    except Exception as e:
        return {"id": cid, "ok": False, "error": f"rename failed: {e}"}

    # Reload
    try:
        proc = subprocess.run(
            ["/usr/local/sbin/configctl", "filter", "reload"],
            timeout=30, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            shutil.copyfile(backup_path, CONFIG_PATH)
            subprocess.run(["/usr/local/sbin/configctl", "filter", "reload"],
                           timeout=30, capture_output=True)
            out = (proc.stdout + proc.stderr).strip()[:300]
            return {"id": cid, "ok": False,
                    "error": f"filter reload failed (rolled back): {out}"}
    except Exception as e:
        try: shutil.copyfile(backup_path, CONFIG_PATH)
        except Exception: pass
        return {"id": cid, "ok": False,
                "error": f"filter reload exception (rolled back): {e}"}

    # Cleanup backups
    try:
        all_backups = sorted(
            (os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR)
             if n.startswith("config-")),
            key=os.path.getmtime, reverse=True,
        )
        for old in all_backups[BACKUP_RETENTION:]:
            try: os.remove(old)
            except Exception: pass
    except Exception:
        pass

    return {
        "id": cid, "ok": True,
        "result": {
            "variant": variant,
            "fields": sorted(rule_data.keys()),
            "backup": backup_path,
        },
    }


def _legacy_xml_op(cid, args: dict, *, op: str,
                   container_path: str, hash_fn, variant: str) -> dict:
    """Toggle/delete/update a rule located at root/<container_path>/<rule>[index]
    in /conf/config.xml. Identified by index + content-hash to detect
    drift before mutating. Steps: backup → parse → locate → hash check
    → mutate (variant-specific) → atomic write → sanity-parse → filter
    reload → rollback on any failure.

    variant: 'filter' | 'nat-pf' | 'nat-outbound' — picks which extra
    XML fields the mutate-update branch knows about.
    """
    if not LEGACY_EDIT_ENABLED:
        return {"id": cid, "ok": False,
                "error": "legacy edit not enabled (set OSF_AGENT_ALLOW_LEGACY_EDIT=1)"}

    try:
        index = int(args.get("index"))
    except (TypeError, ValueError):
        return {"id": cid, "ok": False, "error": "missing/invalid 'index'"}
    expected_hash = (args.get("hash") or "").strip()
    if not expected_hash:
        return {"id": cid, "ok": False, "error": "missing 'hash'"}

    # Backup
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"config-{variant}-{op}-{ts}.xml")
    try:
        shutil.copyfile(CONFIG_PATH, backup_path)
    except Exception as e:
        return {"id": cid, "ok": False, "error": f"backup failed: {e}"}

    # Parse + locate. container_path is a slash-separated path of
    # element names beneath root (e.g. 'filter' or 'nat/outbound').
    try:
        tree = ET.parse(CONFIG_PATH)
    except Exception as e:
        return {"id": cid, "ok": False, "error": f"config parse failed: {e}"}
    root = tree.getroot()
    container = root
    for part in container_path.split("/"):
        container = container.find(part) if container is not None else None
    if container is None:
        return {"id": cid, "ok": False,
                "error": f"no <{container_path}> tree in config.xml"}
    rules = container.findall("rule")
    if index < 0 or index >= len(rules):
        return {"id": cid, "ok": False,
                "error": f"index {index} out of range (have {len(rules)})"}
    target = rules[index]
    actual_hash = hash_fn(target)
    if actual_hash != expected_hash:
        return {"id": cid, "ok": False,
                "error": f"hash mismatch (expected {expected_hash}, got {actual_hash}) — rule changed since last probe; reload UI and retry"}
    # Bind names the rest of the function uses
    flt = container

    # Mutate
    if op == "toggle":
        dis = target.find("disabled")
        if dis is not None:
            target.remove(dis)
            new_state = "enabled"
        else:
            ET.SubElement(target, "disabled").text = "1"
            new_state = "disabled"
    elif op == "delete":
        flt.remove(target)
        new_state = "deleted"
    elif op == "update":
        # Apply field-by-field updates to the existing rule. The legacy
        # config.xml structure is flat-ish per rule:
        #   <type>pass|block|reject</type>
        #   <interface>lan</interface>
        #   <ipprotocol>inet|inet6</ipprotocol>
        #   <protocol>tcp</protocol>
        #   <descr>...</descr>
        #   <quick>1</quick>
        #   <disabled>1</disabled> (presence = disabled)
        #   <source>…</source>      sub-tree
        #   <destination>…</destination>  sub-tree
        # We map our normalised _EDITABLE_FIELDS keys onto these tags.
        updates = args.get("updates") or {}
        if not isinstance(updates, dict) or not updates:
            return {"id": cid, "ok": False, "error": "updates dict is empty"}

        def _set_text(parent, tag: str, value: str):
            el = parent.find(tag)
            if value == "" or value is None:
                if el is not None:
                    parent.remove(el)
                return
            if el is None:
                el = ET.SubElement(parent, tag)
            el.text = str(value)

        def _set_addr(parent, sub_tag: str, host: str, port: str):
            """Rewrite <source>/<destination>. host can be 'any', a CIDR,
            'iface', 'iface:ip', '!host' (not), or empty (= any)."""
            elem = parent.find(sub_tag)
            if elem is None:
                elem = ET.SubElement(parent, sub_tag)
            # Wipe existing children — we fully rebuild this sub-tree
            for child in list(elem):
                elem.remove(child)
            host = (host or "").strip()
            not_flag = host.startswith("!")
            if not_flag:
                host = host[1:]
                ET.SubElement(elem, "not").text = "1"
            if not host or host == "any":
                ET.SubElement(elem, "any").text = "1"
            elif "/" in host or host[:1].isdigit() or ":" in host:
                ET.SubElement(elem, "network").text = host
            else:
                # Looks like an alias name or plain hostname
                ET.SubElement(elem, "address").text = host
            if port:
                ET.SubElement(elem, "port").text = str(port)

        # enabled flips the <disabled/> presence
        if "enabled" in updates:
            want = str(updates["enabled"]) == "1"
            dis = target.find("disabled")
            if want and dis is not None:
                target.remove(dis)
            elif not want and dis is None:
                ET.SubElement(target, "disabled").text = "1"

        # Direct text fields. The base set works for filter-rules;
        # NAT variants don't carry <type>/<direction>/<gateway> but DO
        # carry <target> and (for port-forward) <local-port>.
        if variant == "filter":
            FIELD_MAP = {
                "action": "type",
                "interface": "interface",
                "ipprotocol": "ipprotocol",
                "protocol": "protocol",
                "description": "descr",
                "gateway": "gateway",
                "direction": "direction",
            }
        elif variant == "nat-pf":
            FIELD_MAP = {
                "interface": "interface",
                "ipprotocol": "ipprotocol",
                "protocol": "protocol",
                "description": "descr",
                "target": "target",
                "local_port": "local-port",
            }
        elif variant == "nat-outbound":
            FIELD_MAP = {
                "interface": "interface",
                "ipprotocol": "ipprotocol",
                "protocol": "protocol",
                "description": "descr",
                "target": "target",
            }
        else:
            FIELD_MAP = {}
        for src, dst in FIELD_MAP.items():
            if src in updates:
                _set_text(target, dst, updates[src])

        # quick + log are presence-flags like disabled
        for flag_field in ("quick", "log"):
            if flag_field in updates:
                want = str(updates[flag_field]) == "1"
                el = target.find(flag_field)
                if want and el is None:
                    ET.SubElement(target, flag_field).text = "1"
                elif not want and el is not None:
                    target.remove(el)

        # source/destination are sub-trees. We only rebuild when at least
        # one half (host or port) is present in updates so we don't
        # accidentally wipe the existing port when only host changed.
        src_host = updates.get("source_net")
        src_port = updates.get("source_port")
        if src_host is not None or src_port is not None:
            existing_port = ""
            if src_port is None:
                p = target.find("source")
                if p is not None and p.find("port") is not None:
                    existing_port = (p.find("port").text or "").strip()
                src_port = existing_port
            if src_host is None:
                # Keep current host structure if only port changed
                p = target.find("source")
                if p is not None:
                    pe = p.find("port")
                    if src_port:
                        if pe is None:
                            ET.SubElement(p, "port").text = str(src_port)
                        else:
                            pe.text = str(src_port)
                    elif pe is not None:
                        p.remove(pe)
            else:
                _set_addr(target, "source", src_host, src_port or "")

        dst_host = updates.get("destination_net")
        dst_port = updates.get("destination_port")
        if dst_host is not None or dst_port is not None:
            existing_port = ""
            if dst_port is None:
                p = target.find("destination")
                if p is not None and p.find("port") is not None:
                    existing_port = (p.find("port").text or "").strip()
                dst_port = existing_port
            if dst_host is None:
                p = target.find("destination")
                if p is not None:
                    pe = p.find("port")
                    if dst_port:
                        if pe is None:
                            ET.SubElement(p, "port").text = str(dst_port)
                        else:
                            pe.text = str(dst_port)
                    elif pe is not None:
                        p.remove(pe)
            else:
                _set_addr(target, "destination", dst_host, dst_port or "")

        new_state = f"updated:{','.join(sorted(updates.keys()))}"
    else:
        return {"id": cid, "ok": False, "error": f"unsupported op: {op}"}

    # Atomic write to a temp file inside /conf so rename is atomic on
    # the same filesystem.
    tmp_path = CONFIG_PATH + ".osfleet-tmp"
    try:
        tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
    except Exception as e:
        return {"id": cid, "ok": False, "error": f"write failed: {e}"}

    # Sanity-check the new file by re-parsing it.
    try:
        ET.parse(tmp_path)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return {"id": cid, "ok": False, "error": f"validation failed: {e}"}

    # Replace + reload.
    try:
        os.rename(tmp_path, CONFIG_PATH)
    except Exception as e:
        return {"id": cid, "ok": False, "error": f"rename failed: {e}"}

    try:
        proc = subprocess.run(
            ["/usr/local/sbin/configctl", "filter", "reload"],
            timeout=30, capture_output=True, text=True,
        )
        reload_out = (proc.stdout + proc.stderr).strip()[:300]
        if proc.returncode != 0:
            # Rollback from backup since reload failed
            shutil.copyfile(backup_path, CONFIG_PATH)
            subprocess.run(["/usr/local/sbin/configctl", "filter", "reload"],
                           timeout=30, capture_output=True)
            return {"id": cid, "ok": False,
                    "error": f"filter reload failed (rolled back): {reload_out}"}
    except Exception as e:
        try:
            shutil.copyfile(backup_path, CONFIG_PATH)
        except Exception:
            pass
        return {"id": cid, "ok": False, "error": f"filter reload exception (rolled back): {e}"}

    # Cleanup older backups (keep BACKUP_RETENTION).
    try:
        all_backups = sorted(
            (os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR)
             if n.startswith("config-")),
            key=os.path.getmtime, reverse=True,
        )
        for old in all_backups[BACKUP_RETENTION:]:
            try: os.remove(old)
            except Exception: pass
    except Exception:
        pass

    return {
        "id": cid, "ok": True,
        "result": {
            "op": op, "new_state": new_state,
            "index": index, "hash": expected_hash,
            "backup": backup_path,
        },
    }


def cmd_run(s: dict) -> tuple[str, str, dict]:
    if not s.get("enabled"):
        return ("disabled", "agent disabled in settings", {})
    if not s.get("server_url") or not s.get("agent_token"):
        return ("error", "server_url + agent_token required", {})
    if not s.get("local_api_key") or not s.get("local_api_secret"):
        return ("error", "local_api_key + local_api_secret required", {})

    started = time.time()
    try:
        payload = gather_payload(s)
    except urllib.error.HTTPError as e:
        return ("error", f"local API HTTP {e.code}: {e.reason}", {})
    except Exception as e:
        return ("error", f"local gather failed: {type(e).__name__}: {e}", {})

    # Attach any results from previous-cycle commands that we still
    # need to ack to the server.
    pending_results = _load_pending_results()
    if pending_results:
        payload["command_results"] = pending_results

    payload_size = len(json.dumps(payload))
    ingest_url = f"{s['server_url']}/api/fleet/ingest/{s['agent_token']}"
    try:
        resp = http_post_json(ingest_url, payload, verify=s["verify_tls"],
                              timeout=SERVER_TIMEOUT)
    except urllib.error.HTTPError as e:
        return ("error", f"server HTTP {e.code}: {e.reason}", {})
    except Exception as e:
        return ("error", f"server post failed: {type(e).__name__}: {e}", {})

    # Server ack'd our results — clear the stash. Do this before running
    # new commands so a crash mid-execution can't double-report.
    _stash_results([])

    # Run pending_commands the server handed us. Results land in the
    # stash, get sent in the *next* run. (This intentionally doesn't loop
    # ingest within one run — keeps each run bounded in time.)
    pending = resp.get("pending_commands") or []
    new_results: list[dict] = []
    for cmd_obj in pending:
        if not isinstance(cmd_obj, dict):
            continue
        try:
            new_results.append(_exec_command(cmd_obj, s))
        except Exception as e:
            new_results.append({
                "id": cmd_obj.get("id"), "ok": False,
                "error": f"agent crash: {type(e).__name__}: {str(e)[:200]}",
            })
    if new_results:
        _stash_results(new_results)

    # --- Quick-poll loop ------------------------------------------------
    # After the heavy push we stay around for ~50 s and ping the server
    # every QUICK_POLL_SLEEP seconds for newly queued commands. Each
    # quick request carries any results we just produced and may produce
    # more, which ride along to the next iteration's request.
    quick_url = f"{s['server_url']}/api/fleet/ingest/{s['agent_token']}/quick"
    quick_iters = 0
    quick_cmds = 0
    for _ in range(QUICK_POLL_ITERATIONS):
        try:
            time.sleep(QUICK_POLL_SLEEP)
        except Exception:
            break
        quick_iters += 1
        stash = _load_pending_results()
        body = {"command_results": stash} if stash else {}
        try:
            qresp = http_post_json(quick_url, body, verify=s["verify_tls"],
                                   timeout=QUICK_POLL_TIMEOUT)
        except Exception:
            # Server unreachable mid-cycle — abandon quick-loop, the
            # next cron tick (≤60 s) will retry the full push.
            break
        # Server ack'd whatever we sent; clear the stash before running
        # newly-handed-out commands so a crash mid-execution can't
        # double-report.
        if stash:
            _stash_results([])
        qpending = qresp.get("pending_commands") or []
        if not qpending:
            continue
        new_q_results: list[dict] = []
        for cmd_obj in qpending:
            if not isinstance(cmd_obj, dict):
                continue
            try:
                new_q_results.append(_exec_command(cmd_obj, s))
            except Exception as e:
                new_q_results.append({
                    "id": cmd_obj.get("id"), "ok": False,
                    "error": f"agent crash: {type(e).__name__}: {str(e)[:200]}",
                })
        if new_q_results:
            _stash_results(new_q_results)
            quick_cmds += len(new_q_results)
    # ---------------------------------------------------------------------

    elapsed = time.time() - started
    rules = resp.get("rules_seen") or 0
    plugins = resp.get("plugins_seen") or 0
    certs = resp.get("certs_seen") or 0
    extra = {
        "elapsed_seconds": round(elapsed, 2),
        "rules_seen": rules,
        "plugins_seen": plugins,
        "certs_seen": certs,
        "payload_bytes": payload_size,
        "server_url": s["server_url"],
        "needs_reboot": bool(resp.get("needs_reboot")),
        "updates_pending": int(resp.get("updates_pending") or 0),
        "commands_received": len(pending),
        "commands_executed": len(new_results),
        "results_acked": int(resp.get("results_acked") or 0),
        "quick_iters": quick_iters,
        "quick_cmds": quick_cmds,
    }
    msg = f"sent in {elapsed:.1f}s · {rules} rules · {plugins} plugins · {certs} certs"
    if pending or quick_cmds:
        msg += f" · cmds: {len(pending) + quick_cmds} executed (quick: {quick_cmds})"
    return ("ok", msg, extra)


def cmd_setup(s: dict) -> tuple[str, str]:
    """(Re-)register a cron entry that runs `configctl agent run` every
    interval_minutes minutes. Implementation: write a stub crontab line
    via /usr/local/sbin/configctl cron action — but for first iteration
    we keep it simple and rely on operator running `configctl agent run`
    via existing OPNsense cron model in the GUI."""
    return ("ok", f"setup acknowledged (interval={s.get('interval_minutes')} min)")


# --- Entry point -------------------------------------------------------------


def main() -> int:
    sub = sys.argv[1] if len(sys.argv) > 1 else "run"
    try:
        s = load_settings()
    except Exception as e:
        print(f"settings load failed: {e}")
        return 2

    extra: dict = {}
    if sub == "test":
        status, msg = cmd_test(s)
    elif sub == "run":
        status, msg, extra = cmd_run(s)
    elif sub == "setup":
        status, msg = cmd_setup(s)
    else:
        status, msg = ("error", f"unknown subcommand: {sub!r}")

    print(f"{status}: {msg}")
    write_status(status, msg, **extra)
    return 0 if status in ("ok", "disabled") else 1


if __name__ == "__main__":
    sys.exit(main())
