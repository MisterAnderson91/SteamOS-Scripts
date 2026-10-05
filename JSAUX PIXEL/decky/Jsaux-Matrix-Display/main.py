import asyncio
import fcntl
import json
import os
import random
import re
import shutil
import ssl
import sys
import tempfile
import threading
import time
import base64
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from glob import glob

import decky

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from backend.client import DaemonHttpClient


CHRONOGRAPH_MAX_SECONDS = (99 * 3600) + (59 * 60) + 59
PLAYTIME_REFRESH_SECONDS = 2.0

CUSTOM_ASSET_EXTENSIONS = frozenset({".png", ".gif", ".svg", ".jpg", ".jpeg", ".bmp"})
CUSTOM_ASSET_DIRECTORY_LIMIT = 24
CUSTOM_ASSET_PREVIEW_LIMIT_BYTES = 512 * 1024

# AI Agent(GPT/Codex)额度与会话监控,逻辑移植自 gpt_quota_monitor.py
AI_AGENT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
AI_AGENT_TOKEN_URL = "https://auth.openai.com/oauth/token"
AI_AGENT_CONVERSATIONS_URL = (
    "https://chatgpt.com/backend-api/conversations?offset=0&limit=20&order=updated"
)
AI_AGENT_DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXpYh7rann"
AI_AGENT_SESSION_ACTIVE_MINUTES = 5
AI_AGENT_USAGE_TTL_SECONDS = 600.0
AI_AGENT_WEB_TTL_SECONDS = 60.0
AI_AGENT_TOKENS_TTL_SECONDS = 300.0
AI_AGENT_SCREEN_REFRESH_MIN_SECONDS = 300.0
AI_AGENT_SCREEN_REFRESH_MAX_SECONDS = 720.0
AI_AGENT_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
AI_AGENT_ACCOUNTS_FILENAME = "ai_agent_accounts.json"
AI_AGENT_ACCOUNTS_DIRNAME = "ai_agent_accounts"
AI_AGENT_LOGIN_URL_RE = re.compile(r"https://[^\s\"']+", re.IGNORECASE)
_ai_agent_usage_cache: dict = {}
_ai_agent_web_cache: dict = {"ts": 0.0, "items": []}
_ai_agent_tokens_cache: dict = {"ts": 0.0, "totals": None}
_ai_agent_login: dict = {"proc": None, "output": [], "auth_mtime": 0.0}
_ai_agent_ssl_context: ssl.SSLContext | None = None


def _ai_agent_https_context() -> ssl.SSLContext:
    """Decky 的 PyInstaller Python 默认 CA 路径失效，显式回落到系统 CA 包。"""
    global _ai_agent_ssl_context
    if _ai_agent_ssl_context is None:
        candidates = [os.environ.get("SSL_CERT_FILE", "").strip(),
                      "/etc/ssl/cert.pem",
                      "/etc/ssl/certs/ca-certificates.crt"]
        try:
            import certifi
            candidates.append(certifi.where())
        except Exception:
            pass
        for cafile in candidates:
            if cafile and os.path.isfile(cafile):
                _ai_agent_ssl_context = ssl.create_default_context(cafile=cafile)
                break
        else:
            _ai_agent_ssl_context = ssl.create_default_context()
    return _ai_agent_ssl_context


