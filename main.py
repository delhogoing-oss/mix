#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniPix Telegram Bot  ---  Multi-User + Multi-Account Parallel Edition (v2)

What changed vs v1
------------------
1. MULTI-ACCOUNT  : Ek Telegram user ab kai MiniPix accounts add kar sakta hai
                    aur sab ek saath PARALLEL chalte hain (real threads).
2. MULTI-USER     : Bahut saare Telegram users ek saath bot use kar sakte hain.
                    Ab koi blocking call event-loop ko freeze nahi karti.

Bug fixes
---------
*  watch_episode() ka return-tuple mismatch (2 vs 4 values) -> crash. Fixed.
*  STATS.bump(user, "last_active") -> None + 1 TypeError on /start. Fixed.
*  /set_token hamesha fail hota tha (user_id None -> get_user() False). Fixed
   with JWT claim decoding + /users/me fallbacks.
*  updater.start_polling(application.bot) -> invalid arg (poll_interval=Bot).
*  webhook branch me application.initialize() missing + dead `if False` line.
*  Groq call MiniPix bearer token leak kar raha tha. Ab isolated session.
*  JSON files ka concurrent write -> corruption. Ab lock + atomic replace.
*  Markdown injection: labels/titles ke `_ * [ ` se message crash. Ab escaped.
*  Telegram flood-control: progress edits ab throttled + RetryAfter safe.
*  quizzes_solved har log-line par bump ho raha tha. Ab per-session.
*  Infinite `while any_progress` loop ke liye safety cap.
*  QUIZ SOLVING: error handling improved, session/claim failures now set state,
   model default changed to openai/gpt-oss-120b (override with GROQ_MODEL).
