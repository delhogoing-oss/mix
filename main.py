#!/usr/bin/env python3
"""
MiniPix Unified Telegram Bot (SINGLE FILE)
Combines:
  • main.py        – Telegram bot framework + Groq Quiz Solver (BEST)
  • minipix_auto.py – Option 11: Browse ALL + SMART 8x REPEAT Auto-Watch (BEST)
Features:
  • Per-user Telegram isolation + busy lock
  • threading.Lock for shared JSON I/O
  • Login via OTP, interactive token, or /tokenlogin <token>
  • 8x reward (15→8→5→3→2→2→2→2 coins) with daily-cap-aware smart repeat
  • Groq AI per-user API key for auto quiz (default gpt-oss-120b)
  • Full login / activity logs to DATA_LOG_CHANNEL
  • App version 328 headers + updated API path compatibility
"""

import os
import sys
import json
import time
import re
import logging
import asyncio
import atexit
import threading
import hashlib
from datetime import date, datetime
from typing import Dict, Optional, List, Tuple, Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

_PY_313_PLUS = sys.version_info >= (3, 13)

if _PY_313_PLUS:
    import weakref as _weakref_mod
    _orig_weakref_ref = _weakref_mod.ref

    class _FallbackWeakref:
        __slots__ = ("_obj", "_cb")
        def __init__(self, obj, callback=None):
            self._obj = obj
            self._cb = callback
        def __call__(self):
            return self._obj
        def __hash__(self):
            return hash(id(self._obj))
        def __eq__(self, other):
            if isinstance(other, _FallbackWeakref):
                return self._obj is other._obj
            return NotImplemented

    class _MetaPatchedWeakrefRef(type):
        def __getitem__(cls, item):
            return cls
        def __instancecheck__(cls, instance):
            return isinstance(instance, (cls, _orig_weakref_ref, _FallbackWeakref))

    def _patched_weakref_ref_factory(cls, o, callback=None):
        if cls is _PatchedWeakrefRef:
            try:
                return _orig_weakref_ref(o, callback)
            except TypeError:
                inst = _FallbackWeakref.__new__(_PatchedWeakrefRef)
                _FallbackWeakref.__init__(inst, o, callback)
                return inst
        else:
            try:
                return _orig_weakref_ref.__new__(cls, o, callback)
            except TypeError:
                return object.__new__(cls)

    class _PatchedWeakrefRef(_FallbackWeakref, metaclass=_MetaPatchedWeakrefRef):
        def __new__(cls, o, callback=None):
            return _patched_weakref_ref_factory(cls, o, callback)
        def __init__(self, o, callback=None):
            if not isinstance(self, _FallbackWeakref):
                return
            if getattr(self, "_obj", None) is not None and self._obj is o:
                return
            _FallbackWeakref.__init__(self, o, callback)

    _weakref_mod.ref = _PatchedWeakrefRef

    class _PatchedWeakKeyDictionary(dict):
        def __init__(self, *args, **kwargs):
            super().__init__()
            if args or kwargs:
                self.update(*args, **kwargs)
        def __getitem__(self, key):
            return super().__getitem__(id(key))
        def __setitem__(self, key, value):
            super().__setitem__(id(key), value)
        def __delitem__(self, key):
            super().__delitem__(id(key))
        def __contains__(self, key):
            return super().__contains__(id(key))
        def get(self, key, default=None):
            return super().get(id(key), default)
        def pop(self, key, *args):
            return super().pop(id(key), *args)
        def setdefault(self, key, default=None):
            return super().setdefault(id(key), default)
        def update(self, other=None, **kwargs):
            if other is not None:
                if hasattr(other, "items"):
                    for k, v in other.items():
                        self[k] = v
                else:
                    for k, v in other:
                        self[k] = v
            for k, v in kwargs.items():
                self[k] = v

    _weakref_mod.WeakKeyDictionary = _PatchedWeakKeyDictionary
else:
    _weakref_mod = None

def _needs_slot_patch(cls):
    for _c in cls.__mro__:
        _cls_slots = getattr(_c, "__slots__", None)
        if isinstance(_cls_slots, (tuple, list)) and "__dict__" in _cls_slots:
            return False
    return True

_attr_cache: dict = {}

def _get_attr_cache(self):
    k = id(self)
    d = _attr_cache.get(k)
    if d is None:
        d = {}
        _attr_cache[k] = d
    return d

def _patch_cls_attrs(cls):
    if not _needs_slot_patch(cls):
        return
    if getattr(cls, "_ptb_slots_patched", False):
        return
    try:
        orig_init = cls.__init__
        orig_setattr = cls.__setattr__
        orig_getattr = getattr(cls, "__getattr__", None)

        def _patched_setattr(self, name, value):
            try:
                orig_setattr(self, name, value)
            except AttributeError:
                _get_attr_cache(self)[name] = value

        def _patched_getattr(self, name):
            if orig_getattr is not None:
                try:
                    return orig_getattr(self, name)
                except AttributeError:
                    pass
            d = _attr_cache.get(id(self))
            if d and name in d:
                return d[name]
            raise AttributeError(name)

        def _patched_init(self, *args, **kwargs):
            orig_init(self, *args, **kwargs)

        cls.__init__ = _patched_init
        cls.__setattr__ = _patched_setattr
        cls.__getattr__ = _patched_getattr
        cls._ptb_slots_patched = True
    except Exception:
        pass

if _PY_313_PLUS:
    _patched_any = False
    try:
        from telegram.ext import _updater as _updater_mod
        _patch_cls_attrs(_updater_mod.Updater)
        _patched_any = True
    except Exception as _e:
        print(f"WARNING: Updater patch skipped: {_e}", file=sys.stderr)

    try:
        from telegram.ext import _application as _application_mod
        _patch_cls_attrs(_application_mod.Application)
        _patched_any = True
    except Exception as _e:
        print(f"WARNING: Application patch skipped: {_e}", file=sys.stderr)

    try:
        from telegram.ext import _jobqueue as _jq_mod
        _patch_cls_attrs(_jq_mod.JobQueue)
        _patched_any = True
        try:
            _orig_set_app = _jq_mod.JobQueue.set_application
            def _patched_set_app(self, application):
                try:
                    _orig_set_app(self, application)
                except TypeError:
                    k = id(self)
                    if not hasattr(_jq_mod, "_fallback_app_refs"):
                        _jq_mod._fallback_app_refs = {}
                    _jq_mod._fallback_app_refs[k] = application
            _jq_mod.JobQueue.set_application = _patched_set_app

            _orig_get_app = _jq_mod.JobQueue._get_application
            def _patched_get_app(self):
                try:
                    return _orig_get_app(self)
                except Exception:
                    if hasattr(_jq_mod, "_fallback_app_refs"):
                        return _jq_mod._fallback_app_refs.get(id(self))
                    return None
            _jq_mod.JobQueue._get_application = _patched_get_app
        except Exception:
            pass
    except Exception as _e:
        print(f"WARNING: JobQueue patch skipped: {_e}", file=sys.stderr)

    try:
        from telegram.ext import _callbackqueryhandler as _cqh_mod
        _patch_cls_attrs(_cqh_mod.CallbackQueryHandler)
        _patched_any = True
    except Exception:
        pass

    if not _patched_any:
        print("INFO: PTB slots patches not needed on this version", file=sys.stderr)

# ───────────────────────── Config ─────────────────────────
API_BASE = "https://api.minipix.co/v4"
ACCOUNTS_FILE = "minipix_accounts.json"
USER_GROQ_FILE = "user_groq_keys.json"
LOCK_FILE = "bot.lock"

MAX_WATCHES_PER_EP = 8
REWARDS_BY_WATCH = {1: 15, 2: 8, 3: 5, 4: 3, 5: 2, 6: 2, 7: 2, 8: 2}
QUIZ_QUESTION_DELAY = 10

GLOBAL_GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GLOBAL_GROQ_API_KEY2 = os.environ.get("GROQ_API_KEY2", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")
DATA_LOG_CHANNEL = LOG_CHANNEL_ID

MONGO_URI = os.environ.get("MONGO_URI", "")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "minipix_bot")
MONGO_CONNECT_TIMEOUT_MS = int(os.environ.get("MONGO_CONNECT_TIMEOUT_MS", "15000"))
MONGO_CACHE_TIMEOUT_MS = int(os.environ.get("MONGO_CACHE_TIMEOUT_MS", "5000"))
MONGO_TLS_INSECURE = os.environ.get("MONGO_TLS_INSECURE", "1") == "1"
MAX_GROQ_KEYS_PER_USER = 5

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "allam-2-7b",
    "llama-3.2-90b-text-preview",
    "llama-3.2-11b-text-preview",
    "llama3-groq-70b-8192-tool-use-preview",
    "llama3-groq-8b-8192-tool-use-preview",
]

HEADERS_BASE = {
    "user-agent": "okhttp/4.12.0",
    "accept-encoding": "gzip",
    "x-app-version": "328",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

(WAIT_PHONE, WAIT_OTP, WAIT_TOKEN, WAIT_QUIZ_SESSIONS) = range(4)

_accounts_lock = threading.Lock()
_groq_lock = threading.Lock()
_busy_locks: Dict[int, threading.Lock] = {}
_busy_lock_guard = threading.Lock()

_mongo_client = None
_mongo_db = None
_mongo_lock = threading.Lock()
_mongo_warned = False


def _get_mongo() -> Tuple[Any, Any]:
    global _mongo_client, _mongo_db, _mongo_warned
    if not MONGO_URI:
        return None, None
    if _mongo_client is not None and _mongo_db is not None:
        return _mongo_client, _mongo_db
    with _mongo_lock:
        if _mongo_client is not None and _mongo_db is not None:
            return _mongo_client, _mongo_db
        last_err = None
        attempts = []

        try:
            import certifi
            _ca_file = certifi.where()
        except Exception:
            _ca_file = None

        base_kwargs = {
            "connectTimeoutMS": MONGO_CONNECT_TIMEOUT_MS,
            "socketTimeoutMS": MONGO_CONNECT_TIMEOUT_MS,
            "serverSelectionTimeoutMS": MONGO_CONNECT_TIMEOUT_MS,
        }

        attempts.append(dict(base_kwargs))

        if _ca_file:
            attempts.append(dict(base_kwargs, tlsCAFile=_ca_file))

        if MONGO_TLS_INSECURE:
            attempts.append(dict(base_kwargs, tlsAllowInvalidCertificates=True, tlsAllowInvalidHostnames=True))
            if _ca_file:
                attempts.append(dict(base_kwargs, tlsCAFile=_ca_file, tlsAllowInvalidCertificates=True, tlsAllowInvalidHostnames=True))

        try:
            from pymongo import MongoClient
        except Exception as e:
            last_err = e
            attempts = []

        for kwargs in attempts:
            try:
                _mongo_client = MongoClient(MONGO_URI, **kwargs)
                _mongo_client.admin.command("ping")
                _mongo_db = _mongo_client[MONGO_DB_NAME]
                logger.info("MongoDB connected successfully" + (" (TLS insecure fallback)" if kwargs.get("tlsAllowInvalidCertificates") else ""))
                return _mongo_client, _mongo_db
            except Exception as e:
                last_err = e
                try:
                    if _mongo_client is not None:
                        _mongo_client.close()
                except Exception:
                    pass
                _mongo_client = None
                continue

        if not _mongo_warned:
            err_short = str(last_err) if last_err else "Unknown"
            if len(err_short) > 500:
                err_short = err_short[:500] + "..."
            logger.warning(f"MongoDB connection failed, using JSON fallback: {err_short}")
            logger.warning("Quick fix options:\n"
                           "  1) MongoDB Atlas -> Network Access -> Add IP: 0.0.0.0/0 (Allow All)\n"
                           "  2) MONGO_URI me user:pass correctly fill karo (special chars URL-encoded)\n"
                           "  3) .env me MONGO_TLS_INSECURE=1 already set hai, IP whitelist check karo\n"
                           "  4) Ya phir MONGO_URI= blank rakho -> JSON files use honge")
            _mongo_warned = True
        _mongo_client = None
        _mongo_db = None
        return None, None


def _mongo_accounts_col():
    _, db = _get_mongo()
    if db is None:
        return None
    try:
        col = db["accounts"]
        try:
            col.create_index("label", unique=True)
        except Exception:
            pass
        return col
    except Exception:
        return None


def _mongo_keys_col():
    _, db = _get_mongo()
    if db is None:
        return None
    try:
        col = db["groq_keys"]
        try:
            col.create_index("telegram_user_id", unique=True)
        except Exception:
            pass
        return col
    except Exception:
        return None


_mongo_cache_index_done = False


def _mongo_cache_col():
    global _mongo_cache_index_done
    _, db = _get_mongo()
    if db is None:
        return None
    try:
        col = db["quiz_cache"]
        if not _mongo_cache_index_done:
            with _mongo_lock:
                if not _mongo_cache_index_done:
                    try:
                        col.create_index("qhash", unique=True)
                    except Exception:
                        pass
                    _mongo_cache_index_done = True
        return col
    except Exception:
        return None


def _normalize_text(s: str) -> str:
    t = (s or "").lower().strip()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _quiz_cache_key(question: str, options: List[str]) -> str:
    q_norm = _normalize_text(question)
    opts_sorted = sorted(_normalize_text(o) for o in (options or []))
    raw = q_norm + "||" + "||".join(opts_sorted)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _expected_reward(nth_watch):
    return REWARDS_BY_WATCH.get(int(nth_watch) if nth_watch else 1, 0)


def get_user_busy_lock(user_id: int) -> threading.Lock:
    with _busy_lock_guard:
        if user_id not in _busy_locks:
            _busy_locks[user_id] = threading.Lock()
        return _busy_locks[user_id]


# ───────────────────── Lock file ─────────────────────
def acquire_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        print("Another bot instance is running. Exiting.")
        sys.exit(1)

    def remove_lock():
        try:
            os.unlink(LOCK_FILE)
        except Exception:
            pass

    atexit.register(remove_lock)


# ───────────────────── Log Channel ─────────────────────
def send_log_sync(text: str):
    if not LOG_CHANNEL_ID or not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": LOG_CHANNEL_ID,
                "text": text[:4090],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=12,
        )
    except Exception as e:
        logger.warning(f"Log channel error: {e}")