def _ai_agent_codex_home() -> Path:
    override = os.environ.get("JSAUX_AI_AGENT_CODEX_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path.home() / ".codex"
    # Decky loader runs the plugin as root (HOME=/root); the deck user's
    # codex credentials live under /home/deck and are the canonical store.
    deck_home = Path("/home/deck/.codex")
    if home != deck_home and deck_home.is_dir():
        return deck_home
    return home


def _ai_agent_local_auth_path() -> Path:
    return _ai_agent_codex_home() / "auth.json"


def _ai_agent_accounts_file() -> Path:
    override = os.environ.get("JSAUX_AI_AGENT_ACCOUNTS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return PLUGIN_ROOT / "data" / AI_AGENT_ACCOUNTS_FILENAME


def _ai_agent_accounts_dir() -> Path:
    return PLUGIN_ROOT / "data" / AI_AGENT_ACCOUNTS_DIRNAME


def _ai_agent_sanitize_account_name(name: str) -> str:
    cleaned = re.sub(r"[^\w一-鿿-]", "_", str(name or "").strip())
    return cleaned.strip("_")[:32]


def _ai_agent_library_account_path(name: str) -> Path:
    return _ai_agent_accounts_dir() / f"{_ai_agent_sanitize_account_name(name)}.json"


def _ai_agent_load_library_accounts() -> list:
    try:
        with _ai_agent_accounts_file().open(encoding="utf-8") as f:
            accounts = json.load(f).get("accounts") or []
    except FileNotFoundError:
        return []
    except Exception as exc:
        decky.logger.info(f"AI Agent accounts.json read failed: {exc}")
        return []
    return [a for a in accounts if isinstance(a, dict) and a.get("name")]


def _ai_agent_save_library_accounts(accounts: list) -> None:
    path = _ai_agent_accounts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _ai_agent_file_account_id(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as f:
            return ((json.load(f) or {}).get("tokens") or {}).get("account_id")
    except Exception:
        return None


def _ai_agent_collect_specs() -> list:
    """本机账号始终第一；账号库其余账号去重后追加（与本机同一 account_id 的跳过）。"""
    local_path = _ai_agent_local_auth_path()
    specs = [{"name": "本机账号", "auth_file": str(local_path), "active": True}]
    local_id = _ai_agent_file_account_id(local_path) if local_path.exists() else None
    for entry in _ai_agent_load_library_accounts():
        raw = str(entry.get("auth_file", ""))
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PLUGIN_ROOT / raw
        if not path.exists():
            specs.append({"name": str(entry["name"]), "auth_file": "", "active": False,
                          "error": "account_file_missing"})
            continue
        if local_id and _ai_agent_file_account_id(path) == local_id:
            continue
        specs.append({"name": str(entry["name"]), "auth_file": str(path), "active": False})
    return specs


def _ai_agent_next_account_name() -> str:
    existing = {a["name"] for a in _ai_agent_load_library_accounts()}
    index = 2
    while f"账号 {index}" in existing:
        index += 1
    return "账号 2" if index == 2 else f"账号 {index}"


def _ai_agent_import_account(name: str | None = None) -> dict:
    local_path = _ai_agent_local_auth_path()
    if not local_path.exists():
        return {"ok": False, "error": "ai_agent_no_credentials"}
    account_name = _ai_agent_sanitize_account_name(name) or _ai_agent_next_account_name()
    account_id = _ai_agent_file_account_id(local_path)
    accounts = _ai_agent_load_library_accounts()
    for entry in accounts:
        entry_path = _ai_agent_library_account_path(str(entry["name"]))
        if entry_path.exists() and _ai_agent_file_account_id(entry_path) == account_id:
            return {"ok": False, "error": "ai_agent_account_already_imported"}
    _ai_agent_accounts_dir().mkdir(parents=True, exist_ok=True)
    target = _ai_agent_library_account_path(account_name)
    shutil.copyfile(local_path, target)
    os.chmod(target, 0o600)
    accounts.append({"name": account_name, "auth_file": str(target)})
    _ai_agent_save_library_accounts(accounts)
    return {"ok": True, "name": account_name}


def _ai_agent_enable_account(name: str) -> dict:
    source = _ai_agent_library_account_path(name)
    if not source.exists():
        return {"ok": False, "error": "ai_agent_account_not_found"}
    local_path = _ai_agent_local_auth_path()
    if local_path.exists():
        current_id = _ai_agent_file_account_id(local_path)
        known_ids = {
            _ai_agent_file_account_id(_ai_agent_library_account_path(str(entry["name"])))
            for entry in _ai_agent_load_library_accounts()
        }
        if current_id and current_id not in known_ids:
            # 启用前先把当前本机账号备份进账号库，避免丢失登录态
            backup_name = _ai_agent_next_account_name()
            _ai_agent_accounts_dir().mkdir(parents=True, exist_ok=True)
            backup_path = _ai_agent_library_account_path(backup_name)
            shutil.copyfile(local_path, backup_path)
            os.chmod(backup_path, 0o600)
            accounts = _ai_agent_load_library_accounts()
            accounts.append({"name": backup_name, "auth_file": str(backup_path)})
            _ai_agent_save_library_accounts(accounts)
    else:
        _ai_agent_codex_home().mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, local_path)
    os.chmod(local_path, 0o600)
    return {"ok": True, "enabled": _ai_agent_sanitize_account_name(name)}


def _ai_agent_device_id() -> str:
    path = PLUGIN_ROOT / "data" / "ai_agent_device_id"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            did = path.read_text(encoding="utf-8").strip()
            if did:
                return did
        did = str(uuid.uuid4())
        path.write_text(did, encoding="utf-8")
        return did
    except Exception:
        return str(uuid.uuid4())


def _ai_agent_jwt_client_id(access_token: str) -> str:
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        aud = json.loads(base64.urlsafe_b64decode(payload)).get("aud")
        return aud if isinstance(aud, str) else AI_AGENT_DEFAULT_CLIENT_ID
    except Exception:
        return AI_AGENT_DEFAULT_CLIENT_ID


def _ai_agent_load_auth(path: Path | None = None) -> dict:
    auth: dict = {"access_token": None, "refresh_token": None, "account_id": None}
    target = path or _ai_agent_local_auth_path()
    try:
        with target.open(encoding="utf-8") as f:
            tokens = (json.load(f) or {}).get("tokens") or {}
        auth["access_token"] = tokens.get("access_token")
        auth["refresh_token"] = tokens.get("refresh_token")
        auth["account_id"] = tokens.get("account_id")
    except FileNotFoundError:
        pass
    except Exception as exc:
        decky.logger.info(f"AI Agent auth.json read failed: {exc}")
    return auth


def _ai_agent_save_auth(tokens: dict, path: Path | None = None) -> None:
    path = path or _ai_agent_local_auth_path()
    try:
        with path.open(encoding="utf-8") as f:
            auth = json.load(f)
    except Exception:
        auth = {}
    auth["tokens"] = {**(auth.get("tokens") or {}), **tokens}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(auth, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _ai_agent_refresh_access_token(refresh_token: str, client_id: str,
                                   save_path: Path | None = None) -> dict | None:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        AI_AGENT_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ai_agent_https_context()) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:
        decky.logger.info(f"AI Agent token refresh failed: {exc}")
        return None
    tokens = {
        "access_token": data.get("access_token"),
        "id_token": data.get("id_token"),
        "refresh_token": data.get("refresh_token", refresh_token),
    }
    try:
        _ai_agent_save_auth(tokens, save_path)
    except Exception as exc:
        decky.logger.info(f"AI Agent auth.json write-back failed: {exc}")
    return tokens


def _ai_agent_norm_window(window: dict | None) -> dict | None:
    if not window:
        return None
    return {
        "used_percent": window.get("used_percent"),
        "window_seconds": window.get("limit_window_seconds"),
        "reset_at": window.get("reset_at"),
        "reset_after_seconds": window.get("reset_after_seconds"),
    }


def _ai_agent_fetch_usage(auth: dict, force: bool = False,
                          cache_key: str = "local") -> dict:
    now = time.time()
    cached_entry = _ai_agent_usage_cache.get(cache_key)
    if (
        not force
        and cached_entry is not None
        and now - cached_entry["ts"] < AI_AGENT_USAGE_TTL_SECONDS
    ):
        return cached_entry["account"]

    result = {
        "name": "本机账号", "email": None, "plan_type": None,
        "allowed": None, "limit_reached": None,
        "primary": None, "secondary": None, "extra_limits": [],
        "credits_balance": None, "error": None,
        "fetched_at": int(now),
    }
    if not auth.get("access_token"):
        result["error"] = "ai_agent_no_credentials"
        return result

    data = None
    for attempt in (1, 2):
        req = urllib.request.Request(AI_AGENT_USAGE_URL, headers={
            "Authorization": f"Bearer {auth['access_token']}",
            "chatgpt-account-id": auth.get("account_id") or "",
            "User-Agent": "codex_cli_rs",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15, context=_ai_agent_https_context()) as r:
                data = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and attempt == 1 and auth.get("refresh_token"):
                refreshed = _ai_agent_refresh_access_token(
                    auth["refresh_token"],
                    _ai_agent_jwt_client_id(auth["access_token"] or ""),
                    auth.get("_save_path"),
                )
                if refreshed and refreshed.get("access_token"):
                    auth["access_token"] = refreshed["access_token"]
                    auth["refresh_token"] = refreshed["refresh_token"]
                    continue
            result["error"] = f"ai_agent_http_{exc.code}"
            return result
        except Exception as exc:
            result["error"] = f"ai_agent_request_failed: {exc}"
            return result
        # 官方接口偶发只返回主窗口（5 小时窗口缺失），立即重试一次
        rate_limit = data.get("rate_limit") or {}
        if attempt == 1 and rate_limit.get("primary_window") and not rate_limit.get("secondary_window"):
            continue

    rate_limit = data.get("rate_limit") or {}
    result["email"] = data.get("email")
    result["plan_type"] = data.get("plan_type")
    result["allowed"] = rate_limit.get("allowed")
    result["limit_reached"] = rate_limit.get("limit_reached")
    result["primary"] = _ai_agent_norm_window(rate_limit.get("primary_window"))
    result["secondary"] = _ai_agent_norm_window(rate_limit.get("secondary_window"))
    for item in data.get("additional_rate_limits") or []:
        if isinstance(item, dict) and item.get("rate_limit"):
            sub = item["rate_limit"]
            result["extra_limits"].append({
                "name": item.get("limit_name") or "extra",
                "primary": _ai_agent_norm_window(sub.get("primary_window")),
                "secondary": _ai_agent_norm_window(sub.get("secondary_window")),
            })
    credits = data.get("credits") or {}
    if credits.get("has_credits"):
        result["credits_balance"] = credits.get("balance")

    _ai_agent_usage_cache[cache_key] = {"ts": time.time(), "account": result}
    return result


def _ai_agent_lock_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR)
    except Exception:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except Exception:
        return False
    finally:
        os.close(fd)


def _ai_agent_cli_alive() -> bool:
    try:
        out = subprocess.run(
            ["ps", "-axo", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return False
    for line in out.splitlines():
        cmd = line.strip()
        if not cmd or "codex" not in cmd.lower():
            continue
        if "/Applications/ChatGPT.app/" in cmd or "com.openai." in cmd:
            continue
        if os.path.basename(cmd.split()[0]) == "codex":
            return True
    return False


def _ai_agent_load_thread_names() -> dict:
    names: dict = {}
    try:
        with (_ai_agent_codex_home() / "session_index.jsonl").open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    names[rec.get("id")] = rec.get("thread_name") or ""
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    except Exception as exc:
        decky.logger.info(f"AI Agent session_index read failed: {exc}")
    return names


def _ai_agent_scan_sessions() -> dict:
    now = time.time()
    cutoff = now - AI_AGENT_SESSION_ACTIVE_MINUTES * 60
    names = _ai_agent_load_thread_names()
    cli = _ai_agent_cli_alive()
    items: dict = {}

    def ensure(tid: str) -> dict:
        it = items.get(tid)
        if it is None:
            it = items[tid] = {
                "thread_id": tid, "name": names.get(tid) or "",
                "last_active_ts": 0.0, "sources": [], "live": False,
            }
        return it

    locks_dir = _ai_agent_codex_home() / "thread-writer-locks"
    try:
        for path in locks_dir.iterdir():
            stem = path.name.removesuffix(".lock")
            if not path.name.endswith(".lock") or not AI_AGENT_UUID_RE.fullmatch(stem):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            it = ensure(stem)
            it["last_active_ts"] = max(it["last_active_ts"], mtime)
            if "线程锁" not in it["sources"]:
                it["sources"].append("线程锁")
            if _ai_agent_lock_held(path) or cli:
                it["live"] = True
    except FileNotFoundError:
        pass

    sessions_root = _ai_agent_codex_home() / "sessions"
    if sessions_root.exists():
        for path in sessions_root.rglob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            uuids = AI_AGENT_UUID_RE.findall(path.name)
            tid = uuids[0] if uuids else path.name
            it = ensure(tid)
            it["last_active_ts"] = max(it["last_active_ts"], mtime)
            if "会话文件" not in it["sources"]:
                it["sources"].append("会话文件")
            if cli and mtime >= now - 120:
                it["live"] = True

    running, recent = [], []
    for it in items.values():
        it["ago_seconds"] = int(now - it["last_active_ts"]) if it["last_active_ts"] else None
        it["name"] = it["name"] or "(未命名会话)"
        it["status"] = "running" if it["live"] else "recent"
        it["sources"] = sorted(it["sources"])
        (running if it["live"] else recent).append(it)
    running.sort(key=lambda x: -(x["last_active_ts"] or 0))
    recent.sort(key=lambda x: -(x["last_active_ts"] or 0))
    return {
        "running_count": len(running),
        "recent_count": len(recent),
        "items": running + recent,
    }


def _ai_agent_fetch_web_activity(auth: dict) -> list:
    now = time.time()
    if now - _ai_agent_web_cache["ts"] < AI_AGENT_WEB_TTL_SECONDS:
        return _ai_agent_web_cache["items"]
    if not auth.get("access_token"):
        return _ai_agent_web_cache["items"]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "oai-device-id": _ai_agent_device_id(),
        "Authorization": f"Bearer {auth['access_token']}",
        "chatgpt-account-id": auth.get("account_id") or "",
    }
    req = urllib.request.Request(AI_AGENT_CONVERSATIONS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ai_agent_https_context()) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:
        decky.logger.info(f"AI Agent web conversations fetch failed: {exc}")
        return _ai_agent_web_cache["items"]

    items = []
    for conversation in data.get("items") or []:
        raw_time = conversation.get("update_time") or ""
        try:
            ts = time.mktime(time.strptime(raw_time[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if ts < now - AI_AGENT_SESSION_ACTIVE_MINUTES * 60:
            continue
        items.append({
            "thread_id": conversation.get("id") or "",
            "name": conversation.get("title") or "(无标题网页对话)",
            "last_active_ts": ts,
            "ago_seconds": int(now - ts),
            "sources": ["网页对话"],
            "status": "running",
        })
    _ai_agent_web_cache["ts"] = time.time()
    _ai_agent_web_cache["items"] = items
    return items


def _ai_agent_format_token_count(total: int) -> str:
    if total < 1000:
        return str(total)
    if total < 1000**2:
        return f"{total // 1000}K"
    if total < 1000**3:
        return f"{total // 1000**2}M"
    return f"{total // 1000**3}B"


def _ai_agent_last_session_tokens(path: Path) -> int | None:
    """读取 rollout 文件尾部的最近一次 token 累计（不含缓存命中输入）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > 262144:
                handle.seek(-262144, os.SEEK_END)
            tail = handle.read().decode("utf-8", "ignore")
    except OSError:
        return None
    marker = '"total_token_usage":'
    index = tail.rfind(marker)
    if index < 0:
        return None
    fragment = tail[index + len(marker):]
    end = fragment.find("}")
    if end < 0:
        return None
    try:
        usage = json.loads(fragment[:end + 1])
        return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _ai_agent_token_totals(force: bool = False) -> dict:
    """按 5 小时 / 7 天滑动窗口聚合本机 codex 会话 Token 用量。"""
    now = time.time()
    cached = _ai_agent_tokens_cache
    if not force and cached["totals"] is not None and now - cached["ts"] < AI_AGENT_TOKENS_TTL_SECONDS:
        return cached["totals"]
    sessions_root = _ai_agent_codex_home() / "sessions"
    fallback = "0" if sessions_root.exists() else None
    totals = {"tokens_5h": fallback, "tokens_7d": fallback}
    sums = {"tokens_5h": 0, "tokens_7d": 0}
    seen = {"tokens_5h": False, "tokens_7d": False}
    try:
        candidates = list(sessions_root.rglob("*.jsonl")) if sessions_root.exists() else []
    except OSError:
        candidates = []
    for path in candidates:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > 7 * 86400:
            continue
        total = _ai_agent_last_session_tokens(path)
        if total is None:
            continue
        if age <= 5 * 3600:
            sums["tokens_5h"] += total
            seen["tokens_5h"] = True
        sums["tokens_7d"] += total
        seen["tokens_7d"] = True
    for key, value in sums.items():
        if seen[key]:
            totals[key] = _ai_agent_format_token_count(value)
    cached["ts"] = now
    cached["totals"] = totals
    return totals


def _ai_agent_screen_payload(force: bool = False) -> dict:
    """点阵屏 AI Agent 模块的数据载荷：7H/7D 剩余额度、对话数与 Token 总量。

    force=True 时绕过额度缓存实时拉取（切入模块那次），后台轮询走默认缓存。
    """
    status = _ai_agent_status(force=force)
    accounts = status.get("accounts") or []
    active = next((a for a in accounts if a.get("active") and not a.get("error")), None)
    if active is None:
        active = next((a for a in accounts if not a.get("error")), None)
    def _pick_window(prefer_main, extra_key, hours_only):
        """5小时/7天窗口：官方主结构缺失时（接口偶发返回 null）回落到附加限额。"""
        main_key = "secondary" if prefer_main else "primary"
        main_window = (active or {}).get(main_key) or {}
        if main_window.get("used_percent") is not None:
            return main_window
        for extra in (active or {}).get("extra_limits") or []:
            window = extra.get(extra_key) or {}
            if window.get("used_percent") is None:
                continue
            if hours_only:
                seconds = window.get("window_seconds") or 0
                if not seconds or seconds > 48 * 3600:
                    continue
            return window
        return None

    def _remaining(window):
        if window is None or window.get("used_percent") is None:
            return -1
        return max(0, min(100, 100 - int(window["used_percent"])))

    short_window = _pick_window(True, "primary", True)
    long_window = _pick_window(False, "secondary", False)
    tokens = status.get("tokens") or {}
    conversations = len((status.get("sessions") or {}).get("items") or [])
    return {
        "conversations": min(conversations, 999),
        "primaryPercent": _remaining(short_window),
        "secondaryPercent": _remaining(long_window),
        "tokensPrimary": tokens.get("tokens_5h") or "--",
        "tokensSecondary": tokens.get("tokens_7d") or "--",
    }


def _ai_agent_status(force: bool = False) -> dict:
    accounts = []
    active_auth = {"access_token": None, "refresh_token": None, "account_id": None}
    for spec in _ai_agent_collect_specs():
        if spec.get("error"):
            accounts.append({
                "name": spec["name"], "active": spec.get("active", False),
                "error": spec["error"],
            })
            continue
        auth_path = Path(spec["auth_file"])
        auth = _ai_agent_load_auth(auth_path)
        auth["_save_path"] = auth_path
        usage = _ai_agent_fetch_usage(auth, force, cache_key=f"path:{auth_path}")
        usage["name"] = spec["name"]
        usage["active"] = spec.get("active", False)
        usage["library"] = not spec.get("active", False)
        accounts.append(usage)
        if spec.get("active"):
            active_auth = auth

    local_auth_path = _ai_agent_local_auth_path()
    authenticated = bool(_ai_agent_load_auth(local_auth_path).get("access_token")) if local_auth_path.exists() else False
    local = _ai_agent_scan_sessions()
    merged = {it["thread_id"]: dict(it) for it in local["items"]}
    for it in _ai_agent_fetch_web_activity(active_auth):
        current = merged.get(it["thread_id"])
        if current is None:
            merged[it["thread_id"]] = it
        elif it["last_active_ts"] > current.get("last_active_ts") or 0:
            current["last_active_ts"] = it["last_active_ts"]
            current["ago_seconds"] = it["ago_seconds"]
            current["sources"] = sorted(set(current["sources"]) | set(it["sources"]))
    items = sorted(merged.values(), key=lambda x: -(x.get("last_active_ts") or 0))
    return {
        "accounts": accounts,
        "authenticated": authenticated,
        "sessions": {
            "running_count": sum(1 for it in items if it.get("status") == "running"),
            "recent_count": local["recent_count"],
            "web_count": sum(1 for it in items if "网页对话" in it["sources"]),
            "items": items,
        },
        "tokens": _ai_agent_token_totals(),
        "active_minutes": AI_AGENT_SESSION_ACTIVE_MINUTES,
        "generated_at": int(time.time()),
    }


# ---------------------------------------------------------------- 账号登录

def _ai_agent_codex_bin() -> str:
    return os.environ.get("JSAUX_AI_AGENT_CODEX_BIN", "codex")


def _ai_agent_login_start() -> dict:
    if _ai_agent_login["proc"] is not None and _ai_agent_login["proc"].poll() is None:
        return {"ok": True, "started": False, "running": True}
    codex_bin = _ai_agent_codex_bin()
    if shutil.which(codex_bin) is None:
        return {"ok": False, "error": "ai_agent_codex_cli_not_found"}
    local_path = _ai_agent_local_auth_path()
    try:
        auth_mtime = local_path.stat().st_mtime if local_path.exists() else 0.0
    except OSError:
        auth_mtime = 0.0
    _ai_agent_login["proc"] = subprocess.Popen(
        [codex_bin, "login"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(PLUGIN_ROOT),
    )
    _ai_agent_login["output"] = []
    _ai_agent_login["auth_mtime"] = auth_mtime

    def _reader(proc, sink):
        for line in proc.stdout:
            sink.append(line.rstrip())

    threading.Thread(
        target=_reader,
        args=(_ai_agent_login["proc"], _ai_agent_login["output"]),
        daemon=True,
    ).start()
    return {"ok": True, "started": True, "running": True}


def _ai_agent_login_status() -> dict:
    proc = _ai_agent_login["proc"]
    if proc is None:
        return {"ok": True, "running": False, "finished": False, "started": False,
                "url": None, "changed": False}
    output = "\n".join(_ai_agent_login["output"])
    url_match = AI_AGENT_LOGIN_URL_RE.search(output)
    local_path = _ai_agent_local_auth_path()
    try:
        auth_mtime = local_path.stat().st_mtime if local_path.exists() else 0.0
    except OSError:
        auth_mtime = 0.0
    running = proc.poll() is None
    return {
        "ok": True,
        "started": True,
        "running": running,
        "finished": not running,
        "url": url_match.group(0) if url_match else None,
        "changed": auth_mtime > 0 and auth_mtime != _ai_agent_login["auth_mtime"],
        "exit_code": proc.returncode if not running else None,
    }


def _ai_agent_logout() -> dict:
    """退出登录：备份并移除本机 auth.json，再尽力调用 codex logout 吊销凭证。"""
    local_path = _ai_agent_local_auth_path()
    removed = False
    if local_path.exists():
        try:
            shutil.copy2(local_path, local_path.with_name("auth.json.bak"))
        except OSError:
            pass
        try:
            local_path.unlink()
            removed = True
        except OSError as exc:
            return {"ok": False, "error": f"ai_agent_logout_failed: {exc}"}
    codex_bin = shutil.which(_ai_agent_codex_bin())
    if codex_bin:
        try:
            subprocess.run([codex_bin, "logout"], capture_output=True, timeout=20)
        except Exception:
            pass
    _ai_agent_usage_cache.clear()
    _ai_agent_web_cache.update({"ts": 0.0, "items": []})
    return {"ok": True, "removed": removed}


class Plugin:
    def __init__(self) -> None:
        daemon_url = os.environ.get("JSAUX_DAEMON_URL", "http://127.0.0.1:39052")
        self.client = DaemonHttpClient(daemon_url)
        self._serial_lock = threading.Lock()
        self._dashboard_generation = 0
        self._clock_generation = 0
        self._playtime_generation = 0
        self._playtime_current_app_id = ""
        self._playtime_session_started_at = time.time() - (3 * 3600 + 56 * 60)
        self._playtime_context_lock = threading.Lock()
        self._playtime_session_key: tuple[str, int] | None = None
        self._playtime_baseline_minutes = 0
        self._countdown_generation = 0
        self._countdown_total_seconds = 30 * 60
        self._countdown_started_at = time.time() - (10 * 60 + 1)
        self._chronograph_generation = 0
        self._chronograph_total_seconds = CHRONOGRAPH_MAX_SECONDS
        self._chronograph_started_at = time.time()
        self._chronograph_loop_active = False
        self._chronograph_watchdog_generation = 0
        self._ai_agent_generation = 0
        self._display_locked_until = 0
        self._current_artwork_task = None

    async def get_status(self):
        return await self.client.aget("/v1/device")

    async def get_profiles(self):
        return await self.client.aget("/v1/profiles")

    async def get_modules(self):
        return await self.client.aget("/v1/modules")

    async def get_display_settings(self):
        return await self.client.aget("/v1/settings/display")

    async def get_custom_assets(self):
        return await self.client.aget("/v1/custom/assets")

    async def get_countdown(self):
        return await self.client.aget("/v1/timer/countdown")

    async def get_chronograph(self):
        return await self.client.aget("/v1/timer/chronograph")

    async def get_ai_agent_status(self, force: bool = False):
        return await asyncio.to_thread(_ai_agent_status, bool(force))

    async def ai_agent_import_account(self, name: str = ""):
        return await asyncio.to_thread(_ai_agent_import_account, str(name or ""))

    async def ai_agent_enable_account(self, name: str = ""):
        return await asyncio.to_thread(_ai_agent_enable_account, str(name or ""))

    async def ai_agent_login_start(self):
        return await asyncio.to_thread(_ai_agent_login_start)

    async def ai_agent_login_status(self):
        return await asyncio.to_thread(_ai_agent_login_status)

    async def ai_agent_logout(self):
        return await asyncio.to_thread(_ai_agent_logout)

    async def set_brightness(self, brightness: int):
        return await self.client.apost("/v1/device/brightness", {"brightness": brightness})

    async def set_power(self, enabled: bool):
        return await self.client.apost("/v1/device/power", {"enabled": enabled})

    async def send_test_pattern(self, pattern: str):
        return await self.client.apost("/v1/device/test-pattern", {"pattern": pattern})

    async def apply_profile(self, app_id: str):
        return await self.client.apost("/v1/display/profile/apply", {"appId": app_id})

    async def update_display_settings(self, payload: dict):
        result = await self.client.apost("/v1/settings/display", payload)
        if self._display_update_requests_custom(payload):
            settings = result.get("displaySettings") if isinstance(result, dict) else None
            if not isinstance(settings, dict):
                try:
                    settings = await self.get_display_settings()
                    if isinstance(settings.get("displaySettings"), dict):
                        settings = settings["displaySettings"]
                except Exception as exc:
                    decky.logger.info(f"Chronograph custom activation check skipped: {exc}")
                    settings = None
            if isinstance(settings, dict) and self._settings_have_active_chronograph(settings):
                decky.logger.info("Chronograph monitor restarted after custom display activation")
                return await self.send_chronograph_monitor(CHRONOGRAPH_MAX_SECONDS)
        return result

    @staticmethod
    def _normalize_display_refresh_rate(refresh_rate: float) -> int:
        try:
            normalized = int(float(refresh_rate) + 0.5)
        except (TypeError, ValueError, OverflowError):
            return 0
        return normalized if 1 <= normalized <= 999 else 0

    async def update_display_refresh_rate(self, refresh_rate: float = 0):
        return await self.client.apost(
            "/v1/runtime/display-refresh-rate",
            {"refreshRate": self._normalize_display_refresh_rate(refresh_rate)},
        )

    async def select_custom_asset(self, asset: str):
        if self._is_chronograph_asset(asset):
            return await self.send_chronograph_monitor(CHRONOGRAPH_MAX_SECONDS)
        return await self.client.apost("/v1/custom/assets/select", {"asset": asset})

    async def upload_custom_asset(self, filename: str, content_base64: str):
        return await self.client.apost(
            "/v1/custom/assets/upload",
            {"filename": filename, "content_base64": content_base64},
        )

    async def upload_custom_asset_from_path(self, source_path: str):
        source = Path(source_path).expanduser()
        try:
            source = source.resolve(strict=True)
        except (OSError, RuntimeError):
            return {"ok": False, "error": "custom_asset_file_not_found"}
        if not source.is_file() or source.suffix.lower() not in CUSTOM_ASSET_EXTENSIONS:
            return {"ok": False, "error": "custom_asset_file_not_found"}
        try:
            content_base64 = base64.b64encode(source.read_bytes()).decode("ascii")
        except OSError:
            return {"ok": False, "error": "custom_asset_file_read_failed"}
        return await self.upload_custom_asset(source.name, content_base64)

    async def list_custom_asset_files(self, folder_path: str):
        """List supported images in a folder selected by Decky's native picker."""
        folder = Path(folder_path).expanduser()
        try:
            folder = folder.resolve(strict=True)
        except (OSError, RuntimeError):
            return {"ok": False, "error": "custom_asset_folder_not_found"}
        if not folder.is_dir():
            return {"ok": False, "error": "custom_asset_folder_not_found"}

        try:
            candidates = sorted(
                (
                    item
                    for item in folder.iterdir()
                    if item.is_file() and item.suffix.lower() in CUSTOM_ASSET_EXTENSIONS
                ),
                key=lambda item: item.name.casefold(),
            )
        except OSError:
            return {"ok": False, "error": "custom_asset_folder_read_failed"}

        mime_types = {
            ".png": "image/png",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".bmp": "image/bmp",
        }
        files = []
        for item in candidates[:CUSTOM_ASSET_DIRECTORY_LIMIT]:
            preview = ""
            try:
                data = item.read_bytes()
                if 0 < len(data) <= CUSTOM_ASSET_PREVIEW_LIMIT_BYTES:
                    preview = (
                        f"data:{mime_types[item.suffix.lower()]};base64,"
                        f"{base64.b64encode(data).decode('ascii')}"
                    )
            except OSError:
                # Keep the filename visible. Upload reports a precise read error
                # if the file disappears before the user confirms it.
                pass
            files.append({"name": item.name, "path": str(item), "preview": preview})

        return {
            "ok": True,
            "folder": str(folder),
            "files": files,
            "truncated": len(candidates) > CUSTOM_ASSET_DIRECTORY_LIMIT,
        }

    async def delete_custom_asset(self, asset: str):
        return await self.client.apost("/v1/custom/assets/delete", {"asset": asset})

    async def sync_display(self, module_id: str = "", force: bool = False):
        if not force and time.time() < getattr(self, '_display_locked_until', 0):
            return {"ok": True, "skipped": True}
        normalized_module_id = module_id.lower()
        if normalized_module_id in {"chronograph", "countup"}:
            return await self.send_chronograph_monitor(CHRONOGRAPH_MAX_SECONDS)
        if normalized_module_id == "custom":
            settings = await self.get_display_settings()
            if isinstance(settings.get("displaySettings"), dict):
                settings = settings["displaySettings"]
            custom = settings.get("custom", {})
            if isinstance(custom, dict):
                current_asset = custom.get("current_asset") or custom.get("currentAsset")
                if self._is_chronograph_asset(str(current_asset)):
                    return await self.send_chronograph_monitor(CHRONOGRAPH_MAX_SECONDS)
        if normalized_module_id:
            self._stop_legacy_monitor_loops()
        payload = {"moduleId": module_id} if module_id else {}
        return await self.client.apost("/v1/display/sync", payload)

    async def sync_context_now(self, sync_display: bool = True):
        if time.time() < getattr(self, '_display_locked_until', 0):
            return {"ok": True, "skipped": True}
        context = self._daemon_steam_context()
        payload = dict(context)
        if not sync_display:
            payload["syncDisplay"] = False
        await self.client.apost("/v1/runtime/steam-context", payload)
        return context

    def _daemon_steam_context(self):
        context = dict(self._scan_context())
        context["source"] = "decky"
        app_id = str(context.get("appId") or "")
        try:
            session_started_at = max(0, int(round(float(context.get("sessionStartedAt") or 0))))
        except (TypeError, ValueError):
            session_started_at = 0
        context["sessionStartedAt"] = session_started_at

        if not app_id or not bool(context.get("running")):
            return context

        try:
            reported_total = max(0, int(context.get("totalPlaytimeMinutes") or 0))
        except (TypeError, ValueError):
            reported_total = 0
        session_key = (app_id, session_started_at)
        with self._playtime_context_lock:
            same_session = (
                self._playtime_session_key is not None
                and self._playtime_session_key[0] == app_id
                and abs(self._playtime_session_key[1] - session_started_at) <= 2
            )
            if not same_session:
                self._playtime_session_key = session_key
                self._playtime_baseline_minutes = reported_total
            else:
                context["sessionStartedAt"] = self._playtime_session_key[1]
            context["totalPlaytimeMinutes"] = self._playtime_baseline_minutes
        return context

    def _sync_playtime_context_now(self):
        if time.time() < getattr(self, '_display_locked_until', 0):
            return {"ok": True, "skipped": True}
        context = self._daemon_steam_context()
        payload = dict(context)
        payload["syncDisplay"] = False
        return self.client.post("/v1/runtime/steam-context", payload)

    async def refresh_and_sync(self):
        context = await self.sync_context_now()
        await self.sync_display()
        return {"ok": True, "context": context}

    async def health(self):
        return {"ok": True}

    async def write_serial_hex(self, hex_string: str, port: str = "", baudrate: int = 1000000):
        normalized = "".join(hex_string.strip().split()).upper()
        frame = self._parse_matrix_frame(normalized)
        if frame and frame["data_type"] == 0x01:
            command = frame["command"]
            data = frame["data"]
            if command == 0x01 and len(data) >= 1:
                result = await self.set_power(data[0] != 0)
                return self._daemon_serial_result(result, "power", normalized)
            if command == 0x02 and len(data) >= 1:
                brightness = max(0, min(100, int(data[0])))
                result = await self.set_brightness(brightness)
                return self._daemon_serial_result(result, "brightness", normalized)
            if command == 0x21 and len(data) >= 1:
                return await self._activate_mode_hex(data[0])
        if normalized == "445901040008000004A46CEAA0642CF6":
            return await self._activate_builtin_custom_asset("rainbow_scroll")
        if os.environ.get("JSAUX_ALLOW_RAW_SERIAL", "").lower() in ("1", "true", "yes", "on"):
            return self._write_serial_hex(hex_string, port, baudrate)
        return {
            "ok": True,
            "mode": "daemon-ignored",
            "port": "daemon",
            "written": 0,
            "txHex": self._format_hex(normalized),
            "rxHex": "",
            "message": "Raw serial command ignored because the daemon owns the USB serial port.",
        }

    async def send_dashboard_monitor(self, refresh_rate: float = 0, port: str = "", baudrate: int = 1000000):
        del refresh_rate
        return await self._activate_daemon_module(
            "dashboard",
            display_refresh_rate=0,
        )

    async def send_hardware_monitor(self, refresh_rate: float = 0, port: str = "", baudrate: int = 1000000):
        del refresh_rate
        return await self._activate_daemon_module(
            "system_performance",
            display_refresh_rate=0,
        )

    async def send_clock_monitor(self, port: str = "", baudrate: int = 1000000):
        return await self._activate_daemon_module("productivity")

    async def send_weather_forecast(self, port: str = "", baudrate: int = 1000000):
        return await self._activate_daemon_module("weather")

    async def send_playtime_monitor(self, port: str = "", baudrate: int = 1000000):
        result = await self._activate_daemon_module("playtime", sync_context=True)
        if result.get("ok", True):
            self._start_playtime_monitor_loop(port, baudrate)
        return result

    async def send_countdown_monitor(self, minutes: int = 30, seconds: int = 0, port: str = "", baudrate: int = 1000000):
        self._stop_legacy_monitor_loops()
        total_seconds = int(seconds) if int(seconds or 0) > 0 else int(minutes) * 60
        self._countdown_total_seconds = max(1, min((99 * 3600) + (59 * 60) + 59, total_seconds))
        self._countdown_started_at = time.time()
        result = await self.client.apost(
            "/v1/timer/countdown",
            {"totalSeconds": self._countdown_total_seconds},
        )
        return {"mode": "daemon", "moduleId": "countdown", **result}

    async def send_chronograph_monitor(self, seconds: int = CHRONOGRAPH_MAX_SECONDS, port: str = "", baudrate: int = 1000000):
        self._stop_legacy_monitor_loops()
        self._chronograph_total_seconds = max(1, min(CHRONOGRAPH_MAX_SECONDS, int(seconds)))
        self._chronograph_started_at = time.time()
        decky.logger.info("Chronograph monitor start requested")
        result = await self.client.apost(
            "/v1/timer/chronograph",
            {"totalSeconds": self._chronograph_total_seconds},
        )
        return {"mode": "daemon", "moduleId": "chronograph", **result}

    async def send_ai_agent_monitor(self, port: str = "", baudrate: int = 1000000):
        del port, baudrate
        # Activate first so the screen switches instantly; usage data lands
        # 1-2s later via the push below.
        activation = await self._activate_daemon_module("ai_agent")
        if not activation.get("ok", True):
            return activation
        try:
            # 切入模块时强制实时取一次额度；后续轮询循环才走缓存。
            payload = await asyncio.to_thread(_ai_agent_screen_payload, True)
        except Exception as exc:
            decky.logger.info(f"AI Agent payload build failed: {exc}")
            payload = {
                "conversations": 0,
                "primaryPercent": -1,
                "secondaryPercent": -1,
                "tokensPrimary": "--",
                "tokensSecondary": "--",
            }
        pushed = await self.client.apost("/v1/aiagent", payload)
        self._start_ai_agent_monitor_loop()
        return {"ok": True, "mode": "daemon", "moduleId": "ai_agent", "screen": payload, **pushed}

    def _stop_legacy_monitor_loops(self):
        self._dashboard_generation += 1
        self._clock_generation += 1
        self._playtime_generation += 1
        self._countdown_generation += 1
        self._chronograph_generation += 1
        self._chronograph_loop_active = False
        self._ai_agent_generation += 1

    async def _activate_daemon_module(
        self,
        module_id: str,
        sync_context: bool = False,
        display_refresh_rate: int | None = None,
    ):
        self._stop_legacy_monitor_loops()
        if module_id == "dashboard" or sync_context:
            await self.sync_context_now(sync_display=False)
        payload = {
            "activeModuleId": module_id,
            "autoSyncEnabled": True,
            "timeline": {"enabled": False},
        }
        if display_refresh_rate is not None:
            payload["displayRefreshRate"] = display_refresh_rate
        settings = await self.client.apost(
            "/v1/settings/display",
            payload,
        )
        return {"ok": settings.get("ok", True), "mode": "daemon", "moduleId": module_id, "settings": settings}

    async def _activate_mode_hex(self, mode_hex: int):
        mode_map = {
            0x01: "dashboard",
            0x02: "system_performance",
            0x03: "weather",
            0x04: "productivity",
        }
        if mode_hex in mode_map:
            return await self._activate_daemon_module(mode_map[mode_hex])
        if mode_hex == 0x05:
            return await self.send_playtime_monitor()
        if mode_hex == 0x06:
            return await self.send_countdown_monitor()
        if mode_hex == 0x07:
            return await self.send_chronograph_monitor(CHRONOGRAPH_MAX_SECONDS)
        if mode_hex == 0x08:
            return await self._activate_builtin_custom_asset("rainbow_scroll")
        if mode_hex == 0x09:
            return await self.send_ai_agent_monitor()
        return {
            "ok": True,
            "mode": "daemon-ignored",
            "port": "daemon",
            "written": 0,
            "message": f"Unsupported display mode 0x{mode_hex:02X}; raw serial is disabled while daemon owns USB.",
        }

    def _parse_matrix_frame(self, normalized: str):
        if len(normalized) % 2 != 0:
            return None
        try:
            payload = bytes.fromhex(normalized)
        except ValueError:
            return None
        if len(payload) < 8 or payload[0] != 0x44 or payload[1] != 0x59:
            return None
        data_len = (payload[4] << 8) | payload[5]
        data_end = 6 + data_len
        if len(payload) < data_end:
            return None
        return {
            "data_type": payload[2],
            "command": payload[3],
            "data": payload[6:data_end],
            "crc": payload[data_end:],
        }

    def _format_hex(self, normalized: str):
        return " ".join(normalized[index:index + 2] for index in range(0, len(normalized), 2))

    def _daemon_serial_result(self, result: dict, action: str, normalized: str):
        return {
            "ok": result.get("ok", True),
            "mode": "daemon",
            "action": action,
            "port": "daemon",
            "written": 0,
            "txHex": self._format_hex(normalized),
            "rxHex": "",
            "daemon": result,
        }

    async def _activate_custom_gif(self, asset_name: str, gif_data: bytes):
        encoded = base64.b64encode(gif_data).decode("ascii")
        upload = await self.client.apost(
            "/v1/custom/assets/upload",
            {"filename": asset_name, "content_base64": encoded},
        )
        settings = await self.client.apost(
            "/v1/settings/display",
            {
                "activeModuleId": "custom",
                "autoSyncEnabled": True,
                "timeline": {"enabled": False},
                "custom": {"active": True, "currentAsset": asset_name},
            },
        )
        select = await self.client.apost("/v1/custom/assets/select", {"asset": asset_name})
        sync = await self.client.apost("/v1/display/sync", {"moduleId": "custom"})
        return {
            "ok": sync.get("ok", True),
            "mode": "daemon-custom",
            "moduleId": "custom",
            "asset": asset_name,
            "upload": upload,
            "settings": settings,
            "select": select,
            "sync": sync,
        }

    async def _activate_builtin_custom_asset(self, asset_name: str):
        self._stop_legacy_monitor_loops()
        select = await self.client.apost("/v1/custom/assets/select", {"asset": asset_name})
        settings = await self.client.apost(
            "/v1/settings/display",
            {
                "activeModuleId": "custom",
                "autoSyncEnabled": True,
                "timeline": {"enabled": False},
                "custom": {"active": True, "currentAsset": asset_name},
            },
        )
        sync = await self.client.apost("/v1/display/sync", {"moduleId": "custom"})
        return {
            "ok": sync.get("ok", True),
            "mode": "daemon-custom",
            "moduleId": "custom",
            "asset": asset_name,
            "select": select,
            "settings": settings,
            "sync": sync,
        }

    def _activate_custom_gif_sync(self, asset_name: str, gif_data: bytes):
        encoded = base64.b64encode(gif_data).decode("ascii")
        upload = self.client.post(
            "/v1/custom/assets/upload",
            {"filename": asset_name, "content_base64": encoded},
        )
        settings = self.client.post(
            "/v1/settings/display",
            {
                "activeModuleId": "custom",
                "autoSyncEnabled": True,
                "timeline": {"enabled": False},
                "custom": {"active": True, "currentAsset": asset_name},
            },
        )
        select = self.client.post("/v1/custom/assets/select", {"asset": asset_name})
        sync = self.client.post("/v1/display/sync", {"moduleId": "custom"})
        return {
            "ok": sync.get("ok", True),
            "mode": "daemon-custom",
            "moduleId": "custom",
            "asset": asset_name,
            "upload": upload,
            "settings": settings,
            "select": select,
            "sync": sync,
        }

    def _write_serial_hex(self, hex_string: str, port: str = "", baudrate: int = 1000000):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is not available on this Deck") from exc

        normalized = "".join(hex_string.strip().split())
        if len(normalized) % 2 != 0:
            raise RuntimeError("HEX command has an odd number of characters")

        try:
            payload = bytes.fromhex(normalized)
        except ValueError as exc:
            raise RuntimeError(f"Invalid HEX command: {hex_string}") from exc

        target_port = port or os.environ.get("JSAUX_MATRIX_SERIAL_PORT") or self._detect_serial_port()
        if not target_port:
            raise RuntimeError("No serial device detected")

        try:
            with serial.Serial(
                port=target_port,
                baudrate=int(baudrate),
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.25,
                write_timeout=1,
            ) as serial_port:
                serial_port.reset_input_buffer()
                serial_port.reset_output_buffer()
                written = serial_port.write(payload)
                serial_port.flush()
                reply = serial_port.read(64)
        except Exception as exc:
            raise RuntimeError(f"Serial write failed on {target_port}: {exc}") from exc

        return {
            "ok": written == len(payload),
            "port": target_port,
            "baudrate": int(baudrate),
            "written": written,
            "txHex": payload.hex(" ").upper(),
            "rxHex": reply.hex(" ").upper(),
        }

    def _detect_serial_port(self):
        for candidate in sorted(glob("/dev/ttyUSB*")):
            return candidate
        for candidate in sorted(glob("/dev/ttyACM*")):
            return candidate
        return ""

    def _upload_gif(self, gif_data: bytes, port: str = "", baudrate: int = 1000000):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is not available on this Deck") from exc

        target_port = port or os.environ.get("JSAUX_MATRIX_SERIAL_PORT") or self._detect_serial_port()
        if not target_port:
            raise RuntimeError("No serial device detected")

        frames = self._encode_gif_upload(gif_data)
        try:
            with self._serial_lock:
                with serial.Serial(
                    port=target_port,
                    baudrate=int(baudrate),
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.25,
                    write_timeout=1,
                ) as serial_port:
                    serial_port.reset_input_buffer()
                    serial_port.reset_output_buffer()
                    written = 0
                    for frame in frames:
                        written += serial_port.write(frame)
                        serial_port.flush()
                        time.sleep(0.01)
                    try:
                        reply = serial_port.read(64)
                    except Exception as exc:
                        decky.logger.info(f"Dashboard upload ACK read skipped: {exc}")
                        reply = b""
        except Exception as exc:
            raise RuntimeError(f"Dashboard upload failed on {target_port}: {exc}") from exc

        return {
            "ok": True,
            "port": target_port,
            "baudrate": int(baudrate),
            "written": written,
            "txHex": f"{len(frames)} frames / {len(gif_data)} GIF bytes",
            "rxHex": reply.hex(" ").upper(),
        }

    def _start_dashboard_monitor_loop(self, port: str = "", baudrate: int = 1000000):
        self._dashboard_generation += 1
        generation = self._dashboard_generation

        def run():
            for _ in range(180):
                time.sleep(1.0)
                if generation != self._dashboard_generation:
                    return
                try:
                    self._upload_gif(self._build_dashboard_gif(), port, baudrate)
                except Exception as exc:
                    decky.logger.info(f"Dashboard monitor refresh stopped: {exc}")
                    return

        thread = threading.Thread(target=run, name="jsaux-dashboard-monitor", daemon=True)
        thread.start()

    def _start_clock_monitor_loop(self, port: str = "", baudrate: int = 1000000):
        self._clock_generation += 1
        generation = self._clock_generation

        def run():
            for _ in range(600):
                time.sleep(max(0.2, 1.02 - (time.time() % 1.0)))
                if generation != self._clock_generation:
                    return
                try:
                    self._upload_gif(self._build_clock_monitor_gif(), port, baudrate)
                except Exception as exc:
                    decky.logger.info(f"Clock monitor refresh stopped: {exc}")
                    return

        thread = threading.Thread(target=run, name="jsaux-clock-monitor", daemon=True)
        thread.start()

    def _start_playtime_monitor_loop(self, port: str = "", baudrate: int = 1000000):
        del port, baudrate
        self._playtime_generation += 1
        generation = self._playtime_generation

        def run():
            while generation == self._playtime_generation:
                time.sleep(max(1.0, PLAYTIME_REFRESH_SECONDS - (time.time() % PLAYTIME_REFRESH_SECONDS)))
                if generation != self._playtime_generation:
                    return
                try:
                    self._sync_playtime_context_now()
                except Exception as exc:
                    decky.logger.info(f"Playtime context refresh skipped: {exc}")

        thread = threading.Thread(target=run, name="jsaux-playtime-monitor", daemon=True)
        thread.start()

    def _start_ai_agent_monitor_loop(self):
        self._ai_agent_generation += 1
        generation = self._ai_agent_generation

        def run():
            while generation == self._ai_agent_generation:
                time.sleep(random.uniform(AI_AGENT_SCREEN_REFRESH_MIN_SECONDS, AI_AGENT_SCREEN_REFRESH_MAX_SECONDS))
                if generation != self._ai_agent_generation:
                    return
                try:
                    payload = _ai_agent_screen_payload()
                    self.client.post("/v1/aiagent", payload)
                except Exception as exc:
                    decky.logger.info(f"AI Agent screen refresh skipped: {exc}")

        thread = threading.Thread(target=run, name="jsaux-ai-agent-monitor", daemon=True)
        thread.start()

    def _start_countdown_monitor_loop(self, port: str = "", baudrate: int = 1000000):
        self._countdown_generation += 1
        generation = self._countdown_generation

        def run():
            for _ in range(self._countdown_total_seconds + 2):
                time.sleep(max(0.2, 1.02 - (time.time() % 1.0)))
                if generation != self._countdown_generation:
                    return
                if not self._current_custom_asset_is("plugin-countdown.gif"):
                    decky.logger.info("Countdown monitor loop stopped by selected UI change")
                    return
                try:
                    self._activate_custom_gif_sync("plugin-countdown.gif", self._build_countdown_monitor_gif())
                except Exception as exc:
                    decky.logger.info(f"Countdown monitor refresh stopped: {exc}")
                    return

        thread = threading.Thread(target=run, name="jsaux-countdown-monitor", daemon=True)
        thread.start()

    def _start_chronograph_monitor_loop(self, port: str = "", baudrate: int = 1000000):
        self._chronograph_generation += 1
        generation = self._chronograph_generation
        self._chronograph_loop_active = True

        def run():
            try:
                next_refresh = time.time()
                for _ in range(self._chronograph_total_seconds + 2):
                    if generation != self._chronograph_generation:
                        decky.logger.info("Chronograph monitor loop stopped by mode change")
                        return
                    if not self._current_custom_asset_is_chronograph():
                        decky.logger.info("Chronograph monitor loop stopped by selected UI change")
                        return
                    try:
                        elapsed = self._chronograph_elapsed_seconds()
                        asset_name = self._chronograph_asset_name(elapsed)
                        self._activate_custom_gif_sync(asset_name, self._build_chronograph_monitor_asset())
                        if elapsed % 60 in {29, 30, 31, 32, 33}:
                            decky.logger.info(f"Chronograph monitor refreshed elapsed={elapsed} asset={asset_name}")
                    except Exception as exc:
                        decky.logger.info(f"Chronograph monitor refresh skipped: {exc}")
                    next_refresh += 1.0
                    time.sleep(max(0.2, next_refresh - time.time()))
            finally:
                if generation == self._chronograph_generation:
                    self._chronograph_loop_active = False

        thread = threading.Thread(target=run, name="jsaux-chronograph-monitor", daemon=True)
        thread.start()

    def _ensure_chronograph_monitor_running_for_active_display(self, port: str = "", baudrate: int = 1000000) -> bool:
        if self._chronograph_loop_active:
            return False

        settings = self.client.get("/v1/settings/display")
        if isinstance(settings.get("displaySettings"), dict):
            settings = settings["displaySettings"]
        if not self._settings_have_active_chronograph(settings):
            return False

        self._chronograph_total_seconds = CHRONOGRAPH_MAX_SECONDS
        self._chronograph_started_at = time.time()
        asset_name = self._chronograph_asset_name(0)
        result = self._activate_custom_gif_sync(asset_name, self._build_chronograph_monitor_asset())
        if not result.get("ok", True):
            decky.logger.info(f"Chronograph watchdog restart failed: {result}")
            return False

        self._start_chronograph_monitor_loop(port, baudrate)
        decky.logger.info("Chronograph watchdog restarted active custom display")
        return True

    def _start_chronograph_activation_watchdog(self):
        self._chronograph_watchdog_generation += 1
        generation = self._chronograph_watchdog_generation

        def run():
            while generation == self._chronograph_watchdog_generation:
                time.sleep(1.0)
                if generation != self._chronograph_watchdog_generation:
                    return
                try:
                    self._ensure_chronograph_monitor_running_for_active_display()
                except Exception as exc:
                    decky.logger.info(f"Chronograph watchdog skipped: {exc}")

        thread = threading.Thread(target=run, name="jsaux-chronograph-watchdog", daemon=True)
        thread.start()

    def _chronograph_elapsed_seconds(self) -> int:
        total = max(1, int(self._chronograph_total_seconds))
        return max(0, min(total, int(time.time() - self._chronograph_started_at)))

    def _chronograph_asset_name(self, elapsed_seconds: int | None = None) -> str:
        elapsed = self._chronograph_elapsed_seconds() if elapsed_seconds is None else int(elapsed_seconds)
        return f"plugin-chronograph-{elapsed % 60:02d}.bmp"

    def _is_chronograph_asset(self, asset: str) -> bool:
        return asset == "plugin-chronograph.gif" or bool(re.fullmatch(r"plugin-chronograph-\d{2}\.(gif|bmp)", asset))

    def _current_custom_asset(self) -> str:
        try:
            settings = self.client.get("/v1/settings/display")
            if isinstance(settings.get("displaySettings"), dict):
                settings = settings["displaySettings"]
            active_module = str(settings.get("activeModuleId") or settings.get("active_module_id") or "")
            if active_module.lower() not in {"custom", "custom_upload"}:
                return ""
            custom = settings.get("custom", {})
            if isinstance(custom, dict):
                return str(custom.get("current_asset") or custom.get("currentAsset") or "")
        except Exception as exc:
            decky.logger.info(f"Current custom asset check failed: {exc}")
        return ""

    def _current_custom_asset_is(self, asset: str) -> bool:
        return self._current_custom_asset() == asset

    def _current_custom_asset_is_chronograph(self) -> bool:
        return self._is_chronograph_asset(self._current_custom_asset())

    def _display_update_requests_custom(self, payload: dict) -> bool:
        active_module = str(payload.get("activeModuleId") or payload.get("active_module_id") or "")
        if active_module.lower() == "custom":
            return True
        custom = payload.get("custom", {})
        if isinstance(custom, dict):
            current_asset = str(custom.get("current_asset") or custom.get("currentAsset") or "")
            return self._is_chronograph_asset(current_asset)
        return False

    def _settings_have_active_chronograph(self, settings: dict) -> bool:
        active_module = str(settings.get("activeModuleId") or settings.get("active_module_id") or "")
        custom = settings.get("custom", {})
        current_asset = ""
        if isinstance(custom, dict):
            current_asset = str(custom.get("current_asset") or custom.get("currentAsset") or "")
        return active_module.lower() == "custom" and self._is_chronograph_asset(current_asset)

    def _crc16_kermit(self, data: bytes) -> int:
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc >> 1) ^ 0x8408) if (crc & 1) else (crc >> 1)
        return crc & 0xFFFF

    def _standard_frame(self, transport_type: int, command: int, payload: bytes) -> bytes:
        frame = bytes([0x44, 0x59, transport_type & 0xFF, command & 0xFF]) + len(payload).to_bytes(2, "big") + payload
        return frame + self._crc16_kermit(frame).to_bytes(2, "big")

    def _encode_gif_upload(self, gif_data: bytes, chunk_size: int = 498) -> list[bytes]:
        import zlib

        frames = [self._standard_frame(0x01, 0x04, len(gif_data).to_bytes(4, "big") + zlib.crc32(gif_data).to_bytes(4, "big"))]
        sequence = 0
        for offset in range(0, len(gif_data), chunk_size):
            chunk = gif_data[offset : offset + chunk_size]
            payload = sequence.to_bytes(2, "big") + chunk
            frames.append(self._standard_frame(0x02, 0x02, payload))
            sequence = (sequence + 1) & 0xFFFF
        frames.append(self._standard_frame(0x01, 0x0B, b"\x01"))
        return frames

    def _build_dashboard_gif(self) -> bytes:
        width, height = 64, 54
        palette = [
            (0, 0, 0),
            (255, 255, 255),
            (65, 65, 65),
            (33, 32, 33),
            (255, 85, 0),
            (255, 0, 0),
            (255, 130, 0),
            (0, 0, 0),
        ]
        pixels = self._render_dashboard_pixels(time.localtime(), self._sample_dashboard_metrics())
        return self._gif_from_indexed(width, height, palette, pixels)

    def _build_hardware_monitor_gif(self) -> bytes:
        width, height = 64, 54
        palette = [
            (0, 0, 0),
            (255, 255, 255),
            (65, 65, 65),
            (255, 128, 0),
            (255, 0, 0),
            (0, 0, 0),
            (0, 0, 0),
            (0, 0, 0),
        ]
        bg, white, gray, orange, red = range(5)
        pixels = [bg] * (width * height)

        def dot(x: int, y: int, color: int):
            if 0 <= x < width and 0 <= y < height:
                pixels[y * width + x] = color

        def rect(x: int, y: int, w: int, h: int, color: int):
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    dot(xx, yy, color)

        glyphs = {
            "0": [31, 17, 17, 17, 17, 17, 31],
            "1": [4, 12, 4, 4, 4, 4, 31],
            "2": [31, 1, 1, 31, 16, 16, 31],
            "3": [31, 1, 1, 15, 1, 1, 31],
            "4": [17, 17, 17, 31, 1, 1, 1],
            "5": [31, 16, 16, 31, 1, 1, 31],
            "6": [31, 16, 16, 31, 17, 17, 31],
            "7": [31, 1, 2, 4, 4, 4, 4],
            "8": [31, 17, 17, 31, 17, 17, 31],
            "9": [31, 17, 17, 31, 1, 1, 31],
            "A": [14, 17, 17, 31, 17, 17, 17],
            "C": [31, 16, 16, 16, 16, 16, 31],
            "F": [31, 16, 16, 30, 16, 16, 16],
            "G": [31, 16, 16, 23, 17, 17, 31],
            "M": [17, 27, 21, 21, 17, 17, 17],
            "P": [30, 17, 17, 30, 16, 16, 16],
            "R": [30, 17, 17, 30, 20, 18, 17],
            "S": [31, 16, 16, 31, 1, 1, 31],
            "U": [17, 17, 17, 17, 17, 17, 31],
            "%": [17, 1, 2, 4, 8, 16, 17],
        }

        def text_width(text: str) -> int:
            return 0 if not text else len(text) * 6 - 1

        def text5(text: str, x: int, y: int, color: int = white):
            cx = x
            for ch in text.upper():
                if ch == " ":
                    cx += 6
                    continue
                rows = glyphs.get(ch)
                if rows:
                    for yy, bits in enumerate(rows):
                        for xx in range(5):
                            if bits & (1 << (4 - xx)):
                                dot(cx + xx, y + yy, color)
                cx += 6

        def right_text(text: str, right_x: int, y: int):
            text5(text, right_x - text_width(text) + 1, y)

        def bar(x: int, y: int, w: int, percent: int):
            for tick_x in range(x, x + w, 5):
                rect(tick_x, y - 1, 1, 3, gray)
            if percent < 0:
                return
            percent = max(0, min(100, percent))
            filled = max(0, min(w, percent * w // 100))
            if filled:
                rect(x, y, filled, 2, orange)
            rect(max(x, min(x + filled, x + w - 1)), y, 1, 2, red)

        gpu, cpu, ram, fps = self._sample_dashboard_metrics()
        right_text(f"{fps if fps >= 0 else '--'} FPS", 62, 2)

        bar(2, 12, 39, gpu)
        text5("GPU", 2, 17)
        right_text(f"{gpu if gpu >= 0 else '--'}%", 62, 17)

        bar(2, 26, 39, cpu)
        text5("CPU", 2, 31)
        right_text(f"{cpu if cpu >= 0 else '--'}%", 62, 31)

        bar(2, 40, 39, ram)
        text5("RAM", 2, 45)
        right_text(f"{ram if ram >= 0 else '--'}%", 62, 45)

        return self._gif_from_indexed(width, height, palette, pixels)

    def _build_clock_monitor_gif(self) -> bytes:
        width, height = 64, 54
        palette = [
            (0, 0, 0),
            (255, 255, 255),
            (65, 65, 65),
            (255, 215, 0),
            (0, 191, 255),
            (255, 0, 0),
            (255, 87, 51),
            (0, 0, 0),
        ]
        bg, white, gray, yellow, blue, red, orange = range(7)
        pixels = [bg] * (width * height)

        def dot(x: int, y: int, color: int):
            if 0 <= x < width and 0 <= y < height:
                pixels[y * width + x] = color

        def rect(x: int, y: int, w: int, h: int, color: int):
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    dot(xx, yy, color)

        tiny = {
            "0": [7, 5, 5, 5, 7], "1": [2, 6, 2, 2, 7], "2": [7, 1, 7, 4, 7], "3": [7, 1, 7, 1, 7],
            "4": [5, 5, 7, 1, 1], "5": [7, 4, 7, 1, 7], "6": [7, 4, 7, 5, 7], "7": [7, 1, 2, 2, 2],
            "8": [7, 5, 7, 5, 7], "9": [7, 5, 7, 1, 7], "A": [2, 5, 7, 5, 5], "B": [6, 5, 6, 5, 6],
            "C": [7, 4, 4, 4, 7], "D": [6, 5, 5, 5, 6], "E": [7, 4, 6, 4, 7], "F": [7, 4, 6, 4, 4],
            "H": [5, 5, 7, 5, 5], "I": [7, 2, 2, 2, 7], "L": [4, 4, 4, 4, 7], "M": [5, 7, 7, 5, 5], "N": [5, 7, 7, 7, 5],
            "O": [7, 5, 5, 5, 7], "P": [7, 5, 7, 4, 4], "R": [7, 5, 7, 6, 5], "S": [7, 4, 7, 1, 7],
            "T": [7, 2, 2, 2, 2], "U": [5, 5, 5, 5, 7], "W": [5, 5, 7, 7, 5], "Y": [5, 5, 2, 2, 2],
            "-": [0, 0, 7, 0, 0],
        }

        def text3(text: str, x: int, y: int, color: int = white):
            cx = x
            for ch in text.upper():
                if ch == " ":
                    cx += 4
                    continue
                rows = tiny.get(ch)
                if rows:
                    for yy, bits in enumerate(rows):
                        for xx in range(3):
                            if bits & (1 << (2 - xx)):
                                dot(cx + xx, y + yy, color)
                cx += 4

        big = {
            "0": [31, 17, 17, 17, 17, 17, 31],
            "1": [4, 12, 4, 4, 4, 4, 31],
            "2": [31, 1, 1, 31, 16, 16, 31],
            "3": [31, 1, 1, 15, 1, 1, 31],
            "4": [17, 17, 17, 31, 1, 1, 1],
            "5": [31, 16, 16, 31, 1, 1, 31],
            "6": [31, 16, 16, 31, 17, 17, 31],
            "7": [31, 1, 2, 4, 4, 4, 4],
            "8": [31, 17, 17, 31, 17, 17, 31],
            "9": [31, 17, 17, 31, 1, 1, 31],
        }

        def big_char(ch: str, x: int, y: int):
            if ch == " ":
                return
            rows = big.get(ch)
            if not rows:
                return
            for yy, bits in enumerate(rows):
                for xx in range(5):
                    if bits & (1 << (4 - xx)):
                        rect(x + xx * 2, y + yy * 2, 2, 2, white)

        sunny = [0b00011000, 0b01011010, 0b00111100, 0b11111111, 0b11111111, 0b00111100, 0b01011010, 0b00011000]
        for yy, bits in enumerate(sunny):
            for xx in range(8):
                if bits & (1 << (7 - xx)):
                    dot(54 + xx, 2 + yy, yellow)

        now = time.localtime()
        months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
        weekdays = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
        text3(f"{months[now.tm_mon - 1]} {now.tm_mday:02d} - {weekdays[now.tm_wday]}", 2, 2)
        text3("AM" if now.tm_hour < 12 else "PM", 2, 14)
        hour = now.tm_hour % 12 or 12
        hour_text = f"{hour:02d}"
        minute_text = f"{now.tm_min:02d}"

        time_center_top = 20
        time_center_height = 26
        digit_width = 10
        digit_gap = 2
        colon_width = 2
        colon_gap = 3
        time_y = time_center_top + (time_center_height - 14) // 2
        colon_x = (width // 2) - (colon_width // 2)
        side_width = digit_width * 2 + digit_gap
        hour_x = colon_x - colon_gap - side_width
        minute_x = colon_x + colon_width + colon_gap

        big_char(hour_text[0], hour_x, time_y)
        big_char(hour_text[1], hour_x + digit_width + digit_gap, time_y)
        if (int(time.time() * 1000) // 500) % 2 == 0:
            rect(colon_x, time_y + 4, colon_width, 3, white)
            rect(colon_x, time_y + 12, colon_width, 3, white)
        big_char(minute_text[0], minute_x, time_y)
        big_char(minute_text[1], minute_x + digit_width + digit_gap, time_y)

        for i in range(30):
            x = 3 + i * 2
            dot(x, 49, gray)
            dot(x, 51, gray)
        second = now.tm_sec
        active = second % 30
        y = 49 if second < 30 else 51
        rect(3 + active * 2, y, 2, 2, red)

        return self._gif_from_indexed(width, height, palette, pixels)

    def _build_weather_forecast_gif(self) -> bytes:
        width, height = 64, 54
        palette = [
            (0, 0, 0),
            (255, 255, 255),
            (255, 215, 0),
            (211, 84, 0),
            (157, 223, 255),
            (132, 132, 132),
            (0, 128, 255),
            (255, 235, 59),
        ]
        bg, white, sun_body, sun_ray, blue, gray, rain_blue, bolt = range(8)
        pixels = [bg] * (width * height)

        def dot(x: int, y: int, color: int):
            if 0 <= x < width and 0 <= y < height:
                pixels[y * width + x] = color

        def rect(x: int, y: int, w: int, h: int, color: int):
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    dot(xx, yy, color)

        tiny = {
            "0": [7, 5, 5, 5, 7], "1": [2, 6, 2, 2, 7], "2": [7, 1, 7, 4, 7], "3": [7, 1, 7, 1, 7],
            "4": [5, 5, 7, 1, 1], "5": [7, 4, 7, 1, 7], "6": [7, 4, 7, 5, 7], "7": [7, 1, 2, 2, 2],
            "8": [7, 5, 7, 5, 7], "9": [7, 5, 7, 1, 7], "A": [2, 5, 7, 5, 5], "B": [6, 5, 6, 5, 6],
            "C": [7, 4, 4, 4, 7], "D": [6, 5, 5, 5, 6], "E": [7, 4, 6, 4, 7], "F": [7, 4, 6, 4, 4],
            "G": [7, 4, 5, 5, 7], "H": [5, 5, 7, 5, 5], "I": [7, 2, 2, 2, 7], "K": [5, 5, 6, 5, 5],
            "L": [4, 4, 4, 4, 7], "M": [5, 7, 5, 5, 5], "N": [5, 7, 7, 7, 5], "O": [7, 5, 5, 5, 7],
            "P": [7, 5, 7, 4, 4], "R": [7, 5, 7, 6, 5], "S": [7, 4, 7, 1, 7], "T": [7, 2, 2, 2, 2],
            "U": [5, 5, 5, 5, 7], "W": [5, 5, 7, 7, 5], "Y": [5, 5, 2, 2, 2], "-": [0, 0, 7, 0, 0],
            "%": [5, 1, 2, 4, 5],
        }

        def text3(text: str, x: int, y: int, color: int = white, max_chars: int | None = None):
            cx = x
            for ch in text.upper()[:max_chars]:
                if ch == " ":
                    cx += 2
                    continue
                rows = tiny.get(ch)
                if rows:
                    for yy, bits in enumerate(rows):
                        for xx in range(3):
                            if bits & (1 << (2 - xx)):
                                dot(cx + xx, y + yy, color)
                cx += 4

        def mini_icon(kind: str, x: int, y: int):
            if kind == "sun":
                bitmap = [0b00011000, 0b01011010, 0b00111100, 0b11111111, 0b11111111, 0b00111100, 0b01011010, 0b00011000]
                for yy, bits in enumerate(bitmap):
                    for xx in range(8):
                        if bits & (1 << (7 - xx)):
                            dot(x + xx, y + yy, sun_ray if yy < 2 or yy > 5 or xx < 2 or xx > 5 else sun_body)
            elif kind == "rain":
                bitmap = [0b00111000, 0b01111100, 0b11111110, 0b01111110, 0, 0b01001000, 0b00100100, 0b01001000]
                for yy, bits in enumerate(bitmap):
                    for xx in range(8):
                        if bits & (1 << (7 - xx)):
                            dot(x + xx, y + yy, gray if yy < 4 else rain_blue)
            else:
                bitmap = [0b00111000, 0b01111100, 0b11111110, 0b01110110, 0b00011000, 0b00110000, 0b01100000, 0b01000000]
                for yy, bits in enumerate(bitmap):
                    for xx in range(8):
                        if bits & (1 << (7 - xx)):
                            dot(x + xx, y + yy, bolt if yy >= 4 or xx >= 4 else gray)

        def large_sun(cx: int, cy: int):
            for x, y, w, h in [(cx - 2, cy - 12, 5, 3), (cx - 2, cy + 10, 5, 3), (cx - 12, cy - 2, 3, 5), (cx + 10, cy - 2, 3, 5)]:
                rect(x, y, w, h, sun_ray)
            for x, y in [(cx - 9, cy - 9), (cx + 7, cy - 9), (cx - 9, cy + 7), (cx + 7, cy + 7)]:
                rect(x, y, 3, 3, sun_ray)
            for yy in range(-10, 11):
                for xx in range(-10, 11):
                    d2 = xx * xx + yy * yy
                    if d2 <= 92:
                        dot(cx + xx, cy + yy, sun_ray if d2 > 58 else sun_body)
            rect(cx - 15, cy + 1, 7, 6, blue)
            rect(cx - 9, cy - 2, 10, 8, white)
            rect(cx - 3, cy + 1, 9, 6, gray)
            rect(cx - 13, cy + 5, 20, 5, white)
            for i in range(3):
                rect(cx - 11 + i * 6, cy + 11, 2, 2, rain_blue)

        now = time.localtime()
        months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
        weekdays = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
        text3(f"{months[now.tm_mon - 1]}{now.tm_mday:02d}-{weekdays[now.tm_wday]}", 0, 2, white)
        text3("CLR", 0, 11, white)
        large_sun(20, 28)
        for idx, kind in enumerate(("sun", "rain", "thunder")):
            row_y = (0, 14, 28)[idx]
            day = weekdays[(now.tm_wday + idx + 1) % 7]
            text3(day, 42, row_y + 2, white)
            mini_icon(kind, 55, row_y)
        text3("30C", 1, 43, white)
        text3("TEMP", 0, 49, white)
        text3("60%", 22, 43, white)
        text3("RH", 22, 49, white)
        text3("25KMH", 40, 43, white)
        text3("WS", 40, 49, white)

        return self._gif_from_indexed(width, height, palette, pixels)

    def _build_playtime_monitor_gif(self) -> bytes:
        width, height = 64, 54
        palette = [
            (0, 0, 0),
            (255, 255, 255),
            (102, 102, 102),
            (255, 0, 0),
            (100, 0, 255),
            (255, 240, 0),
            (255, 0, 180),
            (250, 145, 0),
            (250, 237, 0),
        ]
        bg, white, gray, red, purple, yellow, pink, orange, trophy_yellow = range(9)
        pixels = [bg] * (width * height)

        def dot(x: int, y: int, color: int):
            if 0 <= x < width and 0 <= y < height:
                pixels[y * width + x] = color

        def rect(x: int, y: int, w: int, h: int, color: int):
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    dot(xx, yy, color)

        tiny = {
            "0": [7, 5, 5, 5, 7], "1": [2, 6, 2, 2, 7], "2": [7, 1, 7, 4, 7],
            "3": [7, 1, 7, 1, 7], "4": [5, 5, 7, 1, 1], "5": [7, 4, 7, 1, 7],
            "6": [7, 4, 7, 5, 7], "7": [7, 1, 2, 2, 2], "8": [7, 5, 7, 5, 7],
            "9": [7, 5, 7, 1, 7], "A": [2, 5, 7, 5, 5], "B": [6, 5, 6, 5, 6],
            "C": [7, 4, 4, 4, 7], "D": [6, 5, 5, 5, 6], "E": [7, 4, 6, 4, 7],
            "F": [7, 4, 6, 4, 4], "G": [7, 4, 5, 5, 7], "H": [5, 5, 7, 5, 5],
            "I": [7, 2, 2, 2, 7], "K": [5, 5, 6, 5, 5], "L": [4, 4, 4, 4, 7],
            "M": [5, 7, 7, 5, 5], "N": [5, 7, 7, 7, 5], "O": [7, 5, 5, 5, 7],
            "P": [7, 5, 7, 4, 4], "R": [7, 5, 7, 6, 5], "S": [7, 4, 7, 1, 7],
            "T": [7, 2, 2, 2, 2], "U": [5, 5, 5, 5, 7], "V": [5, 5, 5, 5, 2],
            "W": [5, 5, 7, 7, 5], "Y": [5, 5, 2, 2, 2], "-": [0, 0, 7, 0, 0],
            " ": [0, 0, 0, 0, 0],
        }
        medium = {
            "0": [0b011110, 0b111111, 0b110011, 0b110011, 0b110011, 0b110011, 0b111111, 0b011110],
            "1": [0b111000, 0b111100, 0b001100, 0b001100, 0b001100, 0b001100, 0b111111, 0b111111],
            "2": [0b011111, 0b111111, 0b000011, 0b011111, 0b111110, 0b110000, 0b111111, 0b011111],
            "3": [0b011111, 0b111111, 0b000011, 0b011111, 0b011111, 0b000011, 0b111111, 0b011110],
            "4": [0b110011, 0b110011, 0b110011, 0b111111, 0b011111, 0b000011, 0b000011, 0b000011],
            "5": [0b011111, 0b111111, 0b110000, 0b111110, 0b011111, 0b000011, 0b111111, 0b111110],
            "6": [0b011111, 0b111111, 0b110000, 0b111110, 0b111111, 0b110011, 0b111111, 0b011110],
            "7": [0b111110, 0b111111, 0b000011, 0b000011, 0b000011, 0b000011, 0b000011, 0b000011],
            "8": [0b011110, 0b111111, 0b110011, 0b111111, 0b111111, 0b110011, 0b111111, 0b011110],
            "9": [0b011110, 0b111111, 0b110011, 0b111111, 0b011111, 0b000011, 0b111111, 0b111110],
        }

        def tiny_width(text: str) -> int:
            return 0 if not text else len(text) * 4 - 1

        def draw_tiny(text: str, x: int, y: int, color: int = white):
            cx = x
            for ch in text.upper():
                rows = tiny.get(ch, tiny[" "])
                for yy, bits in enumerate(rows):
                    for xx in range(3):
                        if bits & (1 << (2 - xx)):
                            dot(cx + xx, y + yy, color)
                cx += 4

        def split_title(title: str) -> tuple[str, str] | None:
            title = " ".join(title.upper().split())
            if tiny_width(title) <= 60:
                return None
            words = title.split(" ")
            first = ""
            second = ""
            for word in words:
                candidate = word if not first else f"{first} {word}"
                if tiny_width(candidate) <= 60 or not first:
                    first = candidate
                else:
                    second = word if not second else f"{second} {word}"
            return first[:15], second[:15]

        def draw_tiny_time(text: str, right_x: int, y: int):
            width_px = sum(2 if ch == ":" else 4 for ch in text[:5]) - 1
            x = right_x - width_px + 1
            for ch in text[:5]:
                if ch == ":":
                    dot(x, y + 1, white)
                    dot(x, y + 3, white)
                    x += 2
                else:
                    draw_tiny(ch, x, y)
                    x += 4

        def draw_medium_digit(ch: str, x: int, y: int):
            rows = medium.get(ch)
            if not rows:
                return
            for yy, bits in enumerate(rows):
                for xx in range(6):
                    if bits & (1 << (5 - xx)):
                        dot(x + xx, y + yy, white)

        context = self._scan_context()
        title = context.get("title") or "STARDEW VALLEY"
        if len(title) < 4 or title.lower() in {"steam", "reaper"}:
            title = "STARDEW VALLEY"
        clean_title = " ".join(title.upper().split())
        if clean_title == "STARDEW VALLEY":
            draw_tiny("STARDEW", 2, 2)
            draw_tiny("VALLEY", 2, 9)
        else:
            split = split_title(clean_title)
            if split is None:
                draw_tiny(clean_title[:15], 2, 2)
            else:
                draw_tiny(split[0], 2, 2)
                draw_tiny(split[1], 2, 9)

        pad_rows = (
            ".....B...",
            "....B....",
            ".BBBBBBB.",
            "BBYBBBYBB",
            "BYYYBYBYB",
            "BBYBBBYBM",
            "BBBMBBBBM",
            ".BM...MM.",
        )
        for yy, row in enumerate(pad_rows):
            for xx, ch in enumerate(row):
                if ch == "B":
                    dot(2 + xx, 19 + yy, purple)
                elif ch == "Y":
                    dot(2 + xx, 19 + yy, yellow)
                elif ch == "M":
                    dot(2 + xx, 19 + yy, pink)

        app_id = context.get("appId", "")
        session_started_at = context.get("sessionStartedAt") or 0
        if app_id and app_id != self._playtime_current_app_id:
            self._playtime_current_app_id = app_id
            self._playtime_session_started_at = session_started_at or time.time()
        if session_started_at:
            self._playtime_session_started_at = float(session_started_at)

        session_minutes = max(0, int((time.time() - self._playtime_session_started_at) // 60))
        draw_tiny_time(f"{(session_minutes // 60) % 100:02d}:{session_minutes % 60:02d}", 61, 22)

        for tick_x in (2, 11, 21, 31, 41, 51, 61):
            dot(tick_x, 29, gray)
            dot(tick_x, 34, gray)
        dot(2, 30, gray)
        dot(2, 33, gray)
        for tick_x in range(6, 62, 5):
            dot(tick_x, 30, gray)
            dot(tick_x, 33, gray)
        filled = max(0, min(31, int((session_minutes % 300) * 40 / 300)))
        rect(2, 31, filled, 2, white)
        rect(2 + filled, 31, 1, 2, red)

        trophy_rows = (
            "..YYYYO..",
            "YYWYYYOYY",
            "Y.WYYYO.Y",
            "Y.WYYYO.Y",
            ".YYWYYOY.",
            "...YYO...",
            "....Y....",
            "....Y....",
            "..WYYYO..",
            ".YYYYYYO.",
        )
        for yy, row in enumerate(trophy_rows):
            for xx, ch in enumerate(row):
                if ch == "Y":
                    dot(2 + xx, 42 + yy, trophy_yellow)
                elif ch == "W":
                    dot(2 + xx, 42 + yy, white)
                elif ch == "O":
                    dot(2 + xx, 42 + yy, orange)

        total_minutes = max(0, int(context.get("totalPlaytimeMinutes") or 0))
        digits = f"{min(99999, (total_minutes + session_minutes) // 60):05d}" if total_minutes else "19806"
        x = 18
        for ch in digits:
            draw_medium_digit(ch, x, 43)
            x += 7
        small_h = (0b100, 0b100, 0b111, 0b101, 0b101)
        for yy, bits in enumerate(small_h):
            for xx in range(3):
                if bits & (1 << (2 - xx)):
                    dot(54 + xx, 46 + yy, white)
        rect(13, 42, 3, 1, gray)
        rect(13, 43, 1, 2, gray)
        dot(15, 44, gray)
        rect(13, 51, 3, 1, gray)
        rect(13, 49, 1, 2, gray)
        dot(15, 49, gray)
        rect(59, 42, 3, 1, gray)
        dot(59, 44, gray)
        rect(61, 43, 1, 2, gray)
        rect(59, 51, 3, 1, gray)
        dot(59, 49, gray)
        rect(61, 49, 1, 2, gray)

        return self._gif_from_indexed(width, height, palette, pixels)

    @staticmethod
    def _countdown_display_state(total: int, remaining: int) -> tuple[str, int, int]:
        total = max(1, int(total))
        remaining = max(0, min(total, int(remaining)))
        if total >= 3600:
            time_text = f"{min(99, remaining // 3600):02d}:{(remaining // 60) % 60:02d}"
            seconds_in_minute = remaining % 60
            bar_units = 60 if seconds_in_minute == 0 and remaining > 0 else seconds_in_minute
            return time_text, bar_units, 60
        if total >= 60:
            return f"{min(99, remaining // 60):02d}:{remaining % 60:02d}", 60, 60
        return f"00:{remaining:02d}", 60, 60

    def _build_countdown_monitor_gif(
        self,
        count_up: bool = False,
        elapsed_override: int | None = None,
        image_format: str = "gif",
    ) -> bytes:
        width, height = 64, 54
        palette = [
            (0, 0, 0),
            (255, 255, 255),
            (102, 102, 102),
            (255, 105, 0),
            (255, 0, 0),
            (160, 255, 0),
            (255, 105, 0),
            (96, 0, 255),
        ]
        bg, white, gray, orange, red, green, sand, purple = range(8)
        pixels = [bg] * (width * height)

        def dot(x: int, y: int, color: int):
            if 0 <= x < width and 0 <= y < height:
                pixels[y * width + x] = color

        def rect(x: int, y: int, w: int, h: int, color: int):
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    dot(xx, yy, color)

        tiny = {
            "0": [7, 5, 5, 5, 7], "1": [2, 6, 2, 2, 7], "2": [7, 1, 7, 4, 7], "3": [7, 1, 7, 1, 7],
            "4": [5, 5, 7, 1, 1], "5": [7, 4, 7, 1, 7], "6": [7, 4, 7, 5, 7], "7": [7, 1, 2, 2, 2],
            "8": [7, 5, 7, 5, 7], "9": [7, 5, 7, 1, 7], "A": [2, 5, 7, 5, 5], "C": [7, 4, 4, 4, 7],
            "D": [6, 5, 5, 5, 6], "E": [7, 4, 7, 4, 7], "I": [7, 2, 2, 2, 7], "M": [5, 7, 5, 5, 5],
            "N": [5, 7, 7, 7, 5], "O": [7, 5, 5, 5, 7], "R": [7, 5, 6, 5, 5], "S": [3, 4, 2, 1, 6], "T": [7, 2, 2, 2, 2],
            "U": [5, 5, 5, 5, 7], "W": [5, 5, 7, 7, 5],
        }

        def text3(text: str, x: int, y: int, color: int = white):
            cx = x
            for ch in text.upper():
                if ch == " ":
                    cx += 4
                    continue
                rows = tiny.get(ch)
                if rows:
                    for yy, bits in enumerate(rows):
                        for xx in range(3):
                            if bits & (1 << (2 - xx)):
                                dot(cx + xx, y + yy, color)
                cx += 4

        clock_big = {
            "0": (12, [0x7FE0, 0xFFF0, 0xFFF0, 0xFFF0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xFFF0, 0xFFF0, 0xFFF0, 0x7FE0]),
            "1": (12, [0x0F00, 0x0F00, 0x7F00, 0x7F00, 0x7F00, 0x7F00, 0x0F00, 0x0F00, 0x0F00, 0x0F00, 0x0F00, 0x0F00, 0x0F00, 0x0F00, 0x0F00, 0x0F00, 0xFFF0, 0xFFF0, 0xFFF0, 0xFFF0]),
            "2": (12, [0xFFE0, 0xFFF0, 0xFFF0, 0xFFF0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x7FF0, 0xFFF0, 0xFFF0, 0xFFE0, 0xF000, 0xF000, 0xF000, 0xF000, 0xFFF0, 0xFFF0, 0xFFF0, 0x7FF0]),
            "3": (12, [0xFFE0, 0xFFF0, 0xFFF0, 0xFFF0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0xFFF0, 0xFFF0, 0xFFF0, 0xFFF0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0xFFF0, 0xFFF0, 0xFFF0, 0xFFE0]),
            "4": (12, [0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xFFF0, 0xFFF0, 0xFFF0, 0x7FF0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0]),
            "5": (12, [0x7FF0, 0xFFF0, 0xFFF0, 0xFFF0, 0xF000, 0xF000, 0xF000, 0xF000, 0xFFE0, 0xFFF0, 0xFFF0, 0x7FF0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0xFFF0, 0xFFF0, 0xFFF0, 0xFFE0]),
            "6": (12, [0x7FF0, 0xFFF0, 0xFFF0, 0xFFF0, 0xF000, 0xF000, 0xF000, 0xF000, 0xFFE0, 0xFFF0, 0xFFF0, 0xFFF0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xFFF0, 0xFFF0, 0xFFF0, 0x7FE0]),
            "7": (12, [0xFFE0, 0xFFF0, 0xFFF0, 0xFFF0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0x00F0]),
            "8": (12, [0x7FE0, 0xFFF0, 0xFFF0, 0xFFF0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xFFF0, 0xFFF0, 0xFFF0, 0xFFF0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xFFF0, 0xFFF0, 0xFFF0, 0x7FE0]),
            "9": (12, [0x7FE0, 0xFFF0, 0xFFF0, 0xFFF0, 0xF0F0, 0xF0F0, 0xF0F0, 0xF0F0, 0xFFF0, 0xFFF0, 0xFFF0, 0x7FF0, 0x00F0, 0x00F0, 0x00F0, 0x00F0, 0xFFF0, 0xFFF0, 0xFFF0, 0xFFE0]),
            ":": (4, [0x0000, 0x0000, 0x0000, 0x0000, 0xF000, 0xF000, 0xF000, 0xF000, 0x0000, 0x0000, 0x0000, 0x0000, 0xF000, 0xF000, 0xF000, 0xF000, 0x0000, 0x0000, 0x0000, 0x0000]),
        }

        def clock_big_char(ch: str, x: int, y: int):
            glyph = clock_big.get(ch)
            if not glyph:
                return
            glyph_width, rows = glyph
            for yy, bits in enumerate(rows):
                for xx in range(glyph_width):
                    if bits & (1 << (15 - xx)):
                        dot(x + xx, y + yy, white)

        def hourglass(x: int, y: int):
            rows = (
                "GGGGG",
                "GOOOG",
                ".GOG.",
                "..G..",
                ".G.G.",
                "G.O.G",
                "GGGGG",
            )
            for yy, row in enumerate(rows):
                for xx, ch in enumerate(row):
                    if ch == "G":
                        dot(x + xx, y + yy, green)
                    elif ch == "O":
                        dot(x + xx, y + yy, sand)

        def timer_icon(x: int, y: int):
            rows = (
                "..GGG..",
                ".GPPPG.",
                "GPPGPPG",
                "GPPGGPG",
                "GPPPPPG",
                ".GPPPG.",
                "..GGG..",
            )
            for yy, row in enumerate(rows):
                for xx, ch in enumerate(row):
                    if ch == "G":
                        dot(x + xx, y + yy, green)
                    elif ch == "P":
                        dot(x + xx, y + yy, purple)

        if count_up:
            total = max(1, int(self._chronograph_total_seconds))
            elapsed = elapsed_override if elapsed_override is not None else int(time.time() - self._chronograph_started_at)
            elapsed = max(0, min(total, int(elapsed)))
            time_text = f"{(elapsed // 3600) % 100:02d}:{(elapsed // 60) % 60:02d}"
            bar_units = elapsed % 60
            bar_total = 60
        else:
            total = max(1, int(self._countdown_total_seconds))
            elapsed = max(0, min(total, int(time.time() - self._countdown_started_at)))
            remaining = max(0, total - elapsed)
            time_text, bar_units, bar_total = self._countdown_display_state(total, remaining)
            label_text = f"{total}S" if total < 60 else f"{max(1, total // 60):02d}MIN"
        if count_up:
            text3("TIMER", 2, 2)
            timer_icon(55, 2)
        else:
            text3("COUNTDOWN", 2, 2)
            if total < 60:
                text3(f"{total}", 2, 9)
                tiny_s = (3, 4, 2, 1, 6)
                for yy, bits in enumerate(tiny_s):
                    for xx in range(3):
                        if bits & (1 << (2 - xx)):
                            dot(11 + xx, 9 + yy, white)
            else:
                text3(label_text, 2, 9)
            hourglass(57, 2)

        clock_big_char(time_text[0], 2, 18)
        clock_big_char(time_text[1], 16, 18)
        clock_big_char(":", 30, 18)
        clock_big_char(time_text[3], 36, 18)
        clock_big_char(time_text[4], 50, 18)

        bar_x, bar_y, bar_w = 2, 48, 60
        if count_up:
            for tick_x in [bar_x, *range(bar_x + 9, bar_x + bar_w, 10)]:
                dot(tick_x, bar_y - 2, gray)
                dot(tick_x, bar_y + 3, gray)
            for tick_x in [bar_x, *range(bar_x + 4, bar_x + bar_w, 5)]:
                dot(tick_x, bar_y - 1, gray)
                dot(tick_x, bar_y + 2, gray)
        else:
            for tick_x in range(bar_x + 2, bar_x + bar_w, 3):
                dot(tick_x, bar_y - 1, gray)
                dot(tick_x, bar_y + 2, gray)
            for tick_x in range(bar_x + 5, bar_x + bar_w, 6):
                dot(tick_x, bar_y - 2, gray)
                dot(tick_x, bar_y + 3, gray)
        if count_up:
            current_width = max(1, min(bar_w, (bar_units + 1) * bar_w // bar_total))
        else:
            current_width = max(0, min(bar_w, bar_units * bar_w // bar_total))
        if current_width:
            rect(bar_x, bar_y, current_width, 2, orange)
        cursor_x = bar_x if current_width <= 0 else min(bar_x + bar_w - 1, bar_x + current_width - 1)
        rect(cursor_x, bar_y, 1, 2, red)

        if image_format == "bmp":
            return self._bmp_from_indexed(width, height, palette, pixels)
        return self._gif_from_indexed(width, height, palette, pixels)

    def _build_chronograph_monitor_gif(self) -> bytes:
        return self._build_countdown_monitor_gif(count_up=True)

    def _build_chronograph_monitor_asset(self) -> bytes:
        return self._build_countdown_monitor_gif(count_up=True, image_format="bmp")

    def _build_dashboard_animated_gif(self) -> bytes:
        width, height = 64, 54
        palette = [
            (0, 0, 0),
            (255, 255, 255),
            (65, 65, 65),
            (33, 32, 33),
            (255, 85, 0),
            (255, 0, 0),
            (255, 130, 0),
            (0, 0, 0),
        ]
        metrics = self._sample_dashboard_metrics()
        start = int(time.time())
        frames = [self._render_dashboard_pixels(time.localtime(start + offset), metrics) for offset in range(30)]
        return self._gif_from_rgb222_frames(width, height, palette, frames, delay_cs=100)

    def _render_dashboard_pixels(self, now, metrics: tuple[int, int, int, int]) -> list[int]:
        width, height = 64, 54
        bg, white, gray, dim, orange, red, sun = range(7)
        pixels = [bg] * (width * height)

        def dot(x: int, y: int, color: int):
            if 0 <= x < width and 0 <= y < height:
                pixels[y * width + x] = color

        def rect(x: int, y: int, w: int, h: int, color: int):
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    dot(xx, yy, color)

        tiny = {
            "0": [7, 5, 5, 5, 7], "1": [2, 6, 2, 2, 7], "2": [7, 1, 7, 4, 7], "3": [7, 1, 7, 1, 7],
            "4": [5, 5, 7, 1, 1], "5": [7, 4, 7, 1, 7], "6": [7, 4, 7, 5, 7], "7": [7, 1, 2, 2, 2],
            "8": [7, 5, 7, 5, 7], "9": [7, 5, 7, 1, 7], "A": [2, 5, 7, 5, 5], "B": [6, 5, 6, 5, 6],
            "C": [7, 4, 4, 4, 7], "D": [6, 5, 5, 5, 6], "E": [7, 4, 6, 4, 7], "F": [7, 4, 6, 4, 4],
            "G": [7, 4, 5, 5, 7], "H": [5, 5, 7, 5, 5], "I": [7, 2, 2, 2, 7], "J": [1, 1, 1, 5, 7],
            "K": [5, 5, 6, 5, 5], "L": [4, 4, 4, 4, 7], "M": [5, 7, 7, 5, 5], "N": [5, 7, 7, 7, 5],
            "O": [7, 5, 5, 5, 7], "P": [7, 5, 7, 4, 4], "R": [7, 5, 7, 6, 5], "S": [7, 4, 7, 1, 7],
            "T": [7, 2, 2, 2, 2], "U": [5, 5, 5, 5, 7], "V": [5, 5, 5, 5, 2], "W": [5, 5, 7, 7, 5],
            "X": [5, 5, 2, 5, 5], "Y": [5, 5, 2, 2, 2], "Z": [7, 1, 2, 4, 7], "%": [5, 1, 2, 4, 5],
            "-": [0, 0, 7, 0, 0], ":": [0, 2, 0, 2, 0],
        }

        def text3(text: str, x: int, y: int, color: int = white):
            cx = x
            for ch in text.upper():
                if ch == " ":
                    cx += 4
                    continue
                rows = tiny.get(ch)
                if rows:
                    for yy, bits in enumerate(rows):
                        for xx in range(3):
                            if bits & (1 << (2 - xx)):
                                dot(cx + xx, y + yy, color)
                cx += 4

        large = {
            "0": [31, 17, 19, 21, 21, 25, 17, 17, 31], "1": [4, 12, 20, 4, 4, 4, 4, 4, 31],
            "2": [31, 1, 1, 31, 16, 16, 16, 16, 31], "3": [31, 1, 1, 15, 1, 1, 1, 1, 31],
            "4": [17, 17, 17, 31, 1, 1, 1, 1, 1], "5": [31, 16, 16, 31, 1, 1, 1, 1, 31],
            "6": [31, 16, 16, 31, 17, 17, 17, 17, 31], "7": [31, 1, 1, 2, 4, 4, 4, 4, 4],
            "8": [31, 17, 17, 31, 17, 17, 17, 17, 31], "9": [31, 17, 17, 31, 1, 1, 1, 1, 31],
        }

        def time_text(value: str, x: int, y: int):
            cx = x
            blink = now.tm_sec % 2 == 0
            for ch in value:
                if ch == ":":
                    if blink:
                        rect(cx, y + 2, 1, 2, white)
                        rect(cx, y + 6, 1, 2, white)
                    cx += 2
                    continue
                rows = large.get(ch)
                if rows:
                    for yy, bits in enumerate(rows):
                        for xx in range(5):
                            if bits & (1 << (4 - xx)):
                                dot(cx + xx, y + yy, white)
                cx += 6

        text3(time.strftime("%b %d - %a", now).upper(), 0, 0)
        text3(time.strftime("%p", now).upper(), 56, 0)
        sun_rows = ["..1..1..", "...11...", ".111111.", "..1111..", "11111111", "..1111..", ".111111.", "...11..."]
        for yy, row in enumerate(sun_rows):
            for xx, ch in enumerate(row):
                if ch == "1":
                    dot(1 + xx, 12 + yy, sun)
        hour = now.tm_hour % 12 or 12
        minute = now.tm_min
        value = f"{hour:02d}:{minute:02d}"
        time_text(value, 64 - 25 - 1, 11)

        for i in range(30):
            x = 2 + i * 2
            dot(x, 24, gray)
            dot(x, 26, gray)
            dot(x, 28, gray)
        dot(2 + min(29, now.tm_sec // 2) * 2, 26, red)

        gpu, cpu, ram, fps = metrics

        def metric(label: str, val: str, percent: int, x: int, y: int, bar_y: int, draw_bar: bool):
            text3(f"{label} {val}", x, y)
            if draw_bar:
                rect(x, bar_y - 1, 27, 2, dim)
                if percent < 0:
                    return
                fill = max(1, min(27, int(27 * max(0, min(100, percent)) / 100)))
                rect(x, bar_y - 1, fill, 2, orange)
                rect(x + fill - 1, bar_y - 1, 1, 2, red)

        metric("GPU", f"{gpu if gpu >= 0 else '--'}%", gpu, 1, 31, 40, True)
        metric("CPU", f"{cpu if cpu >= 0 else '--'}%", cpu, 34, 31, 40, True)
        metric("RAM", f"{ram if ram >= 0 else '--'}%", ram, 1, 44, 52, True)
        text3(f"FPS {fps if fps >= 0 else '--'}", 34, 44)
        for yy in range(51, 53, 2):
            for xx in range(34, 61, 2):
                dot(xx, yy, dim)

        return pixels

    def _sample_dashboard_metrics(self):
        """Return the same performance sample used by both native monitor modules.

        The daemon owns the system probes and refresh cadence.  Keeping this
        legacy GIF path on the daemon endpoint prevents it from silently
        falling back to guessed values or a different RAM/FPS definition.
        """

        def percent(value) -> int:
            try:
                normalized = int(float(value))
            except (TypeError, ValueError, OverflowError):
                return -1
            return normalized if 0 <= normalized <= 100 else -1

        def refresh_rate(value) -> int:
            try:
                normalized = int(float(value) + 0.5)
            except (TypeError, ValueError, OverflowError):
                return -1
            return normalized if 1 <= normalized <= 999 else -1

        try:
            payload = self.client.get("/v1/runtime/performance")
            system = payload.get("systemPerformance") if isinstance(payload, dict) else None
            dashboard = payload.get("dashboard") if isinstance(payload, dict) else None
            if not isinstance(system, dict):
                system = {}
            if not isinstance(dashboard, dict):
                dashboard = {}

            def metric_percent(name: str) -> int:
                value = system.get(f"{name}Percent")
                if value is None:
                    value = dashboard.get(f"{name}Percent")
                return percent(value)

            fps_value = payload.get("refreshRate") if isinstance(payload, dict) else None
            if fps_value in (None, ""):
                fps_value = system.get("fpsValue") or dashboard.get("fpsValue")
            return (
                metric_percent("gpu"),
                metric_percent("cpu"),
                metric_percent("ram"),
                refresh_rate(fps_value),
            )
        except Exception as exc:
            decky.logger.info(f"Performance sample unavailable: {exc}")
            return -1, -1, -1, -1

    def _gif_from_indexed(self, width: int, height: int, palette: list[tuple[int, int, int]], pixels: list[int]) -> bytes:
        min_code_size = max(2, (len(palette) - 1).bit_length())
        table_size = 1 << min_code_size
        palette = palette + [(0, 0, 0)] * (table_size - len(palette))
        color_table = bytearray()
        for red, green, blue in palette:
            color_table.extend([red, green, blue])

        image_data = self._gif_lzw(pixels, min_code_size)
        blocks = bytearray()
        for offset in range(0, len(image_data), 255):
            chunk = image_data[offset : offset + 255]
            blocks.append(len(chunk))
            blocks.extend(chunk)
        blocks.append(0)

        gif = bytearray(b"GIF89a")
        gif.extend(width.to_bytes(2, "little"))
        gif.extend(height.to_bytes(2, "little"))
        gif.extend(bytes([0xA0 | (min_code_size - 1), 0x00, 0x00]))
        gif.extend(color_table)
        gif.extend(b"\x2C\x00\x00\x00\x00")
        gif.extend(width.to_bytes(2, "little"))
        gif.extend(height.to_bytes(2, "little"))
        gif.append(0x00)
        gif.append(min_code_size)
        gif.extend(blocks)
        gif.append(0x3B)
        return bytes(gif)

    def _bmp_from_indexed(self, width: int, height: int, palette: list[tuple[int, int, int]], pixels: list[int]) -> bytes:
        row_stride = ((width * 3 + 3) // 4) * 4
        pixel_bytes = bytearray()
        for y in range(height - 1, -1, -1):
            row = bytearray()
            for x in range(width):
                red, green, blue = palette[pixels[y * width + x]]
                row.extend([blue, green, red])
            row.extend(b"\x00" * (row_stride - len(row)))
            pixel_bytes.extend(row)

        file_header_size = 14
        dib_header_size = 40
        pixel_offset = file_header_size + dib_header_size
        file_size = pixel_offset + len(pixel_bytes)

        bmp = bytearray(b"BM")
        bmp.extend(file_size.to_bytes(4, "little"))
        bmp.extend((0).to_bytes(4, "little"))
        bmp.extend(pixel_offset.to_bytes(4, "little"))
        bmp.extend(dib_header_size.to_bytes(4, "little"))
        bmp.extend(width.to_bytes(4, "little", signed=True))
        bmp.extend(height.to_bytes(4, "little", signed=True))
        bmp.extend((1).to_bytes(2, "little"))
        bmp.extend((24).to_bytes(2, "little"))
        bmp.extend((0).to_bytes(4, "little"))
        bmp.extend(len(pixel_bytes).to_bytes(4, "little"))
        bmp.extend((2835).to_bytes(4, "little", signed=True))
        bmp.extend((2835).to_bytes(4, "little", signed=True))
        bmp.extend((0).to_bytes(4, "little"))
        bmp.extend((0).to_bytes(4, "little"))
        bmp.extend(pixel_bytes)
        return bytes(bmp)

    def _gif_from_indexed_frames(
        self,
        width: int,
        height: int,
        palette: list[tuple[int, int, int]],
        frames: list[list[int]],
        delay_cs: int,
    ) -> bytes:
        color_table = bytearray()
        for red, green, blue in palette:
            color_table.extend([red, green, blue])

        min_code_size = 3
        gif = bytearray(b"GIF89a")
        gif.extend(width.to_bytes(2, "little"))
        gif.extend(height.to_bytes(2, "little"))
        gif.extend(bytes([0xA2, 0x00, 0x00]))
        gif.extend(color_table)
        gif.extend(b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00")

        for pixels in frames:
            image_data = self._gif_lzw(pixels, min_code_size)
            blocks = bytearray()
            for offset in range(0, len(image_data), 255):
                chunk = image_data[offset : offset + 255]
                blocks.append(len(chunk))
                blocks.extend(chunk)
            blocks.append(0)

            gif.extend(b"\x21\xF9\x04\x04")
            gif.extend(int(delay_cs).to_bytes(2, "little"))
            gif.extend(b"\x00\x00")
            gif.extend(b"\x2C\x00\x00\x00\x00")
            gif.extend(width.to_bytes(2, "little"))
            gif.extend(height.to_bytes(2, "little"))
            gif.append(0x00)
            gif.append(min_code_size)
            gif.extend(blocks)

        gif.append(0x3B)
        return bytes(gif)

    def _gif_from_rgb222_frames(
        self,
        width: int,
        height: int,
        palette: list[tuple[int, int, int]],
        frames: list[list[int]],
        delay_cs: int,
    ) -> bytes:
        color_table = bytearray()
        for index in range(64):
            color_table.extend([
                ((index >> 4) & 0x03) * 255 // 3,
                ((index >> 2) & 0x03) * 255 // 3,
                (index & 0x03) * 255 // 3,
            ])

        def quantized(frame: list[int]) -> list[int]:
            result: list[int] = []
            for pixel in frame:
                red, green, blue = palette[pixel]
                result.append(((red >> 6) << 4) | ((green >> 6) << 2) | (blue >> 6))
            return result

        min_code_size = 6
        gif = bytearray(b"GIF89a")
        gif.extend(width.to_bytes(2, "little"))
        gif.extend(height.to_bytes(2, "little"))
        gif.extend(bytes([0xF5, 0x00, 0x00]))
        gif.extend(color_table)
        gif.extend(b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00")

        for frame in frames:
            image_data = self._gif_lzw(quantized(frame), min_code_size)
            blocks = bytearray()
            for offset in range(0, len(image_data), 255):
                chunk = image_data[offset : offset + 255]
                blocks.append(len(chunk))
                blocks.extend(chunk)
            blocks.append(0)

            gif.extend(b"\x21\xF9\x04\x04")
            gif.extend(int(delay_cs).to_bytes(2, "little"))
            gif.extend(b"\xFF\x00")
            gif.extend(b"\x2C\x00\x00\x00\x00")
            gif.extend(width.to_bytes(2, "little"))
            gif.extend(height.to_bytes(2, "little"))
            gif.append(0x00)
            gif.append(min_code_size)
            gif.extend(blocks)

        gif.append(0x3B)
        return bytes(gif)

    def _gif_lzw(self, indices: list[int], min_code_size: int) -> bytes:
        clear = 1 << min_code_size
        end = clear + 1
        dictionary = {(i,): i for i in range(clear)}
        next_code = end + 1
        code_size = min_code_size + 1
        max_code = 1 << code_size
        bit_buffer = 0
        bit_count = 0
        output = bytearray()

        def write_code(code: int):
            nonlocal bit_buffer, bit_count
            bit_buffer |= code << bit_count
            bit_count += code_size
            while bit_count >= 8:
                output.append(bit_buffer & 0xFF)
                bit_buffer >>= 8
                bit_count -= 8

        def reset_dictionary():
            nonlocal dictionary, next_code, code_size, max_code
            dictionary = {(i,): i for i in range(clear)}
            next_code = end + 1
            code_size = min_code_size + 1
            max_code = 1 << code_size

        reset_dictionary()
        write_code(clear)
        current: tuple[int, ...] = ()
        for index in indices:
            candidate = current + (index,)
            if candidate in dictionary:
                current = candidate
                continue
            write_code(dictionary[current])
            if next_code < 4096:
                dictionary[candidate] = next_code
                next_code += 1
                if next_code > max_code and code_size < 12:
                    code_size += 1
                    max_code = 1 << code_size
            else:
                write_code(clear)
                reset_dictionary()
            current = (index,)
        if current:
            write_code(dictionary[current])
        write_code(end)
        if bit_count:
            output.append(bit_buffer & 0xFF)
        return bytes(output)

    def _scan_context(self):
        proc_root = Path("/proc")
        if not proc_root.exists():
            return self._empty_steam_context()

        launch_context = self._scan_steam_launch_context(proc_root)
        if launch_context.get("appId"):
            return launch_context

        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            environ_path = entry / "environ"
            try:
                environ = environ_path.read_bytes().decode("utf-8", errors="ignore")
            except OSError:
                continue

            app_id = ""
            for token in environ.split("\0"):
                if token.startswith("SteamAppId="):
                    app_id = token.split("=", 1)[1]
                    break
                if token.startswith("SteamGameId="):
                    app_id = token.split("=", 1)[1]

            if not app_id or app_id == "0":
                continue

            try:
                title = (entry / "comm").read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                title = ""
            title = self._steam_app_name(app_id) or title
            return self._steam_context(app_id, title, self._process_started_at(entry), "decky-env")

        return self._empty_steam_context()

    def _empty_steam_context(self):
        return {
            "appId": "",
            "title": "",
            "source": "decky",
            "sessionStartedAt": 0,
            "totalPlaytimeMinutes": 0,
            "running": False,
        }

    def _scan_recent_steam_context(self):
        newest = None
        userdata = self._steam_root().joinpath("userdata")
        for localconfig in userdata.glob("*/config/localconfig.vdf"):
            try:
                text = localconfig.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in re.finditer(r'"(\d+)"\s*\{', text):
                app_id = match.group(1)
                block = text[match.end() : match.end() + 1400]
                last_played = re.search(r'"LastPlayed"\s+"(\d+)"', block)
                playtime = re.search(r'"Playtime"\s+"?(\d+)"?', block)
                if not last_played:
                    continue
                candidate = (int(last_played.group(1)), app_id, int(playtime.group(1)) if playtime else 0)
                if newest is None or candidate[0] > newest[0]:
                    newest = candidate
        if newest is None:
            return self._empty_steam_context()
        _, app_id, playtime = newest
        title = self._steam_app_name(app_id) or f"APP {app_id}"
        return {
            "appId": app_id,
            "title": title,
            "source": "steam-recent",
            "sessionStartedAt": 0,
            "totalPlaytimeMinutes": playtime,
            "running": False,
        }

    def _steam_context(self, app_id: str, title: str, session_started_at: float, source: str):
        return {
            "appId": app_id,
            "title": title,
            "source": source,
            "sessionStartedAt": session_started_at,
            "totalPlaytimeMinutes": self._steam_playtime_minutes(app_id),
            "running": True,
        }

    def _scan_steam_launch_context(self, proc_root: Path):
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = entry.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="ignore")
            except OSError:
                continue
            match = re.search(r"SteamLaunch\s+AppId=(\d+)", cmdline)
            if not match:
                continue
            app_id = match.group(1)
            if app_id == "0":
                continue
            title = self._steam_app_name(app_id) or self._title_from_steam_cmdline(cmdline) or "GAME"
            return self._steam_context(app_id, title, self._process_started_at(entry), "steam-launch")
        return self._empty_steam_context()

    def _process_started_at(self, proc_entry: Path) -> float:
        try:
            stat = proc_entry.joinpath("stat").read_text(encoding="utf-8", errors="ignore")
            end = stat.rfind(")")
            fields = stat[end + 2 :].split()
            start_ticks = int(fields[19])
            ticks_per_second = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
            uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
            return time.time() - uptime + (start_ticks / ticks_per_second)
        except Exception:
            return 0

    def _title_from_steam_cmdline(self, cmdline: str) -> str:
        marker = "/steamapps/common/"
        if marker not in cmdline:
            return ""
        tail = cmdline.split(marker, 1)[1]
        title = tail.split("/", 1)[0].strip()
        return title.replace("_", " ")

    def _steam_root(self) -> Path:
        return Path(os.environ.get("STEAM_COMPAT_CLIENT_INSTALL_PATH", "/home/deck/.local/share/Steam"))

    def _steam_app_name(self, app_id: str) -> str:
        if not app_id:
            return ""
        for manifest in self._steam_root().joinpath("steamapps").glob(f"**/appmanifest_{app_id}.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = re.search(r'"name"\s+"([^"]+)"', text)
            if match:
                return match.group(1)
        appinfo = self._steam_root().joinpath("appcache", "appinfo.vdf")
        try:
            data = appinfo.read_bytes()
            needle = app_id.encode("ascii")
            position = data.find(needle)
            if position >= 0:
                chunk = data[max(0, position - 192) : position]
                candidates = re.findall(rb"[ -~]{2,120}\x00", chunk)
                for value in reversed(candidates):
                    title = value[:-1].decode("utf-8", errors="ignore").strip()
                    if title and not title.isdigit() and title.lower() not in {"name", "type", "game", "windows"}:
                        return title
        except OSError:
            pass
        return ""

    def _steam_playtime_minutes(self, app_id: str) -> int:
        if not app_id:
            return 0
        userdata = self._steam_root().joinpath("userdata")
        best = 0
        for localconfig in userdata.glob("*/config/localconfig.vdf"):
            try:
                text = localconfig.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = re.search(rf'"{re.escape(app_id)}"\s*\{{(.*?)\n\s*\}}', text, re.S)
            if not match:
                continue
            playtime = re.search(r'"Playtime"\s+"?(\d+)"?', match.group(1))
            if playtime:
                best = max(best, int(playtime.group(1)))
        return best

    async def _main(self):
        with open("/tmp/jsaux_debug.log", "a") as f:
            f.write("Plugin _main starting...\\n")
        try:
            decky.logger.info("JSAUX PIXEL plugin started")
            asyncio.create_task(self._game_launch_monitor())
            asyncio.create_task(self._suspend_monitor())
            
            # Show a startup steam logo
            steam_logo_b64 = "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xxB60B9pc2lnbmF0dXJleBlzZWxmI2p1bWJmPWMycGEuc2lnbmF0dXJlY2FsZ2ZzaGEyNTYAAAJRanVtYgAAAClqdW1kYzJhcwARABCAAACqADibcQNjMnBhLmFzc2VydGlvbnMAAAAAnGp1bWIAAAAoanVtZGNib3IAEQAQgAAAqgA4m3EDYzJwYS5oYXNoLmRhdGEAAAAAbGNib3KkamV4Y2x1c2lvbnOBomVzdGFydBRmbGVuZ3RoGReKY2FsZ2ZzaGEyNTZkaGFzaFggd3NEj/VpuABUaV0zNwpLuqJ63v1ytszxkIU95XTwLghjcGFkTgAAAAAAAAAAAAAAAAAAAAABhGp1bWIAAAApanVtZGNib3IAEQAQgAAAqgA4m3EDYzJwYS5hY3Rpb25zLnYyAAAAAVNjYm9yoWdhY3Rpb25zgqNmYWN0aW9ubGMycGEuY3JlYXRlZGtkZXNjcmlwdGlvbnggQ3JlYXRlZCBieSBHb29nbGUgR2VuZXJhdGl2ZSBBSS5xZGlnaXRhbFNvdXJjZVR5cGV4Rmh0dHA6Ly9jdi5pcHRjLm9yZy9uZXdzY29kZXMvZGlnaXRhbHNvdXJjZXR5cGUvdHJhaW5lZEFsZ29yaXRobWljTWVkaWGjZmFjdGlvbmtjMnBhLmVkaXRlZGtkZXNjcmlwdGlvbngoQXBwbGllZCBpbXBlcmNlcHRpYmxlIFN5bnRoSUQgd2F0ZXJtYXJrLnFkaWdpdGFsU291cmNlVHlwZXhGaHR0cDovL2N2LmlwdGMub3JnL25ld3Njb2Rlcy9kaWdpdGFsc291cmNldHlwZS90cmFpbmVkQWxnb3JpdGhtaWNNZWRpYf/bAEMAAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAf/bAEMBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAf/AABEIBAAEAAMBIgACEQEDEQH/xAAfAAABBQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJCgv/xAC1EAACAQMDAgQDBQUEBAAAAX0BAgMABBEFEiExQQYTUWEHInEUMoGRoQgjQrHBFVLR8CQzYnKCCQoWFxgZ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060BVHbP1/P6en+eq8Hp7H+ufXn/AD3pN72676JfdZie+9/VWutOurenfVetgHQcYODjtj09eOh70uD3yD7fl+R/zg0Z/X/9dFL59/6+exOm9ur0v6fP53/LUfAK5BBHQ/LjjBGDgd84zjPXg9HkkgDoSAuegyMZ4wCR26Dv1ORTSc9vr6t07/hjn19KGHRsn7vTJwMnnAxxwcDscY5IBB8u3fyd/n92ou/4feiQsCcc8enU8r0565HPfA7HopHzDkdCST2549sdcNnjucZzG5xtUcH0Ax0K9zgAHnHHUDpxl2duSQB06HAxkevHUg8H8j1pLqtG9umui/G720XctR67X9U9HHXzuuuy8uik4JGcdMd8fh15z7/XjAbgE4JGBntj6k8e3OD06U/AyeOwPr36c8c8/iMnnGEIz1AABGOfQjp09OnHPuad1pv06K97Rs1ddbaq+vclxTT395xve7bSUXZaKz6J2ael3rGyNhVzngnHtg/QYHbngcfgWMSSwIwpHTHPUg+4PAyPX9RsHnBAA9cMCSpyTzjkg55HB57EZs85PRck8/xZ9QOec+vpzSv970ejVrNWStp2G2+2rt17bWXR+uv3isuOQPvY4yc8Adu3LY6Z5OelLkFgDkYGOpBPzAYOMZ6nOfyOM0hYk854PQEY52nI59j17jpTshjx1AUn8WX/AAwf8eivrfVve+ulmuv4b+mwt91d3ve9tNF1/pfeI3O3/wBmz3IX39fyph+8RnoAPQ9cZPUcHgdOozz1m5JwR7569GH88gD6c9ajkUZyMfdI+uCpA9eOp9vxpq10np00s1vdd/Tr59QSvprr+lnbVeXdbrVEWBkktjj3HfsfzBxjHI60HbwNwH5dfUn1xnr656inbRgeo5PXggAAA5yMYB59R7U0qo9PoePQfoM/Unr0p6aat26Kzta1/wCrWemu5Tt5u60suvu7dLq2v3XF44AI4x33dsn09OvbpxR36jOen5e/Xjqf5cUBR156YGfbjp0wR9c57UuB2/M898nvk+/v+NS7d7/5vfzf9fNyatbtprbTb79lp69AHGMYOf5devfnp6557mj+Q6fh/n1570d/oMd8f4fl0paRF9b/AJaW9P6/EP8AP+f8/Sik5yPT/PP9On400ZHB5X1PX/8AV357cZzxTXy06Pr/AF637Bbz7f8ABt3t669B2B1H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ACuMbqSPTkHOfvHuRgHn2znoucFuNo6dPlGep2jp15xzzxn6AU/Ay3yqMryvA/jbjoRt545HuCOgxwxBGOp7YBIUenU+nryCcnBpbv8Ad5evXt0v30rpy6KyulZK+y/Dv28mIATjAznOPzC/zPHY49wCbSACeCf/AIocdcc9SeRgj0zUmzIAPOAecDqSDxxgYx2x9OmFIJYDttbP5rRs/n26fP8AVBp01t3XRbddmRlSSD69zxjoTyScjkD1poyD1IPynJPb5TwecHnPGMHpwCKmIyByCQcjPrzj6dccfgKjZgDt4yV5x68Hb+QPU9P1F/w71dkrXfTT1Gvn20votNfNeX5aCsRwMcLyOfXBPPXnA/z0R2XgYOdqnKk8ZI446Y4J9Mc8jhrHBxu3Y5PcjgADPfgDk/l1wnBOcDsB+A7enAyMfX3Jp5+T+7f07LvvsLTz0S2V+173t16bXenm7cPlyR1xkArxuUDgjgHjIyevGOMSE4JyOOh9zxzjOMccenfrUA7g44xgenp/ng9eMYqcLlVBPGB9Tx/nPqecCnfVJ9Fa6vpt23tbTpd31QNpLVK+mjV1dNcr013SbsmnvtsbgSODg9+4646e/Q59fQ0uckAHB9COvr/kGlI4AHGDn6+x9uv098UuAecYxj88Ec+vf6Un5fjvptfp5fImUnZ7WTXLd30VrSa1Sad90lpdtK7EH6dsdh/jnPGfTpQOcj9T7/rgfr70EnjjpjPbIye/t/gO9Ic55HA79+eM57D16dDzwMr+un9fr36mb5vf3SvG3W7926Xk9nfdt2W41xk9ecYIA57flnPXrxjOTURKqCOSQRj0K5xnIHXg+mT0qRyDg8FgD7jqOOcnAzkcA7sZyBSMQTlemMcA44OOp5HpnuMjp0r3bdX8+1r/AH62NVayduz6XV0nytrotbLdakecdQcepOevvnOc+mccUbs9AfY44P8Ak/XPp2o9ScehOePQ/ngD8CPcrwRjOM/geT1xwev50aaXS7dbdNbLd97P1XetO29vNLZvz79fIYSoJyDk4/IY6flkflTsg84PHIz156YHfp7DvS44weev6/559ee3FAyc549Pcevrn/OM5obVrb2639Fon37a280htrS17pLW+nTp+a9d+iEk4IHHX69PQ+meoPpg8U4HsffocfTP1x2A79cUmOScnnt2H+f89aWk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ucd8f5yfp34pu8Duffgjvjn8f8OtFuy23YmpNysr3s4q/azd9LKPR30abTtdAWIOMZGM5x3zg844wvoeNwzSMSCTkggd+MAEc+v8JHYY69sq3HXptI9OuOh9+o9+c+jZCcjJzxnP+1nPfnPHX25OOp939ev9diursvWyeui11Wvm9VpvpZMbjcScgg7cN3GDzwMZJIwp6kZBAyQDcrDjcfoueTn6bjjB64yACaRyRg44XGASNp3Hg5HHbGD6ds5MvJ55G7vngenpyR9SOPWhfnt63XT08gcuVX1tdO2ze1r9Vo+2++q0iKkZzyuM9+nGcnju2OD6Y6HClCpIOP6eh69eck/Xk8VIQOfx4yQM5Vjng+mfY8j2GHI6dsc8g5ByBg56D6enOQa/8OraaNPfrb56asHJK9ujjutFdRem999XbXbu3GRyeMdcfT2OcHPHf8qMHB45Azz3ycD8jjj/APVUx4x19+QB1HYjsCcdzyByaQ4yAev09CD7Dt6fl3Qua1/KSSeq1fLe3ydvu9CMjaxBGecg8jHXsMEfN05x9F6BwQxAwTnbjGMZOTgknA+X34POMkukxgZ9zkkD09ff359O9I55H+0o/nn+Q/z0o/r+v6f6jv6f1r+X4CHvgEEH35U5HGR83OBxz2+qNwc+uT6dSf5/1pX6k9+QTz7sPbo2emT37UjAlSRyQSvK9AGbJBHXPT5ckDA4waajd+V1d99uve3TfQaTeibS262vpddte3yuBC52jgg4PBA4OAewOcfeX074xTiMsQc5CkAc/wB4L39gAevocUxl5JPU85546+h9znnn3wKcT8xwTxnJwRj5jxjr149N2Rng4d+t3f5PXTd9VbyXbXUL2s1fzv5W031X3dAYlScHJ9MDqD3HHQDrx14wSCBic4PoQPTqD6npxzzjp0xTSc84xycDHqf5f0ApScknnntn26fz68c4zik3f7l/X37eWm2gN3/r0+XTts+odSxIC9x78n/HGMYGPqaTJPUeg49vXIGerEcdDx7n15/w/wA4FIDnjngnscf54wT6j6Un/lt93/Di/r7iRWJ6g84APA/u5HH8XzH3UcZp/THPHTnHXsBj+XHGPcVBk/8AAeo/znr746VIGBIJPuc8ZJIzx3wBnnnI7c0PT8PxVwfp226fe/vJP888f564/wAaTAzk9cjn6Y9+ueOg9Mer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"
            settings = await self.get_display_settings()
            settings = await self.get_display_settings()
            if isinstance(settings.get("displaySettings"), dict):
                settings = settings["displaySettings"]
            active_module = str(settings.get("activeModuleId") or settings.get("active_module_id") or "")
            custom_settings = settings.get("custom", {})
            current_asset = ""
            if isinstance(custom_settings, dict):
                current_asset = str(custom_settings.get("current_asset") or custom_settings.get("currentAsset") or "")

            res = await self.upload_custom_asset("startup_steam_logo.jpg", steam_logo_b64)
            if res.get("ok"):
                custom = res.get("custom", {})
                uploaded_filename = custom.get("current_asset") or custom.get("currentAsset") or "startup_steam_logo.jpg"
                self._display_locked_until = time.time() + 4.5
                await self._activate_builtin_custom_asset(uploaded_filename)
                await asyncio.sleep(4)
                await self.delete_custom_asset(uploaded_filename)
                
            if active_module:
                if active_module.lower() == "playtime":
                    await self.send_playtime_monitor()
                    decky.logger.info("Playtime monitor resumed on plugin start")
                elif active_module.lower() == "custom" and self._is_chronograph_asset(current_asset):
                    await self.send_chronograph_monitor(CHRONOGRAPH_MAX_SECONDS)
                    decky.logger.info("Chronograph monitor resumed on plugin start")
                elif active_module.lower() == "custom":
                    if current_asset:
                        await self._activate_builtin_custom_asset(current_asset)
                elif active_module.lower() == "dashboard":
                    await self.send_dashboard_monitor()
                elif active_module.lower() == "system_performance":
                    await self.send_hardware_monitor()
                elif active_module.lower() == "productivity":
                    await self.send_clock_monitor()
                elif active_module.lower() == "weather":
                    await self.send_weather_forecast()
                else:
                    await self._activate_daemon_module(active_module)
                    await self._activate_daemon_module(active_module)
            
            with open("/tmp/jsaux_debug.log", "a") as f:
                f.write("Plugin _main finished successfully\\n")
        except Exception as exc:
            with open("/tmp/jsaux_debug.log", "a") as f:
                f.write(f"Plugin _main crashed: {exc}\\n")
            decky.logger.info(f"Active monitor resume skipped: {exc}")

    async def _unload(self):
        self._chronograph_watchdog_generation += 1
        self._stop_legacy_monitor_loops()
        decky.logger.info("JSAUX PIXEL plugin stopped")

    async def _uninstall(self):
        self._remove_companion_desktop_install()
        decky.logger.info("JSAUX PIXEL plugin uninstalled")

    def _remove_companion_desktop_install(self):
        home = Path.home()
        service = home / ".config/systemd/user/jsaux-matrix-hub.service"
        systemctl_env = os.environ.copy()
        systemctl_env.update(
            {
                "HOME": str(home),
                "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
            }
        )
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "jsaux-matrix-hub.service"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=systemctl_env,
        )
        for path in (
            home / ".local/bin/jsaux-matrix-studio",
            home / ".local/bin/jsaux-matrix-daemon",
            home / ".local/bin/jsaux-matrixlink-uninstall",
            home / ".local/share/applications/jsaux-matrixlink.desktop",
            home / ".local/share/applications/jsaux-matrixlink-uninstall.desktop",
            home / "Desktop/JSAUX PIXEL.desktop",
            home / "Desktop/JSAUX MatrixLink.desktop",
            home / ".local/share/icons/hicolor/scalable/apps/jsaux-matrixlink.svg",
            service,
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                decky.logger.info(f"Companion desktop cleanup skipped for {path}: {exc}")
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=systemctl_env,
        )

    async def _migration(self):
        decky.logger.info("JSAUX PIXEL plugin migration")

    async def _suspend_monitor(self):
        try:
            process = await asyncio.create_subprocess_exec(
                "stdbuf", "-oL", "dbus-monitor", "--system", "type='signal',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'",
                stdout=asyncio.subprocess.PIPE
            )
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode('utf-8').strip()
                if "boolean true" in line_str:
                    decky.logger.info("System suspending - turning off display")
                    await self.set_power(False)
                elif "boolean false" in line_str:
                    decky.logger.info("System resuming - restoring display")
                    await self.set_power(True)
        except Exception as e:
            decky.logger.error(f"Suspend monitor crashed: {e}")

    async def _game_launch_monitor(self):
        await asyncio.sleep(5)
        try:
            last_app_id = self._scan_context().get("appId", "")
        except:
            last_app_id = ""
            
        while True:
            await asyncio.sleep(2)
            try:
                context = self._scan_context()
                current_app_id = context.get("appId", "")
                if current_app_id and current_app_id != last_app_id:
                    with open("/tmp/jsaux_debug.log", "a") as f:
                        f.write(f"Game launch monitor detected change: {last_app_id} -> {current_app_id}\n")
                    decky.logger.info(f"Game launched: {current_app_id}")
                    if getattr(self, '_current_artwork_task', None):
                        self._current_artwork_task.cancel()
                    self._current_artwork_task = asyncio.create_task(self._show_game_artwork(current_app_id))
                last_app_id = current_app_id
            except Exception as exc:
                decky.logger.error(f"Game launch monitor error: {exc}")

    async def _show_game_artwork(self, app_id: str):
        with open("/tmp/jsaux_debug.log", "a") as f:
            f.write(f"Entering _show_game_artwork for {app_id}\n")
        try:
            steam_root = self._steam_root()
            librarycache = steam_root / "appcache" / "librarycache"
            
            candidates = [
                librarycache / f"{app_id}_header.jpg",
                librarycache / f"{app_id}_library_hero.jpg",
                librarycache / f"{app_id}_logo.png",
                librarycache / str(app_id) / "header.jpg",
                librarycache / str(app_id) / "library_hero.jpg",
                librarycache / str(app_id) / "logo.png",
            ]
            
            art_path = None
            for p in candidates:
                if p.exists():
                    art_path = p
                    break
            
            if not art_path:
                for grid_dir in (steam_root / "userdata").glob("*/config/grid"):
                    for ext in [".jpg", ".png"]:
                        for suffix in ["p", "hero", ""]:
                            p = grid_dir / f"{app_id}{suffix}{ext}"
                            if p.exists():
                                art_path = p
                                break
                        if art_path:
                            break
                    if art_path:
                        break
                    
            if not art_path:
                with open("/tmp/jsaux_debug.log", "a") as f:
                    f.write(f"No artwork found for {app_id}\n")
                decky.logger.info(f"No artwork found for {app_id}")
                return
                
            with open("/tmp/jsaux_debug.log", "a") as f:
                f.write(f"Found artwork: {art_path.name} (size: {art_path.stat().st_size} bytes)\n")
                
            settings = await self.get_display_settings()
            if isinstance(settings.get("displaySettings"), dict):
                settings = settings["displaySettings"]
            
            active_module = str(settings.get("activeModuleId") or settings.get("active_module_id") or "")
            custom = res.get("custom", {})
            uploaded_filename = custom.get("current_asset") or custom.get("currentAsset") or art_path.name
            with open("/tmp/jsaux_debug.log", "a") as f:
                f.write(f"Syncing custom display for asset {uploaded_filename}...\\n")
                
            if not getattr(self, '_is_in_custom_artwork_mode', False):
                self._original_active_module = active_module
                self._is_in_custom_artwork_mode = True
                
            self._display_locked_until = time.time() + 12.5
            await self._activate_builtin_custom_asset(uploaded_filename)
            
            await asyncio.sleep(12)
            
            if getattr(self, '_current_artwork_task', None) != asyncio.current_task():
                with open("/tmp/jsaux_debug.log", "a") as f:
                    f.write("A newer artwork task is running, skipping restore.\\n")
                return
                
            restore_module = getattr(self, '_original_active_module', active_module)
            self._is_in_custom_artwork_mode = False
            
            with open("/tmp/jsaux_debug.log", "a") as f:
                f.write(f"Restoring active module: {restore_module}\\n")
                
            if restore_module:
                if restore_module.lower() == "custom":
                    custom_settings = settings.get("custom", {})
                    prev_asset = str(custom_settings.get("current_asset") or custom_settings.get("currentAsset") or "")
                    if prev_asset:
                        await self._activate_builtin_custom_asset(prev_asset)
                elif restore_module.lower() == "playtime":
                    await self.send_playtime_monitor()
                elif restore_module.lower() == "dashboard":
                    await self.send_dashboard_monitor()
                elif restore_module.lower() == "system_performance":
                    await self.send_hardware_monitor()
                elif restore_module.lower() == "productivity":
                    await self.send_clock_monitor()
                elif restore_module.lower() == "weather":
                    await self.send_weather_forecast()
                else:
                    await self._activate_daemon_module(restore_module)
                        
            await self.delete_custom_asset(uploaded_filename)
            with open("/tmp/jsaux_debug.log", "a") as f:
                f.write(f"Cleanup done\\n")
                
        except asyncio.CancelledError:
            with open("/tmp/jsaux_debug.log", "a") as f:
                f.write("Artwork display cancelled by a newer launch event\\n")
            return
        except Exception as exc:
            self._is_in_custom_artwork_mode = False
            with open("/tmp/jsaux_debug.log", "a") as f:
                f.write(f"Exception in _show_game_artwork: {exc}\\n")
            decky.logger.error(f"Show artwork failed: {exc}")

