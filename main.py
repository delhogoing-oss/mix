#!/usr/bin/env python3
"""
MiniPix V2 Telegram Bot (Public Multi-User Version)
- Dual Groq API keys per user with automatic fallback on rate limit/error
- Public Multi-User isolation for accounts and sessions
- Active Task Cancellation via /stop command or stop button
"""

import os
import sys
import json
import time
import re
import logging
import asyncio
import atexit
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
QUIZ_QUESTION_DELAY = 10  # fixed to 10 seconds

GLOBAL_GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")

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

# Active tasks tracking for stop signal
stop_flags: Dict[int, bool] = {}


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


# ───────────────────── User Groq Keys (Supports Dual Keys) ─────────────────────
def load_user_groq_keys() -> dict:
    if os.path.exists(USER_GROQ_FILE):
        try:
            with open(USER_GROQ_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_user_groq_keys(data: dict):
    try:
        with open(USER_GROQ_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save groq keys: {e}")


user_groq_keys: dict = load_user_groq_keys()


def get_user_groq_keys(user_id: int) -> List[str]:
    keys = user_groq_keys.get(str(user_id))
    if isinstance(keys, list):
        return [k for k in keys if k]
    elif isinstance(keys, str) and keys:
        return [keys]
    if GLOBAL_GROQ_API_KEY:
        return [GLOBAL_GROQ_API_KEY]
    return []


# ───────────────────── MiniPix Core ─────────────────────
class MiniPixV2:
    def __init__(self, telegram_user_id: int):
        self.telegram_user_id = str(telegram_user_id)
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
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        user_accs = data.get(self.telegram_user_id, {})
                        if isinstance(user_accs, dict):
                            return user_accs.get("accounts", {})
            except Exception:
                return {}
        return {}

    def _save_accounts(self):
        data = {}
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not isinstance(data, dict):
                        data = {}
            except Exception:
                data = {}
        
        if self.telegram_user_id not in data or not isinstance(data[self.telegram_user_id], dict):
            data[self.telegram_user_id] = {}
        
        data[self.telegram_user_id]["accounts"] = self.accounts
        data[self.telegram_user_id]["saved_at"] = date.today().isoformat()

        try:
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False

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
                self._store_current_account(label)
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
        self._store_current_account(label)
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
        stop_flags[int(telegram_user_id)] = False

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

        for si, s in enumerate(all_series, 1):
            if stop_flags.get(int(telegram_user_id)):
                log("🛑 Task stopped by user.")
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

            log(f"[{si}/{len(all_series)}] {title}")
            done = 0
            for ep in episodes:
                if stop_flags.get(int(telegram_user_id)):
                    break
                if total_watched >= max_watches:
                    break
                ep_no = ep.get("episodeNo")
                kp = (str(sid), str(ep_no))
                cnt = watch_counts.get(kp, 0) + self.runtime_watch_counts.get(kp, 0)
                if cnt >= MAX_WATCHES_PER_EP:
                    continue
                ok, st = self.watch_episode(ep, s, allow_repeat=True, nth_watch=cnt + 1)
                if st == "skip":
                    total_skipped += 1
                elif ok:
                    done += 1
                    total_watched += 1
                else:
                    total_failed += 1
                try:
                    self.claim_reward_task(series_id=sid)
                except Exception:
                    pass
            if done:
                log(f" → {done} watches done")

        bal_end = self.get_balance_silent()
        delta = None
        if balance_before is not None and bal_end is not None:
            delta = bal_end - balance_before

        summary = (
            f"<b>🏁 WATCH FINISHED</b>\n"
            f"User: <code>{telegram_user_id}</code>\n"
            f"Watched: {total_watched} | Skipped: {total_skipped} | Failed: {total_failed}\n"
        )
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
            prompt += f" {i}: {opt}\n"
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
        user_keys = get_user_groq_keys(telegram_user_id) if telegram_user_id else []
        if not user_keys and GLOBAL_GROQ_API_KEY:
            user_keys = [GLOBAL_GROQ_API_KEY]

        if not user_keys:
            send_log_sync(f"❌ No Groq keys available for user <code>{telegram_user_id}</code>")
            return None, None, None

        prompt = self._build_quiz_prompt(question, options)

        # Dual token fallback evaluation loop
        for k_idx, api_key in enumerate(user_keys, start=1):
            for model in GROQ_MODELS:
                try:
                    from groq import Groq
                    client = Groq(api_key=api_key)

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
                        return idx, f"K{k_idx}:{model}", answer_text
                except Exception as e:
                    err = str(e).lower()
                    if "rate" in err or "limit" in err or "quota" in err or "429" in err:
                        send_log_sync(f"⏳ Key #{k_idx} Model {model} rate‑limited. Trying next.")
                        continue
                    logger.warning(f"Key #{k_idx} Model {model} failed: {e}")
                    continue

            # HTTP fallback for current API key
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
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
                        return idx, f"K{k_idx}:http-fallback", answer_text
            except Exception as e:
                send_log_sync(f"HTTP fallback error on Key #{k_idx}: {e}")

        return None, None, None

    def run_quiz_auto(
        self,
        max_sessions=3,
        question_delay=QUIZ_QUESTION_DELAY,
        progress_callback=None,
        telegram_user_id=None,
    ):
        stop_flags[int(telegram_user_id)] = False

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

        send_log_sync(
            f"🧠 QUIZ STARTED | User <code>{telegram_user_id}</code> | Sessions: {max_sessions}"
        )

        for session_num in range(1, max_sessions + 1):
            if stop_flags.get(int(telegram_user_id)):
                log("🛑 Quiz stopped by user.")
                break
            log(f"--- Session {session_num}/{max_sessions} ---")

            session_id, question_obj, session_meta = None, None, None
            for attempt in range(2):
                if stop_flags.get(int(telegram_user_id)):
                    break
                session_id, question_obj, session_meta = self.quiz_start_session()
                if session_id and question_obj:
                    break
                if attempt == 0:
                    log("⚠️ Session start failed, retrying in 3s...")
                    time.sleep(3)

            if stop_flags.get(int(telegram_user_id)):
                log("🛑 Quiz stopped by user.")
                break

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
                if stop_flags.get(int(telegram_user_id)):
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

                # Delay check loop to allow fast stopping
                for _ in range(question_delay):
                    if stop_flags.get(int(telegram_user_id)):
                        break
                    time.sleep(1)

                if stop_flags.get(int(telegram_user_id)):
                    break

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
                        except Exception:
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
                f"Questions: {q_count} | Correct: {correct_count} | Wrong: {wrong_count}\n"
                f"Coins earned: {session_coins}"
            )
            log(session_summary)
            send_log_sync(f"<b>{session_summary}</b>")

            sessions_done += 1
            if session_num < max_sessions and not stop_flags.get(int(telegram_user_id)):
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


def get_bot(user_id: int) -> MiniPixV2:
    if user_id not in user_bots:
        user_bots[user_id] = MiniPixV2(telegram_user_id=user_id)
    return user_bots[user_id]


def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💰 Balance"), KeyboardButton("📊 Campaign")],
            [KeyboardButton("👥 Accounts"), KeyboardButton("➕ Login")],
            [KeyboardButton("🎬 Watch All (4x)"), KeyboardButton("🧠 Quiz Status")],
            [KeyboardButton("🤖 Run Quiz"), KeyboardButton("🔑 Set Groq Key")],
            [KeyboardButton("🛑 Stop Task"), KeyboardButton("ℹ️ Help")],
        ],
        resize_keyboard=True,
    )