# ───────────────────── User Groq Keys ─────────────────────
def load_user_groq_keys() -> dict:
    base = {}
    if os.path.exists(USER_GROQ_FILE):
        try:
            with _groq_lock:
                with open(USER_GROQ_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        base = data
        except Exception:
            base = {}
    try:
        col = _mongo_keys_col()
        if col is not None:
            for doc in col.find({}, max_time_ms=MONGO_CONNECT_TIMEOUT_MS):
                uid = doc.get("telegram_user_id")
                keys = doc.get("keys")
                if uid and isinstance(keys, (list, str)):
                    base[str(uid)] = keys
    except Exception:
        pass
    return base


def save_user_groq_keys(data: dict):
    try:
        with _groq_lock:
            with open(USER_GROQ_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save groq keys: {e}")
    try:
        col = _mongo_keys_col()
        if col is not None:
            now_iso = datetime.utcnow().isoformat()
            for uid, keys in (data or {}).items():
                if isinstance(keys, list):
                    keys_list = [k for k in keys if isinstance(k, str) and k]
                elif isinstance(keys, str) and keys:
                    keys_list = [keys]
                else:
                    continue
                try:
                    col.replace_one(
                        {"telegram_user_id": str(uid)},
                        {"telegram_user_id": str(uid), "keys": keys_list, "updated_at": now_iso},
                        upsert=True,
                    )
                except Exception:
                    continue
    except Exception:
        pass


def _save_groq_keys_for_user(user_id: str, keys_data):
    if not isinstance(user_id, str):
        user_id = str(user_id)
    user_groq_keys[user_id] = keys_data
    save_user_groq_keys(user_groq_keys)
    try:
        col = _mongo_keys_col()
        if col is not None:
            if isinstance(keys_data, list):
                keys_list = [k for k in keys_data if isinstance(k, str) and k]
            elif isinstance(keys_data, str) and keys_data:
                keys_list = [keys_data]
            else:
                keys_list = []
            if keys_list:
                col.replace_one(
                    {"telegram_user_id": user_id},
                    {"telegram_user_id": user_id, "keys": keys_list, "updated_at": datetime.utcnow().isoformat()},
                    upsert=True,
                )
            else:
                col.delete_one({"telegram_user_id": user_id})
    except Exception:
        pass


def _ensure_groq_loaded():
    pass


user_groq_keys: dict = load_user_groq_keys()


def get_user_groq_key(user_id: int, key_index: int = 0) -> Optional[str]:
    keys = user_groq_keys.get(str(user_id))
    if isinstance(keys, list):
        keys_list = [k for k in keys if k]
        if keys_list:
            safe_idx = (key_index % len(keys_list)) if keys_list else 0
            if 0 <= safe_idx < len(keys_list):
                return keys_list[safe_idx]
            return keys_list[0]
    elif isinstance(keys, str) and keys:
        return keys
    global_keys = [k for k in [GLOBAL_GROQ_API_KEY, GLOBAL_GROQ_API_KEY2] if k]
    if global_keys:
        safe_idx = (key_index % len(global_keys)) if global_keys else 0
        if 0 <= safe_idx < len(global_keys):
            return global_keys[safe_idx]
        return global_keys[0]
    return None


def get_user_groq_key_count(user_id: int) -> int:
    keys = user_groq_keys.get(str(user_id))
    if isinstance(keys, list):
        c = len([k for k in keys if k])
        if c > 0:
            return c
    elif isinstance(keys, str) and keys:
        return 1
    return len([k for k in [GLOBAL_GROQ_API_KEY, GLOBAL_GROQ_API_KEY2] if k])


# ───────────────────── MiniPix Core (UNIFIED) ─────────────────────
class MiniPixV2:
    def __init__(self):
        self.access_token = None
        self.user_id = None
        self.profile_id = None
        self.phone = None
        self.session = requests.Session()
        self.session.headers.update(HEADERS_BASE)
        self.device_id = "65969f0b7041fabc"
        self.device_info = "Xiaomi"
        self.watch_history = {}
        self.watch_history_raw = []
        self.runtime_watch_counts = {}
        self.last_profile = {}
        self.current_account_label = None
        self.accounts = self._load_accounts()

    def _load_accounts(self):
        base = {}
        candidates = [ACCOUNTS_FILE]
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            candidates.append(os.path.join(script_dir, ACCOUNTS_FILE))
        except Exception:
            pass
        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                with _accounts_lock:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            loaded = (
                                data.get("accounts", {})
                                if isinstance(data.get("accounts"), dict)
                                else data
                            )
                            if isinstance(loaded, dict):
                                for k, v in loaded.items():
                                    if isinstance(v, dict) and v.get("access_token"):
                                        base[k] = {
                                            "access_token": v.get("access_token", ""),
                                            "user_id": v.get("user_id") or v.get("uid") or v.get("_id"),
                                            "profile_id": v.get("profile_id") or v.get("master_profile") or v.get("pid"),
                                            "phone": v.get("phone") or v.get("mobile"),
                                            "added_on": v.get("added_on") or date.today().isoformat(),
                                        }
            except Exception:
                pass
        try:
            col = _mongo_accounts_col()
            if col is not None:
                for doc in col.find({}, max_time_ms=MONGO_CONNECT_TIMEOUT_MS):
                    lbl = doc.get("label")
                    if not lbl:
                        continue
                    entry = {
                        "access_token": doc.get("access_token", ""),
                        "user_id": doc.get("user_id"),
                        "profile_id": doc.get("profile_id"),
                        "phone": doc.get("phone"),
                        "added_on": doc.get("added_on") or date.today().isoformat(),
                    }
                    if entry["access_token"]:
                        base[lbl] = entry
        except Exception:
            pass
        return base

    def _save_accounts(self):
        payload = {"accounts": self.accounts, "saved_at": date.today().isoformat()}
        ok_json = False
        try:
            with _accounts_lock:
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                ok_json = True
        except Exception:
            ok_json = False
        try:
            col = _mongo_accounts_col()
            if col is not None:
                bot_id = (TELEGRAM_BOT_TOKEN[:12]) if TELEGRAM_BOT_TOKEN else None
                now_iso = datetime.utcnow().isoformat()
                for label, acc in (self.accounts or {}).items():
                    if not isinstance(acc, dict):
                        continue
                    doc = {
                        "label": label,
                        "access_token": acc.get("access_token", ""),
                        "user_id": acc.get("user_id"),
                        "profile_id": acc.get("profile_id"),
                        "phone": acc.get("phone"),
                        "added_on": acc.get("added_on") or date.today().isoformat(),
                        "last_updated": now_iso,
                        "bot_id": bot_id,
                    }
                    if not doc["access_token"]:
                        continue
                    try:
                        col.replace_one({"label": label}, doc, upsert=True)
                    except Exception:
                        continue
        except Exception:
            pass
        return ok_json

    def _store_current_account(self, label=None):
        if not (self.access_token and self.user_id):
            return False
        lbl = (
            label
            or self.phone
            or self.current_account_label
            or f"acc_{str(self.user_id)[-6:]}"
        )
        self.current_account_label = lbl
        self.accounts[lbl] = {
            "access_token": self.access_token,
            "user_id": self.user_id,
            "profile_id": self.profile_id,
            "phone": self.phone,
            "added_on": date.today().isoformat(),
        }
        return self._save_accounts()

    def list_accounts(self):
        return list(self.accounts.keys())

    def switch_account(self, label):
        if label not in self.accounts:
            return False, f"Account '{label}' not found"
        acc = self.accounts[label]
        token = acc.get("access_token")
        if not token:
            return False, "No token"
        self._reset_state()
        ok = self.login_with_token(
            token,
            user_id=acc.get("user_id"),
            profile_id=acc.get("profile_id"),
            label=label,
            phone=acc.get("phone"),
        )
        if ok:
            send_log_sync(
                f"🔄 SWITCH ACCOUNT | User <code>{label}</code>\n"
                f"Phone: {self.phone or '?'}\n"
                f"Balance: {self.get_balance()}"
            )
            return True, f"Switched to {label}"
        return False, "Invalid/expired token ya API unavailable"

    def remove_account(self, label):
        if label not in self.accounts:
            return False
        del self.accounts[label]
        self._save_accounts()
        try:
            col = _mongo_accounts_col()
            if col is not None:
                col.delete_one({"label": label})
        except Exception:
            pass
        if self.current_account_label == label:
            self._reset_state()
        return True

    def _reset_state(self):
        self.access_token = None
        self.user_id = None
        self.profile_id = None
        self.phone = None
        self.current_account_label = None
        self.watch_history = {}
        self.watch_history_raw = []
        self.runtime_watch_counts = {}
        self.last_profile = {}
        if "authorization" in self.session.headers:
            del self.session.headers["authorization"]

    def _req(self, method, path, **kwargs):
        url = f"{API_BASE}{path}"
        try:
            r = self.session.request(method, url, timeout=30, **kwargs)
            try:
                data = r.json()
            except Exception:
                data = r.text
            return r.status_code, data
        except Exception as e:
            return 0, str(e)

    # ───────── Login
    def login_otp_generate(self, phone):
        self.phone = phone
        payload = {"phone_number": phone}
        sc, data = self._req(
            "POST",
            "/login/generate-otp",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        send_log_sync(
            f"📡 OTP generate response:\n"
            f"Status: {sc}\n"
            f"Data: {json.dumps(data, ensure_ascii=False)[:500]}"
        )
        if sc == 200 and isinstance(data, dict):
            if data.get("message") == "OTP sent" or data.get("success"):
                return data.get("session_token") or data.get("sessionToken")
            else:
                error_msg = data.get("message") or data.get("error") or "Unknown error"
                send_log_sync(f"❌ OTP generation failed: {error_msg}")
        else:
            send_log_sync(f"❌ OTP generation HTTP {sc}: {str(data)[:200]}")
        return None

    def login_otp_verify(self, session_token, otp, save_label=None):
        payload = {
            "client_id": "android",
            "device_id": self.device_id,
            "device_info": self.device_info,
            "otp": otp,
            "phone_number": self.phone,
            "session_token": session_token,
        }
        sc, data = self._req(
            "POST",
            "/login/verify-otp",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict) and data.get("access_token"):
            self.access_token = data["access_token"]
            self.user_id = data.get("id") or data.get("_id")
            self.session.headers["authorization"] = f"Bearer {self.access_token}"
            self.get_user()
            self._store_current_account(save_label)
            ref_code = (
                data.get("referralCode")
                or data.get("referral_code")
                or (isinstance(data.get("user"), dict) and data["user"].get("referralCode"))
            )
            ref_by = (
                data.get("referredBy")
                or data.get("referred_by")
                or (isinstance(data.get("user"), dict) and data["user"].get("referredBy"))
            )
            source = data.get("source")
            send_log_sync(
                f"✅ OTP LOGIN SUCCESS\n"
                f"User ID: <code>{self.user_id}</code>\n"
                f"Phone: {self.phone or '?'}\n"
                f"referralCode: {ref_code or '-'}\n"
                f"referredBy: {ref_by or '-'}\n"
                f"source: {source or '-'}\n"
                f"Balance: {self.get_balance()}\n"
                f"<pre>{json.dumps(data, ensure_ascii=False)[:600]}</pre>"
            )
            return True
        send_log_sync(
            f"❌ OTP verify failed: {sc} {json.dumps(data, ensure_ascii=False)[:300]}"
        )
        return False

    @staticmethod
    def _decode_jwt_payload(token):
        if not token or not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        rem = len(payload_b64) % 4
        if rem:
            payload_b64 += "=" * (4 - rem)
        try:
            import base64
            raw = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
            obj = json.loads(raw.decode("utf-8"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        try:
            import base64
            raw = base64.b64decode(payload_b64.encode("utf-8"))
            obj = json.loads(raw.decode("utf-8"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        return None

    def login_with_token(self, token, user_id=None, profile_id=None, label=None, phone=None):
        if not token:
            return False
        raw = None
        sc = 0
        jwt = self._decode_jwt_payload(token)
        if not user_id and isinstance(jwt, dict):
            user_id = (
                jwt.get("userId")
                or jwt.get("uid")
                or jwt.get("sub")
                or jwt.get("user_id")
                or jwt.get("_id")
                or jwt.get("id")
            )
        if not profile_id and isinstance(jwt, dict):
            profile_id = jwt.get("masterProfile") or jwt.get("master_profile") or jwt.get("pid")
        if not phone and isinstance(jwt, dict):
            phone = jwt.get("mobile") or jwt.get("phone")

        self.access_token = token
        self.session.headers["authorization"] = f"Bearer {self.access_token}"

        if not user_id:
            sc, raw = self._req("GET", "/users/me")
            if sc == 200 and isinstance(raw, dict):
                user_id = raw.get("_id") or raw.get("id") or raw.get("userId")
                if not profile_id:
                    profile_id = raw.get("master_profile") or raw.get("masterProfile")
                if not phone:
                    phone = raw.get("mobile") or raw.get("phone")

        if not user_id:
            self._reset_state()
            return False

        self.user_id = user_id
        if profile_id:
            self.profile_id = profile_id
        if phone and not self.phone:
            self.phone = phone

        if not self.get_user():
            self._reset_state()
            return False
        self._store_current_account(label)
        ref_code = None
        ref_by = None
        source = None
        if isinstance(raw, dict):
            ref_code = raw.get("referralCode") or raw.get("referral_code")
            ref_by = raw.get("referredBy") or raw.get("referred_by")
            source = raw.get("source") or raw.get("signupSource")
        send_log_sync(
            f"✅ TOKEN LOGIN SUCCESS\n"
            f"User ID: <code>{self.user_id}</code>\n"
            f"Phone: {self.phone or '?'}\n"
            f"referralCode: {ref_code or '-'}\n"
            f"referredBy: {ref_by or '-'}\n"
            f"source: {source or '-'}\n"
            f"Balance: {self.get_balance()}"
        )
        return True

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
        sc, data = self._req(
            "PATCH",
            f"/users/{self.user_id}/profiles/{self.profile_id}/open_app",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        return sc == 200 and isinstance(data, dict) and data.get("success")

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

    def get_balance_silent(self):
        return self.get_balance()

    def get_campaign_status(self):
        sc, data = self._req("GET", "/watch-campaign/status")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            cap = data.get("dailyVideoCap", {}) or {}
            return {
                "enabled": data.get("enabled", False),
                "cap": cap.get("cap", 0),
                "used": cap.get("used", 0),
                "reached": cap.get("reached", False),
                "blockWatching": cap.get("blockWatching", False),
            }
        return {
            "enabled": False,
            "cap": 0,
            "used": 0,
            "reached": False,
            "blockWatching": False,
        }

    # ───────── Series / Episodes discovery (from minipix_auto Option 11)
    def _collect_series_deep(self, obj, out_dict):
        if obj is None:
            return
        if isinstance(obj, dict):
            if (obj.get("_id") or obj.get("id") or obj.get("series_id")) and (
                obj.get("title")
                or obj.get("numberOfEpisodes") is not None
                or obj.get("totalEpisodes") is not None
                or obj.get("watchLadderEnabled") is not None
                or obj.get("cardImage")
                or obj.get("hindiTitle")
            ):
                sid = obj.get("_id") or obj.get("id") or obj.get("series_id")
                if sid and sid not in out_dict:
                    out_dict[sid] = obj
            for v in obj.values():
                self._collect_series_deep(v, out_dict)
        elif isinstance(obj, list):
            for item in obj:
                self._collect_series_deep(item, out_dict)

    def get_all_series(self, page_size=100, max_pages=20):
        found = {}
        tried = 0
        home_urls = [
            ("GET", "/short_search?page=home", False),
        ]
        for method, tmpl, _p in home_urls:
            tried += 1
            try:
                sc, data = self._req(method, tmpl)
            except Exception:
                sc, data = 0, None
            if sc == 200 and isinstance(data, (dict, list)):
                self._collect_series_deep(data, found)
        endpoints = [
            ("GET", "/webseries?page={p}&pageSize={ps}", True),
            ("GET", "/discover?type=webseries&page={p}&pageSize={ps}", True),
            ("GET", "/home?page={p}&pageSize={ps}", False),
            ("GET", "/discover/webseries?page={p}&pageSize={ps}", True),
            ("GET", "/series?page={p}&pageSize={ps}", True),
            ("GET", "/content?type=webseries&page={p}&pageSize={ps}", True),
        ]
        for method, tmpl, is_series_list in endpoints:
            for page in range(1, max_pages + 1):
                url = tmpl.format(p=page, ps=page_size)
                tried += 1
                try:
                    sc, data = self._req(method, url)
                except Exception:
                    continue
                if not (sc == 200 and isinstance(data, dict)):
                    continue
                self._collect_series_deep(data, found)
                series_candidates = []
                for k in (
                    "webseries",
                    "series",
                    "data",
                    "items",
                    "results",
                    "contents",
                    "list",
                ):
                    if isinstance(data.get(k), list):
                        series_candidates.extend(data[k])
                inner = None
                if isinstance(data.get("data"), dict):
                    inner = data["data"]
                elif isinstance(data.get("response"), dict):
                    inner = data["response"]
                if inner:
                    for k in (
                        "webseries",
                        "series",
                        "items",
                        "results",
                        "contents",
                        "list",
                        "data",
                    ):
                        if isinstance(inner.get(k), list):
                            series_candidates.extend(inner[k])
                if not series_candidates:
                    if (
                        is_series_list
                        and isinstance(data, dict)
                        and (data.get("_id") or data.get("title"))
                    ):
                        series_candidates = [data]
                if not series_candidates:
                    break
                for s in series_candidates:
                    if not isinstance(s, dict):
                        continue
                    sid = s.get("_id") or s.get("id") or s.get("series_id")
                    if not sid:
                        continue
                    if sid not in found:
                        found[sid] = s
                total_reported = data.get("total") or (inner or {}).get("total") or 0
                if total_reported and len(found) >= int(total_reported):
                    break
                if len(series_candidates) < int(page_size * 0.5):
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
        sc, data = self._req(
            "GET",
            f"/episodes?series_id={series_id}&page={page}&pageSize={page_size}",
        )
        if sc == 200 and isinstance(data, dict):
            return data.get("episodes", []), data.get("total", 0)
        return [], 0

    def get_profile(self):
        if not (self.user_id and self.profile_id):
            return None
        sc, data = self._req(
            "GET", f"/users/{self.user_id}/profiles/{self.profile_id}"
        )
        if sc == 200 and isinstance(data, dict):
            profile = data.get("profile", {}) or {}
            self.last_profile = profile
            history = (
                profile.get("watchHistory", [])
                or profile.get("watched", [])
                or []
            )
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
                    self.watch_history[key] = {
                        "watchedPct": cur_pct,
                        "time": cur_time,
                    }
            return profile
        return None

    def get_watched_set_from_profile(self):
        watched = set()
        counts = self.get_watch_counts_from_profile()
        for k, c in counts.items():
            if c >= 1:
                watched.add(k)
        return watched

    def get_watch_counts_from_profile(self):
        counts = {}
        raw_history = []
        try:
            self.get_profile()
        except Exception:
            pass
        profile = getattr(self, "last_profile", None) or {}
        if isinstance(profile, dict):
            watched_list = (
                profile.get("watched") or profile.get("watchHistory") or []
            )
            if isinstance(watched_list, list):
                raw_history = watched_list
        if isinstance(getattr(self, "watch_history_raw", None), list):
            raw_history = raw_history + self.watch_history_raw
        for item in raw_history:
            if not isinstance(item, dict):
                continue
            sid = item.get("id") or item.get("series_id")
            ep = item.get("episodeNo") or item.get("episode_no")
            pct = int(item.get("watchedPct") or item.get("progress") or 0)
            if sid and ep and pct >= 80:
                k = (str(sid), str(ep))
                counts[k] = counts.get(k, 0) + 1
        if isinstance(self.watch_history, dict):
            for (sid, ep_no), info in self.watch_history.items():
                pct = (
                    int(info.get("watchedPct") or 0)
                    if isinstance(info, dict)
                    else 0
                )
                if pct >= 80:
                    k = (str(sid), str(ep_no))
                    if counts.get(k, 0) < 1:
                        counts[k] = max(counts.get(k, 0), 1)
        runtime = getattr(self, "runtime_watch_counts", None)
        if isinstance(runtime, dict):
            for k, c in runtime.items():
                counts[k] = max(counts.get(k, 0), c)
        return counts

    # ───────── Unlock episode (from minipix_auto)
    def unlock_episode(self, series_id, ep_id, ep_no, playback_url=None):
        if not (series_id and ep_id):
            return False
        ok = False
        unlock_candidates = [
            (
                "POST",
                f"/episodes/{ep_id}/unlock",
                {"series_id": series_id, "episodeNo": ep_no, "campaign": False},
            ),
            (
                "POST",
                "/episodes/unlock",
                {
                    "series_id": series_id,
                    "episode_id": ep_id,
                    "episodeNo": ep_no,
                    "campaign": False,
                },
            ),
            (
                "POST",
                f"/webseries/{series_id}/episodes/{ep_no}/unlock",
                {"campaign": False},
            ),
            (
                "POST",
                "/coins/unlock-episode",
                {
                    "series_id": series_id,
                    "episode_id": ep_id,
                    "episodeNo": ep_no,
                },
            ),
            (
                "POST",
                "/watch-campaign/unlock",
                {"seriesId": series_id, "episode_id": ep_id, "campaign": False},
            ),
        ]
        for method, path, body in unlock_candidates:
            try:
                sc, d = self._req(
                    method,
                    path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
                if sc and sc < 500 and isinstance(d, dict):
                    if d.get("success") is True:
                        ok = True
                        break
                    if sc == 200 and d.get("unlocked"):
                        ok = True
                        break
                    if sc == 200 and "success" not in d:
                        ok = True
                        break
            except Exception:
                continue
        return ok

    # ───────── Watch progress + coin report (from minipix_auto Option 11)
    def _update_watch_progress(
        self,
        series_id,
        series_title,
        hindi_title,
        episode_no,
        tc_in_ms,
        tc_out_ms,
        detail_image,
        watched_pct,
        campaign=False,
    ):
        if not (self.user_id and self.profile_id):
            return False
        try:
            watched_pct = int(watched_pct or 0)
        except Exception:
            watched_pct = 0
        if not tc_in_ms:
            tc_in_ms = 0
        if not tc_out_ms or tc_out_ms <= tc_in_ms:
            tc_out_ms = tc_in_ms + 60000
        duration = tc_out_ms - tc_in_ms
        if watched_pct >= 100:
            current_time_ms = tc_out_ms
        else:
            current_time_ms = int(tc_in_ms + (duration * watched_pct / 100))
        if watched_pct == 99:
            stored_pct = 99
        elif watched_pct >= 100:
            stored_pct = 100
        else:
            stored_pct = watched_pct
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
        ok1 = False
        try:
            payload_patch = {"watched": watch_obj}
            sc1, d1 = self._req(
                "PATCH",
                f"/users/{self.user_id}/profiles/{self.profile_id}",
                headers={"content-type": "application/json; charset=utf-8"},
                data=json.dumps(payload_patch, ensure_ascii=False).encode("utf-8"),
            )
            ok1 = sc1 == 200 and isinstance(d1, dict) and d1.get("success")
        except Exception:
            pass

        ok2 = False
        try:
            for path in (
                f"/users/{self.user_id}/profiles/{self.profile_id}/watch-history/update",
                "/watch-history/update",
                f"/profiles/{self.profile_id}/watch-history/update",
            ):
                payload_wh = {"watched": watch_obj, "campaign": False}
                sc2, d2 = self._req(
                    "POST",
                    path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(payload_wh, ensure_ascii=False).encode("utf-8"),
                )
                if sc2 and sc2 < 500:
                    if isinstance(d2, dict) and d2.get("success"):
                        ok2 = True
                        break
                    if sc2 == 200:
                        ok2 = True
                        break
        except Exception:
            pass
        return ok1 or ok2

    def _report_watch_progress_to_coins(
        self, series_id, episode_no, watched_pct, series_title=""
    ):
        if not (self.user_id and self.profile_id):
            return False
        try:
            watched_pct = int(watched_pct or 0)
        except Exception:
            watched_pct = 0
        ep_str = str(episode_no)
        bodies = [
            {
                "series_id": series_id,
                "episode_no": episode_no,
                "episodeNo": episode_no,
                "progress": watched_pct,
                "watchedPct": watched_pct,
                "campaign": False,
                "task_type": "watch_ladder",
            },
            {
                "type": "watch_ladder",
                "seriesId": series_id,
                "episode": ep_str,
                "watched": watched_pct,
                "campaign": False,
            },
            {
                "task_id": f"watch_ladder_{series_id}",
                "progress_delta": 1,
                "series_id": series_id,
                "episode_no": episode_no,
                "campaign": False,
            },
        ]
        endpoints = [
            ("POST", "/coins/progress-report", bodies[0]),
            ("POST", "/coins/tasks/progress", bodies[0]),
            ("POST", f"/coins/tasks/watch_ladder_{series_id}/progress", bodies[0]),
            ("POST", "/coins/watch-progress", bodies[1]),
            ("POST", "/coins/report-watched", bodies[1]),
            ("POST", "/coins/tasks/update", bodies[2]),
            ("POST", "/watch-ladder/progress", bodies[0]),
        ]
        any_ok = False
        for method, path, body in endpoints:
            try:
                sc, d = self._req(
                    method,
                    path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
                if sc and sc < 500 and isinstance(d, dict):
                    if d.get("success") is True:
                        any_ok = True
                        break
                    if sc == 200 and "success" not in d:
                        any_ok = True
                        break
            except Exception:
                continue
        return any_ok

    def _start_task_for_series(self, series_id):
        task_id = f"watch_ladder_{series_id}"
        candidates = [
            (
                "POST",
                f"/coins/tasks/{task_id}/start",
                {"series_id": series_id, "campaign": False},
            ),
            (
                "POST",
                "/coins/tasks/start",
                {"task_id": task_id, "series_id": series_id, "campaign": False},
            ),
            ("POST", "/watch-ladder/start", {"series_id": series_id, "campaign": False}),
            (
                "POST",
                "/coins/start-task",
                {"task_id": task_id, "campaign": False},
            ),
            ("POST", f"/coins/tasks/watch_ladder_{series_id}/resume", {}),
        ]
        any_ok = False
        for method, path, body in candidates:
            try:
                sc, d = self._req(
                    method,
                    path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
                )
                if sc and sc < 500:
                    if isinstance(d, dict) and d.get("success") is True:
                        any_ok = True
                        break
                    if sc == 200:
                        any_ok = True
                        break
            except Exception:
                pass
        return any_ok

    def watch_campaign_select_series(self, series_id):
        payload = {
            "seriesId": series_id,
            "series_id": series_id,
            "campaign": False,
        }
        candidates = [
            ("POST", "/watch-campaign/session/start", payload),
            ("POST", "/watch-campaign/start", payload),
            ("POST", "/watch-campaign/select-series", payload),
            ("POST", "/watch-campaign/select", payload),
            ("PUT", "/watch-campaign/select", payload),
            ("PATCH", "/watch-campaign/select", payload),
            ("POST", "/watch-ladder/start-session", payload),
            ("POST", "/campaigns/watch/start", {"series_id": series_id}),
        ]
        confirmed = False
        for method, path, body in candidates:
            try:
                sc, data = self._req(
                    method,
                    path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
                if sc and sc < 500 and isinstance(data, dict):
                    if data.get("success"):
                        confirmed = True
                        break
                    if sc == 200:
                        confirmed = True
                        break
            except Exception:
                pass
        if confirmed:
            try:
                self._start_task_for_series(series_id)
            except Exception:
                pass
        else:
            try:
                self._start_task_for_series(series_id)
            except Exception:
                pass
        return True

    def claim_reward_task(self, task_id=None, series_id=None):
        if not task_id and series_id:
            task_id = f"watch_ladder_{series_id}"
        if series_id:
            try:
                self._report_watch_progress_to_coins(series_id, 0, 100)
            except Exception:
                pass
        candidates = []
        if task_id:
            candidates.append(("POST", f"/coins/tasks/{task_id}/claim", None))
            candidates.append(
                ("POST", "/coins/tasks/claim", {"task_id": task_id, "campaign": False})
            )
            candidates.append(("POST", f"/coins/tasks/{task_id}/reward", None))
            candidates.append(("POST", f"/coins/tasks/{task_id}/complete", {}))
            candidates.append(
                ("POST", "/coins/tasks/complete", {"task_id": task_id, "campaign": False})
            )
        if series_id:
            candidates.append(
                ("POST", f"/watch-ladder/claim", {"series_id": series_id, "campaign": False})
            )
            candidates.append(("POST", f"/coins/watch-ladder/{series_id}/claim", None))
            candidates.append(
                (
                    "POST",
                    f"/coins/claim",
                    {"series_id": series_id, "type": "watch_ladder", "campaign": False},
                )
            )
            candidates.append(
                (
                    "POST",
                    "/coins/redeem",
                    {"series_id": series_id, "task": "watch_ladder", "campaign": False},
                )
            )
            candidates.append(
                ("POST", f"/watch-ladder/{series_id}/complete", {"campaign": False})
            )
        any_ok = False
        last_msg = ""
        for entry in candidates:
            method, path = entry[0], entry[1]
            body = entry[2] if len(entry) > 2 else None
            try:
                sc, data = self._req(
                    method,
                    path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
                )
                if sc and sc < 500 and isinstance(data, dict):
                    if data.get("success") is True:
                        any_ok = True
                        break
                    if data.get("success") is False:
                        last_msg = (
                            data.get("message")
                            or data.get("error")
                            or str(data)[:80]
                        )
                    if sc == 200 and "success" not in data:
                        continue
                elif sc and sc < 500 and isinstance(data, str):
                    if sc == 200 and len(data or "") > 0:
                        any_ok = True
                        break
            except Exception:
                continue
        return any_ok

    def refresh_tasks_and_balance(self):
        try:
            self.get_balance()
        except Exception:
            pass
        sc, data = self._req("GET", "/coins/tasks?all=1")
        updated = []
        if sc == 200 and isinstance(data, dict):
            tasks = data.get("tasks") or data.get("data") or []
            if isinstance(tasks, dict):
                tasks = tasks.get("tasks") or []
            for t in tasks if isinstance(tasks, list) else []:
                if isinstance(t, dict) and t.get("task_type") == "watch_ladder":
                    updated.append(t)
        return updated

    # ───────── Watch episode (UPGRADED from minipix_auto Option 11)
    def watch_episode(
        self,
        episode,
        series_info,
        delay_multiplier=0.0,
        campaign=False,
        min_watch_pct=80,
        allow_repeat=False,
        nth_watch=None,
    ):
        if not isinstance(series_info, dict):
            return False, "invalid_series"
        if not isinstance(episode, dict):
            return False, "invalid_episode"
        series_id = (
            series_info.get("_id")
            or series_info.get("id")
            or series_info.get("series_id")
        )
        if not series_id:
            return False, "no_series_id"
        ep_no = (
            episode.get("episodeNo")
            or episode.get("episode_no")
            or episode.get("number")
            or 0
        )
        series_title = series_info.get("title") or ""
        hindi_title = series_info.get("hindiTitle") or series_title
        detail_image = (
            series_info.get("cardImage") or series_info.get("longVerticalImage") or ""
        )
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
        tc_in_ms = tc_in * 1000
        tc_out_ms = tc_out * 1000
        dur_sec = tc_out - tc_in
        history_key = (series_id, ep_no)
        current_pct = int(
            self.watch_history.get(history_key, {}).get("watchedPct", 0) or 0
        )
        if not allow_repeat and current_pct >= min_watch_pct:
            return True, "skip"

        if not episode.get("coinUnlocked", True):
            ep_id = episode.get("_id") or episode.get("id")
            url = episode.get("playbackUrl")
            try:
                self.unlock_episode(series_id, ep_id, ep_no, playback_url=url)
            except Exception:
                pass

        progress_steps = [1, 50, 80, 99, 100, 100]
        any_fail = False
        reported_coin_progress = False
        for pct in progress_steps:
            if not allow_repeat and pct < current_pct:
                continue
            ok = self._update_watch_progress(
                series_id,
                series_title,
                hindi_title,
                ep_no,
                tc_in_ms,
                tc_out_ms,
                detail_image,
                pct,
                campaign=False,
            )
            if not ok:
                any_fail = True
            if pct >= 80 and not reported_coin_progress:
                try:
                    self._report_watch_progress_to_coins(
                        series_id, ep_no, pct, series_title
                    )
                    reported_coin_progress = True
                except Exception:
                    pass
            if delay_multiplier > 0:
                try:
                    idx_cur = progress_steps.index(pct)
                    prev_pct = progress_steps[idx_cur - 1] if idx_cur > 0 else 0
                    delta = pct - prev_pct
                    delay = dur_sec * delay_multiplier * delta / 100
                    if delay > 0:
                        time.sleep(min(delay, 2))
                except Exception:
                    time.sleep(0.15)
            else:
                time.sleep(0.15)
        if not reported_coin_progress:
            try:
                self._report_watch_progress_to_coins(series_id, ep_no, 100, series_title)
            except Exception:
                pass
        try:
            self.claim_reward_task(series_id=series_id, task_id=None)
        except Exception:
            pass
        time.sleep(0.5)
        self.watch_history[history_key] = {"watchedPct": 100, "time": tc_out_ms}
        rk = (str(series_id), str(ep_no))
        self.runtime_watch_counts[rk] = self.runtime_watch_counts.get(rk, 0) + 1
        self.watch_history_raw.append(
            {
                "id": series_id,
                "series_id": series_id,
                "episodeNo": ep_no,
                "episode_no": ep_no,
                "watchedPct": 100,
                "progress": 100,
                "time": tc_out_ms,
            }
        )
        return True, "done"

    # ─────────────────── OPTION 11: Browse ALL + SMART 8x REPEAT Watch (BEST)
    def browse_and_watch_all_smart_repeat(
        self,
        progress_callback=None,
        max_watches=250,
        telegram_user_id=None,
    ):
        def log(msg):
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass
            if any(
                x in msg
                for x in [
                    "→",
                    "finished",
                    "Campaign",
                    "Fetching",
                    "Soft limit",
                    "▶️",
                    "🏁",
                    "📊",
                    "🛑",
                ]
            ):
                send_log_sync(
                    f"<b>🎬 WATCH (Opt11)</b> | User <code>{telegram_user_id}</code>\n{msg[:1800]}"
                )

        log(
            f"🌐 Option 11 mode: Browse ALL + SMART 8x REPEAT\n"
            f"Rewards/ep: 1=+15 | 2=+8 | 3=+5 | 4=+3 | 5-8=+2 each (total +39/ep)\n"
            f"Checking campaign..."
        )
        cap_status = self.get_campaign_status()
        daily_used = cap_status.get("used", 0)
        daily_cap = cap_status.get("cap", 0)
        log(
            f"Campaign: {'ON' if cap_status['enabled'] else 'OFF'} | "
            f"{cap_status['used']}/{cap_status['cap']} "
            f"{'[REACHED]' if cap_status['reached'] else ''}"
        )

        log("Fetching all series (multi-endpoint)...")
        all_series = self.get_all_series()
        if not all_series:
            return {"error": "No series found"}

        try:
            self.get_profile()
        except Exception:
            pass
        watch_counts = self.get_watch_counts_from_profile()

        def _ep_key(e):
            n = e.get("episodeNo")
            try:
                return int(n)
            except Exception:
                try:
                    return float(n)
                except Exception:
                    return 0

        total_watched_all = 0
        total_skip_all = 0
        total_fail_all = 0
        balance_before = self.get_balance_silent()

        def _check_daily_cap(local_watched):
            if daily_cap and daily_cap > 0:
                total_today = daily_used + local_watched
                if (
                    total_today >= daily_cap
                    or cap_status.get("reached")
                    or cap_status.get("blockWatching")
                ):
                    log(
                        f"🛑 DAILY CAP REACHED! ({daily_used}+{local_watched} >= cap {daily_cap}) Stop."
                    )
                    return True
            return False

        log(f"Series found: {len(all_series)}. Smart-repeat mode = ALL series, 8x each ep.")

        for idx, s in enumerate(all_series, 1):
            if max_watches is not None and total_watched_all >= max_watches:
                log(f"🛑 Soft limit reached ({max_watches} watches total).")
                break
            if _check_daily_cap(total_watched_all):
                break

            sid = s.get("_id") or s.get("id") or s.get("series_id")
            if not sid:
                continue
            title = s.get("title") or s.get("name") or "(no title)"
            n_total = int(s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0)

            log(f"=== Series {idx}/{len(all_series)}: {title} (id={sid}) ===")

            try:
                real_info = self.get_series(sid) or s
                real_sid = (
                    (real_info or {}).get("_id")
                    or (real_info or {}).get("id")
                    or sid
                )
                episodes, _ = self.get_episodes(
                    real_sid, page=1, page_size=max(200, n_total or 500)
                )
                episodes_sorted = (
                    sorted(episodes, key=_ep_key) if episodes else []
                )
            except Exception:
                real_info = s
                real_sid = sid
                episodes_sorted = []

            if not episodes_sorted:
                log("  ⚠️ No episodes — skip series.")
                continue

            try:
                self.watch_campaign_select_series(real_sid)
            except Exception:
                pass
            try:
                self._start_task_for_series(real_sid)
            except Exception:
                pass

            series_done = series_skip = series_fail = 0
            any_series_progress = True
            loop_count = 0
            while any_series_progress:
                loop_count += 1
                any_series_progress = False
                if loop_count > MAX_WATCHES_PER_EP + 1:
                    break
                for ep in episodes_sorted:
                    if max_watches is not None and total_watched_all >= max_watches:
                        break
                    if _check_daily_cap(total_watched_all):
                        break
                    ep_no = (
                        ep.get("episodeNo") or ep.get("episode_no") or 0
                    )
                    key_pair = (str(real_sid), str(ep_no))
                    cur_count = watch_counts.get(key_pair, 0)
                    if cur_count >= MAX_WATCHES_PER_EP:
                        continue
                    nth = cur_count + 1
                    try:
                        ok, status = self.watch_episode(
                            ep,
                            real_info or s,
                            allow_repeat=True,
                            nth_watch=nth,
                        )
                    except Exception:
                        ok, status = False, "exception"
                    if status == "skip":
                        series_skip += 1
                    elif ok:
                        series_done += 1
                        total_watched_all += 1
                        watch_counts[key_pair] = nth
                        any_series_progress = True
                    else:
                        series_fail += 1

            total_skip_all += series_skip
            total_fail_all += series_fail
            log(
                f"  [{title}] → Done: {series_done} | Skip: {series_skip} | Fail: {series_fail}"
            )

            try:
                self.claim_reward_task(series_id=real_sid)
            except Exception:
                pass
            try:
                refreshed_counts = self.get_watch_counts_from_profile()
                for k, c in refreshed_counts.items():
                    if c > watch_counts.get(k, 0):
                        watch_counts[k] = c
            except Exception:
                pass

        bal_end = self.get_balance_silent()
        delta = None
        if balance_before is not None and bal_end is not None:
            delta = bal_end - balance_before

        summary = (
            f"<b>🏁 WATCH (Opt11) FINISHED</b>\n"
            f"User: <code>{telegram_user_id}</code>\n"
            f"Watched: {total_watched_all} | Skipped: {total_skip_all} | Failed: {total_fail_all}\n"
        )
        if delta is not None:
            summary += (
                f"Balance: {balance_before} → {bal_end} ({delta:+d})"
            )
        send_log_sync(summary)

        return {
            "watched": total_watched_all,
            "skipped": total_skip_all,
            "failed": total_fail_all,
            "balance_before": balance_before,
            "balance_after": bal_end,
            "delta": delta,
        }

    # ─────────────────── PER-SERIES Smart Watch (user requested specific series)
    def get_series_list_paginated(self, page=1, per_page=10):
        all_series = self.get_all_series() or []
        total = len(all_series)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(int(page or 1), total_pages))
        start = (page - 1) * per_page
        return (
            all_series[start : start + per_page],
            total,
            total_pages,
            page,
        )

    def get_series_detail(self, series_id):
        info = self.get_series(series_id)
        eps, total = self.get_episodes(series_id, page=1, page_size=500)
        try:
            self.get_profile()
        except Exception:
            pass
        counts = self.get_watch_counts_from_profile()
        ep_status = []
        for ep in eps:
            ep_no = str(ep.get("episodeNo") or ep.get("episode_no") or 0)
            key = (str(series_id), ep_no)
            c = counts.get(key, 0)
            ep_status.append(
                {
                    "episode": ep,
                    "watches": c,
                    "maxed": c >= MAX_WATCHES_PER_EP,
                }
            )
        return {
            "series": info,
            "episodes": eps,
            "ep_status": ep_status,
            "total_episodes": total or len(eps),
            "watch_counts": counts,
        }

    def watch_single_series_smart_repeat(
        self,
        series_id,
        progress_callback=None,
        max_watches=None,
        telegram_user_id=None,
    ):
        def log(msg):
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass
            if any(
                x in msg
                for x in [
                    "→",
                    "finished",
                    "Fetching",
                    "Soft limit",
                    "▶️",
                    "🏁",
                    "🛑",
                    "📊",
                ]
            ):
                send_log_sync(
                    f"<b>🎬 WATCH (Series)</b> | User <code>{telegram_user_id}</code> | sid=<code>{series_id}</code>\n{msg[:1800]}"
                )

        log(f"🎯 Specific series mode: id={series_id}\nRewards/ep: 15→8→5→3→2→2→2→2 (total +39/ep)")
        cap_status = self.get_campaign_status()
        daily_used = cap_status.get("used", 0)
        daily_cap = cap_status.get("cap", 0)
        log(
            f"Campaign: {'ON' if cap_status['enabled'] else 'OFF'} | "
            f"{daily_used}/{daily_cap} {'[REACHED]' if cap_status['reached'] else ''}"
        )

        real_info = self.get_series(series_id) or {}
        real_sid = (
            (real_info or {}).get("_id")
            or (real_info or {}).get("id")
            or series_id
        )
        title = (real_info or {}).get("title") or (real_info or {}).get("name") or f"Series {real_sid}"
        log(f"Fetching episodes for: {title}")
        try:
            self.get_profile()
        except Exception:
            pass
        watch_counts = self.get_watch_counts_from_profile()
        eps, _ = self.get_episodes(real_sid, page=1, page_size=500)

        def _ep_key(e):
            n = e.get("episodeNo")
            try:
                return int(n)
            except Exception:
                try:
                    return float(n)
                except Exception:
                    return 0

        episodes_sorted = sorted(eps, key=_ep_key) if eps else []
        if not episodes_sorted:
            return {"error": "Series me koi episodes nahi mile."}

        try:
            self.watch_campaign_select_series(real_sid)
        except Exception:
            pass
        try:
            self._start_task_for_series(real_sid)
        except Exception:
            pass

        balance_before = self.get_balance_silent()
        total_watched_all = total_skip = total_fail = 0
        max_allowed = (
            max_watches
            if max_watches is not None
            else MAX_WATCHES_PER_EP * max(1, len(episodes_sorted))
        )

        def _check_daily_cap(local):
            if daily_cap and daily_cap > 0:
                if (
                    daily_used + local >= daily_cap
                    or cap_status.get("reached")
                    or cap_status.get("blockWatching")
                ):
                    log(
                        f"🛑 DAILY CAP REACHED! ({daily_used}+{local} >= {daily_cap}) Stop."
                    )
                    return True
            return False

        any_series_progress = True
        loop_count = 0
        while any_series_progress:
            loop_count += 1
            any_series_progress = False
            if loop_count > MAX_WATCHES_PER_EP + 1:
                break
            for ep in episodes_sorted:
                if total_watched_all >= max_allowed:
                    log(f"🛑 Soft limit ({max_allowed} watches).")
                    break
                if _check_daily_cap(total_watched_all):
                    break
                ep_no = ep.get("episodeNo") or ep.get("episode_no") or 0
                key_pair = (str(real_sid), str(ep_no))
                cur_count = watch_counts.get(key_pair, 0)
                if cur_count >= MAX_WATCHES_PER_EP:
                    continue
                nth = cur_count + 1
                try:
                    ok, status = self.watch_episode(
                        ep,
                        real_info or {},
                        allow_repeat=True,
                        nth_watch=nth,
                    )
                except Exception:
                    ok, status = False, "exception"
                if status == "skip":
                    total_skip += 1
                elif ok:
                    total_watched_all += 1
                    watch_counts[key_pair] = nth
                    any_series_progress = True
                else:
                    total_fail += 1

        try:
            self.claim_reward_task(series_id=real_sid)
        except Exception:
            pass
        bal_end = self.get_balance_silent()
        delta = None
        if balance_before is not None and bal_end is not None:
            delta = bal_end - balance_before

        summary = (
            f"<b>🏁 WATCH (Series) FINISHED</b>\n"
            f"User: <code>{telegram_user_id}</code>\n"
            f"Series: {title} (<code>{real_sid}</code>)\n"
            f"Watched: {total_watched_all} | Skip: {total_skip} | Fail: {total_fail}\n"
        )
        if delta is not None:
            summary += f"Balance: {balance_before} → {bal_end} ({delta:+d})"
        send_log_sync(summary)

        return {
            "series_id": real_sid,
            "series_title": title,
            "watched": total_watched_all,
            "skipped": total_skip,
            "failed": total_fail,
            "balance_before": balance_before,
            "balance_after": bal_end,
            "delta": delta,
        }

    def watch_one_episode(
        self,
        series_id,
        episode_no,
        progress_callback=None,
        telegram_user_id=None,
    ):
        def log(msg):
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass
            send_log_sync(
                f"<b>🎬 WATCH (1 Ep)</b> | User <code>{telegram_user_id}</code> | S{series_id} E{episode_no}\n{msg[:1800]}"
            )

        log(f"▶️ Single ep: S{series_id} E{episode_no}")
        cap_status = self.get_campaign_status()
        real_info = self.get_series(series_id) or {}
        real_sid = (
            (real_info or {}).get("_id")
            or (real_info or {}).get("id")
            or series_id
        )
        eps, _ = self.get_episodes(real_sid, page=1, page_size=500)
        target = None
        for e in eps:
            en = str(e.get("episodeNo") or e.get("episode_no") or "")
            if en == str(episode_no):
                target = e
                break
        if not target:
            return {"error": f"Episode {episode_no} series {series_id} me nahi mila."}
        try:
            self.watch_campaign_select_series(real_sid)
        except Exception:
            pass
        try:
            self._start_task_for_series(real_sid)
        except Exception:
            pass
        try:
            self.get_profile()
        except Exception:
            pass
        counts = self.get_watch_counts_from_profile()
        key_pair = (str(real_sid), str(episode_no))
        cur_count = counts.get(key_pair, 0)
        if cur_count >= MAX_WATCHES_PER_EP:
            return {
                "error": f"Episode already {MAX_WATCHES_PER_EP}x watched. No more reward."
            }
        nth = cur_count + 1
        bal_before = self.get_balance_silent()
        try:
            ok, status = self.watch_episode(
                target, real_info or {}, allow_repeat=True, nth_watch=nth
            )
        except Exception as ee:
            ok, status = False, f"exception: {ee}"
        try:
            self.claim_reward_task(series_id=real_sid)
        except Exception:
            pass
        bal_end = self.get_balance_silent()
        delta = None
        if bal_before is not None and bal_end is not None:
            delta = bal_end - bal_before
        reward_label = _expected_reward(nth)
        log(
            f"Result: ok={ok} status={status} nth={nth} (+{reward_label} expected) delta={delta}"
        )
        return {
            "ok": ok,
            "status": status,
            "nth_watch": nth,
            "reward_expected": reward_label,
            "balance_before": bal_before,
            "balance_after": bal_end,
            "delta": delta,
        }

    # ─────────────────── QUIZ (from main.py – GROQ BEST)
    def get_quiz_status(self):
        try:
            sc, data = self._req("GET", "/quiz/status")
            if sc == 200 and isinstance(data, dict) and data.get("success"):
                return data
        except Exception:
            pass
        try:
            sc_c, data_c = self._req("GET", "/coins/tasks?all=1")
            if sc_c == 200 and isinstance(data_c, dict):
                merged = {"success": True, "tasks": data_c.get("tasks", [])}
                daily = {}
                tasks = data_c.get("tasks") or []
                if isinstance(tasks, list):
                    for t in tasks:
                        if isinstance(t, dict) and t.get("task_type") == "quiz":
                            period = t.get("period")
                            if period == "daily":
                                info = t.get("info") or {}
                                if isinstance(info, dict):
                                    daily = info
                                    break
                merged["dailyAttempts"] = daily or {"exhausted": False}
                return merged
        except Exception:
            pass
        return {"success": True, "dailyAttempts": {"exhausted": False}}

    def quiz_start_session(self):
        sc, data = self._req(
            "POST",
            "/quiz/session/start",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps({}).encode("utf-8"),
        )
        send_log_sync(
            f"📡 quiz/session/start response:\n"
            f"Status: {sc}\n"
            f"Data: {json.dumps(data, ensure_ascii=False)[:500]}"
        )
        if sc == 200 and isinstance(data, dict):
            if data.get("success") is True or data.get("status") == "success":
                session_obj = data.get("session") or {}
                question_obj = data.get("question")
                sid = (
                    session_obj.get("sessionId")
                    or data.get("sessionId")
                    or data.get("_id")
                )
                if not question_obj:
                    question_obj = (
                        data.get("data", {}).get("question")
                        or data.get("next", {}).get("question")
                    )
                if sid and question_obj:
                    return sid, question_obj, session_obj
                else:
                    send_log_sync(
                        f"⚠️ Missing sessionId or question in response.\n"
                        f"sid={sid}, question_obj={question_obj is not None}"
                    )
            else:
                send_log_sync(
                    f"❌ Quiz start returned success=False: {data.get('message', data)}"
                )
        else:
            send_log_sync(f"❌ Quiz start HTTP {sc}: {str(data)[:300]}")
        return None, None, None

    def quiz_submit_answer(self, session_id, question_id, chosen_index):
        payload = {
            "sessionId": session_id,
            "questionId": question_id,
            "chosenIndex": chosen_index,
        }
        sc, data = self._req(
            "POST",
            "/quiz/session/answer",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict):
            return data
        return None

    def quiz_use_lifeline(self, session_id, question_id):
        payload = {"sessionId": session_id, "questionId": question_id}
        sc, data = self._req(
            "POST",
            "/quiz/session/lifeline",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data.get("removedOptions", [])
        return None

    def quiz_ad_ack(self, session_id):
        payload = {"sessionId": session_id}
        sc, data = self._req(
            "POST",
            "/quiz/session/ad-ack",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data.get("question")
        return None

    def _build_quiz_prompt(self, question, options):
        prompt = (
            "Solve this multiple-choice question. "
            "Return ONLY the integer index of the correct option (0, 1, 2, ...). "
            "No explanation, no extra words, just a single digit integer.\n\n"
            f"Question (both Hindi and English provided):\n"
            f"{question}\n\n"
            "Options:\n"
        )
        for i, opt in enumerate(options):
            prompt += f"  {i}: {opt}\n"
        prompt += "\nCorrect option index (integer only): "
        return prompt

    def _parse_quiz_answer(self, answer_text, options):
        if not answer_text:
            return None
        t = answer_text.strip()
        m = re.search(r"\b(\d+)\b", t)
        if m:
            idx = int(m.group(1))
            if 0 <= idx < len(options):
                return idx
        for i, opt in enumerate(options):
            opt_clean = str(opt).strip().lower()
            if opt_clean and opt_clean in t.lower():
                return i
        m2 = re.search(r"option\s*(\d+)", t, flags=re.IGNORECASE)
        if m2:
            idx = int(m2.group(1))
            if 1 <= idx <= len(options):
                return idx - 1
        return None

    def ask_groq(self, question, options, telegram_user_id: int = None, key_index: int = 0):
        qhash = _quiz_cache_key(question, options)
        try:
            col = _mongo_cache_col()
            if col is not None:
                doc = None
                try:
                    doc = col.find_one({"qhash": qhash}, max_time_ms=MONGO_CACHE_TIMEOUT_MS)
                except Exception:
                    doc = None
                if doc is not None:
                    idx = doc.get("correct_index")
                    if isinstance(idx, int) and 0 <= idx < len(options):
                        try:
                            col.update_one({"qhash": qhash}, {"$inc": {"hits": 1}})
                        except Exception:
                            pass
                        model_tag = doc.get("model_used", "cached") or "cached"
                        tag = f"[CACHE] {model_tag}"
                        correct_text = doc.get("correct_text", "") or (options[idx] if idx < len(options) else "")
                        return idx, tag, correct_text
        except Exception:
            pass

        api_key = (
            get_user_groq_key(telegram_user_id, key_index)
            if telegram_user_id
            else (GLOBAL_GROQ_API_KEY2 if key_index == 1 and GLOBAL_GROQ_API_KEY2 else GLOBAL_GROQ_API_KEY)
        )
        if not api_key:
            send_log_sync(f"❌ No Groq key for user <code>{telegram_user_id}</code> (idx={key_index})")
            return None, None, None

        prompt = self._build_quiz_prompt(question, options)
        n_opts = len(options)
        systems = [
            (
                "You are a smart quiz solver. "
                "Read the question and options carefully. "
                "Reply with ONLY a single integer number (0, 1, 2 or 3). "
                "Do not write any explanation or extra text."
            ),
            (
                f"Return ONLY the index of the correct option. "
                f"There are exactly {n_opts} options (0 to {n_opts-1}). "
                "Reply with a single integer, nothing else."
            ),
            (
                "Choose the correct option index. "
                f"Possible answers: 0-{n_opts-1}. Reply just the number."
            ),
        ]

        def _cache_write(idx_i, tag_i, raw_i):
            try:
                col_cw = _mongo_cache_col()
                if col_cw is None:
                    return
                if idx_i is None or not (0 <= idx_i < len(options)):
                    return
                correct_text_i = options[idx_i] if idx_i < len(options) else ""
                doc_cw = {
                    "qhash": qhash,
                    "question": question,
                    "options": list(options or []),
                    "correct_index": idx_i,
                    "correct_text": correct_text_i,
                    "model_used": tag_i or "",
                    "solved_at": datetime.utcnow().isoformat(),
                    "hits": 0,
                }
                try:
                    col_cw.update_one({"qhash": qhash}, {"$setOnInsert": doc_cw}, upsert=True)
                except Exception:
                    pass
            except Exception:
                pass

        for model in GROQ_MODELS:
            for sys_i, sys_msg in enumerate(systems):
                try:
                    from groq import Groq

                    client = Groq(api_key=api_key, timeout=20.0, max_retries=0)
                    completion = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": sys_msg},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.0,
                        max_tokens=20,
                        timeout=15.0,
                    )
                    answer_text = (completion.choices[0].message.content or "").strip()
                    idx = self._parse_quiz_answer(answer_text, options)
                    if idx is not None:
                        tag = f"{model}" if sys_i == 0 else f"{model}/s{sys_i+1}"
                        _cache_write(idx, tag, answer_text)
                        return idx, tag, answer_text
                except Exception as e:
                    err = str(e).lower()
                    if "rate" in err or "limit" in err or "quota" in err or "429" in err:
                        send_log_sync(f"⏳ Model {model} rate‑limited, trying next.")
                        break
                    logger.warning(f"Model {model} attempt {sys_i+1} failed: {e}")
                    continue

        http_models = [m for m in GROQ_MODELS if "/" not in m][:6] or GROQ_MODELS[:6]
        for model in http_models:
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": f"Reply with ONLY one number between 0 and {n_opts-1}. Nothing else.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.0,
                        "max_tokens": 10,
                    },
                    timeout=15,
                )
                if r.status_code == 200:
                    try:
                        answer_text = r.json()["choices"][0]["message"]["content"].strip()
                        idx = self._parse_quiz_answer(answer_text, options)
                        if idx is not None:
                            tag = f"http:{model}"
                            _cache_write(idx, tag, answer_text)
                            return idx, tag, answer_text
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"HTTP model {model} fallback error: {e}")
                continue

        return None, None, None

    def run_quiz_auto(
        self,
        max_sessions=3,
        question_delay=QUIZ_QUESTION_DELAY,
        progress_callback=None,
        telegram_user_id=None,
    ):
        def log(msg):
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass

        if not self.user_id:
            if not self.get_user():
                return {"error": "User not authenticated. Re-login required."}

        status = self.get_quiz_status()
        if not status:
            return {"error": "Could not fetch quiz status"}
        daily = status.get("dailyAttempts", {}) or {}
        if daily.get("exhausted"):
            return {"error": "Daily quiz attempts exhausted"}

        total_coins = 0
        sessions_done = 0
        debug_lines = []
        failed_attempts = 0
        total_question_count = 0
        key_count = get_user_groq_key_count(telegram_user_id) if telegram_user_id else len([k for k in [GLOBAL_GROQ_API_KEY, GLOBAL_GROQ_API_KEY2] if k])

        send_log_sync(
            f"🧠 QUIZ STARTED | User <code>{telegram_user_id}</code> | Sessions: {max_sessions} | Groq Keys: {key_count}"
        )

        for session_num in range(1, max_sessions + 1):
            log(f"--- Session {session_num}/{max_sessions} ---")

            session_id, question_obj, session_meta = None, None, None
            for attempt in range(2):
                session_id, question_obj, session_meta = self.quiz_start_session()
                if session_id and question_obj:
                    break
                if attempt == 0:
                    log("⚠️ Session start failed, retrying in 3s...")
                    time.sleep(3)

            if not session_id or not question_obj:
                log("❌ Failed to start session after retry")
                send_log_sync(
                    f"❌ Session start failed | User <code>{telegram_user_id}</code>"
                )
                failed_attempts += 1
                if failed_attempts >= 2:
                    log("Aborting: too many failed attempts to start session.")
                    break
                time.sleep(3)
                continue

            hearts = session_meta.get("hearts", 3) if session_meta else 3

            if hearts == 0:
                log(f"💔 Session has 0 hearts – cannot continue.")
                failed_attempts += 1
                if failed_attempts >= 2:
                    log("Aborting: repeated dead sessions.")
                    break
                time.sleep(5)
                continue

            failed_attempts = 0
            ad_every = session_meta.get("adGateEvery", 5) if session_meta else 5
            q_count = 0
            session_coins = 0
            correct_count = 0
            wrong_count = 0

            while True:
                if hearts <= 0:
                    log("No hearts left")
                    break
                if not question_obj or not isinstance(question_obj, dict):
                    break

                q_id = question_obj.get("questionId")
                q_text_hi = question_obj.get("questionHi") or ""
                q_text_en = question_obj.get("questionEn") or ""
                options = question_obj.get("options", [])
                q_idx = question_obj.get("index", q_count)
                q_total = question_obj.get("total", "?")

                q_count += 1
                total_question_count += 1
                key_index = 0 if key_count <= 1 else ((total_question_count - 1) % key_count)
                combined = q_text_hi
                if q_text_en and q_text_en != q_text_hi:
                    combined = (
                        f"{q_text_hi}\n[EN: {q_text_en}]" if q_text_hi else q_text_en
                    )

                if not q_id or len(options) < 2:
                    send_log_sync(
                        f"⚠️ Invalid question: q_id={q_id}, options={len(options)}"
                    )
                    break

                correct_index, model_used, raw_answer = self.ask_groq(
                    combined, options, telegram_user_id=telegram_user_id, key_index=key_index
                )
                if correct_index is None:
                    send_log_sync(
                        f"❌ All models failed for Q{q_idx+1}/{q_total} (id={q_id}). Skipping submit, hearts -=1."
                    )
                    log(
                        f"⚠️  Q{q_idx+1}: All models failed → -1 heart."
                    )
                    wrong_count += 1
                    hearts -= 1
                    debug_lines.append(
                        f"Q{q_idx+1}/{q_total}: ❌ SKIP | Model: ALL FAILED | Raw: N/A | Chose: SKIP | -1 heart"
                    )
                    if len(debug_lines) > 15:
                        debug_lines.pop(0)
                    continue

                correct_index = max(0, min(correct_index, len(options) - 1))
                chosen_text = options[correct_index]

                time.sleep(question_delay)

                result = self.quiz_submit_answer(
                    session_id, q_id, correct_index
                )
                if not result:
                    break

                if result.get("success"):
                    correct_flag = result.get("correct", False)
                    coins_earned = int(result.get("coinsEarned") or 0)
                    session_coins = result.get("coinsSoFar", 0)
                    hearts = int(result.get("hearts", hearts))
                    total_coins += coins_earned
                    if correct_flag:
                        correct_count += 1
                    else:
                        wrong_count += 1

                    status_emoji = "✅" if correct_flag else "❌"
                    correct_idx_server = result.get("correctIndex")
                    correct_server_text = ""
                    if correct_idx_server is not None:
                        try:
                            correct_server_text = f" (Correct: {correct_idx_server} '{options[correct_idx_server]}')"
                        except Exception:
                            pass
                    debug_line = (
                        f"Q{q_idx+1}/{q_total}: {status_emoji} | "
                        f"Key: {key_index+1}/{key_count} | Model: {model_used} | Raw: '{raw_answer[:30]}' | "
                        f"Chose: [{correct_index}] {chosen_text}{correct_server_text} | "
                        f"+{coins_earned}¢ | hearts {hearts}"
                    )
                    send_log_sync(f"<b>{debug_line}</b>")
                    debug_lines.append(debug_line)
                    if len(debug_lines) > 15:
                        debug_lines.pop(0)

                    user_debug = "\n".join(debug_lines)
                    log(f"--- Quiz running ---\n{user_debug}")

                    next_info = result.get("next")
                    if not next_info:
                        log(f"Session complete • {session_coins} coins")
                        break

                    if isinstance(next_info, dict):
                        if "question" in next_info and isinstance(
                            next_info.get("question"), dict
                        ):
                            question_obj = next_info["question"]
                            session_id = result.get("sessionId") or session_id
                            continue
                        if "result" in next_info:
                            break
                        if next_info.get("questionId"):
                            question_obj = next_info
                            session_id = result.get("sessionId") or session_id
                            continue

                    if q_count > 0 and ad_every > 0 and (q_count % ad_every == 0):
                        nq = self.quiz_ad_ack(session_id)
                        if nq and isinstance(nq, dict):
                            question_obj = nq
                            continue
                        else:
                            break
                    break
                else:
                    break

            session_summary = (
                f"🏁 Session {session_num} finished\n"
                f"Questions: {q_count}  |  Correct: {correct_count}  |  Wrong: {wrong_count}\n"
                f"Coins earned: {session_coins}"
            )
            log(session_summary)
            send_log_sync(f"<b>{session_summary}</b>")

            sessions_done += 1
            if session_num < max_sessions:
                time.sleep(2)

        final = (
            f"<b>🏁 QUIZ FINISHED</b>\n"
            f"User: <code>{telegram_user_id}</code>\n"
            f"Sessions: {sessions_done}\n"
            f"Total coins earned: ~{total_coins}\n"
            f"Balance now: {self.get_balance()}"
        )
        send_log_sync(final)

        return {
            "sessions": sessions_done,
            "total_coins": total_coins,
            "balance": self.get_balance(),
        }


# ───────────────────── Per-user instances ─────────────────────
user_bots: Dict[int, MiniPixV2] = {}
_user_bots_lock = threading.Lock()


def get_bot(user_id: int) -> MiniPixV2:
    with _user_bots_lock:
        if user_id not in user_bots:
            user_bots[user_id] = MiniPixV2()
        return user_bots[user_id]


def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💰 Balance"), KeyboardButton("📊 Campaign")],
            [KeyboardButton("👥 Accounts"), KeyboardButton("➕ Login")],
            [
                KeyboardButton("🎬 Browse Series"),
                KeyboardButton("🎬 Watch All (8x)"),
            ],
            [
                KeyboardButton("🧠 Quiz Status"),
                KeyboardButton("🤖 Run Quiz"),
            ],
            [KeyboardButton("🔑 Set Groq Key"), KeyboardButton("🔑 Token Login")],
            [KeyboardButton("ℹ️ Help")],
        ],
        resize_keyboard=True,
    )


def build_series_keyboard(series_page, total_pages, page_series):
    kb = []
    for s in page_series:
        sid = str(s.get("_id") or s.get("id") or s.get("series_id") or "")
        if not sid:
            continue
        title = (s.get("title") or s.get("name") or "(no title)")[:32]
        n_ep = int(s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0)
        sid_short = sid if len(sid) <= 10 else sid[:7] + ".."
        label = f"[{sid_short}] {title}  [{n_ep}ep]"
        kb.append([InlineKeyboardButton(label, callback_data=f"sr_sel:{sid}")])
    nav = []
    if series_page > 1:
        nav.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"sr_pg:{series_page-1}")
        )
    nav.append(
        InlineKeyboardButton(f"📄 {series_page}/{total_pages}", callback_data="sr_noop")
    )
    if series_page < total_pages:
        nav.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"sr_pg:{series_page+1}")
        )
    if nav:
        kb.append(nav)
    return InlineKeyboardMarkup(kb)


def build_episode_keyboard(series_id, ep_status, series_title=None):
    kb = []
    header = []
    kb.append(
        [
            InlineKeyboardButton(
                f"🔥 Watch ALL (8x) This Series",
                callback_data=f"sr_all4x:{series_id}",
            )
        ]
    )
    kb.append(
        [
            InlineKeyboardButton(
                f"🔙 Back to Series List", callback_data="sr_back"
            )
        ]
    )
    row = []
    for idx, st in enumerate(ep_status):
        ep = st["episode"]
        n = ep.get("episodeNo") or ep.get("episode_no") or idx + 1
        w = st["watches"]
        if w >= MAX_WATCHES_PER_EP:
            icon = "✔"
        elif w > 0:
            icon = f"{w}/8"
        else:
            icon = "▶"
        label = f"E{n} {icon}"
        row.append(
            InlineKeyboardButton(
                label, callback_data=f"sr_ep:{series_id}:{n}"
            )
        )
        if len(row) == 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    return InlineKeyboardMarkup(kb)


# ───────────────────── Telegram Handlers ─────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot = get_bot(user.id)
    text = (
        f"👋 Hi {user.first_name}!\n\n"
        "MiniPix Unified Bot ready.\n\n"
        "🔐 *Login*\n"
        "• /login – OTP (Phone) login\n"
        "• /tokenlogin `<token>` – direct Bearer token login\n\n"
        "🎬 *Watch*\n"
        "• /series – browse series (button me `[series_id]` dikhta hai)\n"
        "• /watch – Smart 8x Watch *ALL* series (Option 11)\n"
        "• /watch `<SERIES_ID>` – uss SERIES ke saare eps 8x watch\n\n"
        "🧠 *Quiz*\n"
        "• /setgroq `gsk_xxx` – apna Groq key set karo\n"
        "• /addkey `gsk_xxx` – aur ek key add karo (max 5)\n"
        "• /listkeys – saari keys list karo\n"
        "• /removekey `1` – index se key hatao\n"
        "• /setkeys `k1 k2 k3` – sab keys replace karo\n"
        "• /mygroq – apne keys check karo\n"
        "• /quizrun – Auto Quiz solve (Groq AI)\n"
        "• /quiz – quiz status\n"
    )
    if bot.access_token:
        text += f"\n✅ Logged in: {bot.current_account_label or bot.phone}"
    else:
        text += "\n⚠️ Not logged in → /login"
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 *Commands*\n\n"

        "➡️ *Login & Accounts*\n"
        "/login – OTP (Phone) ya Bearer Token login\n"
        "/tokenlogin `JWT_TOKEN` – direct token login (2 tarike)\n"
        "/accounts – saved accounts list / switch\n"
        "/logout – logout\n\n"

        "➡️ *Series / Watch*\n"
        "/series `[page]` – list all series (with ID)\n"
        "/watch – Option 11: *ALL* series 8x smart watch\n"
        "/watch `SERIES_ID` – *specific* series ke saare episodes 8x watch\n\n"

        "➡️ *Quiz*\n"
        "/quiz – quiz status (hearts, daily cap)\n"
        "/quizrun – Groq AI auto quiz solve\n\n"

        "➡️ *Groq API Key Management (Multi-Key Speed)*\n"
        "/setgroq `gsk_xxx` – apna Groq API key set (1 ya multiple)\n"
        "/mygroq – apne saare keys check karo\n"
        "/addkey `gsk_xxx` – aur ek naya key add karo (max 5)\n"
        "/listkeys – saari keys index ke saath list\n"
        "/removekey `N` – Nth index wali key hatao\n"
        "/setkeys `k1 k2 k3` – purani keys hatake nayi set karo\n\n"
        "🚀 *Multi-Key System:* Multiple keys set karne se har question\n"
        "   alag-alag key use hoga → double/triple speed (rate limit × keys)\n\n"

        "➡️ *Misc*\n"
        "/start – main menu\n"
        "/balance – coin balance\n"
        "/campaign – campaign + daily cap\n\n"

        "---\n\n"

        "🔑 *Token Ka Format (Kaise Milega?)*\n\n"

        "Bearer token *MiniPix app ka auth token* hai. Ye `HTTP Toolkit` ya `Fiddler Classic` se packet capture karke milta hai:\n\n"

        "1. HTTP Toolkit / Fiddler ko open karo\n"
        "2. MiniPix app open karo → login karo\n"
        "3. App ke kisi request ko capture karo (jisme `Authorization` header ho)\n"
        "4. Usme dikhe: `Authorization: Bearer eyJhbGciOiJIUzI1NiIs...`\n"
        "5. Wo *pura JWT* (`eyJ` se start hone wala long string) yaha bhejo:\n\n"

        "```\n"
        "/tokenlogin eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiI2N...wxyz\n"
        "```\n\n"

        "Token starts with: `eyJ` (always)\n"
        "Token length: 200+ chars\n"
        "⚠️ Token 30 din baad expire hota hai → tab naya lagana padega.\n\n"

        "Free Groq key: https://console.groq.com/keys"
    )
    await update.message.reply_text(
        help_text, parse_mode="Markdown", reply_markup=main_menu_keyboard()
    )


def _user_keys_as_list(uid_str: str) -> List[str]:
    raw = user_groq_keys.get(uid_str)
    if isinstance(raw, list):
        return [k for k in raw if isinstance(k, str) and k]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _user_keys_pattern_line(count: int) -> str:
    if count <= 1:
        return ""
    parts = [f"Q{i+1}→Key{(((i) % count) + 1)}" for i in range(min(count * 2, 6))]
    return "🚀 Alternate mode ACTIVE: " + ", ".join(parts) + "..."


async def set_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage (1 key):\n`/setgroq gsk_your_key_here`\n\n"
            "Usage (multi keys - alternate for speed):\n`/setgroq gsk_key1 gsk_key2 ...`\n\n"
            "Free key: https://console.groq.com/keys\n"
            f"Max keys per user: {MAX_GROQ_KEYS_PER_USER}",
            parse_mode="Markdown",
        )
        return

    raw_keys = [k.strip() for k in context.args[:MAX_GROQ_KEYS_PER_USER]]
    valid_keys = []
    for k in raw_keys:
        if not k.startswith("gsk_"):
            await update.message.reply_text(f"❌ Invalid key `{k[:10]}...`. Must start with `gsk_`")
            return
        valid_keys.append(k)
    if not valid_keys:
        await update.message.reply_text("❌ Koi valid key nahi mili.")
        return

    user_id = str(update.effective_user.id)
    if len(valid_keys) == 1:
        user_groq_keys[user_id] = valid_keys[0]
        msg = "✅ Groq API key saved!\nAb quiz use kar sakte ho."
    else:
        user_groq_keys[user_id] = valid_keys
        msg = f"✅ {len(valid_keys)} Groq keys saved!\n" + _user_keys_pattern_line(len(valid_keys))
    save_user_groq_keys(user_groq_keys)
    await update.message.reply_text(msg)