*  STARTUP FIX: config = BotConfig() instead of BotConfig.load() (TypeError).
"""

import asyncio
import base64
import functools
import io
import json
import os
import random
import string
import sys
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
    RETRY_AVAILABLE = True
except Exception:
    RETRY_AVAILABLE = False


# --------------------------------------------------------------------------
# Windows console encoding
# --------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# Optional deps
# --------------------------------------------------------------------------
try:
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = None
    ParseMode = None
    Application = None
    ApplicationBuilder = None
    CommandHandler = None
    ContextTypes = None
    MessageHandler = None
    filters = None

    class _TelegramMissing(Exception):
        """Placeholder so except-clauses stay valid without the library."""

    BadRequest = Forbidden = NetworkError = RetryAfter = TimedOut = _TelegramMissing
    print("[!] python-telegram-bot install nahi mila. Install karo:")
    print("    pip install 'python-telegram-bot[rate-limiter]==21.*' requests")

try:
    from telegram.ext import AIORateLimiter
    RATE_LIMITER_AVAILABLE = True
except Exception:
    RATE_LIMITER_AVAILABLE = False
    AIORateLimiter = None

try:
    from flask import Flask, jsonify
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    Flask = None
    jsonify = None
    print("[i] Flask nahi mila --- keep-alive server disable rahega. pip install flask")


# --------------------------------------------------------------------------
# Constants / tunables
# --------------------------------------------------------------------------
API_BASE = "https://api.minipix.co/v4"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "").strip() or BASE_DIR

try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR

CONFIG_FILE = os.path.join(DATA_DIR, "bot_config.json")
USERS_FILE = os.path.join(DATA_DIR, "users_data.json")
STATS_FILE = os.path.join(DATA_DIR, "usage_stats.json")

MAX_WATCHES_PER_EP = 4
REWARDS_BY_WATCH = {1: 15, 2: 8, 3: 5, 4: 3}


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except Exception:
        return default


def _env_float(name, default):
    try:
        return float(str(os.environ.get(name, "")).strip() or default)
    except Exception:
        return default


# How many MiniPix accounts a single Telegram user may register.
MAX_ACCOUNTS_PER_USER = _env_int("MAX_ACCOUNTS_PER_USER", 10)
# Global worker threads shared by every user (this is the real concurrency cap).
MAX_WORKERS = _env_int("MAX_WORKERS", 32)
# How many of ONE user's accounts may run at the same instant (fairness).
PER_USER_PARALLEL = _env_int("PER_USER_PARALLEL", 6)
# Seconds between Telegram progress-message edits.
PROGRESS_INTERVAL = _env_float("PROGRESS_INTERVAL", 4.0)
# Catalog (series/episodes) cache TTL, shared across all accounts.
CATALOG_TTL = _env_int("CATALOG_TTL", 900)
# Hard safety cap so a watch loop can never spin forever.
MAX_WATCH_ROUNDS = _env_int("MAX_WATCH_ROUNDS", MAX_WATCHES_PER_EP + 2)

ADMIN_IDS = set()
for _chunk in str(os.environ.get("ADMIN_IDS", "")).replace(" ", "").split(","):
    if _chunk.strip().isdigit():
        ADMIN_IDS.add(int(_chunk.strip()))

HEADERS_BASE = {
    "user-agent": "okhttp/4.12.0",
    "accept-encoding": "gzip",
}

EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="minipix")


def _expected_reward(nth_watch):
    try:
        return REWARDS_BY_WATCH.get(int(nth_watch or 1), 0)
    except Exception:
        return 0


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
_MD_SPECIALS = ("\\", "_", "*", "[", "]", "`")


def md(text):
    """Escape legacy-Markdown specials so user data can never break a message."""
    s = "" if text is None else str(text)
    for ch in _MD_SPECIALS:
        s = s.replace(ch, "\\" + ch)
    return s


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def gen_device_id():
    return "".join(random.choice("0123456789abcdef") for _ in range(16))


def mask(value, keep=4):
    s = str(value or "")
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def fmt_duration(seconds):
    try:
        seconds = int(max(0, seconds))
    except Exception:
        return "0s"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def atomic_write_json(path, obj):
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[!] Write error {os.path.basename(path)}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[!] Read error {os.path.basename(path)}: {e}")
        return default


def decode_jwt_payload(token):
    """Best-effort JWT payload decode (no signature check --- only for claims)."""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def jwt_is_expired(token):
    claims = decode_jwt_payload(token)
    exp = claims.get("exp")
    try:
        return bool(exp) and float(exp) < time.time()
    except Exception:
        return False


def normalize_phone(raw):
    s = str(raw or "").strip().replace(" ", "").replace("-", "")
    if s.startswith("+"):
        return s
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 10:
        return "+91" + digits[-10:]
    return None


# --------------------------------------------------------------------------
# Bot config
# --------------------------------------------------------------------------
class BotConfig:
    def __init__(self):
        self.bot_token = ""
        self.log_channel_id = ""
        self.port = _env_int("PORT", 8080)
        self.webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
        self.load()

    def load(self):
        env_token = os.environ.get("BOT_TOKEN", "").strip()
        env_log = os.environ.get("LOG_CHANNEL_ID", "").strip()
        if env_token:
            self.bot_token = env_token
            self.log_channel_id = env_log
            if os.environ.get("RAILWAY_ENVIRONMENT_NAME") or os.environ.get("RAILWAY_STATIC_URL"):
                print("[OK] Railway environment detected --- using ENV vars for config.")
                return

        disk = read_json(CONFIG_FILE, {})
        if isinstance(disk, dict):
            if not self.bot_token:
                self.bot_token = str(disk.get("bot_token") or "")
            if not self.log_channel_id:
                self.log_channel_id = str(disk.get("log_channel_id") or "")

        if self.bot_token:
            return

        print(f"\n[!] Bot config nahi mila: {CONFIG_FILE}")
        print("    ENV set karo:  BOT_TOKEN=xyz LOG_CHANNEL_ID=-100123 python minipix_bot_v2.py")

        # Only prompt on a real terminal --- never block a container boot.
        if not sys.stdin or not sys.stdin.isatty():
            return
        try:
            self.bot_token = input("\n    Bot token daalo (ya empty chhod do): ").strip()
            self.log_channel_id = input("    Log channel ID daalo (ya empty): ").strip()
            if self.bot_token:
                self.save()
        except Exception:
            pass

    def save(self):
        atomic_write_json(CONFIG_FILE, {
            "bot_token": self.bot_token,
            "log_channel_id": self.log_channel_id,
        })


BOT_CONFIG = BotConfig()


# --------------------------------------------------------------------------
# Usage stats (thread-safe, debounced writes)
# --------------------------------------------------------------------------
class UserStats:
    _BLANK = {
        "total_watches": 0,
        "total_coins_earned": 0,
        "quizzes_solved": 0,
        "series_done": 0,
        "accounts": 0,
        "last_active": None,
        "joined_at": None,
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._dirty = False
        self._last_save = 0.0
        self.data = {}
        self.load()

    def load(self):
        raw = read_json(STATS_FILE, {})
        with self._lock:
            self.data = {}
            if isinstance(raw, dict):
                for uid, rec in raw.items():
                    if not isinstance(rec, dict):
                        continue
                    merged = dict(self._BLANK)
                    merged.update(rec)
                    self.data[str(uid)] = merged

    def ensure(self, user_id):
        uid = str(user_id)
        with self._lock:
            rec = self.data.get(uid)
            if rec is None:
                rec = dict(self._BLANK)
                rec["joined_at"] = now_iso()
                self.data[uid] = rec
                self._dirty = True
            return rec

    def bump(self, user_id, key, amount=1):
        """Increment a numeric counter. Non-numeric existing values are reset."""
        with self._lock:
            rec = self.ensure(user_id)
            current = rec.get(key)
            if not isinstance(current, (int, float)) or isinstance(current, bool):
                current = 0
            rec[key] = current + amount
            rec["last_active"] = now_iso()
            self._dirty = True
        self.maybe_save()

    def touch(self, user_id):
        with self._lock:
            rec = self.ensure(user_id)
            rec["last_active"] = now_iso()
            rec["joined_at"] = rec.get("joined_at") or now_iso()
            self._dirty = True
        self.maybe_save()

    def set_value(self, user_id, key, value):
        with self._lock:
            self.ensure(user_id)[key] = value
            self._dirty = True
        self.maybe_save()

    def get(self, user_id):
        with self._lock:
            return dict(self.data.get(str(user_id)) or self._BLANK)

    def snapshot(self):
        with self._lock:
            return {k: dict(v) for k, v in self.data.items()}

    def maybe_save(self, min_interval=2.0):
        with self._lock:
            if not self._dirty:
                return
            if (time.time() - self._last_save) < min_interval:
                return
        self.save()

    def save(self):
        with self._lock:
            payload = {k: dict(v) for k, v in self.data.items()}
            self._dirty = False
            self._last_save = time.time()
        atomic_write_json(STATS_FILE, payload)


STATS = UserStats()


# --------------------------------------------------------------------------
# User + multi-account store (thread-safe, schema v2 with v1 migration)
# --------------------------------------------------------------------------
class UserStore:
    SCHEMA = 2

    def __init__(self):
        self._lock = threading.RLock()
        self.users = {}
        self.load()

    # ---- persistence -----------------------------------------------------
    @staticmethod
    def blank_user():
        return {
            "groq_api_key": None,
            "accounts": {},
            "counter": 0,
            "active": None,
            "pending_otp": None,
            "created_at": now_iso(),
        }

    @staticmethod
    def blank_account(acc_id, label):
        return {
            "id": acc_id,
            "label": label,
            "token": None,
            "user_id": None,
            "profile_id": None,
            "phone": None,
            "device_id": gen_device_id(),
            "enabled": True,
            "added_at": now_iso(),
            "last_run": None,
            "watches": 0,
            "coins": 0,
            "quizzes": 0,
            "last_error": None,
        }

    def load(self):
        raw = read_json(USERS_FILE, {})
        with self._lock:
            if isinstance(raw, dict) and int(raw.get("_schema") or 0) >= 2:
                self.users = raw.get("users") or {}
                self._heal()
                return
            self.users = self._migrate_v1(raw if isinstance(raw, dict) else {})
            self._heal()
        print(f"[OK] User store loaded: {len(self.users)} users, {self.total_accounts()} accounts")
        self.save()

    def _migrate_v1(self, raw):
        out = {}
        migrated = 0
        for uid, old in raw.items():
            if str(uid).startswith("_") or not isinstance(old, dict):
                continue
            rec = self.blank_user()
            rec["groq_api_key"] = old.get("groq_api_key")
            if old.get("minipix_token"):
                acc = self.blank_account("a1", "Account 1")
                acc["token"] = old.get("minipix_token")
                acc["user_id"] = old.get("minipix_user_id")
                acc["profile_id"] = old.get("minipix_profile_id")
                acc["phone"] = old.get("minipix_phone")
                acc["device_id"] = "65969f0b7041fabc"  # keep the original device id
                rec["accounts"]["a1"] = acc
                rec["counter"] = 1
                rec["active"] = "a1"
                migrated += 1
            out[str(uid)] = rec
        if migrated:
            print(f"[OK] Migrated {migrated} single-account user(s) to multi-account schema.")
        return out

    def _heal(self):
        """Repair partially-written or hand-edited records."""
        for uid, rec in list(self.users.items()):
            if not isinstance(rec, dict):
                self.users[uid] = self.blank_user()
                continue
            base = self.blank_user()
            base.update(rec)
            accounts = base.get("accounts")
            if not isinstance(accounts, dict):
                accounts = {}
            fixed = {}
            for aid, acc in accounts.items():
                if not isinstance(acc, dict):
                    continue
                merged = self.blank_account(str(aid), f"Account {len(fixed) + 1}")
                merged.update(acc)
                merged["id"] = str(aid)
                if not merged.get("device_id"):
                    merged["device_id"] = gen_device_id()
                fixed[str(aid)] = merged
            base["accounts"] = fixed
            try:
                base["counter"] = max(int(base.get("counter") or 0), len(fixed))
            except Exception:
                base["counter"] = len(fixed)
            if base.get("active") not in fixed:
                base["active"] = next(iter(fixed), None)
            self.users[uid] = base

    def save(self):
        with self._lock:
            payload = {
                "_schema": self.SCHEMA,
                "_saved_at": now_iso(),
                "users": json.loads(json.dumps(self.users, default=str)),
            }
        atomic_write_json(USERS_FILE, payload)

    # ---- user level ------------------------------------------------------
    def user(self, user_id):
        uid = str(user_id)
        with self._lock:
            rec = self.users.get(uid)
            if rec is None:
                rec = self.blank_user()
                self.users[uid] = rec
                self.save()
            return rec

    def get_groq(self, user_id):
        with self._lock:
            return self.user(user_id).get("groq_api_key")

    def set_groq(self, user_id, key):
        with self._lock:
            self.user(user_id)["groq_api_key"] = key
        self.save()

    def set_pending_otp(self, user_id, session_token, phone, label=None, account_id=None):
        with self._lock:
            self.user(user_id)["pending_otp"] = {
                "session": session_token,
                "phone": phone,
                "label": label,
                "account_id": account_id,
                "at": now_iso(),
            }
        self.save()

    def get_pending_otp(self, user_id):
        with self._lock:
            pending = self.user(user_id).get("pending_otp")
            return dict(pending) if isinstance(pending, dict) else None

    def clear_pending_otp(self, user_id):
        with self._lock:
            self.user(user_id)["pending_otp"] = None
        self.save()

    # ---- account level ---------------------------------------------------
    def list_accounts(self, user_id):
        with self._lock:
            accounts = self.user(user_id).get("accounts") or {}
            ordered = sorted(accounts.values(), key=lambda a: str(a.get("added_at") or ""))
            return [dict(a) for a in ordered]

    def enabled_accounts(self, user_id):
        return [a for a in self.list_accounts(user_id) if a.get("enabled") and a.get("token")]

    def snapshot(self):
        with self._lock:
            return {uid: dict(rec) for uid, rec in self.users.items()}

    def user_count(self):
        with self._lock:
            return len(self.users)

    def total_accounts(self):
        with self._lock:
            return sum(len(u.get("accounts") or {}) for u in self.users.values())

    def add_account(self, user_id, token, minipix_user_id=None, profile_id=None,
                    phone=None, label=None, device_id=None):
        with self._lock:
            rec = self.user(user_id)
            accounts = rec["accounts"]

            # Same MiniPix identity already present? -> update instead of duplicate.
            for acc in accounts.values():
                same_user = minipix_user_id and acc.get("user_id") == minipix_user_id
                same_phone = phone and acc.get("phone") == phone
                if same_user or same_phone:
                    acc["token"] = token
                    acc["user_id"] = minipix_user_id or acc.get("user_id")
                    acc["profile_id"] = profile_id or acc.get("profile_id")
                    acc["phone"] = phone or acc.get("phone")
                    acc["last_error"] = None
                    acc["enabled"] = True
                    if label:
                        acc["label"] = label
                    self.save()
                    return dict(acc), False

            if len(accounts) >= MAX_ACCOUNTS_PER_USER:
                return None, False

            rec["counter"] = int(rec.get("counter") or 0) + 1
            acc_id = f"a{rec['counter']}"
            while acc_id in accounts:
                rec["counter"] += 1
                acc_id = f"a{rec['counter']}"

            acc = self.blank_account(acc_id, label or f"Account {len(accounts) + 1}")
            acc["token"] = token
            acc["user_id"] = minipix_user_id
            acc["profile_id"] = profile_id
            acc["phone"] = phone
            if device_id:
                acc["device_id"] = device_id
            accounts[acc_id] = acc
            if not rec.get("active"):
                rec["active"] = acc_id
            self.save()
            STATS.set_value(user_id, "accounts", len(accounts))
            return dict(acc), True

    def update_account(self, user_id, acc_id, **fields):
        with self._lock:
            acc = (self.user(user_id).get("accounts") or {}).get(str(acc_id))
            if not acc:
                return None
            acc.update({k: v for k, v in fields.items() if k not in ("id",)})
            result = dict(acc)
        self.save()
        return result

    def bump_account(self, user_id, acc_id, **deltas):
        with self._lock:
            acc = (self.user(user_id).get("accounts") or {}).get(str(acc_id))
            if not acc:
                return
            for key, delta in deltas.items():
                current = acc.get(key)
                if not isinstance(current, (int, float)) or isinstance(current, bool):
                    current = 0
                acc[key] = current + delta

    def remove_account(self, user_id, acc_id):
        with self._lock:
            rec = self.user(user_id)
            acc = rec["accounts"].pop(str(acc_id), None)
            if acc and rec.get("active") == str(acc_id):
                rec["active"] = next(iter(rec["accounts"]), None)
            if acc:
                STATS.set_value(user_id, "accounts", len(rec["accounts"]))
        self.save()
        return acc

    def set_active(self, user_id, acc_id):
        with self._lock:
            rec = self.user(user_id)
            if str(acc_id) in rec["accounts"]:
                rec["active"] = str(acc_id)
                self.save()
                return True
        return False

    def resolve(self, user_id, ref):
        """Resolve an account by id (a2), 1-based index (2), or label (case-insensitive)."""
        if ref is None:
            return None
        ref = str(ref).strip()
        if not ref:
            return None
        accounts = self.list_accounts(user_id)
        low = ref.lower()
        for acc in accounts:
            if str(acc.get("id", "")).lower() == low:
                return acc
        if ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(accounts):
                return accounts[idx]
        for acc in accounts:
            if str(acc.get("label", "")).strip().lower() == low:
                return acc
        for acc in accounts:
            if low and low in str(acc.get("label", "")).strip().lower():
                return acc
        return None

    def resolve_many(self, user_id, refs):
        picked = OrderedDict()
        for ref in refs:
            acc = self.resolve(user_id, ref)
            if acc:
                picked[acc["id"]] = acc
        return list(picked.values())


STORE = UserStore()


# --------------------------------------------------------------------------
# Shared catalog cache
# Series + episode lists are the same for every account, so we fetch them once
# and share across all parallel workers. This is what makes N accounts cheap.
# --------------------------------------------------------------------------
class CatalogCache:
    def __init__(self, ttl=CATALOG_TTL):
        self.ttl = ttl
        self._guard = threading.Lock()
        self._key_locks = {}
        self._store = {}

    def _lock_for(self, key):
        with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def get_or_fetch(self, key, fetcher):
        with self._guard:
            entry = self._store.get(key)
            if entry and (time.time() - entry[0]) < self.ttl:
                return entry[1]

        # Only one thread fetches a given key; the rest reuse the result.
        with self._lock_for(key):
            with self._guard:
                entry = self._store.get(key)
                if entry and (time.time() - entry[0]) < self.ttl:
                    return entry[1]
            try:
                value = fetcher()
            except Exception:
                value = None
            if value:
                with self._guard:
                    self._store[key] = (time.time(), value)
            return value

    def series_list(self, session, max_pages=8):
        return self.get_or_fetch(
            f"series:{max_pages}",
            lambda: session.get_all_series(max_pages=max_pages),
        ) or []

    def episodes(self, session, series_id, page_size=300):
        key = f"eps:{series_id}:{page_size}"
        result = self.get_or_fetch(
            key,
            lambda: (session.get_episodes(series_id, page=1, page_size=page_size)[0] or None),
        )
        return result or []

    def stats(self):
        with self._guard:
            return {"keys": len(self._store), "ttl": self.ttl}

    def clear(self):
        with self._guard:
            self._store.clear()


CATALOG = CatalogCache()


# --------------------------------------------------------------------------
# MiniPix session --- one instance per ACCOUNT (never shared between threads)
# --------------------------------------------------------------------------
def build_http_session():
    s = requests.Session()
    s.headers.update(HEADERS_BASE)
    if RETRY_AVAILABLE:
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PATCH", "PUT"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retry)
    else:
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=2)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# Separate pooled session for Groq so the MiniPix bearer never leaks out.
GROQ_HTTP = requests.Session()
GROQ_HTTP.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=32, max_retries=1))


class MiniPixUserSession:
    def __init__(self, account=None, groq_api_key=None):
        account = dict(account or {})
        self.account_id = account.get("id")
        self.label = account.get("label") or "Account"
        self.access_token = account.get("token")
        self.user_id = account.get("user_id")
        self.profile_id = account.get("profile_id")
        self.phone = account.get("phone")
        self.device_id = account.get("device_id") or gen_device_id()
        self.device_info = account.get("device_info") or "Xiaomi"

        self.session = build_http_session()
        self.watch_history = {}
        self.watch_history_raw = []
        self.runtime_watch_counts = {}
        self.groq_api_key = groq_api_key
        self.groq_cache = {}
        self.quiz_qbank = {}
        self.auth_failed = False
        self.last_status = None

        if self.access_token:
            self.session.headers["authorization"] = f"Bearer {self.access_token}"

    # ---- persistence helpers --------------------------------------------
    def account_fields(self):
        return {
            "token": self.access_token,
            "user_id": self.user_id,
            "profile_id": self.profile_id,
            "phone": self.phone,
            "device_id": self.device_id,
        }

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    # ---- transport -------------------------------------------------------
    def _req(self, method, path, **kwargs):
        url = f"{API_BASE}{path}"
        kwargs.setdefault("timeout", 30)
        try:
            r = self.session.request(method, url, **kwargs)
            self.last_status = r.status_code
            if r.status_code in (401, 403):
                self.auth_failed = True
            try:
                data = r.json()
            except Exception:
                data = r.text
            return r.status_code, data
        except Exception:
            self.last_status = 0
            return 0, None

    def _post_json(self, path, body, method="POST"):
        return self._req(
            method, path,
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )

    # ---- auth ------------------------------------------------------------
    def login_otp_generate(self, phone):
        self.phone = phone
        sc, data = self._post_json("/login/generate-otp", {"phone_number": phone})
        if sc == 200 and isinstance(data, dict):
            if data.get("session_token"):
                return data.get("session_token")
            if str(data.get("message", "")).lower().startswith("otp"):
                return data.get("session_token")
        return None

    def login_otp_verify(self, session_token, otp):
        payload = {
            "client_id": "android",
            "device_id": self.device_id,
            "device_info": self.device_info,
            "otp": otp,
            "phone_number": self.phone,
            "session_token": session_token,
        }
        sc, data = self._post_json("/login/verify-otp", payload)
        if sc == 200 and isinstance(data, dict) and data.get("access_token"):
            self.access_token = data["access_token"]
            self.auth_failed = False
            self.user_id = data.get("id") or data.get("_id") or data.get("user_id")
            self.session.headers["authorization"] = f"Bearer {self.access_token}"
            if not self.user_id:
                claims = decode_jwt_payload(self.access_token)
                self.user_id = (claims.get("_id") or claims.get("id")
                                or claims.get("sub") or claims.get("userId"))
            self.get_user()
            return True
        return False

    def login_with_token(self, token, user_id=None, profile_id=None):
        """Token login. v1 always failed here because user_id stayed None."""
        token = str(token or "").strip()
        if not token:
            return False, "empty token"
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        self.access_token = token
        self.auth_failed = False
        self.session.headers["authorization"] = f"Bearer {token}"

        if jwt_is_expired(token):
            return False, "token expired"

        claims = decode_jwt_payload(token)
        self.user_id = (user_id or claims.get("_id") or claims.get("id")
                        or claims.get("sub") or claims.get("userId")
                        or claims.get("user_id"))
        self.profile_id = (profile_id or claims.get("master_profile")
                           or claims.get("profile_id") or claims.get("profileId"))

        if self.user_id and self.get_user():
            return True, "ok"

        # Fallback: ask the API who this token belongs to.
        for path in ("/users/me", "/me", "/users/profile", "/profile"):
            sc, data = self._req("GET", path)
            if sc != 200 or not isinstance(data, dict):
                continue
            node = data.get("user") if isinstance(data.get("user"), dict) else data
            uid = node.get("_id") or node.get("id") or node.get("user_id")
            if not uid:
                continue
            self.user_id = uid
            self.profile_id = node.get("master_profile") or self.profile_id
            if self.get_user():
                return True, "ok"

        if self.auth_failed:
            return False, "token rejected (401/403)"
        return False, "user id resolve nahi hua"

    def ensure_auth(self):
        if not self.access_token:
            return False
        if jwt_is_expired(self.access_token):
            self.auth_failed = True
            return False
        if not self.user_id:
            claims = decode_jwt_payload(self.access_token)
            self.user_id = (claims.get("_id") or claims.get("id")
                            or claims.get("sub") or claims.get("userId"))
        if self.user_id and self.get_user():
            return True
        ok, _ = self.login_with_token(self.access_token)
        return ok

    def get_user(self):
        if not self.user_id:
            return False
        sc, data = self._req("GET", f"/users/{self.user_id}")
        if sc == 200 and isinstance(data, dict):
            self.user_id = data.get("_id", self.user_id)
            self.profile_id = data.get("master_profile", self.profile_id)
            phone = data.get("mobile")
            if phone and not self.phone:
                self.phone = phone
            return True
        return False

    def open_app(self):
        if not (self.user_id and self.profile_id):
            return False
        payload = {"openApp": {"_id": self.user_id, "date": date.today().isoformat()}}
        sc, data = self._post_json(
            f"/users/{self.user_id}/profiles/{self.profile_id}/open_app", payload, method="PATCH"
        )
        return bool(sc == 200 and isinstance(data, dict) and data.get("success"))

    # ---- coins / status --------------------------------------------------
    def get_balance_silent(self):
        try:
            sc, data = self._req("GET", "/coins/balance")
            if sc == 200 and isinstance(data, dict):
                coins = data.get("coins")
                if isinstance(coins, dict):
                    coins = coins.get("coins")
                return coins
            if sc == 200 and isinstance(data, (int, float)):
                return int(data)
        except Exception:
            pass
        return None

    def get_balance(self):
        sc, data = self._req("GET", "/coins/balance")
        if sc == 200 and isinstance(data, dict):
            coins = data.get("coins", 0)
            if isinstance(coins, dict):
                coins = coins.get("coins", 0)
            return coins
        if sc == 200 and isinstance(data, (int, float)):
            return int(data)
        return None

    def get_campaign_status(self):
        sc, data = self._req("GET", "/watch-campaign/status")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            cap = data.get("dailyVideoCap", {}) or {}
            return {
                "enabled": data.get("enabled", False),
                "cap": cap.get("cap", 0) or 0,
                "used": cap.get("used", 0) or 0,
                "reached": cap.get("reached", False),
                "blockWatching": cap.get("blockWatching", False),
            }
        return {"enabled": False, "cap": 0, "used": 0, "reached": False, "blockWatching": False}

    # ---- catalog ---------------------------------------------------------
    def _collect_series_deep(self, obj, out_dict, depth=0):
        if obj is None or depth > 12:
            return
        if isinstance(obj, dict):
            has_id = obj.get("_id") or obj.get("id") or obj.get("series_id")
            looks_like_series = (
                obj.get("title")
                or obj.get("numberOfEpisodes") is not None
                or obj.get("totalEpisodes") is not None
                or obj.get("cardImage")
                or obj.get("hindiTitle")
            )
            if has_id and looks_like_series:
                sid = has_id
                if sid not in out_dict:
                    out_dict[sid] = obj
            for v in obj.values():
                self._collect_series_deep(v, out_dict, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                self._collect_series_deep(item, out_dict, depth + 1)

    def get_all_series(self, page_size=100, max_pages=10):
        found = {}
        try:
            sc, data = self._req("GET", "/short_search?page=home")
            if sc == 200 and isinstance(data, (dict, list)):
                self._collect_series_deep(data, found)
        except Exception:
            pass

        endpoints = (
            "/webseries?page={p}&pageSize={ps}",
            "/discover?type=webseries&page={p}&pageSize={ps}",
            "/series?page={p}&pageSize={ps}",
        )
        for tmpl in endpoints:
            for page in range(1, max_pages + 1):
                try:
                    sc, data = self._req("GET", tmpl.format(p=page, ps=page_size))
                except Exception:
                    break
                if not (sc == 200 and isinstance(data, dict)):
                    break
                self._collect_series_deep(data, found)

                candidates = []
                for k in ("webseries", "series", "data", "items", "results", "contents", "list"):
                    if isinstance(data.get(k), list):
                        candidates.extend(data[k])
                inner = data.get("data") if isinstance(data.get("data"), dict) else (
                    data.get("response") if isinstance(data.get("response"), dict) else None
                )
                if inner:
                    for k in ("webseries", "series", "items", "results", "contents", "list", "data"):
                        if isinstance(inner.get(k), list):
                            candidates.extend(inner[k])
                if not candidates:
                    break
                for s in candidates:
                    if not isinstance(s, dict):
                        continue
                    sid = s.get("_id") or s.get("id") or s.get("series_id")
                    if sid and sid not in found:
                        found[sid] = s
                if len(candidates) < int(page_size * 0.5):
                    break

        series_list = list(found.values())

        def _sort_key(s):
            try:
                return -int(s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0)
            except Exception:
                return 0

        series_list.sort(key=_sort_key)
        return series_list

    def get_series(self, series_id):
        sc, data = self._req("GET", f"/webseries/{series_id}")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data
        return None

    def get_episodes(self, series_id, page=1, page_size=50):
        sc, data = self._req("GET", f"/episodes?series_id={series_id}&page={page}&pageSize={page_size}")
        if sc == 200 and isinstance(data, dict):
            return data.get("episodes", []) or [], data.get("total", 0) or 0
        return [], 0

    # ---- profile / history ----------------------------------------------
    def get_profile(self):
        if not (self.user_id and self.profile_id):
            return None
        sc, data = self._req("GET", f"/users/{self.user_id}/profiles/{self.profile_id}")
        if sc == 200 and isinstance(data, dict):
            profile = data.get("profile", {}) or {}
            history = profile.get("watchHistory", []) or profile.get("watched", []) or []
            if not isinstance(history, list):
                history = []
            self.watch_history_raw = list(history)
            self.watch_history = {}
            for wh in history:
                if not isinstance(wh, dict):
                    continue
                key = (wh.get("id"), wh.get("episodeNo"))
                prev = self.watch_history.get(key) or {"watchedPct": 0, "time": 0}
                cur_pct = wh.get("watchedPct", 0) or 0
                cur_time = wh.get("time", 0) or 0
                if cur_pct >= (prev.get("watchedPct") or 0):
                    self.watch_history[key] = {"watchedPct": cur_pct, "time": cur_time}
            return profile
        return None

    def get_watch_counts_from_profile(self):
        counts = {}
        try:
            self.get_profile()
        except Exception:
            pass
        for item in (self.watch_history_raw or []):
            if not isinstance(item, dict):
                continue
            sid = item.get("id") or item.get("series_id")
            ep = item.get("episodeNo") or item.get("episode_no")
            try:
                pct = int(item.get("watchedPct") or item.get("progress") or 0)
            except Exception:
                pct = 0
            if sid and ep and pct >= 80:
                k = (str(sid), str(ep))
                counts[k] = counts.get(k, 0) + 1
        for (sid, ep_no), info in self.watch_history.items():
            try:
                pct = int(info.get("watchedPct") or 0) if isinstance(info, dict) else 0
            except Exception:
                pct = 0
            if pct >= 80:
                k = (str(sid), str(ep_no))
                counts[k] = max(counts.get(k, 0), 1)
        for k, c in (self.runtime_watch_counts or {}).items():
            counts[k] = max(counts.get(k, 0), c)
        return counts

    # ---- watch pipeline --------------------------------------------------
    def _update_watch_progress(self, series_id, series_title, hindi_title, episode_no,
                               tc_in_ms, tc_out_ms, detail_image, watched_pct):
        if not (self.user_id and self.profile_id):
            return False
        try:
            watched_pct = int(watched_pct or 0)
        except Exception:
            watched_pct = 0
        tc_in_ms = tc_in_ms or 0
        if not tc_out_ms or tc_out_ms <= tc_in_ms:
            tc_out_ms = tc_in_ms + 60000
        duration = tc_out_ms - tc_in_ms
        current_time_ms = int(tc_in_ms + (duration * watched_pct / 100)) if watched_pct < 100 else tc_out_ms
        stored_pct = 99 if watched_pct == 99 else (100 if watched_pct >= 100 else watched_pct)
        watch_obj = {
            "id": series_id,
            "title": series_title,
            "hindiTitle": hindi_title,
            "episodeNo": episode_no,
            "tcInMs": tc_in_ms,
            "tcOutMs": tc_out_ms,
            "detailImage": detail_image,
            "type": "episode",
            "progress": 100 if watched_pct >= 100 else watched_pct,
            "time": current_time_ms,
            "watchedPct": stored_pct,
            "campaign": False,
        }
        ok = False
        try:
            sc1, d1 = self._post_json(
                f"/users/{self.user_id}/profiles/{self.profile_id}",
                {"watched": watch_obj}, method="PATCH",
            )
            ok = sc1 == 200 and isinstance(d1, dict) and bool(d1.get("success"))
        except Exception:
            pass
        if ok:
            return True
        for path in (
            f"/users/{self.user_id}/profiles/{self.profile_id}/watch-history/update",
            "/watch-history/update",
        ):
            try:
                sc2, d2 = self._post_json(path, {"watched": watch_obj, "campaign": False})
                if sc2 and sc2 < 500:
                    if isinstance(d2, dict) and d2.get("success"):
                        return True
                    if sc2 == 200:
                        return True
            except Exception:
                continue
        return False

    def _report_watch_progress_to_coins(self, series_id, episode_no, watched_pct, series_title=""):
        if not (self.user_id and self.profile_id):
            return False
        try:
            watched_pct = int(watched_pct or 0)
        except Exception:
            watched_pct = 0
        ep_str = str(episode_no)
        body_a = {
            "series_id": series_id, "episode_no": episode_no, "episodeNo": episode_no,
            "progress": watched_pct, "watchedPct": watched_pct,
            "campaign": False, "task_type": "watch_ladder",
        }
        body_b = {
            "type": "watch_ladder", "seriesId": series_id, "episode": ep_str,
            "watched": watched_pct, "campaign": False,
        }
        endpoints = (
            ("/coins/progress-report", body_a),
            ("/coins/tasks/progress", body_a),
            ("/coins/watch-progress", body_b),
            ("/coins/report-watched", body_b),
            ("/watch-ladder/progress", body_a),
        )
        for path, body in endpoints:
            try:
                sc, d = self._post_json(path, body)
                if sc and sc < 500 and isinstance(d, dict):
                    if d.get("success") is True:
                        return True
                    if sc == 200 and "success" not in d:
                        return True
            except Exception:
                continue
        return False

    def _start_task_for_series(self, series_id):
        task_id = f"watch_ladder_{series_id}"
        candidates = (
            (f"/coins/tasks/{task_id}/start", {"series_id": series_id, "campaign": False}),
            ("/watch-campaign/start", {"seriesId": series_id, "series_id": series_id, "campaign": False}),
            ("/watch-campaign/select-series", {"seriesId": series_id, "series_id": series_id, "campaign": False}),
        )
        for path, body in candidates:
            try:
                sc, d = self._post_json(path, body)
                if sc and sc < 500:
                    if isinstance(d, dict) and d.get("success") is True:
                        return True
                    if sc == 200:
                        return True
            except Exception:
                continue
        return False

    def watch_campaign_select_series(self, series_id):
        self._start_task_for_series(series_id)
        return True

    def claim_reward_task(self, series_id=None):
        if series_id:
            try:
                self._report_watch_progress_to_coins(series_id, 0, 100)
            except Exception:
                pass
        if not series_id:
            return False, None
        task_id = f"watch_ladder_{series_id}"
        candidates = (
            (f"/coins/tasks/{task_id}/claim", None),
            ("/coins/tasks/claim", {"task_id": task_id, "campaign": False}),
            (f"/coins/tasks/{task_id}/reward", None),
            ("/watch-ladder/claim", {"series_id": series_id, "campaign": False}),
            ("/coins/claim", {"series_id": series_id, "type": "watch_ladder", "campaign": False}),
        )
        for path, body in candidates:
            try:
                if body is None:
                    sc, data = self._req("POST", path)
                else:
                    sc, data = self._post_json(path, body)
                if sc and sc < 500 and isinstance(data, dict):
                    if data.get("success") is True:
                        coins = (data.get("coins") or data.get("reward_coins")
                                 or data.get("reward") or 0)
                        return True, coins
                    if sc == 200 and "success" not in data:
                        return True, None
            except Exception:
                continue
        return False, None

    def unlock_episode(self, series_id, ep_id, ep_no):
        if not (series_id and ep_id):
            return False
        candidates = (
            (f"/episodes/{ep_id}/unlock", {"series_id": series_id, "episodeNo": ep_no, "campaign": False}),
            ("/episodes/unlock", {"series_id": series_id, "episode_id": ep_id, "episodeNo": ep_no, "campaign": False}),
            ("/coins/unlock-episode", {"series_id": series_id, "episode_id": ep_id, "episodeNo": ep_no}),
        )
        for path, body in candidates:
            try:
                sc, d = self._post_json(path, body)
                if sc and sc < 500 and isinstance(d, dict):
                    if d.get("success") is True:
                        return True
                    if sc == 200 and d.get("unlocked"):
                        return True
            except Exception:
                continue
        return False

    def watch_episode(self, episode, series_info, allow_repeat=False, nth_watch=None, cancel=None):
        """Always returns a 4-tuple: (ok, status, gained, expected).

        v1 returned 2-tuples on some paths while callers unpacked 4 -> ValueError.
        """
        expected = _expected_reward(nth_watch) if nth_watch is not None else None

        if not isinstance(series_info, dict) or not isinstance(episode, dict):
            return False, "invalid", 0, expected
        series_id = series_info.get("_id") or series_info.get("id") or series_info.get("series_id")
        if not series_id:
            return False, "no_series_id", 0, expected

        ep_no = episode.get("episodeNo") or episode.get("episode_no") or episode.get("number") or 0
        series_title = series_info.get("title") or ""
        hindi_title = series_info.get("hindiTitle") or series_title
        detail_image = series_info.get("cardImage") or series_info.get("longVerticalImage") or ""

        try:
            tc_in = int(episode.get("tcIn") or 0)
        except Exception:
            tc_in = 0
        try:
            tc_out = int(episode.get("tcOut") or (tc_in + 60))
        except Exception:
            tc_out = tc_in + 60
        if tc_out <= tc_in:
            tc_out = tc_in + 60
        tc_in_ms, tc_out_ms = tc_in * 1000, tc_out * 1000

        history_key = (series_id, ep_no)
        try:
            current_pct = int(self.watch_history.get(history_key, {}).get("watchedPct", 0) or 0)
        except Exception:
            current_pct = 0
        if not allow_repeat and current_pct >= 80:
            return True, "skip", 0, expected

        if not episode.get("coinUnlocked", True):
            try:
                self.unlock_episode(series_id, episode.get("_id") or episode.get("id"), ep_no)
            except Exception:
                pass

        reported = False
        for pct in (1, 50, 80, 99, 100, 100):
            if cancel is not None and cancel.is_set():
                return False, "cancelled", 0, expected
            if not allow_repeat and pct < current_pct:
                continue
            self._update_watch_progress(
                series_id, series_title, hindi_title, ep_no,
                tc_in_ms, tc_out_ms, detail_image, pct,
            )
            if pct >= 80 and not reported:
                try:
                    self._report_watch_progress_to_coins(series_id, ep_no, pct, series_title)
                    reported = True
                except Exception:
                    pass
            if cancel is not None:
                if cancel.wait(0.15):
                    return False, "cancelled", 0, expected
            else:
                time.sleep(0.15)

        if not reported:
            try:
                self._report_watch_progress_to_coins(series_id, ep_no, 100, series_title)
            except Exception:
                pass

        bal_before = self.get_balance_silent()
        try:
            self.claim_reward_task(series_id=series_id)
        except Exception:
            pass
        if cancel is not None:
            cancel.wait(0.6)
        else:
            time.sleep(0.6)
        bal_after = self.get_balance_silent()

        gained = 0
        if bal_before is not None and bal_after is not None:
            gained = max(0, (bal_after or 0) - (bal_before or 0))

        self.watch_history[history_key] = {"watchedPct": 100, "time": tc_out_ms}
        rk = (str(series_id), str(ep_no))
        self.runtime_watch_counts[rk] = self.runtime_watch_counts.get(rk, 0) + 1
        self.watch_history_raw.append({
            "id": series_id, "series_id": series_id, "episodeNo": ep_no,
            "watchedPct": 100, "progress": 100, "time": tc_out_ms,
        })
        return True, "done", gained, expected

    # ---- quiz ------------------------------------------------------------
    def solve_quiz_with_groq(self, question, opts, force=False):
        opts = [str(x) for x in (opts or [])]
        if not opts or not self.groq_api_key:
            return -1, None

        qid = question.get("questionId") or question.get("id") or ""
        qhi = str(question.get("questionHi") or "")
        qen = str(question.get("questionEn") or "")
        topic = str(question.get("topic") or "")
        qtype = question.get("type") or "unknown"

        cache_key = (qid, tuple(opts), qhi, qen, topic)
        if not force and cache_key in self.groq_cache:
            return self.groq_cache[cache_key]

        lines = [
            "# MINI-QUIZ QUESTION (LEVEL-1 KIDS)",
            "",
            f"**Type**: {qtype}",
            f"**Question (Hi)**: {qhi}",
            f"**Question (En)**: {qen}",
        ]
        if topic:
            lines.append(f"**Topic**: {topic}")
        lines += ["", "**Options (index = N from 0)**:"]
        for i, o in enumerate(opts):
            lines.append(f"  N={i}  ->  {o}")
        lines += [
            "",
            "## INSTRUCTIONS",
            "- This is a KIDS/LEVEL-1 multiple choice question inside an Indian short-video app.",
            "- Pick the SINGLE best correct option index.",
            "- Return ONLY a strict JSON object, exactly one line, in this shape:",
            '  {"chosenIndex": N, "reasoning": "short reasoning"}',
            f"- N MUST be an integer between 0 and {len(opts) - 1} inclusive.",
            "- Strict JSON only, no markdown, no extra text.",
            "",
            "## OUTPUT (strict JSON only)",
        ]
        prompt = "\n".join(lines)

        chosen_index, reasoning = -1, None
        try:
            # Use the model specified, but if it fails, you can change the default.
            # The model "openai/gpt-oss-120b" might not be available on Groq.
            # Set GROQ_MODEL environment variable to override.
            model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
            body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 256,
                "response_format": {"type": "json_object"},
            }
            # Isolated session: v1 reused the MiniPix session and leaked its bearer.
            resp = GROQ_HTTP.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {str(self.groq_api_key).strip()}",
                    "content-type": "application/json",
                },
                data=json.dumps(body),
                timeout=30,
            )
            if resp.status_code == 200:
                choices = (resp.json() or {}).get("choices") or []
                if choices:
                    raw_txt = (choices[0].get("message") or {}).get("content") or ""
                    chosen_index, reasoning = self._parse_quiz_json(raw_txt, len(opts))
            else:
                # Log the error but continue
                print(f"[!] Groq API error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[!] Groq call exception: {e}")

        if chosen_index is not None and 0 <= int(chosen_index) < len(opts):
            result = (int(chosen_index), reasoning or "groq")
            self.groq_cache[cache_key] = result
            return result
        return -1, None

    def _parse_quiz_json(self, raw_text, n_options):
        import re as _re
        if not raw_text:
            return -1, None
        chosen_index, reasoning = -1, None
        s = str(raw_text).strip().replace("```json", "").replace("```", "").strip()
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                if "chosenIndex" in d:
                    try:
                        chosen_index = int(d["chosenIndex"])
                    except Exception:
                        chosen_index = -1
                if "reasoning" in d:
                    reasoning = str(d["reasoning"])
                if isinstance(d.get("data"), dict) and "chosenIndex" in d["data"]:
                    try:
                        chosen_index = int(d["data"]["chosenIndex"])
                    except Exception:
                        pass
        except Exception:
            pass
        if chosen_index < 0:
            m = _re.search(r'"chosenIndex"\s*:\s*(-?\d+)', s)
            if m:
                try:
                    chosen_index = int(m.group(1))
                except Exception:
                    pass
        if chosen_index < 0:
            m = _re.search(r"\bN\s*=\s*(\d+)\b", s)
            if m:
                try:
                    chosen_index = int(m.group(1))
                except Exception:
                    pass
        if reasoning is None:
            m = _re.search(r'"reasoning"\s*:\s*"([^"]{0,120})"', s)
            if m:
                reasoning = m.group(1)
        if chosen_index is not None and 0 <= int(chosen_index) < n_options:
            return int(chosen_index), reasoning
        return -1, reasoning

    def quiz_get_status(self):
        sc, data = self._req("GET", "/quiz/status")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data
        return None

    def quiz_start_session(self):
        sc, data = self._post_json("/quiz/session/start", {"campaign": False})
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data
        return None

    def quiz_submit_answer(self, session_id, question_id, chosen_index):
        sc, data = self._post_json("/quiz/session/answer", {
            "sessionId": session_id,
            "questionId": question_id,
            "chosenIndex": int(chosen_index),
        })
        if sc == 200 and isinstance(data, dict):
            return data
        return None

    def quiz_claim_final(self, session_id):
        if not session_id:
            return False, None
        candidates = (
            ("/quiz/session/claim", {"sessionId": session_id, "campaign": False}),
            ("/quiz/session/complete", {"sessionId": session_id, "campaign": False}),
            ("/quiz/claim", {"sessionId": session_id, "campaign": False}),
            ("/coins/quiz-claim", {"sessionId": session_id, "type": "quiz", "campaign": False}),
        )
        for path, body in candidates:
            try:
                sc, d = self._post_json(path, body)
                if sc and sc < 500 and isinstance(d, dict):
                    if d.get("success") is True:
                        coins = (d.get("coins") or d.get("reward_coins")
                                 or d.get("coinsEarned") or d.get("reward") or 0)
                        return True, coins
                    if sc == 200 and "success" not in d:
                        return True, None
            except Exception:
                continue
        return False, None

    def run_auto_quiz(self, max_sessions=5, cancel=None, on_event=None):
        """Structured quiz runner. Emits dict events instead of raw strings."""
        def emit(**kw):
            if on_event:
                try:
                    on_event(kw)
                except Exception:
                    pass

        if not self.groq_api_key:
            emit(type="error", message="Groq API key set nahi hai --- /set_groq_key use karo")
            return {"sessions": 0, "correct": 0, "questions": 0, "coins": 0, "delta": 0, "error": "no_key"}

        bal_start = self.get_balance_silent()
        sessions_done = total_correct = total_questions = total_coins = 0

        for session_num in range(1, int(max_sessions) + 1):
            if cancel is not None and cancel.is_set():
                emit(type="cancelled")
                break

            emit(type="session_start", session=session_num, of=max_sessions)
            sess = self.quiz_start_session()
            if not sess or not sess.get("success"):
                emit(type="session_failed", session=session_num, error="start_failed")
                continue

            session_id = sess.get("sessionId") or sess.get("session_id")
            questions = sess.get("questions") or (sess.get("data") or {}).get("questions") or []
            if not questions:
                emit(type="session_empty", session=session_num)
                continue

            correct_count = 0
            for q_idx, q in enumerate(questions, 1):
                if cancel is not None and cancel.is_set():
                    break
                qid = q.get("questionId") or q.get("id")
                opts = q.get("options") or []
                chosen, _reason = self.solve_quiz_with_groq(q, opts)
                if chosen < 0:
                    # fallback to first option if Groq fails
                    chosen = 0
                resp = self.quiz_submit_answer(session_id, qid, chosen)
                correct = False
                if isinstance(resp, dict):
                    correct = (
                        resp.get("correct") is True
                        or resp.get("isCorrect") is True
                        or (isinstance(resp.get("success"), dict) and resp["success"].get("correct") is True)
                    )
                if correct:
                    correct_count += 1
                total_questions += 1
                emit(type="answer", session=session_num, q=q_idx, correct=correct)

            ok, coins = self.quiz_claim_final(session_id)
            if not ok:
                emit(type="session_claim_failed", session=session_num, error="claim_failed")
            coins = int(coins or 0)
            total_coins += coins
            total_correct += correct_count
            sessions_done += 1
            emit(type="session_done", session=session_num, correct=correct_count,
                 total=len(questions), coins=coins)

            if cancel is not None:
                if cancel.wait(1.0):
                    break
            else:
                time.sleep(1.0)

        bal_end = self.get_balance_silent()
        delta = 0
        if bal_start is not None and bal_end is not None:
            delta = (bal_end or 0) - (bal_start or 0)
        emit(type="finished", sessions=sessions_done, delta=delta)
        return {
            "sessions": sessions_done,
            "correct": total_correct,
            "questions": total_questions,
            "coins": total_coins,
            "delta": delta,
            "balance_start": bal_start,
            "balance_end": bal_end,
        }


# --------------------------------------------------------------------------
# Job tracking --- one Job per (telegram user, run). Accounts run in parallel.
# --------------------------------------------------------------------------
STATE_ICON = {
    "queued": "[..]",
    "running": "[>>]",
    "done": "[OK]",
    "capped": "[!!]",
    "stopped": "[--]",
    "error": "[XX]",
}


class Job:
    def __init__(self, kind, tg_user_id, accounts):
        self.kind = kind
        self.user_id = str(tg_user_id)
        self.cancel = threading.Event()
        self.started_at = time.time()
        self.finished_at = None
        self._lock = threading.RLock()
        self.progress = OrderedDict()
        for acc in accounts:
            self.progress[acc["id"]] = {
                "id": acc["id"],
                "label": acc.get("label") or acc["id"],
                "state": "queued",
                "note": "waiting",
                "watches": 0,
                "coins": 0,
                "series_done": 0,
                "series_total": 0,
                "quiz_sessions": 0,
                "quiz_correct": 0,
                "quiz_questions": 0,
                "balance_start": None,
                "balance_end": None,
                "error": None,
            }

    def set(self, acc_id, **kw):
        with self._lock:
            row = self.progress.get(acc_id)
            if row is not None:
                row.update(kw)

    def inc(self, acc_id, **deltas):
        with self._lock:
            row = self.progress.get(acc_id)
            if row is None:
                return
            for key, delta in deltas.items():
                current = row.get(key)
                if not isinstance(current, (int, float)) or isinstance(current, bool):
                    current = 0
                row[key] = current + delta

    def snapshot(self):
        with self._lock:
            return [dict(v) for v in self.progress.values()]

    def totals(self):
        rows = self.snapshot()
        return {
            "accounts": len(rows),
            "watches": sum(r.get("watches", 0) for r in rows),
            "coins": sum(r.get("coins", 0) for r in rows),
            "quiz_sessions": sum(r.get("quiz_sessions", 0) for r in rows),
            "quiz_correct": sum(r.get("quiz_correct", 0) for r in rows),
            "quiz_questions": sum(r.get("quiz_questions", 0) for r in rows),
            "delta": sum(
                (r.get("balance_end") or 0) - (r.get("balance_start") or 0)
                for r in rows
                if r.get("balance_start") is not None and r.get("balance_end") is not None
            ),
            "running": sum(1 for r in rows if r.get("state") in ("running", "queued")),
            "errors": sum(1 for r in rows if r.get("state") == "error"),
        }

    def elapsed(self):
        return (self.finished_at or time.time()) - self.started_at


class JobManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}

    def start(self, job):
        with self._lock:
            existing = self._jobs.get(job.user_id)
            if existing is not None and existing.finished_at is None:
                return False
            self._jobs[job.user_id] = job
            return True

    def get(self, user_id):
        with self._lock:
            return self._jobs.get(str(user_id))

    def stop(self, user_id):
        with self._lock:
            job = self._jobs.get(str(user_id))
        if job is None or job.finished_at is not None:
            return None
        job.cancel.set()
        return job

    def finish(self, job):
        with self._lock:
            job.finished_at = time.time()
            if self._jobs.get(job.user_id) is job:
                self._jobs.pop(job.user_id, None)

    def active(self):
        with self._lock:
            return {k: v for k, v in self._jobs.items() if v.finished_at is None}


JOBS = JobManager()


# --------------------------------------------------------------------------
# Sync workers --- these run inside EXECUTOR threads, one per account.
# --------------------------------------------------------------------------
def _persist_account(tg_uid, acc_id, session, **extra):
    try:
        fields = session.account_fields()
        fields.update(extra)
        STORE.update_account(tg_uid, acc_id, **fields)
    except Exception:
        pass


def _prepare_session(tg_uid, acc, job):
    """Common login/verify step. Returns a session or None (job row already set)."""
    aid = acc["id"]
    job.set(aid, state="running", note="auth check")
    session = MiniPixUserSession(acc, STORE.get_groq(tg_uid))

    if not session.access_token:
        job.set(aid, state="error", note="not logged in", error="no token")
        session.close()
        return None

    if not session.ensure_auth():
        job.set(aid, state="error", note="token expired",
                error="token expired --- /add_token se dubara add karo")
        STORE.update_account(tg_uid, aid, last_error="token expired")
        session.close()
        return None

    STORE.update_account(tg_uid, aid, last_error=None, last_run=now_iso())
    try:
        session.open_app()
    except Exception:
        pass
    return session


def worker_watch(tg_uid, acc, job, stop_after=None, only_series=None):
    aid = acc["id"]
    session = None
    try:
        session = _prepare_session(tg_uid, acc, job)
        if session is None:
            return

        bal_start = session.get_balance_silent()
        job.set(aid, balance_start=bal_start, note="checking cap")

        cap = session.get_campaign_status()
        daily_used = int(cap.get("used") or 0)
        daily_cap = int(cap.get("cap") or 0)
        if cap.get("blockWatching") or cap.get("reached"):
            job.set(aid, state="capped", note=f"daily cap {daily_used}/{daily_cap}",
                    balance_end=bal_start)
            return

        job.set(aid, note="loading history")
        watch_counts = session.get_watch_counts_from_profile()

        job.set(aid, note="loading catalog")
        if only_series:
            info = session.get_series(only_series)
            series_list = [info] if info else []
            if not series_list:
                job.set(aid, state="error", note="series not found", error="series id galat hai")
                return
        else:
            series_list = CATALOG.series_list(session, max_pages=8)

        job.set(aid, series_total=len(series_list), note="watching")
        total_watched = 0

        for s_idx, s in enumerate(series_list, 1):
            if job.cancel.is_set():
                break
            if stop_after and total_watched >= stop_after:
                break
            if daily_cap and (daily_used + total_watched) >= daily_cap:
                job.set(aid, note=f"daily cap {daily_cap} hit")
                break
            if not isinstance(s, dict):
                continue

            sid = s.get("_id") or s.get("id") or s.get("series_id")
            if not sid:
                continue

            s_info = s if only_series else (session.get_series(sid) or s)
            real_sid = (s_info or {}).get("_id") or (s_info or {}).get("id") or sid
            episodes = CATALOG.episodes(session, real_sid)
            if not episodes:
                continue

            def _ep_key(e):
                try:
                    return int(e.get("episodeNo") or 0)
                except Exception:
                    try:
                        return float(e.get("episodeNo") or 0)
                    except Exception:
                        return 0

            episodes_sorted = sorted(episodes, key=_ep_key)
            session.watch_campaign_select_series(real_sid)

            series_watched = 0
            rounds = 0
            any_progress = True
            while any_progress and rounds < MAX_WATCH_ROUNDS:
                rounds += 1
                any_progress = False
                for ep in episodes_sorted:
                    if job.cancel.is_set():
                        break
                    if stop_after and total_watched >= stop_after:
                        break
                    if daily_cap and (daily_used + total_watched) >= daily_cap:
                        break

                    ep_no = ep.get("episodeNo") or ep.get("episode_no") or 0
                    key = (str(real_sid), str(ep_no))
                    current = watch_counts.get(key, 0)
                    if current >= MAX_WATCHES_PER_EP:
                        continue

                    nth = current + 1
                    ok, status, gained, _expected = session.watch_episode(
                        ep, s_info or s, allow_repeat=True, nth_watch=nth, cancel=job.cancel
                    )
                    if status == "cancelled":
                        break
                    if status == "skip" or not ok:
                        continue

                    watch_counts[key] = nth
                    series_watched += 1
                    total_watched += 1
                    any_progress = True

                    job.inc(aid, watches=1, coins=int(gained or 0))
                    job.set(aid, note=f"S{s_idx}/{len(series_list)} E{ep_no} x{nth}")
                    STATS.bump(tg_uid, "total_watches", 1)
                    if gained:
                        STATS.bump(tg_uid, "total_coins_earned", int(gained))
                    STORE.bump_account(tg_uid, aid, watches=1, coins=int(gained or 0))

            if series_watched:
                job.inc(aid, series_done=1)
                STATS.bump(tg_uid, "series_done", 1)
                _persist_account(tg_uid, aid, session)

        bal_end = session.get_balance_silent()
        state = "stopped" if job.cancel.is_set() else "done"
        job.set(aid, state=state, balance_end=bal_end,
                note="stopped by user" if state == "stopped" else "complete")
        _persist_account(tg_uid, aid, session, last_run=now_iso())

    except Exception as e:
        job.set(aid, state="error", note="crashed", error=f"{type(e).__name__}: {e}")
        print(f"[!] worker_watch {tg_uid}/{aid}: {traceback.format_exc(limit=3)}")
    finally:
        if session is not None:
            session.close()
        STATS.maybe_save(0)


def worker_quiz(tg_uid, acc, job, sessions=5):
    aid = acc["id"]
    session = None
    try:
        session = _prepare_session(tg_uid, acc, job)
        if session is None:
            return

        if not session.groq_api_key:
            job.set(aid, state="error", note="no groq key",
                    error="Groq key missing --- /set_groq_key")
            return

        job.set(aid, note="quiz running")

        def on_event(ev):
            kind = ev.get("type")
            if kind == "session_start":
                job.set(aid, note=f"session {ev.get('session')}/{ev.get('of')}")
            elif kind == "answer":
                job.inc(aid, quiz_questions=1, quiz_correct=1 if ev.get("correct") else 0)
            elif kind == "session_done":
                job.inc(aid, quiz_sessions=1, coins=int(ev.get("coins") or 0))
                STATS.bump(tg_uid, "quizzes_solved", 1)
                STORE.bump_account(tg_uid, aid, quizzes=1, coins=int(ev.get("coins") or 0))
                if ev.get("coins"):
                    STATS.bump(tg_uid, "total_coins_earned", int(ev["coins"]))
            elif kind == "session_failed":
                job.set(aid, state="error", note=f"session {ev.get('session')} start failed",
                        error=ev.get("error", "start_failed"))
            elif kind == "session_claim_failed":
                job.set(aid, state="error", note="claim failed", error="claim_failed")
            elif kind == "error":
                job.set(aid, state="error", note=ev.get("message", "unknown error"),
                        error=ev.get("message"))

        result = session.run_auto_quiz(max_sessions=sessions, cancel=job.cancel, on_event=on_event)

        # If no sessions succeeded, mark error
        if result.get("sessions", 0) == 0 and not job.cancel.is_set():
            job.set(aid, state="error", note="no quiz sessions completed", error="all_failed")

        state = "stopped" if job.cancel.is_set() else "done"
        job.set(
            aid,
            state=state,
            balance_start=result.get("balance_start"),
            balance_end=result.get("balance_end"),
            note="stopped by user" if state == "stopped" else "complete",
        )
        _persist_account(tg_uid, aid, session, last_run=now_iso())

    except Exception as e:
        job.set(aid, state="error", note="crashed", error=f"{type(e).__name__}: {e}")
        print(f"[!] worker_quiz {tg_uid}/{aid}: {traceback.format_exc(limit=3)}")
    finally:
        if session is not None:
            session.close()
        STATS.maybe_save(0)


def probe_account(tg_uid, acc):
    """Lightweight read-only probe used by /accounts, /balance, /status."""
    out = {
        "id": acc["id"],
        "label": acc.get("label") or acc["id"],
        "enabled": bool(acc.get("enabled")),
        "phone": acc.get("phone"),
        "balance": None,
        "cap_used": None,
        "cap_total": None,
        "campaign": None,
        "ok": False,
        "error": None,
    }
    session = None
    try:
        session = MiniPixUserSession(acc, STORE.get_groq(tg_uid))
        if not session.access_token:
            out["error"] = "no token"
            return out
        if not session.ensure_auth():
            out["error"] = "token expired"
            STORE.update_account(tg_uid, acc["id"], last_error="token expired")
            return out
        out["ok"] = True
        out["balance"] = session.get_balance_silent()
        cap = session.get_campaign_status()
        out["cap_used"] = cap.get("used")
        out["cap_total"] = cap.get("cap")
        out["campaign"] = cap.get("enabled")
        _persist_account(tg_uid, acc["id"], session, last_error=None)
    except Exception as e:
        out["error"] = f"{type(e).__name__}"
    finally:
        if session is not None:
            session.close()
    return out


# --------------------------------------------------------------------------
# Telegram helpers
# --------------------------------------------------------------------------
def user_display(user):
    name = ""
    if getattr(user, "first_name", None):
        name += user.first_name
    if getattr(user, "last_name", None):
        name += " " + user.last_name
    if not name:
        name = str(user.id)
    if getattr(user, "username", None):
        name += f" (@{user.username})"
    return name


async def log_to_channel(application, message):
    channel = BOT_CONFIG.log_channel_id
    if not channel or not application:
        return
    try:
        await application.bot.send_message(chat_id=channel, text=str(message)[:4000])
    except Exception:
        pass


async def reply(update, text, markdown=True):
    """Reply with Markdown, falling back to plain text if parsing fails."""
    text = str(text)[:4000]
    try:
        return await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN if markdown else None
        )
    except BadRequest:
        try:
            return await update.message.reply_text(text)
        except Exception:
            return None
    except Exception:
        return None


class ThrottledEditor:
    """Edits one Telegram message safely under flood-control."""

    def __init__(self, message, min_interval=PROGRESS_INTERVAL):
        self.message = message
        self.min_interval = min_interval
        self._last_text = None
        self._last_at = 0.0
        self._lock = asyncio.Lock()

    async def update(self, text, force=False):
        if self.message is None:
            return
        text = str(text)[:4000]
        async with self._lock:
            if not force:
                if text == self._last_text:
                    return
                if (time.time() - self._last_at) < self.min_interval:
                    return
            self._last_text = text
            self._last_at = time.time()
            try:
                await self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 2)) + 0.5)
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                try:
                    await self.message.edit_text(text)
                except Exception:
                    pass
            except (TimedOut, NetworkError, Forbidden):
                pass
            except Exception:
                pass


# --------------------------------------------------------------------------
# Progress rendering
# --------------------------------------------------------------------------
def render_job(job, title, final=False):
    rows = job.snapshot()
    totals = job.totals()
    is_quiz = job.kind == "quiz"

    head = "FINISHED" if final else "RUNNING"
    lines = [f"*{md(title)}*  --  _{head}_", ""]

    for idx, r in enumerate(rows, 1):
        icon = STATE_ICON.get(r.get("state"), "[..]")
        label = md(str(r.get("label") or r.get("id"))[:16])

        if is_quiz:
            detail = (f"{r.get('quiz_sessions', 0)} sess | "
                      f"{r.get('quiz_correct', 0)}/{r.get('quiz_questions', 0)} ok")
        else:
            detail = f"{r.get('watches', 0)} eps"
            if r.get("series_total"):
                detail += f" | {r.get('series_done', 0)}/{r.get('series_total')} ser"

        coins = int(r.get("coins") or 0)
        line = f"`{idx}.` {icon} *{label}* -- {detail} | +{coins}c"
        lines.append(line)

        note = r.get("error") if r.get("state") == "error" else r.get("note")
        if note:
            lines.append(f"     _{md(str(note)[:56])}_")

    lines.append("")
    if is_quiz:
        lines.append(
            f"*Total:* {totals['quiz_sessions']} sessions | "
            f"{totals['quiz_correct']}/{totals['quiz_questions']} correct"
        )
    else:
        lines.append(f"*Total:* {totals['watches']} episodes")

    lines.append(f"*Coins:* +{totals['coins']}  |  *Net balance:* +{totals['delta']}")
    lines.append(f"*Accounts:* {totals['accounts']} ({totals['running']} active)")
    lines.append(f"*Time:* {fmt_duration(job.elapsed())}")

    if not final:
        lines.append("")
        lines.append("_Stop karne ke liye_ /stop")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Parallel run orchestrator
# --------------------------------------------------------------------------
def parse_run_args(user_id, args):
    """Parse `N` (limit) and `on=a1,a2` (account filter) from command args."""
    limit = None
    refs = []
    leftovers = []
    for raw in (args or []):
        token = str(raw).strip()
        if not token:
            continue
        low = token.lower()
        if low.startswith("on=") or low.startswith("acc="):
            value = token.split("=", 1)[1]
            refs.extend([p for p in value.replace(";", ",").split(",") if p.strip()])
        elif token.isdigit() and limit is None:
            limit = int(token)
        else:
            leftovers.append(token)
    targets = STORE.resolve_many(user_id, refs) if refs else None
    return limit, targets, leftovers


async def run_parallel_job(update, context, kind, title, worker, args_desc="", targets=None):
    """Fan an operation out across every enabled account of one Telegram user.

    Each account gets its own thread from EXECUTOR, so:
      * one user's N accounts run in parallel with each other, AND
      * every other Telegram user keeps running at the same time, because the
        asyncio event loop is never blocked by requests.
    """
    user = update.effective_user
    STATS.touch(user.id)

    accounts = targets if targets is not None else STORE.enabled_accounts(user.id)
    if not accounts:
        total = len(STORE.list_accounts(user.id))
        if total == 0:
            await reply(update,
                        "Abhi koi account add nahi hai.\n\n"
                        "Add karo:\n`/add_account 9876543210 Main`\n"
                        "ya token se:\n`/add_token <token> Main`")
        else:
            await reply(update,
                        "Koi *enabled* account nahi mila.\n"
                        "`/accounts` se list dekho, `/enable all` se sab on karo.")
        return

    job = Job(kind, user.id, accounts)
    if not JOBS.start(job):
        await reply(update, "Ek run pehle se chal raha hai. Pehle /stop karo.")
        return

    header = f"{title} ({len(accounts)} account{'s' if len(accounts) != 1 else ''})"
    if args_desc:
        header += f" {args_desc}"

    msg = await reply(update, render_job(job, header))
    editor = ThrottledEditor(msg)

    loop = asyncio.get_running_loop()
    gate = asyncio.Semaphore(max(1, PER_USER_PARALLEL))

    async def run_one(account):
        async with gate:
            if job.cancel.is_set():
                job.set(account["id"], state="stopped", note="cancelled before start")
                return
            await loop.run_in_executor(EXECUTOR, functools.partial(worker, user.id, account, job))

    tasks = [asyncio.create_task(run_one(a)) for a in accounts]

    async def ticker():
        try:
            while True:
                await asyncio.sleep(PROGRESS_INTERVAL)
                await editor.update(render_job(job, header))
        except asyncio.CancelledError:
            return

    tick_task = asyncio.create_task(ticker())
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        tick_task.cancel()
        try:
            await tick_task
        except (asyncio.CancelledError, Exception):
            pass
        JOBS.finish(job)
        STATS.save()

    await editor.update(render_job(job, header, final=True), force=True)

    totals = job.totals()
    await log_to_channel(
        context.application,
        f"{title} done -- {user_display(user)} (id {user.id})\n"
        f"Accounts: {totals['accounts']} | Watches: {totals['watches']} | "
        f"Quiz: {totals['quiz_sessions']} | Coins: +{totals['coins']} | "
        f"Net: +{totals['delta']} | {fmt_duration(job.elapsed())}"
    )


async def probe_all(user_id, accounts):
    """Fetch balance/cap for many accounts concurrently."""
    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(EXECUTOR, functools.partial(probe_account, user_id, acc))
        for acc in accounts
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    clean = []
    for acc, res in zip(accounts, results):
        if isinstance(res, dict):
            clean.append(res)
        else:
            clean.append({
                "id": acc["id"], "label": acc.get("label"), "enabled": acc.get("enabled"),
                "phone": acc.get("phone"), "balance": None, "cap_used": None,
                "cap_total": None, "campaign": None, "ok": False, "error": "probe failed",
            })
    return clean


# --------------------------------------------------------------------------
# Commands --- onboarding
# --------------------------------------------------------------------------
HELP_TEXT = (
    "*MiniPix Automation Bot*  (multi-user + multi-account)\n\n"
    "*Account manage karo*\n"
    "/add\\_account `<phone> [label]` -- OTP se naya account jodo\n"
    "/otp `<code>` -- OTP verify karo\n"
    "/add\\_token `<token> [label]` -- token se account jodo\n"
    "/accounts -- saare accounts + balance\n"
    "/rename `<ref> <label>` -- naam badlo\n"
    "/enable `<ref|all>` , /disable `<ref|all>`\n"
    "/remove\\_account `<ref>` -- account hatao\n"
    "/set\\_groq\\_key `<key>` -- quiz ke liye Groq key\n\n"
    "*Chalao (sab accounts PARALLEL)*\n"
    "/watch\\_all `[N] [on=a1,a2]` -- sab series watch\n"
    "/watch\\_one `<series_id> [N]` -- ek series watch\n"
    "/quiz `[N]` -- mini-quiz auto solve\n"
    "/stop -- chalu run rok do\n\n"
    "*Info*\n"
    "/balance , /status , /series\\_list , /my\\_stats , /help\n\n"
    "_ref_ = account number (`1`), id (`a1`) ya label (`Main`)."
)


async def start_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    accounts = STORE.list_accounts(user.id)
    enabled = [a for a in accounts if a.get("enabled") and a.get("token")]
    groq = STORE.get_groq(user.id)

    text = (
        f"Namaste *{md(user.first_name or 'dost')}*\n\n"
        + HELP_TEXT
        + "\n\n*Aapka status*\n"
        + f"  Accounts: *{len(accounts)}* (enabled: *{len(enabled)}*, max {MAX_ACCOUNTS_PER_USER})\n"
        + f"  Groq key: {'set' if groq else 'NOT set -- quiz nahi chalega'}\n"
    )
    await reply(update, text)
    await log_to_channel(
        context.application,
        f"/start -- {user_display(user)} (id {user.id}) | accounts: {len(accounts)}"
    )


async def help_cmd(update, context):
    await reply(update, HELP_TEXT)


# --------------------------------------------------------------------------
# Commands --- account management
# --------------------------------------------------------------------------
async def add_account_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    args = context.args or []

    if not args:
        await reply(update,
                    "*Naya account jodo:*\n`/add_account 9876543210 Main`\n\n"
                    "Ya seedha 10-digit number bhej do.")
        return

    phone = normalize_phone(args[0])
    if not phone:
        await reply(update, "Valid 10-digit phone number daalo.")
        return

    label = " ".join(args[1:]).strip()[:24] or None

    existing = STORE.list_accounts(user.id)
    already = next((a for a in existing if a.get("phone") == phone), None)
    if not already and len(existing) >= MAX_ACCOUNTS_PER_USER:
        await reply(update,
                    f"Limit poori: max *{MAX_ACCOUNTS_PER_USER}* accounts per user.\n"
                    "Pehle `/remove_account <ref>` karo.")
        return

    device_id = (already or {}).get("device_id") or gen_device_id()
    session = MiniPixUserSession({"device_id": device_id})

    loop = asyncio.get_running_loop()
    token = await loop.run_in_executor(
        EXECUTOR, functools.partial(session.login_otp_generate, phone)
    )
    session.close()

    if not token:
        await reply(update, "OTP generate nahi hua. Number check karo ya thodi der baad try karo.")
        return

    STORE.set_pending_otp(user.id, token, phone, label=label,
                          account_id=(already or {}).get("id"))
    await reply(update,
                f"OTP bhej diya `{md(phone)}` par.\n\n"
                "Ab bhejo:\n`/otp 123456`\n\n_Ya sirf 6-digit code type kar do._")
    await log_to_channel(context.application,
                         f"OTP request -- {user_display(user)} | {phone}")


async def otp_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    args = context.args or []

    otp = args[0].strip() if args else ""
    if not otp:
        text = (update.message.text or "").strip()
        if text.isdigit() and len(text) == 6:
            otp = text

    if not otp or not otp.isdigit():
        await reply(update, "6-digit OTP daalo: `/otp 123456`")
        return

    pending = STORE.get_pending_otp(user.id)
    if not pending or not pending.get("session") or not pending.get("phone"):
        await reply(update, "Pehle `/add_account <phone>` karo.")
        return

    existing = STORE.resolve(user.id, pending.get("account_id")) if pending.get("account_id") else None
    device_id = (existing or {}).get("device_id") or gen_device_id()

    session = MiniPixUserSession({"device_id": device_id, "phone": pending["phone"]})
    session.phone = pending["phone"]

    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        EXECUTOR, functools.partial(session.login_otp_verify, pending["session"], otp)
    )

    if not ok:
        session.close()
        await reply(update, "OTP galat ya expire ho gaya. Firse `/add_account` karo.")
        return

    balance = await loop.run_in_executor(EXECUTOR, session.get_balance_silent)
    try:
        await loop.run_in_executor(EXECUTOR, session.open_app)
    except Exception:
        pass

    account, created = STORE.add_account(
        user.id,
        token=session.access_token,
        minipix_user_id=session.user_id,
        profile_id=session.profile_id,
        phone=session.phone,
        label=pending.get("label"),
        device_id=device_id,
    )
    session.close()
    STORE.clear_pending_otp(user.id)

    if not account:
        await reply(update, f"Limit poori: max {MAX_ACCOUNTS_PER_USER} accounts.")
        return

    total = len(STORE.list_accounts(user.id))
    verb = "Add ho gaya" if created else "Update ho gaya"
    await reply(update,
                f"*Login success -- {verb}*\n\n"
                f"  Label: *{md(account['label'])}*  (ref `{account['id']}`)\n"
                f"  Phone: `{md(account.get('phone'))}`\n"
                f"  Balance: *{balance if balance is not None else '?'}* coins\n"
                f"  Total accounts: *{total}*\n\n"
                "Ab `/watch_all` chalao -- sab accounts ek saath chalenge.")
    await log_to_channel(
        context.application,
        f"LOGIN OK -- {user_display(user)} | {account.get('phone')} | "
        f"minipix {account.get('user_id')} | total {total}"
    )


async def add_token_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    args = context.args or []

    if not args:
        await reply(update,
                    "*Token se account jodo:*\n`/add_token <access_token> [label]`\n\n"
                    "_Security: bhejne ke baad message auto-delete karne ki koshish hogi._")
        return

    token = args[0].strip()
    label = " ".join(args[1:]).strip()[:24] or None

    # Token chat me na pada rahe.
    try:
        await update.message.delete()
    except Exception:
        pass

    existing = STORE.list_accounts(user.id)
    if len(existing) >= MAX_ACCOUNTS_PER_USER:
        await reply(update, f"Limit poori: max *{MAX_ACCOUNTS_PER_USER}* accounts.")
        return

    device_id = gen_device_id()
    session = MiniPixUserSession({"device_id": device_id})

    loop = asyncio.get_running_loop()
    ok, why = await loop.run_in_executor(
        EXECUTOR, functools.partial(session.login_with_token, token)
    )

    if not ok:
        session.close()
        await reply(update, f"Token accept nahi hua: *{md(why)}*")
        return

    balance = await loop.run_in_executor(EXECUTOR, session.get_balance_silent)
    try:
        await loop.run_in_executor(EXECUTOR, session.open_app)
    except Exception:
        pass

    account, created = STORE.add_account(
        user.id,
        token=session.access_token,
        minipix_user_id=session.user_id,
        profile_id=session.profile_id,
        phone=session.phone,
        label=label,
        device_id=device_id,
    )
    session.close()

    if not account:
        await reply(update, f"Limit poori: max {MAX_ACCOUNTS_PER_USER} accounts.")
        return

    total = len(STORE.list_accounts(user.id))
    verb = "Add ho gaya" if created else "Update ho gaya"
    await reply(update,
                f"*Token OK -- {verb}*\n\n"
                f"  Label: *{md(account['label'])}*  (ref `{account['id']}`)\n"
                f"  MiniPix ID: `{md(mask(account.get('user_id'), 6))}`\n"
                f"  Balance: *{balance if balance is not None else '?'}* coins\n"
                f"  Total accounts: *{total}*")
    await log_to_channel(
        context.application,
        f"TOKEN LOGIN -- {user_display(user)} | minipix {account.get('user_id')} | total {total}"
    )


async def accounts_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    accounts = STORE.list_accounts(user.id)

    if not accounts:
        await reply(update,
                    "Abhi koi account nahi hai.\n\n"
                    "`/add_account 9876543210 Main`\n`/add_token <token> Alt`")
        return

    msg = await reply(update, f"*{len(accounts)} accounts* -- balance check ho raha hai...")
    results = await probe_all(user.id, accounts)

    lines = [f"*Your accounts ({len(accounts)}/{MAX_ACCOUNTS_PER_USER})*", ""]
    total_balance = 0
    have_balance = False

    for idx, (acc, res) in enumerate(zip(accounts, results), 1):
        flag = "ON " if acc.get("enabled") else "OFF"
        label = md(str(acc.get("label") or acc["id"])[:18])
        lines.append(f"`{idx}.` *{label}*  `{acc['id']}`  [{flag}]")

        if acc.get("phone"):
            lines.append(f"     phone: `{md(acc['phone'])}`")

        if res.get("ok"):
            bal = res.get("balance")
            if isinstance(bal, (int, float)):
                total_balance += int(bal)
                have_balance = True
            cap = f"{res.get('cap_used')}/{res.get('cap_total')}"
            lines.append(f"     coins: *{bal if bal is not None else '?'}*  |  daily cap: `{cap}`")
        else:
            lines.append(f"     _{md(res.get('error') or 'check failed')}_")

        stats_bits = []
        if acc.get("watches"):
            stats_bits.append(f"{acc['watches']} eps")
        if acc.get("quizzes"):
            stats_bits.append(f"{acc['quizzes']} quiz")
        if stats_bits:
            lines.append(f"     lifetime: {' | '.join(stats_bits)}")
        lines.append("")

    if have_balance:
        lines.append(f"*Combined balance:* {total_balance} coins")
    lines.append("")
    lines.append("_Manage:_ /enable /disable /rename /remove\\_account")

    editor = ThrottledEditor(msg, min_interval=0)
    await editor.update("\n".join(lines), force=True)


async def rename_cmd(update, context):
    user = update.effective_user
    args = context.args or []
    if len(args) < 2:
        await reply(update, "Use: `/rename <ref> <new label>`")
        return
    acc = STORE.resolve(user.id, args[0])
    if not acc:
        await reply(update, f"Account `{md(args[0])}` nahi mila. `/accounts` dekho.")
        return
    label = " ".join(args[1:]).strip()[:24]
    if not label:
        await reply(update, "Naya label khali nahi ho sakta.")
        return
    STORE.update_account(user.id, acc["id"], label=label)
    await reply(update, f"`{acc['id']}` ka naam ab *{md(label)}* hai.")


async def _toggle(update, context, enabled):
    user = update.effective_user
    args = context.args or []
    word = "enable" if enabled else "disable"

    if not args:
        await reply(update, f"Use: `/{word} <ref>` ya `/{word} all`")
        return

    if str(args[0]).lower() == "all":
        accounts = STORE.list_accounts(user.id)
        for acc in accounts:
            STORE.update_account(user.id, acc["id"], enabled=enabled)
        await reply(update, f"Sab *{len(accounts)}* accounts {word}d.")
        return

    changed = []
    for ref in args:
        acc = STORE.resolve(user.id, ref)
        if acc:
            STORE.update_account(user.id, acc["id"], enabled=enabled)
            changed.append(acc.get("label") or acc["id"])

    if not changed:
        await reply(update, "Koi matching account nahi mila. `/accounts` dekho.")
        return
    await reply(update, f"{word.capitalize()}d: *{md(', '.join(changed))}*")


async def enable_cmd(update, context):
    await _toggle(update, context, True)


async def disable_cmd(update, context):
    await _toggle(update, context, False)


async def remove_account_cmd(update, context):
    user = update.effective_user
    args = context.args or []
    if not args:
        await reply(update, "Use: `/remove_account <ref>`  (ya `all`)")
        return

    if str(args[0]).lower() == "all":
        accounts = STORE.list_accounts(user.id)
        for acc in accounts:
            STORE.remove_account(user.id, acc["id"])
        await reply(update, f"Sab *{len(accounts)}* accounts hata diye.")
        return

    acc = STORE.resolve(user.id, args[0])
    if not acc:
        await reply(update, f"Account `{md(args[0])}` nahi mila.")
        return
    STORE.remove_account(user.id, acc["id"])
    await reply(update, f"*{md(acc.get('label') or acc['id'])}* hata diya.")


async def set_groq_key_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    args = context.args or []
    if not args:
        await reply(update,
                    "*Groq API key set karo:*\n`/set_groq_key gsk_xxxxx`\n\n"
                    "Key: https://console.groq.com/keys\n"
                    "_Ye key aapke saare accounts ke quiz ke liye use hogi._")
        return
    key = args[0].strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    STORE.set_groq(user.id, key)
    await reply(update, "Groq key set ho gayi. Ab `/quiz` chala sakte ho.")
    await log_to_channel(context.application,
                         f"GROQ key set -- {user_display(user)} | len {len(key)}")


async def stop_cmd(update, context):
    user = update.effective_user
    job = JOBS.stop(user.id)
    if job is None:
        await reply(update, "Abhi koi run chalu nahi hai.")
        return
    await reply(update, "Stop signal bhej diya -- accounts safely ruk rahe hain...")


# --------------------------------------------------------------------------
# Commands --- info
# --------------------------------------------------------------------------
async def balance_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    accounts = STORE.list_accounts(user.id)
    if not accounts:
        await reply(update, "Koi account nahi hai. `/add_account 9876543210 Main`")
        return

    msg = await reply(update, "Balance check ho raha hai (sab accounts parallel)...")
    results = await probe_all(user.id, accounts)

    lines = ["*Coin balance*", ""]
    total = 0
    counted = 0
    for idx, res in enumerate(results, 1):
        label = md(str(res.get("label"))[:18])
        bal = res.get("balance")
        if res.get("ok") and isinstance(bal, (int, float)):
            total += int(bal)
            counted += 1
            lines.append(f"`{idx}.` *{label}* -- *{bal}* coins")
        else:
            why = md(res.get("error") or "unavailable")
            lines.append(f"`{idx}.` *{label}* -- _{why}_")

    lines.append("")
    lines.append(f"*Total ({counted} accounts):* {total} coins")
    await ThrottledEditor(msg, min_interval=0).update("\n".join(lines), force=True)


async def status_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    accounts = STORE.list_accounts(user.id)
    if not accounts:
        await reply(update, "Koi account nahi hai. `/add_account 9876543210 Main`")
        return

    msg = await reply(update, "Status check ho raha hai...")
    results = await probe_all(user.id, accounts)

    lines = ["*MiniPix status*", ""]
    for idx, res in enumerate(results, 1):
        label = md(str(res.get("label"))[:18])
        if not res.get("ok"):
            why = md(res.get("error") or "unavailable")
            lines.append(f"`{idx}.` *{label}* -- _{why}_")
            continue
        campaign = "ON" if res.get("campaign") else "OFF"
        used = res.get("cap_used")
        cap = res.get("cap_total")
        reached = "  *[CAP HIT]*" if (cap and used is not None and used >= cap) else ""
        lines.append(f"`{idx}.` *{label}*")
        lines.append(f"     coins: *{res.get('balance')}*  |  campaign: {campaign}")
        lines.append(f"     daily cap: `{used}/{cap}`{reached}")

    job = JOBS.get(user.id)
    if job is not None and job.finished_at is None:
        t = job.totals()
        lines.append("")
        lines.append(f"_Run chalu:_ {t['running']} active | {t['watches']} eps | +{t['coins']}c -- /stop")

    await ThrottledEditor(msg, min_interval=0).update("\n".join(lines), force=True)


async def series_list_cmd(update, context):
    user = update.effective_user
    STATS.touch(user.id)
    accounts = STORE.enabled_accounts(user.id)
    if not accounts:
        await reply(update, "Pehle account add karo: `/add_account 9876543210 Main`")
        return

    msg = await reply(update, "Series load ho rahi hain...")
    account = accounts[0]
    groq = STORE.get_groq(user.id)
    loop = asyncio.get_running_loop()

    def _load():
        session = MiniPixUserSession(account, groq)
        try:
            if not session.ensure_auth():
                return None, None
            counts = session.get_watch_counts_from_profile()
            return CATALOG.series_list(session, max_pages=5), counts
        finally:
            session.close()

    series, counts = await loop.run_in_executor(EXECUTOR, _load)
    editor = ThrottledEditor(msg, min_interval=0)

    if series is None:
        await editor.update("Token expire ho gaya. `/add_token` se dubara add karo.", force=True)
        return
    if not series:
        await editor.update("Koi series nahi mili.", force=True)
        return

    counts = counts or {}
    label = md(str(account.get("label") or "account 1"))
    lines = [f"*Series list* (top 25 of {len(series)})",
             f"_Slots {label} ke hisaab se_", ""]

    for i, s in enumerate(series[:25], 1):
        sid = s.get("_id") or s.get("id") or s.get("series_id")
        title = md(str(s.get("title") or s.get("name") or "?")[:28])
        n_eps = s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0
        try:
            slots = sum(
                max(0, MAX_WATCHES_PER_EP - counts.get((str(sid), str(e)), 0))
                for e in range(1, int(n_eps) + 1)
            )
        except Exception:
            slots = 0
        lines.append(f"`{i}.` *{title}*")
        lines.append(f"     `{sid}`")
        lines.append(f"     {n_eps} eps | {slots} slots left")

    lines.append("")
    lines.append("_Chalao:_ `/watch_one <series_id>`")
    await editor.update("\n".join(lines)[:4000], force=True)


# --------------------------------------------------------------------------
# Commands --- parallel runs (sab accounts ek saath)
# --------------------------------------------------------------------------
async def watch_all_cmd(update, context):
    user = update.effective_user
    limit, targets, _rest = parse_run_args(user.id, context.args)

    def bound(tg_uid, account, job):
        return worker_watch(tg_uid, account, job, stop_after=limit)

    await run_parallel_job(
        update, context,
        kind="watch",
        title="Smart Watch",
        worker=bound,
        args_desc=(f"stop @ {limit} eps" if limit else ""),
        targets=targets,
    )


async def watch_one_cmd(update, context):
    user = update.effective_user
    args = list(context.args or [])
    if not args:
        await reply(update,
                    "*Ek series chalao:*\n`/watch_one <series_id> [N]`\n\n"
                    "Series ID ke liye `/series_list` use karo.")
        return

    series_id = args[0].strip()
    limit, targets, _rest = parse_run_args(user.id, args[1:])

    def bound(tg_uid, account, job):
        return worker_watch(tg_uid, account, job, stop_after=limit, only_series=series_id)

    await run_parallel_job(
        update, context,
        kind="watch",
        title="Watch Series",
        worker=bound,
        args_desc=f"`{md(series_id[:14])}`",
        targets=targets,
    )


async def quiz_cmd(update, context):
    user = update.effective_user
    if not STORE.get_groq(user.id):
        await reply(update,
                    "*Groq API key nahi hai.*\n\n`/set_groq_key gsk_xxxxx`\n\n"
                    "Key yahan se lo: https://console.groq.com/keys")
        return

    limit, targets, _rest = parse_run_args(user.id, context.args)
    sessions = max(1, min(int(limit or 5), 20))

    def bound(tg_uid, account, job):
        return worker_quiz(tg_uid, account, job, sessions=sessions)

    await run_parallel_job(
        update, context,
        kind="quiz",
        title="Auto Quiz",
        worker=bound,
        args_desc=f"{sessions} sessions",
        targets=targets,
    )


# --------------------------------------------------------------------------
# Commands --- stats
# --------------------------------------------------------------------------
async def my_stats_cmd(update, context):
    user = update.effective_user
    data = STATS.get(user.id)
    accounts = STORE.list_accounts(user.id)
    enabled = [a for a in accounts if a.get("enabled") and a.get("token")]

    joined = md(str(data.get("joined_at") or "N/A")[:19])
    active = md(str(data.get("last_active") or "N/A")[:19])

    lines = [
        "*Your usage stats*", "",
        f"  Telegram ID: `{user.id}`",
        f"  Accounts: *{len(accounts)}* (enabled *{len(enabled)}*)",
        f"  Groq key: {'set' if STORE.get_groq(user.id) else 'not set'}",
        f"  Joined: `{joined}`",
        f"  Last active: `{active}`", "",
        f"  Episodes watched: *{data.get('total_watches', 0)}*",
        f"  Coins earned: *+{data.get('total_coins_earned', 0)}*",
        f"  Quiz sessions: *{data.get('quizzes_solved', 0)}*",
        f"  Series completed: *{data.get('series_done', 0)}*",
    ]

    if accounts:
        lines.append("")
        lines.append("*Per account (lifetime)*")
        for idx, acc in enumerate(accounts, 1):
            label = md(str(acc.get("label") or acc["id"])[:18])
            eps = int(acc.get("watches") or 0)
            qz = int(acc.get("quizzes") or 0)
            cn = int(acc.get("coins") or 0)
            lines.append(f"`{idx}.` {label} -- {eps} eps | {qz} quiz | +{cn}c")

    await reply(update, "\n".join(lines))


async def admin_stats_cmd(update, context):
    user = update.effective_user
    if ADMIN_IDS and user.id not in ADMIN_IDS:
        await reply(update, "Ye command sirf admin ke liye hai.")
        return

    snapshot = STATS.snapshot()
    total_users = len(snapshot)
    total_watches = sum(int(v.get("total_watches") or 0) for v in snapshot.values())
    total_coins = sum(int(v.get("total_coins_earned") or 0) for v in snapshot.values())
    total_quiz = sum(int(v.get("quizzes_solved") or 0) for v in snapshot.values())
    active_jobs = JOBS.active()

    lines = [
        "*GLOBAL STATS*", "",
        f"  Users: {total_users}",
        f"  Accounts: {STORE.total_accounts()}",
        f"  Episodes: {total_watches}",
        f"  Coins: +{total_coins}",
        f"  Quiz sessions: {total_quiz}",
        f"  Running jobs: {len(active_jobs)}",
        f"  Workers: {MAX_WORKERS} total | {PER_USER_PARALLEL} per user",
        f"  Catalog cache keys: {CATALOG.stats().get('keys')}", "",
        "*Top 10 users by episodes*",
    ]

    ranked = sorted(snapshot.items(),
                    key=lambda kv: int(kv[1].get("total_watches") or 0),
                    reverse=True)
    for i, (uid, d) in enumerate(ranked[:10], 1):
        lines.append(
            f"`{i}.` `{uid}` -- {int(d.get('total_watches') or 0)} eps | "
            f"+{int(d.get('total_coins_earned') or 0)}c | "
            f"{int(d.get('quizzes_solved') or 0)} quiz | "
            f"{int(d.get('accounts') or 0)} acc"
        )

    if active_jobs:
        lines.append("")
        lines.append("*Active runs*")
        for uid, job in list(active_jobs.items())[:10]:
            t = job.totals()
            lines.append(
                f"  `{uid}` {job.kind}: {t['running']}/{t['accounts']} acc | "
                f"{t['watches']} eps | {fmt_duration(job.elapsed())}"
            )

    await reply(update, "\n".join(lines)[:4000])


# --------------------------------------------------------------------------
# Free text + global error handler
# --------------------------------------------------------------------------
async def handle_text(update, context):
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    text = update.message.text.strip()

    if text.isdigit():
        if len(text) == 6 and STORE.get_pending_otp(user.id):
            context.args = [text]
            await otp_cmd(update, context)
            return
        if len(text) == 10:
            context.args = [text]
            await add_account_cmd(update, context)
            return

    await reply(update, "Command samajh nahi aaya. /help dekho.")


async def on_error(update, context):
    err = getattr(context, "error", None)
    print(f"[!] Handler error: {type(err).__name__}: {err}")
    try:
        traceback.print_exception(type(err), err, err.__traceback__, limit=5)
    except Exception:
        pass
    try:
        message = getattr(update, "effective_message", None)
        if message is not None:
            await message.reply_text("Kuch gadbad ho gayi. Dubara try karo ya /help dekho.")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Flask keep-alive (Railway / Render health checks)
# --------------------------------------------------------------------------
if Flask is not None:
    flask_app = Flask(__name__)

    @flask_app.route("/")
    def home():
        snapshot = STATS.snapshot()
        jobs = JOBS.active()
        return {
            "service": "MiniPix Automation Bot",
            "version": "2.0 (multi-user / multi-account)",
            "status": "running",
            "users": len(snapshot),
            "accounts": STORE.total_accounts(),
            "active_runs": len(jobs),
            "workers": {"total": MAX_WORKERS, "per_user": PER_USER_PARALLEL},
            "limits": {"max_accounts_per_user": MAX_ACCOUNTS_PER_USER},
            "time": now_iso(),
        }

    @flask_app.route("/health")
    def health():
        return {
            "ok": True,
            "users": len(STATS.snapshot()),
            "accounts": STORE.total_accounts(),
            "active_runs": len(JOBS.active()),
            "time": now_iso(),
        }
else:
    flask_app = None


# --------------------------------------------------------------------------
# Telegram application wiring
# --------------------------------------------------------------------------
def build_tg_app(token):
    builder = Application.builder().token(token)

    # Rate limiter is an optional extra (python-telegram-bot[rate-limiter]).
    if AIORateLimiter is not None:
        try:
            builder = builder.rate_limiter(AIORateLimiter())
        except Exception as exc:
            print(f"[i] Rate limiter unavailable: {exc}")

    try:
        builder = builder.concurrent_updates(True)
    except Exception:
        pass

    application = builder.build()

    handlers = [
        ("start", start_cmd),
        ("help", help_cmd),
        # account management
        ("add_account", add_account_cmd),
        ("login", add_account_cmd),
        ("otp", otp_cmd),
        ("add_token", add_token_cmd),
        ("set_token", add_token_cmd),
        ("accounts", accounts_cmd),
        ("rename", rename_cmd),
        ("enable", enable_cmd),
        ("disable", disable_cmd),
        ("remove_account", remove_account_cmd),
        ("set_groq_key", set_groq_key_cmd),
        # info
        ("balance", balance_cmd),
        ("status", status_cmd),
        ("series_list", series_list_cmd),
        ("my_stats", my_stats_cmd),
        ("admin_stats", admin_stats_cmd),
        # runs
        ("watch_all", watch_all_cmd),
        ("watch_one", watch_one_cmd),
        ("quiz", quiz_cmd),
        ("stop", stop_cmd),
    ]
    for name, fn in handlers:
        application.add_handler(CommandHandler(name, fn))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(on_error)
    return application


async def _run_telegram(application, webhook_url, port):
    """Correct PTB v20+ lifecycle: initialize -> start -> updater."""
    await application.initialize()
    await application.start()

    if webhook_url:
        url = webhook_url.rstrip("/") + "/telegram"
        print(f"[i] Webhook mode: {url}")
        await application.updater.start_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=url,
            drop_pending_updates=True,
        )
    else:
        print("[i] Polling mode started")
        await application.updater.start_polling(drop_pending_updates=True)

    # Block forever until cancelled.
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        try:
            await application.updater.stop()
        except Exception:
            pass
        await application.stop()
        await application.shutdown()


def _start_telegram_in_thread(application, webhook_url, port):
    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_telegram(application, webhook_url, port))
        except Exception as exc:
            print(f"[!] Telegram loop crashed: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    thread = threading.Thread(target=runner, name="telegram", daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def _banner():
    print("=" * 62)
    print("  MiniPix Automation Bot  v2.0")
    print("  multi-user  +  multi-account  +  parallel runs")
    print("=" * 62)
    print(f"  data dir            : {DATA_DIR}")
    print(f"  users loaded        : {len(STORE.snapshot())}")
    print(f"  accounts loaded     : {STORE.total_accounts()}")
    print(f"  worker threads      : {MAX_WORKERS}")
    print(f"  parallel per user   : {PER_USER_PARALLEL}")
    print(f"  max accounts / user : {MAX_ACCOUNTS_PER_USER}")
    print(f"  admins              : {sorted(ADMIN_IDS) if ADMIN_IDS else 'open'}")
    print("=" * 62)


def main():
    if Application is None:
        print("[X] python-telegram-bot missing. Install:")
        print("    pip install 'python-telegram-bot[rate-limiter]==21.*' requests flask")
        sys.exit(1)

    # FIXED: use BotConfig() instead of BotConfig.load()
    config = BotConfig()
    token = config.bot_token
    if not token:
        print("[X] BOT_TOKEN nahi mila. Environment variable set karo ya bot_config.json banao.")
        sys.exit(1)

    _banner()

    application = build_tg_app(token)

    port = _env_int("PORT", 8080)
    webhook_url = (os.environ.get("WEBHOOK_URL", "") or "").strip()
    if not webhook_url:
        static = (os.environ.get("RAILWAY_STATIC_URL", "") or "").strip()
        public = (os.environ.get("RAILWAY_PUBLIC_DOMAIN", "") or "").strip()
        if static:
            webhook_url = static if static.startswith("http") else "https://" + static
        elif public:
            webhook_url = "https://" + public

    # Webhook mode owns the port itself, so Flask only runs in polling mode.
    if webhook_url:
        thread = _start_telegram_in_thread(application, webhook_url, port)
        try:
            while thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    elif flask_app is not None:
        _start_telegram_in_thread(application, "", port)
        print(f"[i] Keep-alive server on 0.0.0.0:{port}")
        try:
            flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except KeyboardInterrupt:
            pass
    else:
        print("[i] Flask missing -- polling only")
        try:
            asyncio.run(_run_telegram(application, "", port))
        except KeyboardInterrupt:
            pass

    print("\n[i] Shutting down -- saving state...")
    try:
        STORE.save()
        STATS.save()
    except Exception as exc:
        print(f"[!] Save failed: {exc}")
    try:
        EXECUTOR.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    print("[i] Bye.")


if __name__ == "__main__":
    main()