def stop_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Stop Active Task", callback_data="task:stop")]
    ])


# ───────────────────── Handlers ─────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot = get_bot(user.id)
    text = (
        f"👋 Hi {user.first_name}!\n\n"
        "MiniPix V2 Public Bot ready.\n\n"
        "• /setgroq – apna 1 ya 2 Groq API keys set karo\n"
        "• /login – apna account login karo\n"
        "• /watch – 4x watch automation\n"
        "• /quiz – quiz status check\n"
        "• /stop – running task ko stop karo\n"
    )
    if bot.access_token:
        text += f"\n✅ Logged in: {bot.current_account_label or bot.phone}"
    else:
        text += "\n⚠️ Not logged in → /login"
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "/start – main menu\n"
        "/balance – coin balance\n"
        "/campaign – watch campaign\n"
        "/accounts – list / switch accounts\n"
        "/login – OTP or Token login\n"
        "/watch – smart 4x watch\n"
        "/quiz – quiz status\n"
        "/setgroq `gsk_key1` `gsk_key2` – 1 ya 2 Groq keys set karein\n"
        "/mygroq – saved Groq keys check karein\n"
        "/stop – chal rahe task ko rokein\n"
        "/logout – logout\n\n"
        "Free keys: https://console.groq.com/keys",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def set_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n`/setgroq gsk_key1 gsk_key2`\n\n"
            "Aap ek saath 2 keys add kar sakte hain. Agar 1st limits ho jaye to 2nd auto use hoga.\n"
            "Free keys: https://console.groq.com/keys",
            parse_mode="Markdown",
        )
        return

    keys = [k.strip() for k in context.args if k.strip().startswith("gsk_")]
    if not keys:
        await update.message.reply_text("❌ Invalid key format. Must start with `gsk_`")
        return

    keys = keys[:2]
    user_id = str(update.effective_user.id)
    user_groq_keys[user_id] = keys
    save_user_groq_keys(user_groq_keys)
    
    await update.message.reply_text(
        f"✅ {len(keys)} Groq API key(s) saved!\nAb quiz solve kar sakte ho."
    )