async def my_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uid_str = str(uid)
    keys_list = _user_keys_as_list(uid_str)
    count = len(keys_list)
    if count == 0:
        await update.message.reply_text(
            "❌ Koi key set nahi hai.\n\n`/setgroq gsk_xxxxxxxx` ya `/addkey gsk_xxxxxxxx`",
            parse_mode="Markdown",
        )
        return
    lines = []
    for i, k in enumerate(keys_list):
        masked = k[:10] + "..." + k[-4:]
        lines.append(f"✅ Key {i+1}: `{masked}`")
    if count >= 2:
        lines.append("")
        lines.append(_user_keys_pattern_line(count))
    lines.append("")
    lines.append(f"Commands: `/listkeys`, `/addkey gsk_xxx`, `/removekey N`, `/setkeys k1 k2 ...`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def add_key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/addkey gsk_your_new_key`\nEk time pe 1 key add hoti hai.",
            parse_mode="Markdown",
        )
        return
    new_key = context.args[0].strip()
    if not new_key.startswith("gsk_"):
        await update.message.reply_text(f"❌ Invalid key. Must start with `gsk_`", parse_mode="Markdown")
        return
    uid_str = str(update.effective_user.id)
    keys_list = _user_keys_as_list(uid_str)
    if new_key in keys_list:
        await update.message.reply_text("ℹ️ Ye key pehle se hi added hai (duplicate).")
        return
    if len(keys_list) >= MAX_GROQ_KEYS_PER_USER:
        await update.message.reply_text(
            f"❌ Max {MAX_GROQ_KEYS_PER_USER} keys allowed. Pehle `/removekey N` se koi key hatao."
        )
        return
    keys_list.append(new_key)
    _save_groq_keys_for_user(uid_str, keys_list)
    msg = f"✅ Key added! Total keys: {len(keys_list)}"
    pat = _user_keys_pattern_line(len(keys_list))
    if pat:
        msg += "\n" + pat
    await update.message.reply_text(msg)


