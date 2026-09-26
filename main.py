#!/usr/bin/env python3
"""
MiniPix Unified Telegram Bot (SINGLE FILE)
Combines:
  • main.py        – Telegram bot framework + Groq Quiz Solver (BEST)
  • minipix_auto.py – Option 11: Browse ALL + Auto-Watch Each Ep 1x (BEST)
Features:
  • Per-user Telegram isolation + busy lock
  • threading.Lock for shared JSON I/O
  • Login via OTP, interactive token, or /tokenlogin <token>
  • 1x reward (15 coins/ep max) with daily-cap-aware watch
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
import random
import uuid
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

MAX_WATCHES_PER_EP = 1
REWARDS_BY_WATCH = {1: 15}
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
    "qwen/qwen3.8-27b",
    "allam-2-7b",
]

_OKHTTP_VERSIONS = [
    "okhttp/4.11.0",
    "okhttp/4.12.0",
    "okhttp/4.10.0",
    "okhttp/4.9.3",
    "okhttp/4.9.2",
    "okhttp/4.8.1",
    "okhttp/4.7.2",
]

_APP_VERSIONS = ["332"]

_DEVICE_BRANDS = [
    "Xiaomi", "Xiaomi Redmi", "Xiaomi Poco", "Samsung", "OnePlus",
    "Realme", "OPPO", "Vivo", "Motorola", "Nokia",
    "Infinix", "Tecno", "iQOO", "Nothing", "Google Pixel",
]

_DEVICE_MODELS = {
    "Xiaomi": ["Redmi Note 12", "Redmi Note 11", "Redmi Note 10", "Mi 11 Lite", "Redmi 12", "Poco X5", "Poco M6 Pro", "Redmi Note 13"],
    "Xiaomi Redmi": ["Redmi Note 12 Pro", "Redmi Note 11S", "Redmi 10 Prime", "Redmi A2 Plus", "Redmi 12C"],
    "Xiaomi Poco": ["Poco X5 Pro", "Poco F5", "Poco M6 Pro", "Poco C65", "Poco X6 Neo"],
    "Samsung": ["Galaxy M34", "Galaxy M14", "Galaxy A14", "Galaxy A24", "Galaxy A34", "Galaxy S21 FE", "Galaxy F34"],
    "OnePlus": ["OnePlus Nord CE 3", "OnePlus Nord 2T", "OnePlus 11R", "OnePlus Nord CE 4"],
    "Realme": ["Realme Narzo 60X", "Realme 11X", "Realme C55", "Realme Narzo N55", "Realme 12"],
    "OPPO": ["OPPO A78", "OPPO A58", "OPPO F23", "OPPO Reno 8T", "OPPO K12x"],
    "Vivo": ["Vivo Y36", "Vivo Y27", "Vivo T2x", "Vivo V27e", "Vivo Y100A"],
    "Motorola": ["Moto G54", "Moto G32", "Moto Edge 40 Neo", "Moto G14", "Moto G62"],
    "Nokia": ["Nokia G42", "Nokia C32", "Nokia G11 Plus", "Nokia HMD Pulse+"],
    "Infinix": ["Infinix HOT 30i", "Infinix SMART 7", "Infinix NOTE 30", "Infinix ZERO 30"],
    "Tecno": ["Tecno Spark 10", "Tecno POP 7", "Tecno POVA 5", "Tecno CAMON 20"],
    "iQOO": ["iQOO Z7 Lite", "iQOO Z7s", "iQOO Neo 7", "iQOO Z9 Lite"],
    "Nothing": ["Nothing Phone 2", "Nothing Phone 1", "Nothing Phone 2a"],
    "Google Pixel": ["Pixel 7a", "Pixel 6a", "Pixel 8", "Pixel 7", "Pixel 8a"],
}

_OS_VERSIONS = [
    "Android 13", "Android 14", "Android 12", "Android 11", "Android 15",
]

_MANUFACTURER_LIST = ["Xiaomi", "samsung", "OnePlus", "realme", "OPPO", "vivo", "motorola", "HMD Global", "INFINIX", "Tecno", "iQOO", "Nothing", "Google"]

_NETWORK_HEADERS = [
    {"x-network-type": "WIFI", "x-network-carrier": "Jio"},
    {"x-network-type": "WIFI", "x-network-carrier": "Airtel"},
    {"x-network-type": "4G", "x-network-carrier": "Jio"},
    {"x-network-type": "4G", "x-network-carrier": "Airtel"},
    {"x-network-type": "4G", "x-network-carrier": "Vi"},
    {"x-network-type": "5G", "x-network-carrier": "Jio"},
    {"x-network-type": "5G", "x-network-carrier": "Airtel"},
    {},
    {},
    {},
]


def _rand_hex(n: int) -> str:
    return "".join(random.choices("0123456789abcdef", k=n))


def generate_device_id() -> str:
    if random.random() < 0.3:
        return str(uuid.uuid4()).replace("-", "")[:16]
    if random.random() < 0.5:
        return _rand_hex(16)
    if random.random() < 0.6:
        return hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:16]
    return hashlib.sha1(str(random.random()).encode()).hexdigest()[:16]


def generate_device_info() -> str:
    brand = random.choice(_DEVICE_BRANDS)
    candidates = _DEVICE_MODELS.get(brand) or ["Generic Device"]
    model = random.choice(candidates)
    os_ver = random.choice(_OS_VERSIONS)
    sep = random.choice(["; ", " | ", "/", "__"])
    formats = [
        f"{brand} {model}{sep}{os_ver}",
        f"{model}{sep}{os_ver}",
        f"{brand}/{model}/{os_ver}",
        f"{os_ver} {brand} {model}",
        f"{model} {os_ver}",
    ]
    return random.choice(formats)


def generate_user_agent() -> str:
    okhttp = random.choice(_OKHTTP_VERSIONS)
    return okhttp


def generate_headers() -> Dict[str, str]:
    return {
        "user-agent": generate_user_agent(),
        "accept-encoding": "gzip",
        "x-app-version": random.choice(_APP_VERSIONS),
    }


def jitter(base: float, amount: float = 0.6, min_val: float = 0.0) -> float:
    if base <= 0:
        return max(min_val, random.uniform(0, amount))
    half = base * amount
    lo = max(min_val, base - half)
    hi = base + half
    return random.uniform(lo, hi)


def short_sleep(base_ms: float) -> None:
    time.sleep(jitter(base_ms / 1000.0, 0.5, 0.005))


def medium_sleep(base_ms: float) -> None:
    time.sleep(jitter(base_ms / 1000.0, 0.7, 0.01))


def make_progress_steps(nth_watch: Optional[int] = None) -> List[int]:
    base = [1, random.randint(72, 88), random.randint(95, 99), 100]
    if random.random() < 0.55:
        base.insert(1, random.randint(40, 68))
    if random.random() < 0.22:
        base.insert(random.randint(2, 3), random.randint(88, 97))
    if nth_watch is not None and nth_watch >= 1:
        if random.random() < 0.75:
            base = [1, random.randint(82, 92), random.randint(96, 99), 100]
            if random.random() < 0.45:
                base.insert(1, random.randint(55, 78))
    if random.random() < 0.12:
        base.append(100)
    return sorted(set(base))

HEADERS_BASE = {
    "user-agent": "okhttp/4.12.0",
    "accept-encoding": "gzip",
    "x-app-version": "332",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

(WAIT_PHONE, WAIT_OTP, WAIT_TOKEN, WAIT_QUIZ_SESSIONS, WAIT_MULTI_QUIZ_ACCOUNTS, WAIT_MULTI_QUIZ_LEVEL, WAIT_MULTI_QUIZ_CONFIRM, WAIT_RUNQUIZ_SESSIONS, WAIT_MULTI_QUIZ_TOTAL_ROTATIONS, WAIT_TOKENLOGIN_TOKEN, WAIT_TOKEN_PHONE) = range(11)

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
            now_iso = datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()
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
                    {"telegram_user_id": user_id, "keys": keys_list, "updated_at": datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()},
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
        self.device_id = generate_device_id()
        self.device_info = generate_device_info()
        self._req_counter = 0
        self._device_frozen = False
        self._session_active = False
        self._rotate_headers(full=True)
        self.watch_history = {}
        self.watch_history_raw = []
        self.runtime_watch_counts = {}
        self.last_profile = {}
        self.current_account_label = None
        self.referral_code = None
        self.referred_by = None
        self.login_source = None
        self.telegram_owner_id = None
        self.accounts = self._load_accounts()

    def _rotate_headers(self, full=False):
        if self._session_active:
            return
        try:
            cur_auth = self.session.headers.get("authorization") if hasattr(self, "session") else None
        except Exception:
            cur_auth = None
        new_hdrs = generate_headers()
        if not self._device_frozen:
            if full:
                self.device_id = generate_device_id()
                self.device_info = generate_device_info()
            else:
                if random.random() < 0.2:
                    self.device_id = generate_device_id()
                if random.random() < 0.15:
                    self.device_info = generate_device_info()
        try:
            self.session.headers.clear()
            self.session.headers.update(new_hdrs)
        except Exception:
            pass
        if cur_auth:
            try:
                self.session.headers["authorization"] = cur_auth
            except Exception:
                pass

    def _load_accounts(self):
        base = {}
        owner_id = self.telegram_owner_id
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
                                        v_owner = v.get("telegram_owner_id")
                                        if owner_id is None:
                                            pass
                                        elif v_owner is None:
                                            pass
                                        elif v_owner != owner_id:
                                            continue
                                        base[k] = {
                                            "access_token": v.get("access_token", ""),
                                            "user_id": v.get("user_id") or v.get("uid") or v.get("_id"),
                                            "profile_id": v.get("profile_id") or v.get("master_profile") or v.get("pid"),
                                            "phone": v.get("phone") or v.get("mobile"),
                                            "added_on": v.get("added_on") or date.today().isoformat(),
                                            "telegram_owner_id": v_owner,
                                        }
            except Exception:
                pass
        try:
            col = _mongo_accounts_col()
            if col is not None:
                query = {}
                if owner_id is not None:
                    query = {"$or": [{"telegram_owner_id": owner_id}, {"telegram_owner_id": {"$exists": False}}]}
                for doc in col.find(query, max_time_ms=MONGO_CONNECT_TIMEOUT_MS):
                    lbl = doc.get("label")
                    if not lbl:
                        continue
                    doc_owner = doc.get("telegram_owner_id")
                    if owner_id is not None and doc_owner is not None and doc_owner != owner_id:
                        continue
                    entry = {
                        "access_token": doc.get("access_token", ""),
                        "user_id": doc.get("user_id"),
                        "profile_id": doc.get("profile_id"),
                        "phone": doc.get("phone"),
                        "added_on": doc.get("added_on") or date.today().isoformat(),
                        "telegram_owner_id": doc_owner,
                    }
                    if entry["access_token"]:
                        base[lbl] = entry
        except Exception:
            pass
        return base

    def _save_accounts(self):
        owner_id = self.telegram_owner_id
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
                now_iso = datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()
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
                        "telegram_owner_id": owner_id,
                    }
                    if not doc["access_token"]:
                        continue
                    try:
                        filt = {"label": label}
                        if owner_id is not None:
                            filt = {"$or": [{"label": label, "telegram_owner_id": owner_id}, {"label": label, "telegram_owner_id": {"$exists": False}}]}
                        col.replace_one(filt, doc, upsert=True)
                    except Exception:
                        try:
                            col.replace_one({"label": label, "telegram_owner_id": owner_id}, doc, upsert=True)
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
            "telegram_owner_id": self.telegram_owner_id,
            "_cached_balance": getattr(self, "_cached_balance", None),
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
            if col is not None and self.telegram_owner_id is not None:
                filt = {"label": label, "telegram_owner_id": self.telegram_owner_id}
                try:
                    col.delete_one(filt)
                except Exception:
                    filt2 = {"$or": [{"label": label, "telegram_owner_id": self.telegram_owner_id}, {"label": label, "telegram_owner_id": {"$exists": False}}]}
                    try:
                        col.delete_one(filt2)
                    except Exception:
                        pass
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
        self.referral_code = None
        self.referred_by = None
        self.login_source = None
        self._device_frozen = False
        self._session_active = False
        if "authorization" in self.session.headers:
            del self.session.headers["authorization"]

    def _req(self, method, path, **kwargs):
        url = f"{API_BASE}{path}"
        self._req_counter += 1
        if not self._session_active and self._req_counter % random.randint(8, 25) == 0:
            self._rotate_headers(full=random.random() < 0.25)
        try:
            pre_sleep = jitter(3, 0.8, 0)
            if pre_sleep > 0:
                time.sleep(pre_sleep / 1000.0)
            if "timeout" in kwargs:
                timeout_val = kwargs.pop("timeout")
            else:
                timeout_val = random.randint(20, 45)
            r = self.session.request(method, url, timeout=timeout_val, **kwargs)
            try:
                data = r.json()
            except Exception:
                data = r.text
            post_sleep = jitter(12, 0.7, 2)
            time.sleep(post_sleep / 1000.0)
            return r.status_code, data
        except Exception as e:
            return 0, str(e)

    # ───────── Login
    def login_otp_generate(self, phone):
        self.phone = phone
        self._rotate_headers(full=True)
        medium_sleep(random.randint(150, 450))
        payload = {"phone_number": phone}
        sc, data = self._req(
            "POST",
            "/login/generate-otp",
            headers={
                "content-type": "application/json; charset=utf-8",
                "x-minipix-integrity-error": "ERR_8000",
            },
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
        medium_sleep(random.randint(600, 1600))
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
            jwt_payload = self._decode_jwt_payload(self.access_token)
            if isinstance(jwt_payload, dict) and jwt_payload.get("nonce"):
                self.device_id = str(jwt_payload["nonce"])
            self._device_frozen = True
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
            self.referral_code = ref_code
            self.referred_by = ref_by
            self.login_source = source
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
            try:
                self.integrity_attest()
            except Exception:
                pass
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
        self._rotate_headers(full=True)
        if isinstance(jwt, dict) and jwt.get("nonce"):
            self.device_id = str(jwt["nonce"])
        self._device_frozen = True
        medium_sleep(random.randint(120, 380))
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
        self.referral_code = ref_code
        self.referred_by = ref_by
        self.login_source = source
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

    def refresh_session_fingerprint(self, full=False):
        try:
            if not self.access_token:
                return False
            cur_token = self.access_token
            self._rotate_headers(full=full)
            try:
                self.session.headers["authorization"] = f"Bearer {cur_token}"
            except Exception:
                pass
            medium_sleep(random.randint(80, 260))
            ok1 = False
            sc1, raw1 = self._req("GET", "/users/me")
            if sc1 == 200 and isinstance(raw1, dict):
                uid = raw1.get("_id") or raw1.get("id") or raw1.get("userId")
                if uid:
                    if not self.user_id:
                        self.user_id = uid
                    pid = raw1.get("master_profile") or raw1.get("masterProfile")
                    if pid and not self.profile_id:
                        self.profile_id = pid
                    ph = raw1.get("mobile") or raw1.get("phone")
                    if ph and not self.phone:
                        self.phone = ph
                    ok1 = True
            ok2 = False
            if self.user_id:
                sc2, raw2 = self._req("GET", f"/users/{self.user_id}")
                if sc2 == 200 and isinstance(raw2, dict):
                    self.profile_id = raw2.get("master_profile", self.profile_id)
                    ph2 = raw2.get("mobile")
                    if ph2 and not self.phone:
                        self.phone = ph2
                    ok2 = True
            try:
                self.open_app()
            except Exception:
                pass
            try:
                self.integrity_attest()
            except Exception:
                pass
            medium_sleep(random.randint(150, 500))
            return ok1 or ok2
        except Exception:
            try:
                if self.access_token:
                    self.session.headers["authorization"] = f"Bearer {self.access_token}"
            except Exception:
                pass
            return False

    def _refresh_auth_state(self, full=True, with_quiz_status=True):
        if not self.access_token:
            return False
        try:
            if full and not self._device_frozen:
                self.refresh_session_fingerprint(full=True)
            else:
                self.refresh_session_fingerprint(full=False)
        except Exception:
            try:
                self._rotate_headers(full=(full and not self._device_frozen))
                if self.access_token:
                    self.session.headers["authorization"] = f"Bearer {self.access_token}"
            except Exception:
                pass
        ok_me = False
        if self.access_token:
            try:
                sc1, raw1 = self._req("GET", "/users/me")
                if sc1 == 200 and isinstance(raw1, dict):
                    uid = raw1.get("_id") or raw1.get("id") or raw1.get("userId")
                    if uid:
                        if not self.user_id:
                            self.user_id = uid
                        pid = raw1.get("master_profile") or raw1.get("masterProfile")
                        if pid and not self.profile_id:
                            self.profile_id = pid
                        ph = raw1.get("mobile") or raw1.get("phone")
                        if ph and not self.phone:
                            self.phone = ph
                        ok_me = True
            except Exception:
                pass
        ok_user = False
        if self.user_id:
            try:
                sc2, raw2 = self._req("GET", f"/users/{self.user_id}")
                if sc2 == 200 and isinstance(raw2, dict):
                    self.profile_id = raw2.get("master_profile", self.profile_id)
                    ph2 = raw2.get("mobile")
                    if ph2 and not self.phone:
                        self.phone = ph2
                    ok_user = True
            except Exception:
                pass
        try:
            self.open_app()
        except Exception:
            pass
        try:
            self.integrity_attest()
        except Exception:
            pass
        qs_ok = False
        if with_quiz_status:
            try:
                qs = self.get_quiz_status()
                qs_ok = bool(qs and isinstance(qs, dict))
            except Exception:
                pass
        medium_sleep(random.randint(200, 700))
        return bool(ok_me or ok_user or qs_ok)

    def _token_relogin_full_reset(self, progress_log_fn=None):
        saved_token = getattr(self, "access_token", None)
        saved_label = getattr(self, "current_account_label", None)
        saved_phone = getattr(self, "phone", None)
        saved_user_id = getattr(self, "user_id", None)
        saved_profile_id = getattr(self, "profile_id", None)
        if not saved_token:
            if progress_log_fn:
                try:
                    progress_log_fn("❌ Token relogin failed: no saved token available.")
                except Exception:
                    pass
            return False
        def _plog(msg):
            if progress_log_fn:
                try:
                    progress_log_fn(msg)
                except Exception:
                    pass
            send_log_sync(f"🔑 TOKEN RE-LOGIN TRIGGER (enabled=false bypass) | User: <code>{saved_label or saved_user_id or saved_phone or '?'}</code>")
        try:
            self._reset_state()
        except Exception:
            pass
        medium_sleep(random.randint(300, 800))
        relabel = (
            saved_label
            or saved_phone
            or (f"acc_{str(saved_user_id)[-6:]}" if saved_user_id else None)
        )
        ok = self.login_with_token(
            saved_token,
            user_id=saved_user_id,
            profile_id=saved_profile_id,
            label=relabel,
            phone=saved_phone,
        )
        if ok:
            _plog(f"✅ Token Re-login via saved token successful (device_id bound to JWT nonce).")
            try:
                self.open_app()
            except Exception:
                pass
            try:
                self.integrity_attest()
            except Exception:
                pass
            medium_sleep(random.randint(200, 600))
            return True
        _plog("⚠️ login_with_token returned False — fallback to manual refresh.")
        try:
            jwt = self._decode_jwt_payload(saved_token)
            if isinstance(jwt, dict) and jwt.get("nonce"):
                self.device_id = str(jwt["nonce"])
            self._rotate_headers(full=False)
            self.access_token = saved_token
            if saved_user_id and not self.user_id:
                self.user_id = saved_user_id
            if saved_profile_id and not self.profile_id:
                self.profile_id = saved_profile_id
            if saved_phone and not self.phone:
                self.phone = saved_phone
            try:
                self.session.headers["authorization"] = f"Bearer {saved_token}"
            except Exception:
                pass
            self._device_frozen = True
            self._refresh_auth_state(full=False, with_quiz_status=True)
        except Exception:
            pass
        return bool(self.access_token is not None)

    def open_app(self):
        if not (self.user_id and self.profile_id):
            try:
                self.get_user()
            except Exception:
                pass
            if not (self.user_id and self.profile_id):
                return False
        payload = {"openApp": {"_id": self.user_id, "date": date.today().isoformat()}}
        sc, data = self._req(
            "PATCH",
            f"/users/{self.user_id}/profiles/{self.profile_id}",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        return sc == 200 and isinstance(data, dict) and data.get("success")

    def integrity_attest(self):
        last = getattr(self, "_last_attest_ts", 0)
        interval = 6 * 3600 - 120
        if last and (time.time() - last) < interval:
            return True
        ok = False
        candidates = [
            ("POST", "/integrity/attest", None, None),
            ("POST", "/integrity/attest", {}, {"content-type": "application/json; charset=utf-8"}),
            ("POST", "/integrity/verify", None, None),
            ("POST", "/attest", None, None),
        ]
        for method, path, body, hdrs in candidates:
            try:
                kwargs = {}
                if hdrs:
                    kwargs["headers"] = dict(hdrs)
                if body is None:
                    pass
                elif isinstance(body, dict):
                    kwargs["headers"] = kwargs.get("headers") or {}
                    kwargs["headers"]["content-type"] = "application/json; charset=utf-8"
                    kwargs["data"] = json.dumps(body, ensure_ascii=False).encode("utf-8")
                sc, d = self._req(method, path, **kwargs)
                if sc and 200 <= sc < 500:
                    if isinstance(d, dict) and d.get("success"):
                        ok = True
                        break
                    if sc == 200:
                        ok = True
                        break
            except Exception:
                continue
        if ok:
            self._last_attest_ts = time.time()
        try:
            self.open_app()
        except Exception:
            pass
        try:
            self.get_balance()
        except Exception:
            pass
        return ok

    def get_balance(self):
        sc, data = self._req("GET", "/coins/balance")
        coins = None
        if sc == 200 and isinstance(data, dict):
            coins = data.get("coins", 0)
            if isinstance(coins, dict):
                coins = coins.get("coins", 0)
            try:
                coins = int(coins)
            except Exception:
                coins = None
        elif sc == 200 and isinstance(data, (int, float)):
            coins = int(data)
        elif sc == 200 and isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            coins = data[0].get("coins")
            if isinstance(coins, dict):
                coins = coins.get("coins")
            try:
                coins = int(coins) if coins is not None else None
            except Exception:
                coins = None
        cached = getattr(self, "_cached_balance", None)
        try:
            cached_int = int(cached) if cached is not None else None
        except Exception:
            cached_int = None
        if isinstance(coins, int):
            try:
                setattr(self, "_cached_balance", coins)
            except Exception:
                pass
            return coins
        if cached_int is not None:
            return cached_int
        return 0 if coins is None else coins

    def get_balance_robust(self, retries=3):
        best = None
        last_seen_cached = None
        try:
            c = getattr(self, "_cached_balance", None)
            if c is not None:
                last_seen_cached = int(c)
        except Exception:
            last_seen_cached = None
        for i in range(retries):
            try:
                val = self.get_balance()
            except Exception:
                val = None
            try:
                vi = int(val) if val is not None else None
            except Exception:
                vi = None
            if isinstance(vi, int) and vi > 0:
                try:
                    setattr(self, "_cached_balance", vi)
                except Exception:
                    pass
                return vi
            if isinstance(vi, int):
                if best is None:
                    best = vi
                elif vi > best:
                    best = vi
                    try:
                        setattr(self, "_cached_balance", vi)
                    except Exception:
                        pass
            if i < retries - 1:
                time.sleep(random.uniform(0.8, 2.2))
        try:
            cached_now = getattr(self, "_cached_balance", None)
            cached_i = int(cached_now) if cached_now is not None else None
            if isinstance(cached_i, int):
                if best is None or cached_i > best:
                    best = cached_i
        except Exception:
            pass
        try:
            if isinstance(last_seen_cached, int):
                if best is None or (best is not None and last_seen_cached > 0 and best <= 0):
                    best = last_seen_cached
        except Exception:
            pass
        if best is not None:
            return best
        return 0

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
        pct_decimal = watched_pct / 100.0 if watched_pct > 1 else watched_pct
        if watched_pct >= 100:
            progress_val = 100
        elif watched_pct <= 1:
            progress_val = int(watched_pct * 100)
        else:
            progress_val = watched_pct
        watch_obj = {
            "id": series_id,
            "title": series_title or "Series",
            "hindiTitle": "",
            "episodeNo": episode_no,
            "tcInMs": 0,
            "tcOutMs": 180000,
            "detailImage": "",
            "type": "episode",
            "progress": progress_val,
            "time": int(pct_decimal * 1800),
            "watchedPct": pct_decimal,
            "campaign": False,
        }
        ok = False
        try:
            payload_patch = {"watched": watch_obj}
            sc, d = self._req(
                "PATCH",
                f"/users/{self.user_id}/profiles/{self.profile_id}",
                headers={"content-type": "application/json; charset=utf-8"},
                data=json.dumps(payload_patch, ensure_ascii=False).encode("utf-8"),
            )
            ok = sc == 200 and isinstance(d, dict) and d.get("success")
        except Exception:
            pass
        return ok

    def _report_watch_progress_to_coins(
        self, series_id, episode_no, watched_pct, series_title=""
    ):
        if not (self.user_id and self.profile_id):
            return False
        return True

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
        self._session_active = True
        if not isinstance(series_info, dict):
            self._session_active = False
            return False, "invalid_series"
        if not isinstance(episode, dict):
            self._session_active = False
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
            self._session_active = False
            return True, "skip"

        if not episode.get("coinUnlocked", True):
            ep_id = episode.get("_id") or episode.get("id")
            url = episode.get("playbackUrl")
            try:
                self.unlock_episode(series_id, ep_id, ep_no, playback_url=url)
            except Exception:
                pass

        progress_steps = make_progress_steps(nth_watch=nth_watch)
        any_fail = False
        reported_coin_progress = False
        base_step_delay_ms = 22
        if nth_watch and nth_watch <= 1:
            base_step_delay_ms = 18
        if random.random() < 0.18:
            tc_in_ms += random.randint(0, 2000)
        if random.random() < 0.18:
            tc_out_ms += random.randint(-1500, 2500)
        for idx_cur, pct in enumerate(progress_steps):
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
                    short_sleep(random.randint(8, 28))
                    self._report_watch_progress_to_coins(
                        series_id, ep_no, pct, series_title
                    )
                    reported_coin_progress = True
                except Exception:
                    pass
            if delay_multiplier > 0:
                try:
                    prev_pct = progress_steps[idx_cur - 1] if idx_cur > 0 else 0
                    delta = pct - prev_pct
                    if delta <= 0:
                        delta = 1
                    delay = dur_sec * delay_multiplier * delta / 100
                    delay = jitter(delay, 0.4, 0.005)
                    if delay > 0:
                        time.sleep(min(delay, 1.5))
                except Exception:
                    short_sleep(base_step_delay_ms)
            else:
                jitter_ms = base_step_delay_ms + random.randint(-5, 14)
                if idx_cur == 0:
                    jitter_ms += random.randint(2, 14)
                if idx_cur == len(progress_steps) - 1:
                    jitter_ms += random.randint(4, 20)
                short_sleep(max(8, jitter_ms))
        if not reported_coin_progress:
            try:
                short_sleep(random.randint(10, 35))
                self._report_watch_progress_to_coins(series_id, ep_no, 100, series_title)
            except Exception:
                pass
        try:
            short_sleep(random.randint(30, 120))
            self.claim_reward_task(series_id=series_id, task_id=None)
        except Exception:
            pass
        post_watch_ms = random.randint(50, 220)
        if random.random() < 0.08:
            post_watch_ms += random.randint(150, 400)
        short_sleep(post_watch_ms)
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
        self._session_active = False
        return True, "done"

    # ─────────────────── OPTION 11: Browse ALL + Auto-Watch Each Episode 1x (BEST)
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
            f"🌐 Option 11 mode: Browse ALL + 1x Watch Each Episode\n"
            f"Reward/ep: +15 coins (1x max per ep)\n"
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
        if random.random() < 0.7:
            try:
                shuffle_window = min(len(all_series), random.randint(15, max(16, len(all_series))))
                prefix = all_series[:shuffle_window]
                random.shuffle(prefix)
                all_series = prefix + all_series[shuffle_window:]
            except Exception:
                pass
        if random.random() < 0.25:
            try:
                random.shuffle(all_series)
            except Exception:
                pass
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

        log(f"Series found: {len(all_series)}. Watch mode = ALL series, 1x each episode.")

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

            inter_series_ms = random.randint(120, 520)
            if idx > 1 and random.random() < 0.08:
                inter_series_ms += random.randint(600, 2000)
            if idx > 1:
                short_sleep(inter_series_ms)

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
            inner_ep_counter = 0
            for ep in episodes_sorted:
                inner_ep_counter += 1
                if max_watches is not None and total_watched_all >= max_watches:
                    break
                if _check_daily_cap(total_watched_all):
                    break
                ep_no = (
                    ep.get("episodeNo") or ep.get("episode_no") or 0
                )
                key_pair = (str(real_sid), str(ep_no))
                cur_count = watch_counts.get(key_pair, 0)
                if cur_count >= 1:
                    continue
                if inner_ep_counter > 1 and random.random() < 0.05:
                    short_sleep(random.randint(8, 45))
                try:
                    ok, status = self.watch_episode(
                        ep,
                        real_info or s,
                        allow_repeat=False,
                        nth_watch=1,
                    )
                except Exception:
                    ok, status = False, "exception"
                if status == "skip":
                    series_skip += 1
                elif ok:
                    series_done += 1
                    total_watched_all += 1
                    watch_counts[key_pair] = 1
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
            if total_watched_all > 0 and total_watched_all % random.randint(50, 120) == 0:
                try:
                    self._rotate_headers(full=random.random() < 0.25)
                except Exception:
                    pass
                medium_sleep(random.randint(300, 1100))

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

        log(f"🎯 Specific series mode: id={series_id}\nReward/ep: +15 coins (1x max per ep)")
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
            else max(1, len(episodes_sorted))
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

        inner_ep_counter = 0
        for ep in episodes_sorted:
            inner_ep_counter += 1
            if total_watched_all >= max_allowed:
                log(f"🛑 Soft limit ({max_allowed} watches).")
                break
            if _check_daily_cap(total_watched_all):
                break
            ep_no = ep.get("episodeNo") or ep.get("episode_no") or 0
            key_pair = (str(real_sid), str(ep_no))
            cur_count = watch_counts.get(key_pair, 0)
            if cur_count >= 1:
                continue
            if inner_ep_counter > 1 and random.random() < 0.05:
                short_sleep(random.randint(8, 45))
            try:
                ok, status = self.watch_episode(
                    ep,
                    real_info or {},
                    allow_repeat=False,
                    nth_watch=1,
                )
            except Exception:
                ok, status = False, "exception"
            if status == "skip":
                total_skip += 1
            elif ok:
                total_watched_all += 1
                watch_counts[key_pair] = 1
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
        if cur_count >= 1:
            return {
                "error": f"Episode already 1x watched. No more reward (repeat disabled)."
            }
        bal_before = self.get_balance_silent()
        try:
            ok, status = self.watch_episode(
                target, real_info or {}, allow_repeat=False, nth_watch=1
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
        reward_label = 15
        log(
            f"Result: ok={ok} status={status} (+{reward_label} expected) delta={delta}"
        )
        return {
            "ok": ok,
            "status": status,
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

    def _gen_integrity_stub(self):
        import hashlib
        raw = f"{self.device_id}:{self.user_id or 'u'}:{int(time.time())}:{random.random()}"
        h = hashlib.sha256(raw.encode()).hexdigest()
        return h + "." + hashlib.md5(raw[::-1].encode()).hexdigest()

    def quiz_start_session(self, force_fresh_device=False, hint_hard_ban=False):
        self._session_active = True
        try:
            if force_fresh_device:
                try:
                    self._refresh_auth_state(full=True, with_quiz_status=True)
                except Exception:
                    try:
                        self.refresh_session_fingerprint(full=True)
                    except Exception:
                        pass
                medium_sleep(random.randint(400, 900))
            else:
                r = random.random()
                if hint_hard_ban or r < 0.55:
                    try:
                        self._refresh_auth_state(full=(hint_hard_ban or r < 0.25), with_quiz_status=False)
                    except Exception:
                        try:
                            self.refresh_session_fingerprint(full=(hint_hard_ban or r < 0.25))
                        except Exception:
                            pass
                medium_sleep(random.randint(180, 550))
        except Exception:
            pass
        try:
            sc1, raw1 = self._req("GET", "/users/me", timeout=8)
            if sc1 == 200 and isinstance(raw1, dict):
                uid = raw1.get("_id") or raw1.get("id") or raw1.get("userId")
                if uid and not self.user_id:
                    self.user_id = uid
                pid = raw1.get("master_profile") or raw1.get("masterProfile")
                if pid and not self.profile_id:
                    self.profile_id = pid
        except Exception:
            pass
        try:
            self.open_app()
        except Exception:
            pass
        sc, data = self._req(
            "POST",
            "/quiz/session/start",
            headers={
                "content-type": "application/json; charset=utf-8",
                "x-minipix-integrity-error": "ERR_-8",
            },
            data=b"",
        )
        send_log_sync(
            f"📡 quiz/session/start response:\n"
            f"Status: {sc}\n"
            f"Data: {json.dumps(data, ensure_ascii=False)[:500]}"
        )
        diag = {
            "status_code": sc,
            "success": False,
            "enabled": None,
            "exhausted": False,
            "disabled_flag": False,
            "has_session": False,
            "has_question": False,
            "message": None,
            "hard_ban": False,
        }
        if sc == 200 and isinstance(data, dict):
            diag["success"] = (data.get("success") is True or data.get("status") == "success")
            if "enabled" in data:
                diag["enabled"] = bool(data.get("enabled"))
                if diag["enabled"] is False:
                    diag["disabled_flag"] = True
                    if not (data.get("session") or data.get("question") or data.get("sessionId")):
                        diag["hard_ban"] = True
            msg = data.get("message") or data.get("error") or data.get("msg")
            if msg:
                diag["message"] = str(msg)
            daily_info = data.get("dailyAttempts") or data.get("daily") or {}
            if isinstance(daily_info, dict) and daily_info.get("exhausted"):
                diag["exhausted"] = True
            if diag["success"] is True or data.get("status") == "success":
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
                diag["has_session"] = bool(sid)
                diag["has_question"] = bool(question_obj and isinstance(question_obj, dict))
                if sid and question_obj:
                    return sid, question_obj, session_obj, diag
                else:
                    if diag["enabled"] is False:
                        send_log_sync(
                            f"🚫 QUIZ BANNED SIGNAL: enabled=false (fingerprint flagged)\n"
                            f"sid={sid}, question_obj={question_obj is not None}\n"
                            f"msg={diag.get('message')}\n"
                            f"hard_ban={diag.get('hard_ban')}"
                        )
                    else:
                        send_log_sync(
                            f"⚠️ Missing sessionId or question in response.\n"
                            f"sid={sid}, question_obj={question_obj is not None}, "
                            f"enabled={diag.get('enabled')}"
                        )
            else:
                if diag["enabled"] is False:
                    send_log_sync(
                        f"🚫 QUIZ BANNED: success=False AND enabled=false. msg={diag.get('message')} hard_ban={diag.get('hard_ban')}"
                    )
                else:
                    send_log_sync(
                        f"❌ Quiz start returned success=False: {data.get('message', data)}"
                    )
        else:
            send_log_sync(f"❌ Quiz start HTTP {sc}: {str(data)[:300]}")
        self._session_active = False
        return None, None, None, diag

    def quiz_submit_answer(self, session_id, question_id, chosen_index, extra_headers=None):
        payload = {
            "sessionId": session_id,
            "questionId": question_id,
            "chosenIndex": chosen_index,
        }
        hdrs = {
            "content-type": "application/json; charset=utf-8",
        }
        if isinstance(extra_headers, dict):
            hdrs.update(extra_headers)
        sc, data = self._req(
            "POST",
            "/quiz/session/answer",
            headers=hdrs,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict):
            return data
        return None

    def quiz_submit_answer_with_headers(self, session_id, question_id, chosen_index, extra_headers=None):
        return self.quiz_submit_answer(session_id, question_id, chosen_index, extra_headers=extra_headers)

    def quiz_use_lifeline(self, session_id, question_id, extra_headers=None):
        payload = {"sessionId": session_id, "questionId": question_id}
        hdrs = {
            "content-type": "application/json; charset=utf-8",
        }
        if isinstance(extra_headers, dict):
            hdrs.update(extra_headers)
        sc, data = self._req(
            "POST",
            "/quiz/session/lifeline",
            headers=hdrs,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data.get("removedOptions", [])
        return None

    def quiz_ad_ack(self, session_id, extra_headers=None, raw_log_prefix=None, pre_hint_ban=False):
        if pre_hint_ban:
            if raw_log_prefix:
                send_log_sync(f"🩺 {raw_log_prefix} pre_hint_ban=True (answer-level enabled=false) → SKIP all ad-ack probes entirely.")
            return None
        payload = {"sessionId": session_id}
        hdrs = {
            "content-type": "application/json; charset=utf-8",
        }
        if isinstance(extra_headers, dict):
            hdrs.update(extra_headers)
        endpoints = [
            ("POST", "/quiz/session/ad-ack", payload, dict(hdrs)),
            ("POST", "/quiz/session/continue", payload, dict(hdrs)),
            ("GET", f"/quiz/session/{session_id}", None, dict(hdrs)),
        ]
        probe1_enabled_false = False
        probe2_404 = False
        for idx, (method, path, body, cur_hdrs) in enumerate(endpoints):
            if probe1_enabled_false and idx >= 1:
                break
            if probe1_enabled_false and probe2_404:
                break
            try:
                kwargs = {"headers": cur_hdrs, "timeout": 15}
                if body is not None:
                    kwargs["data"] = json.dumps(body, ensure_ascii=False).encode("utf-8")
                sc, data = self._req(method, path, **kwargs)
                if raw_log_prefix:
                    send_log_sync(
                        f"🩺 {raw_log_prefix} ad-ack probe#{idx+1} {method} {path.split('?')[0]}\n"
                        f"Status: {sc}\n"
                        f"Data: {json.dumps(data, ensure_ascii=False)[:600] if isinstance(data,(dict,list)) else str(data)[:400]}"
                    )
                if idx == 0 and sc == 200 and isinstance(data, dict) and data.get("enabled") is False:
                    probe1_enabled_false = True
                if idx == 1 and sc == 404:
                    probe2_404 = True
                    if probe1_enabled_false:
                        if raw_log_prefix:
                            send_log_sync(f"🩺 {raw_log_prefix} enabled=false pattern probe1=200/ef + probe2=404 → CANCEL all remaining probes (idx≥2)")
                        break
                if sc == 404 and idx >= 2 and probe1_enabled_false:
                    continue
                is_ok = (sc == 200 and isinstance(data, dict))
                if not is_ok:
                    continue
                q = None
                for candidate in (
                    data.get("question"),
                    (data.get("data") or {}).get("question") if isinstance(data.get("data"), dict) else None,
                    (data.get("next") or {}).get("question") if isinstance(data.get("next"), dict) else None,
                    data.get("next") if isinstance(data.get("next"), dict) and data.get("next").get("questionId") else None,
                    data.get("nextQuestion"),
                    (data.get("result") or {}).get("question") if isinstance(data.get("result"), dict) else None,
                ):
                    if isinstance(candidate, dict) and (
                        candidate.get("questionId")
                        or candidate.get("options")
                        or candidate.get("questionHi")
                        or candidate.get("questionEn")
                    ):
                        q = candidate
                        break
                    elif isinstance(candidate, dict):
                        q = candidate
                        break
                if q:
                    return q
                if data.get("success") is True and (data.get("hearts") is not None or data.get("coinsSoFar") is not None):
                    if not q:
                        for candidate in (
                            data.get("next"),
                            data.get("data"),
                        ):
                            if isinstance(candidate, dict):
                                sub = candidate.get("question")
                                if isinstance(sub, dict) and (sub.get("questionId") or sub.get("options")):
                                    return sub
            except Exception:
                continue
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
                    cached_text = (doc.get("correct_text") or "").strip()
                    cached_idx = doc.get("correct_index")
                    resolved_idx = None
                    if cached_text:
                        norm_cache_text = _normalize_text(cached_text)
                        for live_i, live_opt in enumerate(options):
                            if _normalize_text(live_opt or "") == norm_cache_text:
                                resolved_idx = live_i
                                break
                        if resolved_idx is None and cached_text:
                            for live_i, live_opt in enumerate(options):
                                live_stripped = (live_opt or "").strip().lower()
                                cache_stripped = cached_text.strip().lower()
                                if live_stripped and cache_stripped and (live_stripped == cache_stripped or cache_stripped in live_stripped or live_stripped in cache_stripped):
                                    resolved_idx = live_i
                                    break
                    if resolved_idx is None and isinstance(cached_idx, int) and 0 <= cached_idx < len(options):
                        try:
                            if cached_text:
                                opt_at_idx = options[cached_idx] or ""
                                norm_opt = _normalize_text(opt_at_idx)
                                norm_txt = _normalize_text(cached_text)
                                if norm_opt == norm_txt:
                                    resolved_idx = cached_idx
                                else:
                                    resolved_idx = cached_idx
                            else:
                                resolved_idx = cached_idx
                        except Exception:
                            resolved_idx = cached_idx if isinstance(cached_idx, int) and 0 <= cached_idx < len(options) else None

                    if resolved_idx is not None and 0 <= resolved_idx < len(options):
                        is_fixed_by_text = cached_idx is not None and resolved_idx != int(cached_idx)
                        try:
                            if is_fixed_by_text:
                                try:
                                    col.update_one(
                                        {"qhash": qhash},
                                        {
                                            "$set": {
                                                "correct_index": int(resolved_idx),
                                                "last_options": [str(o) for o in options],
                                                "index_fixed_at": datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
                                            }
                                        }
                                    )
                                except Exception:
                                    pass
                            else:
                                try:
                                    col.update_one({"qhash": qhash}, {"$inc": {"hits": 1}})
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        model_tag = doc.get("model_used", "cached") or "cached"
                        if is_fixed_by_text:
                            tag = f"[CACHE+FIX] {model_tag}"
                        else:
                            tag = f"[CACHE] {model_tag}"
                        chosen_text = options[resolved_idx]
                        return resolved_idx, tag, chosen_text
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
                    "solved_at": datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
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
        max_questions_per_session=None,
        max_total_questions=None,
    ):
        single_session_mode = (max_sessions == 1 and max_questions_per_session is None and max_total_questions is None)
        if single_session_mode and progress_callback:
            try:
                progress_callback(
                    f"✅ Mode: EXACTLY 1 SESSION per Run Quiz click.\n"
                    f"Next session ke liye fir se '🤖 Run Quiz' dabao.\n"
                    f"(Saves daily login limit, prevents session limit waste)"
                )
            except Exception:
                pass
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

        last_diag = None
        consec_ban_count = 0
        token_relogin_done_this_run = 0
        MAX_TOKEN_RELOGINS_PER_RUN = 2
        for session_num in range(1, max_sessions + 1):
            log(f"--- Session {session_num}/{max_sessions} ---")

            prev_hard_ban = bool(isinstance(last_diag, dict) and last_diag.get("hard_ban"))
            prev_any_ban = bool(isinstance(last_diag, dict) and last_diag.get("disabled_flag"))
            if prev_any_ban:
                consec_ban_count += 1
            else:
                consec_ban_count = 0

            try:
                do_full = (session_num % 2 == 0) or (failed_attempts >= 1) or (consec_ban_count >= 1)
                if prev_hard_ban or consec_ban_count >= 2:
                    do_full = True
                if session_num > 1:
                    self._refresh_auth_state(full=do_full, with_quiz_status=True)
                    medium_sleep(random.randint(600, 1800))
                    if prev_hard_ban:
                        pcool = random.uniform(15.0, 40.0)
                        log(f"🛑 Previous session had HARD BAN → pre-session cool-off {pcool:.1f}s...")
                        send_log_sync(f"🧊 Pre-session {session_num}: hard-ban cool-off {pcool:.1f}s (consec bans={consec_ban_count}).")
                        time.sleep(pcool)
                    elif prev_any_ban and consec_ban_count >= 1:
                        pcool = random.uniform(6.0, 20.0)
                        log(f"🛑 Previous session had BAN → pre-session cool-off {pcool:.1f}s...")
                        time.sleep(pcool)
            except Exception:
                try:
                    if session_num > 1:
                        do_full_fb = (session_num % 2 == 0) or (failed_attempts >= 1) or (consec_ban_count >= 1)
                        self.refresh_session_fingerprint(full=do_full_fb)
                        medium_sleep(random.randint(400, 1100))
                        if prev_hard_ban:
                            pcool = random.uniform(15.0, 35.0)
                            time.sleep(pcool)
                        elif prev_any_ban:
                            pcool = random.uniform(6.0, 18.0)
                            time.sleep(pcool)
                except Exception:
                    pass

            session_id, question_obj, session_meta, diag = None, None, None, None
            ban_detected_any = False
            hard_ban_detected = False

            attempt_1_diag = None
            for slot in (1, 2):
                force = False
                hint_ban = bool(prev_any_ban and consec_ban_count >= 1)
                if slot == 1:
                    if hint_ban:
                        try:
                            self._refresh_auth_state(full=(consec_ban_count >= 2), with_quiz_status=False)
                            medium_sleep(random.randint(300, 800))
                        except Exception:
                            pass
                else:
                    disabled_after_first = bool(isinstance(attempt_1_diag, dict) and attempt_1_diag.get("disabled_flag"))
                    hard_after_first = bool(isinstance(attempt_1_diag, dict) and attempt_1_diag.get("hard_ban"))
                    exhausted_after_first = bool(isinstance(attempt_1_diag, dict) and attempt_1_diag.get("exhausted"))
                    if exhausted_after_first:
                        log("🛑 Daily exhausted from first attempt — skip retry, no second start-session attempt.")
                        diag = attempt_1_diag
                        break
                    if disabled_after_first and token_relogin_done_this_run < MAX_TOKEN_RELOGINS_PER_RUN:
                        log(f"🔑 First attempt got enabled=false → AUTO TOKEN RE-LOGIN (slot 2, attempt to reset ban...")
                        relogin_ok = False
                        try:
                            relogin_ok = self._token_relogin_full_reset(progress_log_fn=log)
                        except Exception as re:
                            log(f"⚠️  Token relogin exception: {re}")
                            relogin_ok = False
                        if relogin_ok:
                            token_relogin_done_this_run += 1
                            pcool = random.uniform(20.0, 45.0)
                            log(f"   Token re-login complete → cool-off {pcool:.1f}s before session start...")
                            send_log_sync(f"✅ TOKEN RE-LOGIN OK slot {token_relogin_done_this_run}/{MAX_TOKEN_RELOGINS_PER_RUN} — cool-off {pcool:.1f}s then start session.")
                            time.sleep(pcool)
                        else:
                            try:
                                self._refresh_auth_state(full=True, with_quiz_status=True)
                            except Exception:
                                try:
                                    self.refresh_session_fingerprint(full=True)
                                except Exception:
                                    pass
                            pcool = random.uniform(15.0, 35.0)
                            time.sleep(pcool)
                        force = True
                        hint_ban = True
                        if hard_after_first:
                            time.sleep(random.uniform(10.0, 20.0))
                    elif disabled_after_first:
                        log(f"🚫 enabled=false again — token relogin used {token_relogin_done_this_run}/{MAX_TOKEN_RELOGINS_PER_RUN}.")
                        not_exhausted_ctx = True
                        try:
                            qs_ctx = self.get_quiz_status() or {}
                            daily_ctx = qs_ctx.get("dailyAttempts", {}) or {}
                            if daily_ctx.get("exhausted"):
                                not_exhausted_ctx = False
                        except Exception:
                            pass
                        if not_exhausted_ctx:
                            send_log_sync(
                                f"⏳ S{session_num} slot-2: Token relogin limit reached BUT daily NOT exhausted → TEMP BAN LIFT WAIT CYCLE (hard_ban={hard_after_first}).\n"
                                f"Will wait+retry up to 3 times to catch the ~2min temp-ban window observed today 12:56→13:01."
                            )
                            temp_wait_success = False
                            TEMP_BAN_RETRIES = 3
                            for tbi in range(TEMP_BAN_RETRIES):
                                if hard_after_first:
                                    t_wait = random.uniform(90.0, 150.0)
                                else:
                                    t_wait = random.uniform(55.0, 100.0)
                                log(f"   Temp-ban wait #{tbi+1}/{TEMP_BAN_RETRIES}: {t_wait:.1f}s ...")
                                send_log_sync(f"⏳ S{session_num} Temp-Ban-Wait #{tbi+1}/{TEMP_BAN_RETRIES}: {t_wait:.0f}s (hard={hard_after_first})")
                                time.sleep(t_wait)
                                try:
                                    if (tbi % 2) == 1:
                                        self._refresh_auth_state(full=True, with_quiz_status=True)
                                    else:
                                        self.refresh_session_fingerprint(full=True)
                                except Exception:
                                    pass
                                medium_sleep(random.randint(300, 900))
                                tcur_diag = None
                                try:
                                    tres = self.quiz_start_session(force_fresh_device=True, hint_hard_ban=hard_after_first)
                                    if isinstance(tres, tuple) and len(tres) >= 4:
                                        ta, tb, tc, td = tres[0], tres[1], tres[2], tres[3]
                                    elif isinstance(tres, tuple) and len(tres) == 3:
                                        ta, tb, tc = tres
                                        td = {}
                                    else:
                                        ta, tb, tc, td = None, None, None, {}
                                except Exception as etr:
                                    ta, tb, tc, td = None, None, None, {"exception": str(etr)}
                                tcur_diag = td
                                if isinstance(td, dict):
                                    if td.get("exhausted"):
                                        log("🛑 During temp-ban wait: exhausted detected → abort.")
                                        failed_attempts = 9
                                        diag = td
                                        break
                                    if td.get("disabled_flag"):
                                        ban_detected_any = True
                                        if td.get("hard_ban"):
                                            hard_ban_detected = True
                                if ta and tb:
                                    session_id, question_obj, session_meta, diag = ta, tb, tc, td
                                    temp_wait_success = True
                                    send_log_sync(f"✅ S{session_num}: Temp ban lifted after {(tbi+1)} wait cycles → session started.")
                                    break
                            if temp_wait_success:
                                break
                            if failed_attempts >= 9:
                                break
                        diag = attempt_1_diag
                        log(f"🚫 enabled=false — temp-ban wait cycles exhausted / token relogin cap hit → stop session_num {session_num}.")
                        break
                    else:
                        log(f"🔄 Retry session start (slot 2/2) — first attempt failed without ban, light cool-off...")
                        try:
                            self._refresh_auth_state(full=False, with_quiz_status=False)
                        except Exception:
                            try:
                                self.refresh_session_fingerprint(full=False)
                            except Exception:
                                pass
                        time.sleep(random.uniform(5.0, 12.0))

                try:
                    if isinstance(diag, dict) and diag.get("disabled_flag"):
                        pass
                except Exception:
                    pass
                cur_diag_out = None
                try:
                    res = self.quiz_start_session(force_fresh_device=force, hint_hard_ban=hint_ban)
                    if isinstance(res, tuple) and len(res) >= 4:
                        session_id, question_obj, session_meta, cur_diag_out = res[0], res[1], res[2], res[3]
                    elif isinstance(res, tuple) and len(res) == 3:
                        session_id, question_obj, session_meta = res
                        cur_diag_out = {}
                    else:
                        session_id, question_obj, session_meta = None, None, None
                        cur_diag_out = {}
                except Exception as e:
                    session_id, question_obj, session_meta, cur_diag_out = None, None, None, {"exception": str(e)}
                diag = cur_diag_out
                if slot == 1:
                    attempt_1_diag = cur_diag_out
                if isinstance(cur_diag_out, dict) and cur_diag_out.get("disabled_flag"):
                    ban_detected_any = True
                    if cur_diag_out.get("hard_ban"):
                        hard_ban_detected = True
                if session_id and question_obj:
                    break
                try:
                    if isinstance(cur_diag_out, dict) and cur_diag_out.get("exhausted"):
                        log("🛑 Daily exhausted (from start response) — abort further sessions.")
                        failed_attempts = 9
                        break
                    qs = self.get_quiz_status() or {}
                    daily_info = qs.get("dailyAttempts", {}) or {}
                    if daily_info.get("exhausted"):
                        log("🛑 Daily quiz exhausted — abort further sessions.")
                        failed_attempts = 9
                        break
                except Exception:
                    pass

            last_diag = diag if isinstance(diag, dict) else None
            if isinstance(last_diag, dict) and not last_diag.get("disabled_flag") and session_id and question_obj:
                pass
            elif ban_detected_any and not (session_id and question_obj):
                pass

            if failed_attempts >= 9:
                break

            if not session_id or not question_obj:
                diag_repr = ""
                if isinstance(diag, dict):
                    parts = []
                    if diag.get("disabled_flag"):
                        parts.append("ENABLED_FALSE=BAN")
                    if diag.get("hard_ban"):
                        parts.append("HARD_BAN")
                    if diag.get("exhausted"):
                        parts.append("EXHAUSTED")
                    if diag.get("message"):
                        parts.append(f"msg={diag.get('message')}")
                    if diag.get("status_code"):
                        parts.append(f"http={diag.get('status_code')}")
                    diag_repr = " | ".join(parts)
                log(f"❌ Failed to start session (no retries beyond 2 slot attempts (save daily 3 cap)" + (f" [{diag_repr}]" if diag_repr else ""))
                send_log_sync(
                    f"❌ Session {session_num} start FAILED (MAX 2 SLOT ONLY, NO MORE — saves daily cap) | User <code>{telegram_user_id}</code>"
                    + (f"\n  Diagnosis: {diag_repr}" if diag_repr else "")
                    + (f"\n  consec_ban_count={consec_ban_count}" if consec_ban_count else "")
                )
                failed_attempts += 1
                if failed_attempts >= 2:
                    log("2+ consecutive session failures → aborting quiz run. Next auto-login se resolve hoga.")
                    break
                try:
                    self._refresh_auth_state(full=True, with_quiz_status=True)
                except Exception:
                    try:
                        self.refresh_session_fingerprint(full=True)
                    except Exception:
                        pass
                if hard_ban_detected or (isinstance(diag, dict) and diag.get("hard_ban")):
                    base_sleep_min = 40.0
                    base_sleep_max = 90.0
                elif ban_detected_any:
                    base_sleep_min = 25.0
                    base_sleep_max = 60.0
                else:
                    base_sleep_min = 12.0
                    base_sleep_max = 30.0
                sleep_s = random.uniform(base_sleep_min, base_sleep_max)
                log(f"   Fail cool-off before next try: {sleep_s:.1f}s")
                time.sleep(sleep_s)
                continue

            hearts = session_meta.get("hearts", 3) if session_meta else 3

            if hearts == 0:
                log(f"💔 Session has 0 hearts – cannot continue.")
                failed_attempts += 1
                if failed_attempts >= 2:
                    log("Aborting: repeated dead sessions.")
                    break
                time.sleep(random.uniform(6.0, 15.0))
                continue

            failed_attempts = 0
            ad_every = session_meta.get("adGateEvery", 5) if session_meta else 5
            q_count = 0
            session_coins = 0
            session_coins_max = 0
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
                
                if max_questions_per_session is not None and q_count > max_questions_per_session:
                    log(f"🎯 Quiz level target hit: {max_questions_per_session} questions per session. Stop session.")
                    break
                
                if max_total_questions is not None and total_question_count > max_total_questions:
                    log(f"🎯 Total questions target hit: {max_total_questions}. Stop all sessions.")
                    failed_attempts = 9
                    break
                
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

                base_think_s = random.uniform(1.0, 20.0)
                thinking_ms = jitter(base_think_s * 1000, 0.25, base_think_s * 600)
                medium_sleep(int(thinking_ms))
                if random.random() < 0.22:
                    short_sleep(random.randint(250, 1800))

                try:
                    q_extra_hdrs = {
                        "x-device-id": self.device_id,
                    }
                    result = self.quiz_submit_answer_with_headers(
                        session_id, q_id, correct_index, q_extra_hdrs
                    )
                except Exception:
                    result = None
                if not result:
                    try:
                        result = self.quiz_submit_answer(session_id, q_id, correct_index)
                    except Exception:
                        result = None
                if not result:
                    break

                if not isinstance(result, dict):
                    result = None
                    break

                mid_session_enabled_false = False
                if isinstance(result, dict) and result.get("enabled") is False:
                    mid_session_enabled_false = True

                if (isinstance(result, dict) and result.get("success")) or mid_session_enabled_false:
                    correct_flag = result.get("correct", False) if not mid_session_enabled_false else False
                    coins_earned_raw = result.get("coinsEarned")
                    try:
                        coins_earned = int(coins_earned_raw) if coins_earned_raw not in (None, "") else 0
                    except Exception:
                        coins_earned = 0
                    try:
                        coins_so_far_raw = result.get("coinsSoFar")
                        if coins_so_far_raw is None or str(coins_so_far_raw).strip() == "":
                            new_so_far = None
                        else:
                            new_so_far = int(coins_so_far_raw)
                    except Exception:
                        new_so_far = None
                    if isinstance(new_so_far, int):
                        if new_so_far >= session_coins_max:
                            session_coins = new_so_far
                            session_coins_max = new_so_far
                        else:
                            session_coins = session_coins_max
                    else:
                        session_coins = session_coins_max
                    try:
                        h_raw = result.get("hearts")
                        if h_raw is None or str(h_raw).strip() == "":
                            pass
                        else:
                            hearts = int(h_raw)
                    except Exception:
                        pass
                    total_coins += coins_earned
                    if not mid_session_enabled_false:
                        if correct_flag:
                            correct_count += 1
                        else:
                            wrong_count += 1

                    status_emoji = "✅" if (correct_flag and not mid_session_enabled_false) else ("🚫" if mid_session_enabled_false else "❌")
                    correct_idx_server = result.get("correctIndex")
                    correct_server_text = ""
                    if correct_idx_server is not None:
                        try:
                            correct_server_text = f" (Correct: {correct_idx_server} '{options[correct_idx_server]}')"
                        except Exception:
                            pass
                    
                    if correct_idx_server is not None and isinstance(correct_idx_server, int) and 0 <= correct_idx_server < len(options):
                        try:
                            qhash_srv = _quiz_cache_key(combined, options)
                            col_srv = _mongo_cache_col()
                            if col_srv is not None:
                                correct_text_srv = options[correct_idx_server]
                                ai_was_wrong = False
                                try:
                                    if ai_choice_idx is not None and ai_choice_idx != correct_idx_server:
                                        ai_was_wrong = True
                                except Exception:
                                    pass
                                quiz_level_val = 1
                                try:
                                    quiz_level_val = int(getattr(self, "_session_quiz_level", 1) or 1)
                                except Exception:
                                    quiz_level_val = 1
                                set_doc_srv = {
                                    "question": combined,
                                    "options": list(options or []),
                                    "correct_index": correct_idx_server,
                                    "correct_text": correct_text_srv,
                                    "model_used": "server_ground_truth",
                                    "server_confirmed_at": datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
                                    "quiz_level": quiz_level_val,
                                    "ai_was_wrong": ai_was_wrong,
                                }
                                try:
                                    if ai_was_wrong:
                                        set_doc_srv["ai_corrected"] = True
                                        set_doc_srv["ai_prev_wrong_index"] = ai_choice_idx
                                    update_spec = {"$set": set_doc_srv, "$setOnInsert": {"hits": 0, "ai_corrections": 0}}
                                    mongo_result = col_srv.update_one({"qhash": qhash_srv}, update_spec, upsert=True)
                                    if getattr(mongo_result, "matched_count", 0) > 0 and ai_was_wrong:
                                        try:
                                            col_srv.update_one({"qhash": qhash_srv}, {"$inc": {"ai_corrections": 1}})
                                        except Exception:
                                            pass
                                except Exception:
                                    try:
                                        doc_srv_fallback_set = {
                                            "qhash": qhash_srv,
                                            "question": combined,
                                            "options": list(options or []),
                                            "correct_index": correct_idx_server,
                                            "correct_text": correct_text_srv,
                                            "model_used": "server_ground_truth",
                                            "solved_at": datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
                                            "ai_corrected": ai_was_wrong,
                                            "ai_was_wrong": ai_was_wrong,
                                            "quiz_level": quiz_level_val,
                                        }
                                        if ai_was_wrong:
                                            doc_srv_fallback_set["ai_prev_wrong_index"] = ai_choice_idx
                                        fallback_spec = {"$set": doc_srv_fallback_set, "$setOnInsert": {"hits": 0, "ai_corrections": 0}}
                                        col_srv.update_one({"qhash": qhash_srv}, fallback_spec, upsert=True)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    
                    debug_line = (
                        f"Q{q_idx+1}/{q_total}: {status_emoji} | "
                        f"Key: {key_index+1}/{key_count} | Model: {model_used} | Raw: '{raw_answer[:30]}' | "
                        f"Chose: [{correct_index}] {chosen_text}{correct_server_text} | "
                        f"+{coins_earned}¢ | hearts {hearts} | sessionCoins {session_coins}"
                        + (" | MID-BAN enabled=false" if mid_session_enabled_false else "")
                    )
                    send_log_sync(f"<b>{debug_line}</b>")
                    debug_lines.append(debug_line)
                    if len(debug_lines) > 15:
                        debug_lines.pop(0)

                    user_debug = "\n".join(debug_lines)
                    log(f"--- Quiz running ---\n{user_debug}")

                    if mid_session_enabled_false:
                        send_log_sync(
                            f"🚨 MID-SESSION BAN detected at Q{q_idx+1} S{session_num} (enabled=false in /answer response).\n"
                            f"Attempting TOKEN RE-LOGIN + fresh session start to finish remaining quota."
                        )
                        log(f"🚨 MID-SESSION enabled=false → token relogin + session restart path")
                        prev_total_coins = total_coins
                        prev_correct_count = correct_count
                        prev_wrong_count = wrong_count
                        prev_q_count = q_count
                        prev_tqc = total_question_count
                        try:
                            relogin_ok = self._token_relogin_full_reset(progress_log_fn=log)
                        except Exception as e:
                            log(f"⚠️ mid-session token relogin exception: {e}")
                            relogin_ok = False
                        if not relogin_ok:
                            try:
                                self._refresh_auth_state(full=True, with_quiz_status=True)
                            except Exception:
                                try:
                                    self.refresh_session_fingerprint(full=True)
                                except Exception:
                                    pass
                        pcool_mid = random.uniform(30.0, 60.0)
                        log(f"   Mid-ban cool-off {pcool_mid:.1f}s before new session start...")
                        time.sleep(pcool_mid)

                        mid_attempt_1_diag = None
                        mid_new_sid = None
                        mid_new_q = None
                        mid_new_meta = None
                        mid_diag_out = None
                        for mid_slot in (1, 2):
                            force_mid = (mid_slot == 2)
                            hint_ban_mid = True
                            if mid_slot == 2:
                                disabled_first = bool(isinstance(mid_attempt_1_diag, dict) and mid_attempt_1_diag.get("disabled_flag"))
                                exhausted_first = bool(isinstance(mid_attempt_1_diag, dict) and mid_attempt_1_diag.get("exhausted"))
                                if exhausted_first:
                                    log("🛑 mid-restart: exhausted at slot 1 — skip slot 2.")
                                    break
                                if disabled_first:
                                    try:
                                        self._refresh_auth_state(full=True, with_quiz_status=True)
                                    except Exception:
                                        try:
                                            self.refresh_session_fingerprint(full=True)
                                        except Exception:
                                            pass
                                    time.sleep(random.uniform(15.0, 35.0))
                            try:
                                mid_res = self.quiz_start_session(force_fresh_device=force_mid, hint_hard_ban=hint_ban_mid)
                                if isinstance(mid_res, tuple) and len(mid_res) >= 4:
                                    a, b, c, d = mid_res[0], mid_res[1], mid_res[2], mid_res[3]
                                elif isinstance(mid_res, tuple) and len(mid_res) == 3:
                                    a, b, c = mid_res
                                    d = {}
                                else:
                                    a, b, c, d = None, None, None, {}
                            except Exception as em:
                                a, b, c, d = None, None, None, {"exception": str(em)}
                            if mid_slot == 1:
                                mid_attempt_1_diag = d
                            mid_new_sid, mid_new_q, mid_new_meta, mid_diag_out = a, b, c, d
                            if mid_new_sid and mid_new_q:
                                break
                            try:
                                if isinstance(d, dict) and d.get("exhausted"):
                                    failed_attempts = 9
                                    break
                                qs_mid = self.get_quiz_status() or {}
                                daily_mid = qs_mid.get("dailyAttempts", {}) or {}
                                if daily_mid.get("exhausted"):
                                    failed_attempts = 9
                                    break
                            except Exception:
                                pass
                        if failed_attempts >= 9 or (not (mid_new_sid and mid_new_q)):
                            log(f"❌ mid-session restart failed. Carrying totals to outer session summary (stop here).")
                            last_diag = mid_diag_out if isinstance(mid_diag_out, dict) else None
                            consec_ban_count += 1
                            if not last_diag:
                                last_diag = {"disabled_flag": True, "hard_ban": True}
                            elif not last_diag.get("disabled_flag"):
                                last_diag["disabled_flag"] = True
                            total_coins = prev_total_coins
                            correct_count = prev_correct_count
                            wrong_count = prev_wrong_count
                            q_count = prev_q_count
                            total_question_count = prev_tqc
                            break
                        try:
                            if mid_new_meta and isinstance(mid_new_meta, dict) and mid_new_meta.get("hearts") is not None:
                                hearts = int(mid_new_meta.get("hearts"))
                            elif hearts <= 0:
                                hearts = 3
                        except Exception:
                            pass
                        if hearts <= 0:
                            hearts = 3
                        try:
                            if mid_new_meta and isinstance(mid_new_meta, dict):
                                ad_every = mid_new_meta.get("adGateEvery", ad_every)
                        except Exception:
                            pass
                        session_id = mid_new_sid
                        question_obj = mid_new_q
                        session_meta = mid_new_meta
                        log(f"✅ Mid-ban restart OK → new sid active. Continue answering (prev totals preserved).")
                        send_log_sync(f"✅ Mid-ban restart success at S{session_num} → continue answering after {q_count} questions already done.")
                        last_diag = mid_diag_out if isinstance(mid_diag_out, dict) else None
                        consec_ban_count = max(0, consec_ban_count)
                        continue

                    if hearts <= 0:
                        log(f"💔 Hearts=0 after this answer → session end (coins so far: {session_coins})")
                        break

                    next_info = result.get("next")
                    result_next_q = None
                    result_next_sid = None
                    if isinstance(next_info, dict):
                        if "question" in next_info and isinstance(next_info.get("question"), dict):
                            result_next_q = next_info["question"]
                            result_next_sid = result.get("sessionId") or next_info.get("sessionId")
                        elif next_info.get("questionId"):
                            result_next_q = next_info
                            result_next_sid = result.get("sessionId") or next_info.get("sessionId")
                        elif "result" in next_info:
                            log(f"next.result present → session natural end.")
                            next_info = "__END__"
                    if not result_next_q and isinstance(result.get("question"), dict):
                        result_next_q = result.get("question")
                        result_next_sid = result.get("sessionId")
                    if not result_next_q and isinstance(result.get("data"), dict):
                        d = result["data"]
                        if isinstance(d.get("question"), dict):
                            result_next_q = d["question"]
                            result_next_sid = d.get("sessionId") or result.get("sessionId")
                        elif isinstance(d.get("next"), dict):
                            n = d["next"]
                            if isinstance(n.get("question"), dict):
                                result_next_q = n["question"]
                                result_next_sid = d.get("sessionId") or result.get("sessionId")
                            elif n.get("questionId"):
                                result_next_q = n
                                result_next_sid = d.get("sessionId") or result.get("sessionId")
                    if not result_next_q and isinstance(result.get("nextQuestion"), dict):
                        result_next_q = result.get("nextQuestion")
                        result_next_sid = result.get("sessionId")
                    if not result_next_q and result.get("sessionId") and result.get("nextQuestionId"):
                        pass

                    if result_next_q and isinstance(result_next_q, dict):
                        question_obj = result_next_q
                        if result_next_sid:
                            session_id = result_next_sid
                        continue

                    if next_info == "__END__":
                        log(f"Session marked as ended (next.result) • {session_coins} coins")
                        break

                    if q_count > 0 and ad_every > 0 and (q_count % ad_every == 0):
                        ad_answer_ban = isinstance(result, dict) and result.get("enabled") is False
                        if ad_answer_ban:
                            log(f"🚨 ad_every block: answer has enabled=false (mid-ban) → skip all ad-gate probes.")
                        else:
                            try:
                                ad_hdrs = {
                                    "x-device-id": self.device_id,
                                }
                                nq = self.quiz_ad_ack(session_id, extra_headers=ad_hdrs, pre_hint_ban=False)
                            except Exception:
                                try:
                                    nq = self.quiz_ad_ack(session_id, pre_hint_ban=False)
                                except Exception:
                                    nq = None
                            if nq and isinstance(nq, dict):
                                question_obj = nq
                                continue

                    if hearts > 0:
                        log(f"🔁 Answer submit returned no next question BUT hearts={hearts}>0 → trying ad-ack/lifeline fallback to keep session alive...")
                        prefix = f"S{session_num}Q{q_idx+1}h{hearts}"
                        send_log_sync(f"🔁 S{session_num} Q{q_idx+1}: success but next missing (correct={correct_flag}), hearts={hearts}>0 → FULL CONTINUATION PROBE. RAW:\n<pre>{json.dumps(result, ensure_ascii=False)[:800]}</pre>")
                        fallback_found = False
                        answer_enabled_false = isinstance(result, dict) and result.get("enabled") is False
                        if answer_enabled_false:
                            send_log_sync(
                                f"🚨 S{session_num} Q{q_idx+1}: Answer result also has enabled=false (mid-session ban). "
                                f"SKIP all ad-ack probes → end session, outer loop will handle temp-ban restart if possible."
                            )
                            log(f"🚨 Answer enabled=false detected in fallback section → end session cleanly (no pointless ad-ack probes)")
                            try:
                                if not last_diag or not isinstance(last_diag, dict):
                                    last_diag = {}
                                if not last_diag.get("disabled_flag"):
                                    last_diag["disabled_flag"] = True
                                last_diag["hard_ban"] = True
                                try:
                                    last_diag["source"] = "mid_session_answer_enabled_false"
                                except Exception:
                                    pass
                            except Exception:
                                pass
                            ban_detected_any = True
                            hard_ban_detected = True
                            break
                        for _t in range(1):
                            try:
                                nq_fb = self.quiz_ad_ack(session_id, raw_log_prefix=f"{prefix}#t{_t+1}", pre_hint_ban=answer_enabled_false)
                            except Exception as e:
                                log(f"   ad-ack try {_t+1} exception: {e}")
                                nq_fb = None
                            if nq_fb and isinstance(nq_fb, dict) and (
                                nq_fb.get("questionId")
                                or nq_fb.get("options")
                                or nq_fb.get("questionHi")
                                or nq_fb.get("questionEn")
                            ):
                                question_obj = nq_fb
                                fallback_found = True
                                log(f"   ad-ack attempt {_t+1} returned next question.")
                                break
                            medium_sleep(random.randint(300, 900))
                        if fallback_found:
                            continue
                        log(f"⚠️ No next question found after all probes — session ends here (hearts={hearts} unused but API offers no next question).")
                        break
                    else:
                        log(f"Session complete (no next + hearts=0) • {session_coins} coins")
                        break
                else:
                    success_false_msg = ""
                    if isinstance(result, dict):
                        success_false_msg = result.get("message") or result.get("error") or ""
                    send_log_sync(
                        f"⚠️ S{session_num} Q{q_idx+1}: submit answer success=false RAW\n"
                        f"<pre>{json.dumps(result, ensure_ascii=False)[:800] if isinstance(result,(dict,list)) else str(result)[:500]}</pre>"
                    )
                    ansfail_enabled_false = isinstance(result, dict) and result.get("enabled") is False
                    if ansfail_enabled_false:
                        send_log_sync(f"🚨 success=false AND enabled=false (mid-session ban) → end session cleanly.")
                        try:
                            if not last_diag or not isinstance(last_diag, dict):
                                last_diag = {}
                            last_diag["disabled_flag"] = True
                            last_diag["hard_ban"] = True
                        except Exception:
                            pass
                        ban_detected_any = True
                        hard_ban_detected = True
                        break
                    if hearts > 0:
                        log(f"⚠️ Answer submit success=false (msg={success_false_msg!r}) BUT hearts={hearts}>0 → ad-ack fallback...")
                        fb_nq = None
                        try:
                            fb_nq = self.quiz_ad_ack(session_id, raw_log_prefix=f"S{session_num}Q{q_idx+1}FAIL", pre_hint_ban=ansfail_enabled_false)
                        except Exception:
                            fb_nq = None
                        if fb_nq and isinstance(fb_nq, dict) and (
                            fb_nq.get("questionId")
                            or fb_nq.get("options")
                            or fb_nq.get("questionHi")
                            or fb_nq.get("questionEn")
                        ):
                            question_obj = fb_nq
                            log("   ad-ack recovered next question after success=false.")
                            continue
                    log(f"❌ Answer submit success=false (msg={success_false_msg!r}) → break session.")
                    break

            if isinstance(session_coins_max, int) and session_coins_max > session_coins:
                session_coins = session_coins_max

            session_summary = (
                f"🏁 Session {session_num} finished\n"
                f"Questions: {q_count}  |  Correct: {correct_count}  |  Wrong: {wrong_count}\n"
                f"Coins earned: {session_coins}"
            )
            log(session_summary)
            send_log_sync(f"<b>{session_summary}</b>")

            sessions_done += 1
            if session_num < max_sessions:
                relogin_ok = False
                cur_token = None
                cur_label = None
                try:
                    cur_token = self.access_token
                    cur_label = self.current_account_label or self.phone or (f"acc_{str(self.user_id)[-6:]}" if self.user_id else None)
                except Exception:
                    cur_token = None
                log(f"🔁 Auto re-login after session {session_num} (device bound to JWT nonce + full server reset)...")
                send_log_sync(f"🔁 POST-SESSION {session_num}: AUTO RE-LOGIN (device_id bound to JWT nonce) before next session. hard_ban={hard_ban_detected} ban={ban_detected_any}")
                try:
                    if cur_token:
                        jwt = self._decode_jwt_payload(cur_token)
                        if isinstance(jwt, dict) and jwt.get("nonce"):
                            self.device_id = str(jwt["nonce"])
                        self._rotate_headers(full=False)
                        try:
                            self.session.headers["x-device-id"] = self.device_id
                        except Exception:
                            pass
                        self.session.headers["authorization"] = f"Bearer {cur_token}"
                        self.access_token = cur_token
                        self._device_frozen = True
                        medium_sleep(random.randint(200, 700))
                        ok_me = False
                        try:
                            sc1, raw1 = self._req("GET", "/users/me", timeout=12)
                            if sc1 == 200 and isinstance(raw1, dict):
                                uid = raw1.get("_id") or raw1.get("id") or raw1.get("userId")
                                if uid:
                                    self.user_id = uid
                                pid = raw1.get("master_profile") or raw1.get("masterProfile")
                                if pid:
                                    self.profile_id = pid
                                ph = raw1.get("mobile") or raw1.get("phone")
                                if ph:
                                    self.phone = ph
                                ok_me = True
                        except Exception:
                            pass
                        ok_user = False
                        if self.user_id:
                            try:
                                sc2, raw2 = self._req("GET", f"/users/{self.user_id}", timeout=12)
                                if sc2 == 200 and isinstance(raw2, dict):
                                    self.profile_id = raw2.get("master_profile", self.profile_id)
                                    ph2 = raw2.get("mobile")
                                    if ph2 and not self.phone:
                                        self.phone = ph2
                                    ok_user = True
                            except Exception:
                                pass
                        try:
                            self.open_app()
                        except Exception:
                            pass
                        try:
                            self.integrity_attest()
                        except Exception:
                            pass
                        try:
                            self.get_quiz_status()
                        except Exception:
                            pass
                        if ok_me or ok_user:
                            try:
                                if cur_label:
                                    self._store_current_account(label=cur_label)
                            except Exception:
                                pass
                            relogin_ok = True
                            send_log_sync(
                                f"✅ AUTO RE-LOGIN OK | device_id(nonce)={str(self.device_id)[-8:]}... | "
                                f"/users/me={ok_me} /users/id={ok_user}"
                            )
                except Exception as re:
                    send_log_sync(f"⚠️ Auto re-login exception: {re}")
                    relogin_ok = False

                if not relogin_ok:
                    try:
                        self._refresh_auth_state(full=False, with_quiz_status=True)
                        send_log_sync(f"♻️ Fallback: light auth-state refresh (relogin did not complete).")
                    except Exception:
                        try:
                            self.refresh_session_fingerprint(full=False)
                        except Exception:
                            pass

                next_sleep_s = random.uniform(60.0, 120.0)
                if hard_ban_detected:
                    next_sleep_s += random.uniform(30.0, 60.0)
                elif ban_detected_any:
                    next_sleep_s += random.uniform(15.0, 30.0)
                tags = []
                if relogin_ok:
                    tags.append("AUTO RE-LOGIN DONE")
                if hard_ban_detected:
                    tags.append("+ HARD BAN extra")
                elif ban_detected_any:
                    tags.append("+ BAN extra")
                log_msg = f"⏱️ Next quiz session in ~{next_sleep_s:.1f}s (" + ", ".join(tags) + ")..."
                send_log_sync(
                    f"⏳ INTER-SESSION delay after session {session_num}: {next_sleep_s:.1f}s. "
                    + " | ".join(tags)
                )
                log(log_msg)
                time.sleep(next_sleep_s)

        self._session_active = False
        final_balance_val = self.get_balance_robust(retries=3)
        try:
            fb_vi = int(final_balance_val)
        except Exception:
            fb_vi = None
        if not isinstance(fb_vi, int) or fb_vi <= 0:
            try:
                cached_bal = getattr(self, "_cached_balance", None)
                if isinstance(cached_bal, int) and cached_bal > 0:
                    final_balance_val = cached_bal
                elif total_coins and isinstance(total_coins, int) and total_coins > 0:
                    final_balance_val = f"≥{total_coins} (balance API returned 0)"
            except Exception:
                pass
        final = (
            f"<b>🏁 QUIZ FINISHED</b>\n"
            f"User: <code>{telegram_user_id}</code>\n"
            f"Sessions: {sessions_done}\n"
            f"Total coins earned: ~{total_coins}\n"
            f"Balance now: {final_balance_val}"
        )
        send_log_sync(final)

        return {
            "sessions": sessions_done,
            "total_coins": total_coins,
            "balance": final_balance_val if isinstance(final_balance_val, int) else total_coins,
        }


# ───────────────────── Per-user instances ─────────────────────
user_bots: Dict[int, MiniPixV2] = {}
_user_bots_lock = threading.Lock()


def get_bot(user_id: int) -> MiniPixV2:
    with _user_bots_lock:
        if user_id not in user_bots:
            bot = MiniPixV2()
            bot.telegram_owner_id = int(user_id)
            bot.accounts = bot._load_accounts()
            user_bots[user_id] = bot
        else:
            existing = user_bots[user_id]
            if existing.telegram_owner_id is None:
                existing.telegram_owner_id = int(user_id)
        return user_bots[user_id]


def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💰 Balance")],
            [KeyboardButton("👥 Accounts"), KeyboardButton("➕ Login")],
            [
                KeyboardButton("🧠 Quiz Status"),
                KeyboardButton("🤖 Run Quiz"),
            ],
            [KeyboardButton("🔄 Multi-Account Quiz")],
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
                f"🔥 Watch All Episodes This Series",
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
            icon = f"{w}/1"
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
        "MiniPix Unified Bot ready — *Quiz-Only Mode*\n\n"
        "🔐 *Login*\n"
        "• /login – OTP (Phone) login\n"
        "• /tokenlogin `<token>` – direct Bearer token login\n\n"
        "👥 *Accounts*\n"
        "• /accounts – saved accounts list / switch\n"
        "• /importaccounts – JSON text se accounts import\n"
        "• *MiniPix accounts JSON* → chat me upload as document → auto-import Mongo + local JSON dono me.\n\n"
        "🧠 *Quiz*\n"
        "• /setgroq `gsk_xxx` – apna Groq key set karo\n"
        "• /addkey `gsk_xxx` – aur ek key add karo (max 5)\n"
        "• /listkeys – saari keys list karo\n"
        "• /removekey `1` – index se key hatao\n"
        "• /setkeys `k1 k2 k3` – sab keys replace karo\n"
        "• /mygroq – apne keys check karo\n"
        "• /quizrun – Auto Quiz solve (1 session per click — **EXACTLY 1 SESSION** per Run)\n"
        "• /quiz – quiz status\n"
    )
    if bot.access_token:
        text += f"\n✅ Logged in: {bot.current_account_label or bot.phone}"
    else:
        text += "\n⚠️ Not logged in → /login"
    await update.message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 *Commands (Quiz-Only Mode)*\n\n"

        "➡️ *Login & Accounts*\n"
        "/login – OTP (Phone) ya Bearer Token login\n"
        "/tokenlogin `JWT_TOKEN` – direct token login (2 tarike)\n"
        "/accounts – saved accounts list / switch\n"
        "/useaccount `<label>` – specific account switch karo\n"
        "/reloadaccounts – saved accounts fir se load karo (file + Mongo)\n"
        "/importaccounts `{JSON}` – inline JSON text se accounts import\n"
        "*JSON Upload* – `minipix_accounts.json` file chat me **document** ke roop me upload karo → auto import Mongo + local JSON dono me\n"
        "/logout – logout\n\n"

        "➡️ *Quiz*\n"
        "/quiz – quiz status (hearts, daily cap)\n"
        "/quizrun – Groq AI auto quiz solve — **EXACTLY 1 SESSION per click**. Next session ke liye fir se `/quizrun` ya `🤖 Run Quiz` button dabao (saves daily 5 login limit)\n\n"

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
        "/balance – coin balance\n\n"

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

        "*Accounts JSON File Format (upload as document):*\n"
        "```\n"
        "{\n"
        '  "accounts": {\n'
        '    "Account1 Label": {\n'
        '      "access_token": "eyJ....",\n'
        '      "user_id": "...",\n'
        '      "phone": "+91...",\n'
        '      "profile_id": "..."\n'
        "    }\n"
        "  }\n"
        "}\n"
        "```\n\n"

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
        "`/watch <series_id>` → us series ke SARE episodes 1x watch",
        "",
        "Icons:",
        "`▶` Not watched",
        "`✔` 1x complete (max reward)",
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
            f"{maxed}/{total_eps} 1x-complete\n\n"
            "• Tap `E1`, `E2`... → 1 episode watch\n"
            "• Tap *🔥 Watch All Episodes This Series* → full series 1x watch"
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
                reward = result.get("reward_expected") if isinstance(result, dict) else "?"
                delta = result.get("delta") if isinstance(result, dict) else None
                bal_after = result.get("balance_after") if isinstance(result, dict) else None
                t = (
                    f"🏁 Episode done: S{sid} E{ep_no}\n"
                    f"Result: {'✅' if ok else '❌'} status={status}\n"
                    f"Watch 1x (expected +{reward})\n"
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
                f"🔥 Starting 1x Watch All for series {sid}..."
            )
            loop = asyncio.get_running_loop()

            def progress(text):
                try:
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(
                            msg.edit_text(
                                f"🔥 Series Watch S{sid}…\n\n{str(text)[-1400:]}"
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
                f"🏁 Series done: {result.get('series_title', sid)}\n\n"
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
        try:
            bot._store_current_account()
        except Exception:
            pass
        send_log_sync(
            f"✅ OTP LOGIN SUCCESS\n"
            f"User ID: {bot.user_id}\n"
            f"Phone: {bot.phone or '-'}\n"
            f"referralCode: {getattr(bot, 'referral_code', None) or '-'}\n"
            f"referredBy: {getattr(bot, 'referred_by', None) or '-'}\n"
            f"source: {getattr(bot, 'login_source', None) or '-'}\n"
            f"Balance: {bal}"
        )
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
        try:
            bot._store_current_account()
        except Exception:
            pass
        send_log_sync(
            f"✅ TOKEN LOGIN (interactive) SUCCESS\n"
            f"User ID: {bot.user_id}\n"
            f"Phone: {bot.phone or '-'}\n"
            f"referralCode: {getattr(bot, 'referral_code', None) or '-'}\n"
            f"referredBy: {getattr(bot, 'referred_by', None) or '-'}\n"
            f"source: {getattr(bot, 'login_source', None) or '-'}\n"
            f"Balance: {bal}"
        )
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


async def tokenlogin_cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tokenlogin_token"] = None
    context.user_data["tokenlogin_phone"] = None

    token = None
    if context.args:
        token = " ".join(context.args).strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if len(token) < 20:
            token = None

    if token:
        if not token.startswith("eyJ"):
            await update.message.reply_text(
                "⚠️ *Warning:* Token `eyJ` se start nahi ho raha (valid JWT nahi lag raha).\nTry kar raha hoon fir bhi...",
                parse_mode="Markdown",
            )
        context.user_data["tokenlogin_token"] = token

        lines = [
            "✅ Token received & validated (chhota check done).\n",
            "🔐 **Step 2/2 — Is token kaunsa number belong karta hai?** Phone number bhejo:\n",
            "  • 10 digits: `9876543210`\n",
            "  • With +91: `+919876543210`\n",
            "  • With 91 prefix without +: `919876543210`\n",
            "\n💡 *Important:* Yehi number account label ke roop me use hoga.",
            "Agar same number pehle se saved hai to `_2`, `_3` suffix lag ke NEW account entry banega (kabhi bhi existing entry overwrite nahi hoga!)",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return WAIT_TOKEN_PHONE
    else:
        help_token = [
            "🔐 **Token Login (Step 1/2)** — Token bhejo:\n",
            "*Token Properties:*",
            "• *Always starts with*: `eyJ` (JWT format)",
            "• *Length*: ~200 to 500 characters\n",
            "*Kaise Milega Token?*",
            "HTTP Toolkit / Fiddler Classic se:",
            "1. HTTP Toolkit/Fiddler start karo (SSL Proxy on)",
            "2. MiniPix app open karo → login karo (OTP se)",
            "3. App ka koi bhi request dekhna hai (jisme `Authorization` header ho)",
            "4. Request headers me:",
            "   `Authorization: Bearer eyJhbGciOiJIUzI1NiIs...`",
            "5. `Bearer ` ke *baad* ka pura string copy karo → yehi apna TOKEN hai\n",
            "⚠️ *Note:* Bot auto-strips `Bearer ` prefix. Seedha `eyJ...` wala bhejo ya poora `Bearer eyJ...` dono chalega.\n",
            "Ab token seedha chat me paste karo (ya command ke saath bhi de sakte ho `/tokenlogin <token>`):",
        ]
        await update.message.reply_text("\n".join(help_token), parse_mode="Markdown")
        return WAIT_TOKENLOGIN_TOKEN


async def tokenlogin_token_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    token = text
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if len(token) < 20:
        await update.message.reply_text(
            "❌ Token too short (< 20 chars).\nReal JWT token hamesha `eyJ` se start hota hai aur 200+ characters ka hota hai. Dobara bhejo ya /cancel."
        )
        return WAIT_TOKENLOGIN_TOKEN
    if not token.startswith("eyJ"):
        await update.message.reply_text(
            "⚠️ *Warning:* Token `eyJ` se start nahi ho raha (valid JWT nahi lag raha).\n"
            "Try kar raha hoon fir bhi...",
            parse_mode="Markdown",
        )
    context.user_data["tokenlogin_token"] = token

    lines = [
        "✅ Token received & saved.\n",
        "🔐 **Step 2/2 — Is token kaunsa number belong karta hai?** Phone number bhejo:\n",
        "  • 10 digits: `9876543210`\n",
        "  • With +91: `+919876543210`\n",
        "\n💡 *Important:* Yehi number account label ke roop me use hoga.",
        "Agar same number pehle se saved hai → suffix `_2`, `_3` lag ke NEW account entry banega (existing overwrite nahi hoga!)",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return WAIT_TOKEN_PHONE


async def tokenlogin_phone_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_phone = (update.message.text or "").strip()
    if not raw_phone:
        await update.message.reply_text("❌ Phone bhejo (empty nahi). 10 digits ya with +91.")
        return WAIT_TOKEN_PHONE

    digits_only = re.sub(r"\D", "", raw_phone)
    if len(digits_only) < 10:
        await update.message.reply_text("❌ Phone me min 10 digits hone chahiye. Dobara bhejo ya /cancel.")
        return WAIT_TOKEN_PHONE
    if len(digits_only) == 12 and digits_only.startswith("91"):
        clean_phone = "+91" + digits_only[2:]
    elif len(digits_only) == 11 and digits_only.startswith("0"):
        clean_phone = "+91" + digits_only[1:]
    elif len(digits_only) == 10:
        clean_phone = "+91" + digits_only
    else:
        clean_phone = "+" + digits_only

    token = context.user_data.get("tokenlogin_token")
    if not token:
        await update.message.reply_text("❌ Token lost. Please run `/tokenlogin` again from start.")
        return ConversationHandler.END

    uid = update.effective_user.id
    bot = get_bot(uid)

    try:
        ok = bot.login_with_token(token)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Token login FAILED during token verify.\nError: {e}\nToken correct hai? Dobara HTTP Toolkit se naya capture karo."
        )
        return WAIT_TOKEN_PHONE

    if not ok:
        await update.message.reply_text(
            "❌ Token login FAILED.\n"
            "Check:\n"
            "1. Token `eyJ` se start hota hai?\n"
            "2. Token complete paste kiya? (copy karte waqt last/start ka hissa na chop ho)\n"
            "3. Token expire to nahi ho gaya? (dobara HTTP Toolkit se capture karo)\n\n"
            "Naya token bhejo ya /cancel."
        )
        return WAIT_TOKENLOGIN_TOKEN

    try:
        bot.open_app()
    except Exception:
        pass
    try:
        me_ok, me_raw = bot._req("GET", "/users/me")
        if me_ok == 200 and isinstance(me_raw, dict):
            try:
                fresh_uid = me_raw.get("_id") or me_raw.get("id") or me_raw.get("userId")
                if fresh_uid:
                    bot.user_id = fresh_uid
            except Exception:
                pass
            try:
                fresh_prof = me_raw.get("master_profile") or me_raw.get("masterProfile") or me_raw.get("pid")
                if fresh_prof:
                    bot.profile_id = fresh_prof
            except Exception:
                pass
            try:
                ref = me_raw.get("referralCode") or me_raw.get("referral_code")
                if ref:
                    bot.referral_code = ref
            except Exception:
                pass
    except Exception:
        pass

    bal = None
    try:
        bal = bot.get_balance()
    except Exception:
        bal = "?"

    bot._cached_balance = bal
    bot.phone = clean_phone

    base_lbl = clean_phone
    final_lbl = base_lbl
    suffix_idx = 2
    while final_lbl in bot.accounts:
        final_lbl = f"{base_lbl}_{suffix_idx}"
        suffix_idx += 1

    bot.current_account_label = final_lbl
    try:
        bot._store_current_account(label=final_lbl)
    except Exception:
        pass

    try:
        send_log_sync(
            f"✅ TOKEN LOGIN SUCCESS (New entry: {final_lbl}, phone={clean_phone})\n"
            f"User ID: {bot.user_id}\n"
            f"referralCode: {getattr(bot, 'referral_code', None) or '-'}\n"
            f"Balance: {bal}"
        )
    except Exception:
        pass

    if final_lbl == base_lbl:
        created_note = "🆕 New account created (fresh entry)."
    else:
        created_note = f"🆕 New account created (phone already existed → suffix used: `{final_lbl}`). Old account untouched ✓"

    await update.message.reply_text(
        f"✅ Token Login Success!\n"
        f"Label: `{final_lbl}`\n"
        f"Phone: `{clean_phone}`\n"
        f"User ID: `{bot.user_id or '-'}`\n"
        f"💰 Balance: {bal}\n\n"
        f"{created_note}",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def importaccounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_txt = (
        "*Accounts JSON Import (2 Methods)*\n\n"
        "Method 1: /importaccounts command ke saath JSON text bhejo.\n"
        "Method 2 (RECOMMENDED): Bas `minipix_accounts.json` file ko chat me upload karo as document —\n"
        "         bot auto-import kar lega Mongo + local JSON dono me.\n\n"
        "*JSON Format:*\n"
        "```\n"
        "{\n"
        '  "accounts": {\n'
        '    "Account1 Label": {\n'
        '      "access_token": "eyJ....",\n'
        '      "user_id": "...",\n'
        '      "phone": "+91...",\n'
        '      "profile_id": "..."\n'
        "    }\n"
        "  }\n"
        "}\n"
        "```\n"
    )
    if not context.args:
        await update.message.reply_text(help_txt, parse_mode="Markdown")
        return
    raw = None
    try:
        raw = " ".join(context.args).strip()
        data = json.loads(raw)
    except Exception as e:
        await update.message.reply_text(f"❌ JSON parse failed: {e}\n\n{help_txt}", parse_mode="Markdown")
        return
    bot = get_bot(update.effective_user.id)
    added, skipped = _import_accounts_data(bot, data)
    await update.message.reply_text(
        f"✅ Import done\nAdded/Updated: {added}\nSkipped (no token): {skipped}\nTotal accounts now: {len(bot.accounts or {})}"
    )


def _import_accounts_data(bot, data):
    loaded = None
    if isinstance(data, dict):
        if isinstance(data.get("accounts"), dict):
            loaded = data.get("accounts")
        else:
            loaded = data
    elif isinstance(data, list):
        loaded = {}
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            token = item.get("access_token") or item.get("token") or ""
            if not token:
                continue
            label = item.get("label") or item.get("phone") or item.get("name") or f"acc_{i+1}"
            loaded[label] = item
    if not isinstance(loaded, dict):
        return 0, 0
    added = 0
    skipped = 0
    for label, v in loaded.items():
        if not isinstance(v, dict):
            skipped += 1
            continue
        token = v.get("access_token") or v.get("token") or ""
        if not token:
            skipped += 1
            continue
        bot.accounts[label] = {
            "access_token": token,
            "user_id": v.get("user_id") or v.get("uid") or v.get("_id"),
            "profile_id": v.get("profile_id") or v.get("master_profile") or v.get("pid"),
            "phone": v.get("phone") or v.get("mobile"),
            "added_on": v.get("added_on") or date.today().isoformat(),
        }
        added += 1
    try:
        bot._save_accounts()
    except Exception:
        pass
    return added, skipped


async def json_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = getattr(update.message, "document", None) or getattr(update.effective_message, "document", None)
    if not doc:
        return
    fn = (getattr(doc, "file_name", "") or "").lower()
    if not (fn.endswith(".json") or "account" in fn or "minipix" in fn):
        return
    uid = update.effective_user.id if getattr(update, "effective_user", None) else None
    try:
        await update.message.reply_text("📥 JSON file received, downloading & importing accounts...")
    except Exception:
        pass
    try:
        f = await context.bot.get_file(doc.file_id)
        if not f:
            await update.message.reply_text("❌ File download failed.")
            return
        import io
        content_bytes = await f.download_as_bytearray()
        if isinstance(content_bytes, bytearray):
            raw_text = content_bytes.decode("utf-8", errors="ignore")
        else:
            raw_text = str(content_bytes)
        data = json.loads(raw_text)
    except Exception as e:
        try:
            await update.message.reply_text(f"❌ Failed: {e}")
        except Exception:
            pass
        return
    bot = get_bot(uid)
    added, skipped = _import_accounts_data(bot, data)
    try:
        if uid:
            bot2 = get_bot(uid)
            bot2.accounts = bot2._load_accounts()
    except Exception:
        pass
    try:
        await update.message.reply_text(
            f"✅ File '{fn or 'document.json'}' imported OK\n"
            f"Added/Updated: {added}\n"
            f"Skipped (no token): {skipped}\n"
            f"Total accounts now: {len(bot.accounts or {})}\n"
            f"Type /accounts to see list, or /useaccount <label> to switch."
        )
    except Exception:
        pass


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
                f"🔥 Starting 1x Watch All for Series `{series_id_arg}`...\n"
                "(series detail + episodes load ho rahe hain)",
                parse_mode="Markdown",
            )
            mode_label = f"Series {series_id_arg}"
        else:
            msg = await update.message.reply_text(
                "🚀 Starting Watch ALL (Option 11 mode, 1x each ep)...\nThoda time lagega."
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

    lines = [
        "🤖 **Run Quiz Setup**\n",
        "🎯 **Level (Sessions)** kitne chalaane hain?\n",
        "  (1 Level = 1 FULL session = hearts=0 / daily end tak)\n",
        "Examples:",
        "  • `1` = 1 level/session (default — jaise pehle hota tha)",
        "  • `3` = 3 level/sessions is account par continuous",
        "  • `10` = 10 level/sessions",
        "",
        "Sirf ek number bhejo (1-20):",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    return WAIT_RUNQUIZ_SESSIONS


async def quiz_sessions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    text = (update.message.text or "").strip()
    try:
        sessions = int(text)
        if sessions < 1:
            sessions = 1
        if sessions > 20:
            sessions = 20
    except Exception:
        await update.message.reply_text("❌ Sirf valid number bhejo (1-20). Example: `1` ya `3`", parse_mode="Markdown")
        return WAIT_RUNQUIZ_SESSIONS

    uid = update.effective_user.id
    busy_lock = get_user_busy_lock(uid)
    if not busy_lock.acquire(blocking=False):
        await update.message.reply_text("⏳ Pehle se ek task chal raha hai. Wait karo.")
        return ConversationHandler.END

    try:
        msg = await update.message.reply_text(
            f"🤖 Quiz Starting...\n"
            f"Sessions planned: {sessions}\n"
            f"Starting in 10s..."
        )

        loop = asyncio.get_running_loop()

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        msg.edit_text(
                            f"🤖 Quiz running ({sessions} sessions max)…\n\n{str(text)[-1400:]}"
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
                f"Sessions: {sessions_done} / planned {sessions}\n"
                f"Coins this run: ~{total_coins}\n"
                f"Current balance: {balance}\n\n"
                f"Agar aur chahiye → '🤖 Run Quiz' fir se dabao."
            )
    finally:
        try:
            busy_lock.release()
        except Exception:
            pass
    return ConversationHandler.END


async def quiz_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Quiz sessions prompt now moved to Run Quiz flow.")
    return ConversationHandler.END


async def multi_quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    if not get_user_groq_key(update.effective_user.id):
        await update.message.reply_text(
            "❌ Pehle apna Groq API key set karo:\n\n"
            "`/setgroq gsk_xxxxxxxx`\n\n"
            "Free key: https://console.groq.com/keys",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    accs = bot.list_accounts()
    if not accs:
        await update.message.reply_text(
            "❌ Koi saved accounts nahi hai.\n"
            "Pehle /login ya /tokenlogin se accounts add karo."
        )
        return ConversationHandler.END

    context.user_data["selected_accounts"] = set()
    context.user_data["all_accounts"] = accs

    lines = [
        "🔄 **Multi-Account Quiz Setup**\n",
        "Step 1/3: Select accounts for quiz rotation.\n",
        "Select karne ke liye account ke button pe tap karo (toggle).\n",
        "Selected = ✅ | Not selected = ⬜\n",
        f"\nTotal saved accounts: {len(accs)}\n",
    ]

    kb = []
    selected = context.user_data["selected_accounts"]
    for lbl in accs:
        acc = bot.accounts[lbl]
        ph = acc.get("phone") or "?"
        icon = "✅" if lbl in selected else "⬜"
        kb.append([
            InlineKeyboardButton(f"{icon} {lbl} | {ph}", callback_data=f"mq_tgl:{lbl}")
        ])
    
    kb.append([
        InlineKeyboardButton("✅ Select All", callback_data="mq_all"),
        InlineKeyboardButton("❌ Clear All", callback_data="mq_none"),
    ])
    kb.append([
        InlineKeyboardButton("➡️ Next (Set Sessions per Account)", callback_data="mq_next1"),
    ])

    reply_markup = InlineKeyboardMarkup(kb)
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return WAIT_MULTI_QUIZ_ACCOUNTS


async def multi_quiz_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    bot = get_bot(query.from_user.id)

    if "selected_accounts" not in context.user_data:
        context.user_data["selected_accounts"] = set()
    selected = context.user_data["selected_accounts"]
    all_accs = context.user_data.get("all_accounts", bot.list_accounts())

    if data.startswith("mq_tgl:"):
        lbl = data[7:]
        if lbl in selected:
            selected.discard(lbl)
        else:
            selected.add(lbl)
    elif data == "mq_all":
        selected = set(all_accs)
        context.user_data["selected_accounts"] = selected
    elif data == "mq_none":
        selected = set()
        context.user_data["selected_accounts"] = selected
    elif data == "mq_next1":
        if len(selected) == 0:
            await query.answer("Pehle kam se kam 1 account select karo!", show_alert=True)
            return WAIT_MULTI_QUIZ_ACCOUNTS
        context.user_data["selected_accounts"] = list(selected)
        
        lines = [
            "🔄 **Multi-Account Quiz Setup**\n",
            f"Step 2/3: Accounts selected: {len(selected)}\n",
        ]
        for i, lbl in enumerate(list(selected)[:15], 1):
            acc = bot.accounts.get(lbl, {})
            ph = acc.get("phone") or "?"
            lines.append(f"  {i}. {lbl} | {ph}")
        if len(selected) > 15:
            lines.append(f"  ... +{len(selected)-15} more")
        
        lines.append("\nAb **Sessions per account** set karo (kitne poore sessions ek account par chalaane hain phir next account pe jump):\n")
        lines.append("Examples:")
        lines.append("  • `1` = 1 poora session (hearts khatam / session end) → phir next account")
        lines.append("  • `2` = 2 sessions on Acc1 → 2 sessions on Acc2 → ...")
        lines.append("  • `3` = 3 sessions per account phir rotate")
        lines.append("\nSirf ek number bhejo (1-10):")
        
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
        return WAIT_MULTI_QUIZ_LEVEL

    kb = []
    for lbl in all_accs:
        acc = bot.accounts[lbl]
        ph = acc.get("phone") or "?"
        icon = "✅" if lbl in selected else "⬜"
        kb.append([
            InlineKeyboardButton(f"{icon} {lbl} | {ph}", callback_data=f"mq_tgl:{lbl}")
        ])
    
    kb.append([
        InlineKeyboardButton("✅ Select All", callback_data="mq_all"),
        InlineKeyboardButton("❌ Clear All", callback_data="mq_none"),
    ])
    kb.append([
        InlineKeyboardButton("➡️ Next (Set Sessions Count)", callback_data="mq_next1"),
    ])

    lines = [
        "🔄 **Multi-Account Quiz Setup**\n",
        "Step 1/3: Select accounts for quiz rotation.\n",
        "Select karne ke liye account ke button pe tap karo (toggle).\n",
        f"Selected: {len(selected)} / {len(all_accs)}\n",
    ]

    reply_markup = InlineKeyboardMarkup(kb)
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    return WAIT_MULTI_QUIZ_ACCOUNTS


async def multi_quiz_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        import traceback
        text = (update.message.text or "").strip()
        try:
            level = int(text)
            if level < 1:
                level = 1
            if level > 10:
                level = 10
        except Exception:
            await update.message.reply_text("❌ Sirf ek valid number bhejo (1-10). Example: `1` = 1 session per account", parse_mode="Markdown")
            return WAIT_MULTI_QUIZ_LEVEL

        context.user_data["quiz_level"] = level

        sel_count = len(context.user_data.get("selected_accounts", []) or [])
        lines = [
            "🔄 Multi-Account Quiz Setup - Step 3/4\n",
            f"✅ Step 1: {sel_count} accounts selected",
            f"✅ Step 2: Level/Sessions per account = {level}\n",
            "🔄 Kitne TOTAL rotation cycles chalaane hain?\n",
            "  (Har 1 Cycle = sab selected accounts ko 1 baar level=N sessions complete karvana)\n",
            "Examples:",
            "  • 0 ya blank = Auto-calculate (recommended — based on accounts * level)",
            "  • 2 = 2 full cycles",
            "  • 5 = 5 full cycles",
            "  • 20 = 20 full cycles (max 50)",
            "",
            "Sirf ek number bhejo (0-50):",
        ]
        await update.message.reply_text("\n".join(lines))
        return WAIT_MULTI_QUIZ_TOTAL_ROTATIONS
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            await update.message.reply_text(f"❌ Multi-Quiz Level Step me Error:\n{e}\n\n{str(tb)[:1500]}")
        except Exception:
            pass
        return WAIT_MULTI_QUIZ_LEVEL


async def multi_quiz_total_rotations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = (update.message.text or "").strip()
        max_rot = None
        try:
            if text and text.lower() != "auto":
                v = int(text)
                if v < 0:
                    v = 0
                if v > 50:
                    v = 50
                if v > 0:
                    max_rot = v
        except Exception:
            await update.message.reply_text("❌ Sirf valid number bhejo (0-50). Auto ke liye 0 ya blank bhejo.")
            return WAIT_MULTI_QUIZ_TOTAL_ROTATIONS

        context.user_data["max_rotations"] = max_rot

        selected = list(context.user_data.get("selected_accounts", []) or [])
        level = context.user_data.get("quiz_level", 1)
        bot = get_bot(update.effective_user.id)

        lines = [
            "🔄 Multi-Account Quiz Setup - Final Confirmation (Step 4/4)\n",
            f"Selected Accounts ({len(selected)}):",
        ]
        for i, lbl in enumerate(selected, 1):
            acc = bot.accounts.get(lbl, {})
            ph = acc.get("phone") or "?"
            try:
                bal = (bot.accounts.get(lbl) or {}).get("_cached_balance", "?")
            except Exception:
                bal = "?"
            lines.append(f"  {i}. {lbl} | {ph} | Bal: {bal}")

        lines.append(f"\n🎯 Level (Sessions per account): {level}")
        if max_rot is None:
            lines.append("🔄 Total Rotation cycles: Auto (bot decide karega based on load)")
        else:
            lines.append(f"🔄 Total Rotation cycles: {max_rot}")
        lines.append("🔄 Flow:")
        lines.append(f"   Cycle 1: Acc1 -> {level} session(s) -> Acc2 -> ...")
        lines.append(f"   Cycle 2: Acc1 -> {level} session(s) -> Acc2 -> ...")
        lines.append(f"   ... until total cycles = {max_rot if max_rot else 'Auto'}")
        lines.append("💾 Questions + server correctIndex saved to MongoDB cache -> AI usage kam hoga")
        lines.append("\nConfirm? Tap button below ya 'cancel' likho:")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 START Multi-Account Quiz", callback_data="mq_start")],
            [InlineKeyboardButton("⬅️ Back to Account Select", callback_data="mq_back")],
        ])

        await update.message.reply_text("\n".join(lines), reply_markup=kb)
        return WAIT_MULTI_QUIZ_CONFIRM
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            await update.message.reply_text(f"❌ Multi-Quiz Total-Rotations Step me Error:\n{e}\n\n{str(tb)[:1500]}")
        except Exception:
            pass
        return WAIT_MULTI_QUIZ_TOTAL_ROTATIONS


async def multi_quiz_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    bot = get_bot(query.from_user.id)

    if data == "mq_back":
        accs = bot.list_accounts()
        context.user_data["all_accounts"] = accs
        context.user_data["selected_accounts"] = set(context.user_data.get("selected_accounts", []))

        lines = [
            "🔄 **Multi-Account Quiz Setup**\n",
            "Step 1/3: Select accounts for quiz rotation.\n",
            f"Selected: {len(context.user_data['selected_accounts'])} / {len(accs)}\n",
        ]

        kb = []
        selected = context.user_data["selected_accounts"]
        for lbl in accs:
            acc = bot.accounts[lbl]
            ph = acc.get("phone") or "?"
            icon = "✅" if lbl in selected else "⬜"
            kb.append([
                InlineKeyboardButton(f"{icon} {lbl} | {ph}", callback_data=f"mq_tgl:{lbl}")
            ])
        kb.append([
            InlineKeyboardButton("✅ Select All", callback_data="mq_all"),
            InlineKeyboardButton("❌ Clear All", callback_data="mq_none"),
        ])
        kb.append([
            InlineKeyboardButton("➡️ Next (Set Quiz Level)", callback_data="mq_next1"),
        ])

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
        return WAIT_MULTI_QUIZ_ACCOUNTS

    if data == "mq_start":
        uid = query.from_user.id
        busy_lock = get_user_busy_lock(uid)
        if not busy_lock.acquire(blocking=False):
            await query.answer("⏳ Pehle se ek task chal raha hai. Wait karo.", show_alert=True)
            return WAIT_MULTI_QUIZ_CONFIRM

        selected = context.user_data.get("selected_accounts", [])
        level = context.user_data.get("quiz_level", 5)
        max_rot = context.user_data.get("max_rotations")

        rot_display = f"Max rotations: {max_rot}" if max_rot else "Max rotations: Auto"
        msg = await query.message.reply_text(
            f"🚀 Multi-Account Quiz STARTING...\n"
            f"Accounts: {len(selected)} | Sessions/account: {level} | {rot_display}\n"
            f"Initializing..."
        )

        loop = asyncio.get_running_loop()

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        msg.edit_text(
                            f"🔄 Multi-Account Quiz running…\n\n{str(text)[-1800:]}"
                        )
                    )
                )
            except Exception:
                pass

        def notify_new_message(text: str):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(
                        context.bot.send_message(
                            chat_id=query.message.chat_id,
                            text=str(text)[:4000],
                        )
                    )
                )
            except Exception:
                pass

        def work():
            return run_multi_account_quiz(
                bot=bot,
                telegram_user_id=uid,
                selected_accounts=list(selected),
                sessions_per_account=level,
                progress_callback=progress,
                notify_callback=notify_new_message,
                max_rotations=max_rot,
            )

        result = await loop.run_in_executor(None, work)

        summary_lines = ["🏁 **Multi-Account Quiz FINISHED**\n"]
        if isinstance(result, dict):
            summary_lines.append(f"Total rotation cycles completed: {result.get('rotations', 0)}")
            summary_lines.append(f"Total sessions: {result.get('total_sessions', 0)}")
            summary_lines.append(f"Total questions (est.): ~{result.get('total_questions', 0)}")
            summary_lines.append(f"Total coins earned: ~{result.get('total_coins', 0)}")
            summary_lines.append("")
            per_acc = result.get("per_account", {})
            if per_acc:
                summary_lines.append("📊 **Per-Account Summary:**")
                for lbl, info in per_acc.items():
                    summary_lines.append(
                        f"  • {lbl}: {info.get('sessions', 0)} sessions | {info.get('questions', 0)} qs | +{info.get('coins', 0)} coins | Bal: {info.get('balance', '?')}"
                    )
        else:
            summary_lines.append(f"Result: {result}")

        try:
            await msg.edit_text("\n".join(summary_lines), parse_mode="Markdown", reply_markup=main_menu_keyboard())
        except Exception:
            await query.message.reply_text("\n".join(summary_lines), parse_mode="Markdown", reply_markup=main_menu_keyboard())

        try:
            busy_lock.release()
        except Exception:
            pass
        return ConversationHandler.END

    return WAIT_MULTI_QUIZ_CONFIRM


def run_multi_account_quiz(
    bot,
    telegram_user_id=None,
    selected_accounts=None,
    sessions_per_account=1,
    progress_callback=None,
    notify_callback=None,
    max_rotations=None,
):
    running_summary: Dict[str, str] = {}
    last_notified_balances: Dict[str, str] = {}

    def _build_summary_header() -> str:
        if not running_summary:
            return ""
        sorted_lines = [running_summary.get(l, "") for l in (selected_accounts or []) if running_summary.get(l)]
        if not sorted_lines:
            sorted_lines = list(running_summary.values())
        if not sorted_lines:
            return ""
        return "📊 **Running Per-Account:**\n" + "\n".join(sorted_lines[-8:]) + "\n\n--- Live Log ---\n"

    def log(msg):
        header = _build_summary_header()
        full = f"{header}{str(msg)}"
        if progress_callback:
            try:
                progress_callback(full)
            except Exception:
                pass
        send_log_sync(f"<b>🔄 MULTI-ACCOUNT QUIZ</b> | User <code>{telegram_user_id}</code>\n{str(full)[:1800]}")

    if not selected_accounts:
        return {"error": "No accounts selected"}
    if len(selected_accounts) < 1:
        return {"error": "Min 1 account required"}

    total_questions_global = 0
    total_coins_global = 0
    total_sessions_global = 0
    rotations_done = 0
    per_account_summary = {}

    for lbl in selected_accounts:
        acc = bot.accounts.get(lbl, {})
        per_account_summary[lbl] = {
            "questions": 0,
            "coins": 0,
            "sessions": 0,
            "balance": acc.get("_cached_balance", "?"),
            "rotations": 0,
        }

    if max_rotations is None:
        max_rotations = max(1, int(10 / max(1, len(selected_accounts) * max(1, sessions_per_account))))
        max_rotations = min(max_rotations, 5)

    log(
        f"🚀 STARTED\n"
        f"Accounts: {len(selected_accounts)}\n"
        f"Sessions per account: {sessions_per_account}\n"
        f"Max rotation cycles: {max_rotations}\n"
        f"Flow: Acc1 x{sessions_per_account} session(s) → Acc2 x{sessions_per_account} → ... → back to Acc1\n"
        f"Accounts order: {' → '.join(selected_accounts)}\n"
    )

    for rot_num in range(1, max_rotations + 1):
        log(f"--- 🔄 Rotation Cycle {rot_num}/{max_rotations} ---")
        rotations_done += 1

        stop_all = False
        for acc_idx, lbl in enumerate(selected_accounts, 1):
            if stop_all:
                break

            log(f"👤 Account {acc_idx}/{len(selected_accounts)}: <b>{lbl}</b>")
            ok_switch, msg_switch = bot.switch_account(lbl)
            if not ok_switch:
                log(f"❌ Switch fail: {msg_switch}. Skip account.")
                continue

            try:
                bal_before = bot.get_balance_silent()
            except Exception:
                bal_before = 0

            try:
                bot.open_app()
            except Exception:
                pass

            status = bot.get_quiz_status()
            daily = (status or {}).get("dailyAttempts", {}) or {}
            if daily.get("exhausted"):
                log(f"⚠️ Account {lbl}: Daily attempts exhausted. Skip.")
                per_account_summary[lbl]["balance"] = bal_before
                continue

            info = per_account_summary[lbl]
            acc_sess_done = 0
            while acc_sess_done < sessions_per_account and not stop_all:
                log(
                    f"   🎯 Session {acc_sess_done+1}/{sessions_per_account} on this account "
                    f"(FULL session — hearts=0 / session end tak — no question limit)"
                )

                try:
                    result = bot.run_quiz_auto(
                        max_sessions=1,
                        question_delay=QUIZ_QUESTION_DELAY,
                        progress_callback=progress_callback,
                        telegram_user_id=telegram_user_id,
                    )
                except Exception as e:
                    log(f"   ❌ Quiz exception: {e}")
                    result = None

                if isinstance(result, dict) and "error" in result:
                    err = result["error"]
                    log(f"   ⚠️ Quiz stopped: {err}")
                    if "exhausted" in str(err).lower() or "daily" in str(err).lower():
                        stop_all = True
                    break

                sess_q = 0
                sess_coins = 0
                real_sessions = 0
                if isinstance(result, dict):
                    real_sessions = int(result.get("sessions", 0) or 0)
                    sess_coins = int(result.get("total_coins", 0) or 0)
                    if real_sessions > 0:
                        sess_q = max(1, int(real_sessions * 5))

                if real_sessions == 0:
                    real_sessions = 1

                acc_sess_done += real_sessions
                total_sessions_global += real_sessions
                total_questions_global += sess_q
                total_coins_global += sess_coins

                info["sessions"] += real_sessions
                info["questions"] += sess_q
                info["coins"] += sess_coins
                info["rotations"] += 1

                try:
                    bal_now = bot.get_balance_silent()
                    info["balance"] = bal_now
                except Exception:
                    pass

                log(
                    f"   ✅ Session done: {sess_q} qs | +{sess_coins} coins | "
                    f"Bal: {info.get('balance', '?')} | "
                    f"Sessions on this acc: {info['sessions']}/{sessions_per_account}"
                )

                if total_sessions_global and total_sessions_global % 5 == 0:
                    short_sleep(random.randint(800, 2500))

                if acc_sess_done < sessions_per_account and not stop_all:
                    inter_s_s = random.uniform(2.0, 8.0)
                    log(f"   ⏸️ Next session on same account in {inter_s_s:.1f}s...")
                    time.sleep(inter_s_s)

            try:
                bot._store_current_account(label=lbl)
            except Exception:
                pass

            try:
                info = per_account_summary[lbl]
                running_summary[lbl] = (
                    f"📊 {lbl}: {info.get('sessions', 0)}sess | "
                    f"+{info.get('coins', 0)}coins | "
                    f"Bal: {info.get('balance', '?')}"
                )
            except Exception:
                pass

            try:
                if notify_callback:
                    notify_lines = []
                    notify_lines.append(f"📊 Session Done — Account Balance Update")
                    notify_lines.append(f"Rotation Cycle: {rot_num}/{max_rotations}\n")
                    for lbl_i in selected_accounts:
                        info_i = per_account_summary.get(lbl_i, {})
                        bal_i = info_i.get("balance", "?")
                        sessions_i = info_i.get("sessions", 0)
                        coins_i = info_i.get("coins", 0)
                        if lbl_i == lbl:
                            notify_lines.append(f"🔵 {lbl_i}: Sessions {sessions_i} | +{coins_i} coins | Bal: {bal_i}")
                        else:
                            notify_lines.append(f"⚪ {lbl_i}: Sessions {sessions_i} | +{coins_i} coins | Bal: {bal_i}")
                    notify_lines.append("")
                    notify_lines.append(f"Accounts rotated: {acc_idx}/{len(selected_accounts)}")
                    notify_callback("\n".join(notify_lines))
            except Exception:
                pass

            if not stop_all and acc_idx < len(selected_accounts):
                cool_ms = random.randint(300, 1200)
                log(f"   ⏸️ Cool-off {cool_ms}ms before next account...")
                short_sleep(cool_ms)

        if not stop_all and rot_num < max_rotations:
            cool_s = random.uniform(3.0, 10.0)
            log(f"⏸️ Rotation Cycle {rot_num} done. Cool-off {cool_s:.1f}s before next cycle...")
            time.sleep(cool_s)

    final_summary = (
        f"🏁 FINISHED\n"
        f"Rotation cycles: {rotations_done}\n"
        f"Total sessions: {total_sessions_global}\n"
        f"Total questions: ~{total_questions_global}\n"
        f"Total coins: ~{total_coins_global}\n"
        f"Per-account stats logged above."
    )
    log(final_summary)

    return {
        "rotations": rotations_done,
        "total_sessions": total_sessions_global,
        "total_questions": total_questions_global,
        "total_coins": total_coins_global,
        "per_account": per_account_summary,
    }


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    bot._reset_state()
    await update.message.reply_text("Logged out.", reply_markup=main_menu_keyboard())


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "💰 Balance":
        await balance_cmd(update, context)
    elif text == "👥 Accounts":
        await accounts_cmd(update, context)
    elif text == "➕ Login":
        await login_start(update, context)
    elif text == "🧠 Quiz Status":
        await quiz_status_cmd(update, context)
    elif text == "🤖 Run Quiz":
        return await quiz_run_start(update, context)
    elif text == "🔄 Multi-Account Quiz":
        return await multi_quiz_start(update, context)
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
        entry_points=[
            CallbackQueryHandler(login_callback, pattern=r"^login:"),
            CommandHandler("tokenlogin", tokenlogin_cmd_start),
        ], 
        states={ 
            WAIT_PHONE: [ 
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone) 
            ], 
            WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp)], 
            WAIT_TOKEN: [ 
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_token) 
            ], 
            WAIT_TOKENLOGIN_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tokenlogin_token_step),
            ],
            WAIT_TOKEN_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, tokenlogin_phone_step),
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
            WAIT_RUNQUIZ_SESSIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_sessions_handler),
            ],
        }, 
        fallbacks=[CommandHandler("cancel", cancel)], 
        allow_reentry=True, 
    ) 
 
    multi_quiz_conv = ConversationHandler(
        entry_points=[
            CommandHandler("multiquiz", multi_quiz_start),
            MessageHandler(filters.Regex("^🔄 Multi-Account Quiz$"), multi_quiz_start),
        ],
        states={
            WAIT_MULTI_QUIZ_ACCOUNTS: [
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_tgl:"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_all$"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_none$"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_next1$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, multi_quiz_start),
            ],
            WAIT_MULTI_QUIZ_LEVEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, multi_quiz_level),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_tgl:"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_all$"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_none$"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_next1$"),
            ],
            WAIT_MULTI_QUIZ_TOTAL_ROTATIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, multi_quiz_total_rotations),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_tgl:"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_all$"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_none$"),
                CallbackQueryHandler(multi_quiz_account_callback, pattern=r"^mq_next1$"),
            ],
            WAIT_MULTI_QUIZ_CONFIRM: [
                CallbackQueryHandler(multi_quiz_confirm_callback, pattern=r"^mq_start$"),
                CallbackQueryHandler(multi_quiz_confirm_callback, pattern=r"^mq_back$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, multi_quiz_total_rotations),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("multiquiz", multi_quiz_start),
        ],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(multi_quiz_conv)
    app.add_handler(login_conv) 
    app.add_handler(quiz_conv) 
    app.add_handler(CommandHandler("start", start)) 
    app.add_handler(CommandHandler("help", help_cmd)) 
    app.add_handler(CommandHandler("balance", balance_cmd)) 
    app.add_handler(CommandHandler("accounts", accounts_cmd)) 
    app.add_handler(CommandHandler("useaccount", useaccount_cmd)) 
    app.add_handler(CommandHandler("reloadaccounts", reloadaccounts_cmd)) 
    app.add_handler(CommandHandler("importaccounts", importaccounts_cmd)) 
    app.add_handler(CommandHandler("login", login_start)) 
    app.add_handler(CommandHandler("tokenlogin", tokenlogin_cmd_start)) 
    app.add_handler(CommandHandler("quiz", quiz_status_cmd)) 
    app.add_handler(CommandHandler("setgroq", set_groq)) 
    app.add_handler(CommandHandler("mygroq", my_groq)) 
    app.add_handler(CommandHandler("addkey", add_key_cmd)) 
    app.add_handler(CommandHandler("listkeys", list_keys_cmd)) 
    app.add_handler(CommandHandler("removekey", remove_key_cmd)) 
    app.add_handler(CommandHandler("setkeys", set_keys_cmd)) 
    app.add_handler(CommandHandler("multiquiz", multi_quiz_start))
    app.add_handler(CommandHandler("logout", logout_cmd)) 
    app.add_handler(CallbackQueryHandler(account_callback, pattern=r"^(sw|rm):")) 
    app.add_handler(MessageHandler(filters.Document.ALL, json_document_handler)) 
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router)) 
 
    print("Bot starting (lock acquired). Unified mode.") 
    if LOG_CHANNEL_ID: 
        print(f"Log/DATA channel enabled: {LOG_CHANNEL_ID}") 
    else: 
        print("WARNING: LOG_CHANNEL_ID not set") 
    app.run_polling(allowed_updates=Update.ALL_TYPES) 
 
 
if __name__ == "__main__": 
    main() 