async def my_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = get_user_groq_keys(update.effective_user.id)
    if keys:
        masked = [k[:10] + "..." + k[-4:] for k in keys]
        msg = "\n".join([f"Key {i+1}: `{m}`" for i, m in enumerate(masked)])
        await update.message.reply_text(f"✅ Saved Keys:\n{msg}", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "❌ Koi key set nahi hai.\n\n`/setgroq gsk_key1 gsk_key2`",
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
        lines.append(f"{i}. {lbl} | {ph}")
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
    uid = update.effective_user.id
    stop_flags[uid] = True
    await update.message.reply_text("🛑 Stop signal sent. Task jaldi hi ruk jayega.")


async def task_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    stop_flags[uid] = True
    await query.edit_message_text("🛑 Task stopping...")


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = get_bot(update.effective_user.id)
    if not bot.access_token:
        await update.message.reply_text("Not logged in. Use /login")
        return

    uid = update.effective_user.id
    stop_flags[uid] = False
    msg = await update.message.reply_text(
        "🚀 Starting smart 4x watch...\nThoda time lagega.",
        reply_markup=stop_inline_keyboard()
    )

    def progress(text):
        try:
            asyncio.get_event_loop().create_task(
                msg.edit_text(
                    f"🚀 Watching...\n\n{text[-900:]}",
                    reply_markup=stop_inline_keyboard()
                )
            )
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: bot.browse_and_watch_all_smart_repeat(
            progress_callback=progress,
            max_watches=250,
            telegram_user_id=uid,
        ),
    )

    if "error" in result:
        await msg.edit_text(f"❌ {result['error']}")
        return

    text = (
        f"🏁 Watch finished\n\n"
        f"Watched: {result['watched']}\n"
        f"Skipped: {result['skipped']}\n"
        f"Failed: {result['failed']}\n"
    )
    if result.get("delta") is not None:
        text += f"💰 {result['balance_before']} → {result['balance_after']} ({result['delta']:+d})"
    await msg.edit_text(text)


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
            "❌ Pehle apne Groq API key(s) set karo:\n\n"
            "`/setgroq gsk_key1 gsk_key2`\n\n"
            "Free keys: https://console.groq.com/keys",
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

    bot = get_bot(update.effective_user.id)
    sessions = context.user_data.get("quiz_sessions", 15)
    telegram_uid = update.effective_user.id
    stop_flags[telegram_uid] = False

    msg = await update.message.reply_text(
        f"🤖 Running {sessions} sessions (delay 10s)...",
        reply_markup=stop_inline_keyboard()
    )

    def progress(text):
        try:
            asyncio.get_event_loop().create_task(
                msg.edit_text(
                    f"🤖 Quiz running...\n\n{text[-900:]}",
                    reply_markup=stop_inline_keyboard()
                )
            )
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: bot.run_quiz_auto(
            max_sessions=sessions,
            question_delay=QUIZ_QUESTION_DELAY,
            progress_callback=progress,
            telegram_user_id=telegram_uid,
        ),
    )

    if "error" in result:
        await msg.edit_text(f"❌ {result['error']}")
    else:
        await msg.edit_text(
            f"🏁 Quiz done\n"
            f"Sessions: {result.get('sessions')}\n"
            f"Coins this run: ~{result.get('total_coins')}\n"
            f"Current balance: {result.get('balance')}"
        )
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
    elif text == "🎬 Watch All (4x)":
        await watch_cmd(update, context)
    elif text == "🧠 Quiz Status":
        await quiz_status_cmd(update, context)
    elif text == "🤖 Run Quiz":
        return await quiz_run_start(update, context)
    elif text == "🔑 Set Groq Key":
        await update.message.reply_text(
            "Apne 1 ya 2 Groq keys bhejo:\n`/setgroq gsk_key1 gsk_key2`\n\n"
            "Free keys: https://console.groq.com/keys",
            parse_mode="Markdown",
        )
    elif text == "🛑 Stop Task":
        await stop_cmd(update, context)
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
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("quiz", quiz_status_cmd))
    app.add_handler(CommandHandler("setgroq", set_groq))
    app.add_handler(CommandHandler("mygroq", my_groq))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CallbackQueryHandler(account_callback, pattern=r"^(sw|rm):"))
    app.add_handler(CallbackQueryHandler(task_stop_callback, pattern=r"^task:stop$"))
    app.add_handler(login_conv)
    app.add_handler(quiz_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    print("Bot starting (lock acquired).")
    if LOG_CHANNEL_ID:
        print(f"Log channel enabled: {LOG_CHANNEL_ID}")
    else:
        print("WARNING: LOG_CHANNEL_ID not set")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