async def list_keys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uid_str = str(uid)
    keys_list = _user_keys_as_list(uid_str)
    count = len(keys_list)
    if count == 0:
        await update.message.reply_text(
            "❌ Koi key set nahi hai.\n\n`/addkey gsk_xxxxxxxx` use karo.",
            parse_mode="Markdown",
        )
        return
    lines = [f"🔑 Aapke paas {count} Groq key{'s' if count != 1 else ''} hai:"]
    for i, k in enumerate(keys_list):
        masked = k[:10] + "..." + k[-4:]
        lines.append(f"  {i+1}. `{masked}`")
    if count >= 2:
        lines.append("")
        lines.append(_user_keys_pattern_line(count))
    lines.append("")
    lines.append("Key hatane ke liye: `/removekey 1` (index number daalo)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def remove_key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/removekey N`  (1-based index)\n\nPehle `/listkeys` se index pata karo.",
            parse_mode="Markdown",
        )
        return
    try:
        n = int(context.args[0])
    except Exception:
        await update.message.reply_text("❌ Index number bhejo. Example: `/removekey 1`")
        return
    if n < 1:
        await update.message.reply_text("❌ Index 1 se chhota nahi ho sakta.")
        return
    uid_str = str(update.effective_user.id)
    keys_list = _user_keys_as_list(uid_str)
    if n > len(keys_list):
        await update.message.reply_text(
            f"❌ Index {n} galat hai. Aapke paas sirf {len(keys_list)} key hai."
        )
        return
    removed = keys_list.pop(n - 1)
    removed_masked = (removed[:10] + "..." + removed[-4:]) if len(removed) > 14 else "key"
    if len(keys_list) == 0:
        _save_groq_keys_for_user(uid_str, [])
        await update.message.reply_text(f"🗑️ Key {removed_masked} removed.\nAb koi key nahi bacha.")
    elif len(keys_list) == 1:
        _save_groq_keys_for_user(uid_str, keys_list[0])
        msg = f"🗑️ Key {removed_masked} removed.\nRemaining: 1 key."
        await update.message.reply_text(msg)
    else:
        _save_groq_keys_for_user(uid_str, keys_list)
        msg = f"🗑️ Key {removed_masked} removed.\nRemaining keys: {len(keys_list)}\n" + _user_keys_pattern_line(len(keys_list))
        await update.message.reply_text(msg)


async def set_keys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/setkeys gsk_k1 gsk_k2 ...`  (purani keys replace ho jayengi)\nMax 5 keys.",
            parse_mode="Markdown",
        )
        return
    raw_keys = [k.strip() for k in context.args[:MAX_GROQ_KEYS_PER_USER]]
    valid_keys = []
    for k in raw_keys:
        if not k.startswith("gsk_"):
            await update.message.reply_text(f"❌ Invalid key `{k[:10]}...`. Must start with `gsk_`", parse_mode="Markdown")
            return
        valid_keys.append(k)
    if not valid_keys:
        await update.message.reply_text("❌ Koi valid key nahi mili.")
        return
    uid_str = str(update.effective_user.id)
    store_val = valid_keys[0] if len(valid_keys) == 1 else valid_keys
    _save_groq_keys_for_user(uid_str, store_val)
    if len(valid_keys) == 1:
        msg = "✅ 1 key set kar di gayi hai."
    else:
        msg = f"✅ {len(valid_keys)} keys set!\n" + _user_keys_pattern_line(len(valid_keys))
    await update.message.reply_text(msg)


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    if not bot.access_token:
        await update.message.reply_text("Not logged in. Use /login")
        return
    coins = bot.get_balance()
    if coins is None:
        await update.message.reply_text("Failed to fetch balance")
    else:
        await update.message.reply_text(
            f"💰 Coin Balance: *{coins}*", parse_mode="Markdown"
        )


async def campaign_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    if not bot.access_token:
        await update.message.reply_text("Not logged in.")
        return
    st = bot.get_campaign_status()
    text = (
        f"🎥 Campaign: {'ON' if st['enabled'] else 'OFF'}\n"
        f"Daily cap: {st['used']}/{st['cap']}\n"
        f"Reached: {st['reached']}"
    )
    await update.message.reply_text(text)


async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    accs = bot.list_accounts()
    if not accs:
        await update.message.reply_text("No saved accounts.")
        return
    lines = [f"👥 Saved Accounts ({len(accs)}):\n"]
    keyboard = []
    for i, lbl in enumerate(accs, 1):
        acc = bot.accounts[lbl]
        ph = acc.get("phone") or "?"
        lines.append(f"{i}. {lbl}  |  {ph}")
        keyboard.append(
            [
                InlineKeyboardButton(f"Switch → {lbl}", callback_data=f"sw:{lbl}"),
                InlineKeyboardButton("❌", callback_data=f"rm:{lbl}"),
            ]
        )
    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    bot = get_bot(query.from_user.id)
    if data.startswith("sw:"):
        label = data[3:]
        ok, msg = bot.switch_account(label)
        if ok:
            bot.open_app()
            bal = bot.get_balance()
            await query.edit_message_text(f"✅ {msg}\n💰 Balance: {bal}")
        else:
            await query.edit_message_text(f"❌ {msg}")
    elif data.startswith("rm:"):
        label = data[3:]
        if bot.remove_account(label):
            await query.edit_message_text(f"Removed: {label}")
        else:
            await query.edit_message_text("Remove failed")


async def useaccount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    if not context.args:
        accs = bot.list_accounts()
        if not accs:
            await update.message.reply_text(
                "No saved accounts in `minipix_accounts.json`.\n"
                "Usage: `/useaccount <label>`\n"
                "Example: `/useaccount aa`\n"
                "Ya phir `/accounts` se inline buttons use karo.",
                parse_mode="Markdown",
            )
        else:
            preview = "\n".join(f"  • {i+1}. `{lbl}`" for i, lbl in enumerate(accs))
            await update.message.reply_text(
                "Usage: `/useaccount <label>`\n\n"
                f"Available labels:\n{preview}\n\n"
                "Example: `/useaccount aa`",
                parse_mode="Markdown",
            )
        return
    label = " ".join(context.args).strip()
    if not label:
        await update.message.reply_text("Label missing. Usage: `/useaccount bb`", parse_mode="Markdown")
        return
    ok, msg = bot.switch_account(label)
    if ok:
        bot.open_app()
        bal = bot.get_balance()
        await update.message.reply_text(
            f"✅ {msg}\n💰 Balance: {bal}",
            reply_markup=main_menu_keyboard(),
        )
    else:
        accs = bot.list_accounts()
        hint = ""
        if accs:
            hint = "\nAvailable: " + ", ".join(f"`{a}`" for a in accs)
        await update.message.reply_text(f"❌ {msg}{hint}", parse_mode="Markdown")


async def reloadaccounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    before = len(bot.accounts)
    bot.accounts = bot._load_accounts()
    after = len(bot.accounts)
    accs = bot.list_accounts()
    preview = ""
    if accs:
        preview = "\n" + "\n".join(f"  • `{lbl}` → {bot.accounts[lbl].get('phone','?')}" for lbl in accs)
    await update.message.reply_text(
        f"🔄 Accounts reloaded: {before} → {after}{preview}",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


# ───────── Browse Series ─────────
async def series_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bot = get_bot(uid)
    if not bot.access_token:
        await update.message.reply_text("Not logged in. Use /login")
        return
    page = 1
    if context.args:
        try:
            page = max(1, int(context.args[0]))
        except Exception:
            page = 1
    msg = await update.message.reply_text("📚 Series list load ho rahi hai...")
    try:
        page_series, total, total_pages, cur_page = bot.get_series_list_paginated(
            page=page, per_page=10
        )
    except Exception as e:
        await msg.edit_text(f"❌ Series load fail: {e}")
        return
    kb = build_series_keyboard(cur_page, total_pages, page_series)
    text_lines = [
        f"📚 *Series List* — Page {cur_page}/{total_pages}  ({total} total)",
        "",
        "Button mein `[series_id]` dikh raha hai. Use karo:",
        "`/watch <series_id>` → us series ke SARE episodes 8x watch",
        "",
        "Icons:",
        "`▶` Not watched",
        "`1/8–7/8` Watched N times",
        "`✔` 8x complete (max reward)",
        "",
        "Ya kisi series par tap karo → episode menu dikhega.",
    ]
    await msg.edit_text("\n".join(text_lines), parse_mode="Markdown", reply_markup=kb)


async def series_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    bot = get_bot(uid)
    data = query.data

    if data == "sr_noop":
        return

    if not bot.access_token:
        await query.edit_message_text("Not logged in. Use /login")
        return

    if data.startswith("sr_pg:"):
        page = int(data[6:] or "1")
        try:
            page_series, total, total_pages, cur_page = bot.get_series_list_paginated(
                page=page, per_page=10
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Series load fail: {e}")
            return
        kb = build_series_keyboard(cur_page, total_pages, page_series)
        text = (
            f"📚 *Series List* — Page {cur_page}/{total_pages}  ({total} total)\n\n"
            "Kisi series par click karo → episodes + watch options dikhenge."
        )
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
        return

    if data == "sr_back":
        try:
            page_series, total, total_pages, cur_page = bot.get_series_list_paginated(
                page=1, per_page=10
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Series load fail: {e}")
            return
        kb = build_series_keyboard(cur_page, total_pages, page_series)
        text = f"📚 *Series List* — Page {cur_page}/{total_pages}  ({total} total)"
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
        return

    if data.startswith("sr_sel:"):
        sid = data[7:]
        try:
            detail = bot.get_series_detail(sid)
        except Exception as e:
            await query.edit_message_text(f"❌ Series detail fail: {e}")
            return
        info = detail.get("series") or {}
        title = (info.get("title") or info.get("name") or f"Series {sid}")[:60]
        total_eps = detail.get("total_episodes") or 0
        ep_status = detail.get("ep_status") or []
        kb = build_episode_keyboard(sid, ep_status, series_title=title)
        summary_parts = []
        done = maxed = 0
        for st in ep_status:
            if st["watches"] >= MAX_WATCHES_PER_EP:
                maxed += 1
            if st["watches"] >= 1:
                done += 1
        text = (
            f"🎞️ *{title}*\n"
            f"Total episodes: {total_eps}\n"
            f"Progress: {done}/{total_eps} started | "
            f"{maxed}/{total_eps} 8x-complete\n\n"
            "• Tap `E1`, `E2`... → 1 episode watch\n"
            "• Tap *🔥 Watch ALL (8x) This Series* → full series smart-repeat"
        )
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
        return

    if data.startswith("sr_ep:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        sid = parts[1]
        ep_no = parts[2]
        busy_lock = get_user_busy_lock(uid)
        if not busy_lock.acquire(blocking=False):
            await query.answer(
                "⏳ Pehle se ek task chal raha hai.", show_alert=True
            )
            return
        try:
            msg = await query.message.reply_text(
                f"▶️ Starting S{sid} E{ep_no}...\n(1 episode = ~10s)"
            )
            loop = asyncio.get_running_loop()

            def progress(text):
                try:
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(
                            msg.edit_text(
                                f"▶️ S{sid} E{ep_no}…\n\n{str(text)[-1200:]}"
                            )
                        )
                    )
                except Exception:
                    pass

            def work():
                return bot.watch_one_episode(
                    series_id=sid,
                    episode_no=ep_no,
                    progress_callback=progress,
                    telegram_user_id=uid,
                )

            result = await loop.run_in_executor(None, work)

            if isinstance(result, dict) and result.get("error"):
                await msg.edit_text(f"❌ {result['error']}")
            else:
                ok = result.get("ok") if isinstance(result, dict) else False
                status = result.get("status") if isinstance(result, dict) else "?"
                nth = result.get("nth_watch") if isinstance(result, dict) else "?"
                reward = result.get("reward_expected") if isinstance(result, dict) else "?"
                delta = result.get("delta") if isinstance(result, dict) else None
                bal_after = result.get("balance_after") if isinstance(result, dict) else None
                t = (
                    f"🏁 Episode done: S{sid} E{ep_no}\n"
                    f"Result: {'✅' if ok else '❌'} status={status}\n"
                    f"Watch #{nth} (expected +{reward})\n"
                )
                if delta is not None:
                    t += f"💰 Delta: {delta:+d}  |  Balance now: {bal_after}"
                await msg.edit_text(t)
        finally:
            try:
                busy_lock.release()
            except Exception:
                pass
        return

    if data.startswith("sr_all4x:"):
        sid = data[9:]
        busy_lock = get_user_busy_lock(uid)
        if not busy_lock.acquire(blocking=False):
            await query.answer(
                "⏳ Pehle se ek task chal raha hai.", show_alert=True
            )
            return
        try:
            if not bot.access_token:
                await query.edit_message_text("Not logged in.")
                return
            msg = await query.message.reply_text(
                f"🔥 Starting SMART 8x repeat for series {sid}..."
            )
            loop = asyncio.get_running_loop()

            def progress(text):
                try:
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(
                            msg.edit_text(
                                f"🔥 Series 8x Watch S{sid}…\n\n{str(text)[-1400:]}"
                            )
                        )
                    )
                except Exception:
                    pass

            def work():
                return bot.watch_single_series_smart_repeat(
                    series_id=sid,
                    progress_callback=progress,
                    telegram_user_id=uid,
                )

            result = await loop.run_in_executor(None, work)

            if isinstance(result, dict) and result.get("error"):
                await msg.edit_text(f"❌ {result['error']}")
                return
            watched = result.get("watched", 0) if isinstance(result, dict) else 0
            skip = result.get("skipped", 0) if isinstance(result, dict) else 0
            fail = result.get("failed", 0) if isinstance(result, dict) else 0
            t = (
                f"🏁 Series 8x done: {result.get('series_title', sid)}\n\n"
                f"Watched: {watched}\nSkip: {skip}\nFail: {fail}\n"
            )
            if isinstance(result, dict) and result.get("delta") is not None:
                t += (
                    f"💰 {result['balance_before']} → {result['balance_after']} "
                    f"({result['delta']:+d})"
                )
            await msg.edit_text(t)
        finally:
            try:
                busy_lock.release()
            except Exception:
                pass
        return


async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📱 Phone + OTP", callback_data="login:otp")],
        [InlineKeyboardButton("🔑 Bearer Token", callback_data="login:token")],
    ]
    await update.message.reply_text(
        "Choose login method:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def login_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "login:otp":
        await query.edit_message_text("Phone number bhejo (+91... ya 98...):")
        return WAIT_PHONE
    elif query.data == "login:token":
        await query.edit_message_text(
            "*Bearer Token* bhejo:\n\n"
            "Format: `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQi...`\n"
            "(hamesha `eyJ` se start hota hai, ~200+ characters)\n\n"
            "Token kaise milega: HTTP Toolkit/Fiddler se MiniPix app ke requests me "
            "`Authorization: Bearer <TOKEN>` header pakad ke.",
            parse_mode="Markdown",
        )
        return WAIT_TOKEN
    return ConversationHandler.END


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        phone = "+91" + phone.lstrip("0")
    if not re.match(r"^\+[1-9]\d{1,14}$", phone):
        await update.message.reply_text(
            "❌ Invalid phone number. Use format: +91XXXXXXXXXX"
        )
        return WAIT_PHONE

    context.user_data["phone"] = phone
    bot = get_bot(update.effective_user.id)
    st = bot.login_otp_generate(phone)
    if not st:
        await update.message.reply_text(
            "❌ OTP bhejne me fail. Check:\n"
            "• Phone number is correct\n"
            "• Internet connection\n"
            "• Server is reachable\n\n"
            "Try again with /login"
        )
        return ConversationHandler.END
    context.user_data["session_token"] = st
    await update.message.reply_text(f"✅ OTP sent to {phone}\nAb OTP bhejo:")
    return WAIT_OTP


async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    otp = update.message.text.strip()
    bot = get_bot(update.effective_user.id)
    st = context.user_data.get("session_token")
    if not st:
        await update.message.reply_text("Session lost. /login se start karo.")
        return ConversationHandler.END
    ok = bot.login_otp_verify(st, otp)
    if ok:
        bot.open_app()
        bal = bot.get_balance()
        await update.message.reply_text(
            f"✅ Login success!\n💰 Balance: {bal}",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "❌ OTP verify failed. Check OTP and try again."
        )
    return ConversationHandler.END


async def login_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if len(token) < 20:
        await update.message.reply_text(
            "❌ Token too short (< 20 chars).\nValid JWT token `eyJ` se start hota hai aur 200+ chars ka hota hai.\nCancel: /cancel",
        )
        return WAIT_TOKEN
    if not token.startswith("eyJ"):
        await update.message.reply_text(
            "⚠️ Token `eyJ` se start nahi ho raha (JWT nahi lag raha). Phir bhi try kar raha hoon...\n(Cancel: /cancel)",
        )
    bot = get_bot(update.effective_user.id)
    ok = bot.login_with_token(token)
    if ok:
        bot.open_app()
        bal = bot.get_balance()
        await update.message.reply_text(
            f"✅ Token Login Success!\n💰 Balance: {bal}",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "❌ Invalid ya expired token.\nCheck:\n"
            "• Token complete hai? (start `eyJ` + end with chars)\n"
            "• Token expire to nahi ho gaya? (HTTP Toolkit se naya capture karo)\n"
            "Cancel: /cancel",
        )
    return ConversationHandler.END


async def tokenlogin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        help_token = (
            "*Usage:*\n"
            "`/tokenlogin eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiI2Nz...xxxx`\n\n"

            "*Token Properties:*\n"
            "• *Always starts with*: `eyJ` (JWT format)\n"
            "• *Length*: ~200 to 500 characters\n"
            "• *Validity*: ~30 din ke baad expire hota hai\n\n"

            "*Kaise Milega Token?*\n"
            "HTTP Toolkit / Fiddler Classic se:\n"
            "1. HTTP Toolkit/Fiddler start karo (SSL Proxy on)\n"
            "2. MiniPix app open karo → login karo (OTP se)\n"
            "3. App ka koi bhi request dekhna hai (jisme `Authorization` header ho)\n"
            "4. Request headers me:\n"
            "   `Authorization: Bearer eyJhbGciOiJIUzI1NiIs...`\n"
            "5. `Bearer ` ke *baad* ka pura string copy karo → yehi apna TOKEN hai\n\n"

            "*Alternative method:*\n"
            "Chat me `/login` → choose `🔑 Bearer Token` → token send karo.\n\n"

            "⚠️ *Note:* Bot auto-strips `Bearer ` prefix. Seedha `eyJ...` wala bhejo ya poora `Bearer eyJ...` dono chalega."
        )
        await update.message.reply_text(help_token, parse_mode="Markdown")
        return
    token = " ".join(context.args).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if len(token) < 20:
        await update.message.reply_text(
            "❌ Token too short (< 20 chars).\nReal JWT token hamesha `eyJ` se start hota hai aur 200+ characters ka hota hai.",
        )
        return
    if not token.startswith("eyJ"):
        await update.message.reply_text(
            "⚠️ *Warning:* Token `eyJ` se start nahi ho raha (valid JWT nahi lag raha).\n"
            "Try kar raha hoon fir bhi...",
            parse_mode="Markdown",
        )
    bot = get_bot(update.effective_user.id)
    ok = bot.login_with_token(token)
    if ok:
        bot.open_app()
        bal = bot.get_balance()
        await update.message.reply_text(
            f"✅ Token Login Success!\n💰 Balance: {bal}",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "❌ Token login FAILED.\n"
            "Check:\n"
            "1. Token `eyJ` se start hota hai?\n"
            "2. Token complete paste kiya? (copy karte waqt last/start ka hissa na chop ho)\n"
            "3. Token expire to nahi ho gaya? (dobara HTTP Toolkit se capture karo)\n"
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    busy_lock = get_user_busy_lock(uid)
    if not busy_lock.acquire(blocking=False):
        await update.message.reply_text(
            "⏳ Pehle se ek task chal raha hai. Wait karo ya usko poora hone do."
        )
        return
    try:
        bot = get_bot(uid)
        if not bot.access_token:
            await update.message.reply_text("Not logged in. Use /login")
            return

        series_id_arg = None
        if context.args:
            series_id_arg = str(context.args[0]).strip()
            if not series_id_arg:
                series_id_arg = None

        if series_id_arg:
            msg = await update.message.reply_text(
                f"🔥 Starting SMART 8x repeat for Series `{series_id_arg}`...\n"
                "(series detail + episodes load ho rahe hain)",
                parse_mode="Markdown",
            )
            mode_label = f"Series {series_id_arg}"
        else:
            msg = await update.message.reply_text(
                "🚀 Starting Smart 8x Watch (Option 11 mode)...\nThoda time lagega."
            )
            mode_label = "Opt11 ALL"

        loop = asyncio.get_running_loop()

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        msg.edit_text(
                            f"🚀 Watching ({mode_label})…\n\n{str(text)[-1400:]}"
                        )
                    )
                )
            except Exception:
                pass

        def work():
            if series_id_arg:
                return bot.watch_single_series_smart_repeat(
                    series_id=series_id_arg,
                    progress_callback=progress,
                    telegram_user_id=uid,
                )
            return bot.browse_and_watch_all_smart_repeat(
                progress_callback=progress,
                max_watches=250,
                telegram_user_id=uid,
            )

        result = await loop.run_in_executor(None, work)

        if isinstance(result, dict) and "error" in result:
            await msg.edit_text(f"❌ {result['error']}")
            return

        watched = result.get("watched", 0) if isinstance(result, dict) else 0
        skipped = result.get("skipped", 0) if isinstance(result, dict) else 0
        failed = result.get("failed", 0) if isinstance(result, dict) else 0
        text = (
            f"🏁 Watch finished ({mode_label})\n\n"
            f"Watched: {watched}\n"
            f"Skipped: {skipped}\n"
            f"Failed: {failed}\n"
        )
        if isinstance(result, dict) and result.get("delta") is not None:
            text += (
                f"💰 {result['balance_before']} → {result['balance_after']} "
                f"({result['delta']:+d})"
            )
        await msg.edit_text(text)
    finally:
        try:
            busy_lock.release()
        except Exception:
            pass


async def quiz_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    if not bot.access_token:
        await update.message.reply_text("Not logged in.")
        return
    data = bot.get_quiz_status()
    if not data:
        await update.message.reply_text("Failed to get quiz status")
        return
    lvl = data.get("currentLevel", "?")
    cfg = data.get("levelConfig", {}) or {}
    hearts = data.get("hearts", {}) or {}
    daily = data.get("dailyAttempts", {}) or {}
    totals = data.get("totals", {}) or {}
    text = (
        f"🧠 Quiz Status\n\n"
        f"Level: {lvl}\n"
        f"Qs: {cfg.get('questionsCount')} | +{cfg.get('coinsPerCorrect')}/correct\n"
        f"Hearts: {hearts.get('freePerLevel')}/level\n"
        f"Daily: {daily.get('used')}/{daily.get('limit')} "
        f"{'[EXHAUSTED]' if daily.get('exhausted') else ''}\n"
        f"Lifetime coins: {totals.get('coins')}"
    )
    await update.message.reply_text(text)


async def quiz_run_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    if not bot.access_token:
        await update.message.reply_text("Not logged in.")
        return ConversationHandler.END

    if not get_user_groq_key(update.effective_user.id):
        await update.message.reply_text(
            "❌ Pehle apna Groq API key set karo:\n\n"
            "`/setgroq gsk_xxxxxxxx`\n\n"
            "Free key: https://console.groq.com/keys",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text("Kitne quiz sessions? (10-25, default 15):")
    return WAIT_QUIZ_SESSIONS


async def quiz_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip() or "15")
        n = max(10, min(25, n))
    except Exception:
        n = 15
    context.user_data["quiz_sessions"] = n

    uid = update.effective_user.id
    busy_lock = get_user_busy_lock(uid)
    if not busy_lock.acquire(blocking=False):
        await update.message.reply_text(
            "⏳ Pehle se ek task chal raha hai. Wait karo."
        )
        return ConversationHandler.END

    try:
        bot = get_bot(uid)
        sessions = context.user_data.get("quiz_sessions", 15)

        msg = await update.message.reply_text(
            f"🤖 Running {sessions} sessions (delay 10s)..."
        )

        loop = asyncio.get_running_loop()

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        msg.edit_text(
                            f"🤖 Quiz running…\n\n{str(text)[-1400:]}"
                        )
                    )
                )
            except Exception:
                pass

        def work():
            return bot.run_quiz_auto(
                max_sessions=sessions,
                question_delay=QUIZ_QUESTION_DELAY,
                progress_callback=progress,
                telegram_user_id=uid,
            )

        result = await loop.run_in_executor(None, work)

        if isinstance(result, dict) and "error" in result:
            await msg.edit_text(f"❌ {result['error']}")
        else:
            sessions_done = result.get("sessions") if isinstance(result, dict) else "?"
            total_coins = (
                result.get("total_coins") if isinstance(result, dict) else "?"
            )
            balance = result.get("balance") if isinstance(result, dict) else "?"
            await msg.edit_text(
                f"🏁 Quiz done\n"
                f"Sessions: {sessions_done}\n"
                f"Coins this run: ~{total_coins}\n"
                f"Current balance: {balance}"
            )
    finally:
        try:
            busy_lock.release()
        except Exception:
            pass
    return ConversationHandler.END


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    bot._reset_state()
    await update.message.reply_text("Logged out.", reply_markup=main_menu_keyboard())


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "💰 Balance":
        await balance_cmd(update, context)
    elif text == "📊 Campaign":
        await campaign_cmd(update, context)
    elif text == "👥 Accounts":
        await accounts_cmd(update, context)
    elif text == "➕ Login":
        await login_start(update, context)
    elif text == "🎬 Browse Series":
        await series_cmd(update, context)
    elif text == "🎬 Watch All (4x)":
        await watch_cmd(update, context)
    elif text == "🧠 Quiz Status":
        await quiz_status_cmd(update, context)
    elif text == "🤖 Run Quiz":
        return await quiz_run_start(update, context)
    elif text == "🔑 Set Groq Key":
        await update.message.reply_text(
            "Apna Groq key bhejo:\n`/setgroq gsk_xxxxxxxx`\n\n"
            "Free key: https://console.groq.com/keys",
            parse_mode="Markdown",
        )
    elif text == "🔑 Token Login":
        await update.message.reply_text(
            "*Token Login (2 Methods)*\n\n"

            "Method 1 (Fast): `/tokenlogin <your_token>`\n"
            "Example:\n"
            "`/tokenlogin eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQi...`\n\n"

            "Method 2 (Interactive): /login → tap *🔑 Bearer Token*\n\n"

            "*Token Properties:*\n"
            "• JWT hamesha `eyJ` se start hota hai\n"
            "• Length ~ 200-500 chars\n"
            "• HTTP Toolkit/Fiddler se MiniPix ke request ka "
            "`Authorization: Bearer <TOKEN>` header pakad ke nikalo.\n\n"

            "Full help: /help",
            parse_mode="Markdown",
        )
    elif text == "ℹ️ Help":
        await help_cmd(update, context)
    else:
        await update.message.reply_text("Unknown. Use /help")


def main():
    acquire_lock()

    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    login_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_callback, pattern=r"^login:")],
        states={
            WAIT_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)
            ],
            WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp)],
            WAIT_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_token)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    quiz_conv = ConversationHandler(
        entry_points=[
            CommandHandler("quizrun", quiz_run_start),
            MessageHandler(filters.Regex("^🤖 Run Quiz$"), quiz_run_start),
        ],
        states={
            WAIT_QUIZ_SESSIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_sessions)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("campaign", campaign_cmd))
    app.add_handler(CommandHandler("accounts", accounts_cmd))
    app.add_handler(CommandHandler("useaccount", useaccount_cmd))
    app.add_handler(CommandHandler("reloadaccounts", reloadaccounts_cmd))
    app.add_handler(CommandHandler("login", login_start))
    app.add_handler(CommandHandler("tokenlogin", tokenlogin_cmd))
    app.add_handler(CommandHandler("series", series_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("quiz", quiz_status_cmd))
    app.add_handler(CommandHandler("setgroq", set_groq))
    app.add_handler(CommandHandler("mygroq", my_groq))
    app.add_handler(CommandHandler("addkey", add_key_cmd))
    app.add_handler(CommandHandler("listkeys", list_keys_cmd))
    app.add_handler(CommandHandler("removekey", remove_key_cmd))
    app.add_handler(CommandHandler("setkeys", set_keys_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CallbackQueryHandler(account_callback, pattern=r"^(sw|rm):"))
    app.add_handler(
        CallbackQueryHandler(
            series_callback,
            pattern=r"^(sr_(pg|sel|ep|all4x|noop|back))",
        )
    )
    app.add_handler(login_conv)
    app.add_handler(quiz_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    print("Bot starting (lock acquired). Unified mode.")
    if LOG_CHANNEL_ID:
        print(f"Log/DATA channel enabled: {LOG_CHANNEL_ID}")
    else:
        print("WARNING: LOG_CHANNEL_ID not set")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
