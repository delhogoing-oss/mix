#!/usr/bin/env python3
"""
MiniPix V2 Telegram Bot – Public Quiz Bypass
- Per‑user, per‑account Groq keys (up to 2 per account)
- Login data sent to log channel
- Multi‑account quiz (parallel) with stop button
- Keys stored permanently (reusable next day)
- All original features (watch, balance, campaign, etc.) preserved
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
from typing import Dict, Optional, List, Any

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
ACCOUNTS_FILE = "minipix_accounts.json"          # legacy – kept
USER_DATA_FILE = "user_data.json"                # new: stores per‑user account data + keys
LOCK_FILE = "bot.lock"

MAX_WATCHES_PER_EP = 4
REWARDS_BY_WATCH = {1: 15, 2: 8, 3: 5, 4: 3}
QUIZ_QUESTION_DELAY = 10

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

(WAIT_PHONE, WAIT_OTP, WAIT_TOKEN, WAIT_QUIZ_SESSIONS,
 WAIT_SETGROQ_ACCOUNT, WAIT_SETGROQ_KEY1, WAIT_SETGROQ_KEY2) = range(7)


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


# ───────────────────── User Data (per‑user, per‑account) ─────────────────────
def load_user_data() -> dict:
    if os.path.exists(USER_DATA_FILE):
        try:
            with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_user_data(data: dict):
    try:
        with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save user data: {e}")


user_data: dict = load_user_data()


def get_user_accounts(user_id: int) -> dict:
    return user_data.get(str(user_id), {}).get("accounts", {})


def get_account_groq_keys(user_id: int, account_label: str) -> List[str]:
    accs = get_user_accounts(user_id)
    if account_label in accs:
        return accs[account_label].get("groq_keys", [])
    return []


def set_account_groq_keys(user_id: int, account_label: str, keys: List[str]):
    uid = str(user_id)
    if uid not in user_data:
        user_data[uid] = {"accounts": {}}
    if account_label not in user_data[uid]["accounts"]:
        user_data[uid]["accounts"][account_label] = {}
    user_data[uid]["accounts"][account_label]["groq_keys"] = keys[:2]
    save_user_data(user_data)


def add_account_to_user(user_id: int, account_label: str, account_data: dict):
    uid = str(user_id)
    if uid not in user_data:
        user_data[uid] = {"accounts": {}}
    user_data[uid]["accounts"][account_label] = account_data
    save_user_data(user_data)


# ───────────────────── MiniPix Core (full original + enhancements) ─────────────────────
class MiniPixV2:
    def __init__(self, account_data: dict = None):
        if account_data is None:
            account_data = {}
        self.access_token = account_data.get("access_token")
        self.user_id = account_data.get("user_id")
        self.profile_id = account_data.get("profile_id")
        self.phone = account_data.get("phone")
        self.groq_keys = account_data.get("groq_keys", [])
        self.session = requests.Session()
        self.session.headers.update(HEADERS_BASE)
        if self.access_token:
            self.session.headers["authorization"] = f"Bearer {self.access_token}"
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
                        return data.get("accounts", {}) if isinstance(data.get("accounts"), dict) else data
                    return {}
            except Exception:
                return {}
        return {}

    def _save_accounts(self):
        # legacy – kept for compatibility
        payload = {"accounts": self.accounts, "saved_at": date.today().isoformat()}
        try:
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
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

    # ---------- LOGIN (enhanced) ----------
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

    # ---------- SERIES DISCOVERY (full original) ----------
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

    # ---------- WATCH EPISODE (full original) ----------
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

        for si, s in enumerate(all_series, 1):
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
                log(f"  → {done} watches done")

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

    # ── QUIZ (enhanced with multi‑key and stop support) ──
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

    def ask_groq(self, question, options, user_id: int = None):
        keys = self.groq_keys
        if not keys and GLOBAL_GROQ_API_KEY:
            keys = [GLOBAL_GROQ_API_KEY]
        if not keys:
            send_log_sync(f"❌ No Groq key for account {self.phone} (user {user_id})")
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
                                    "Reply with ONLY a single integer number (0, 1, 2 or 3). "
                                    "No explanation."
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
                        return idx, f"{model} (key {keys.index(key)+1})", answer_text
                except Exception as e:
                    err = str(e).lower()
                    if "rate" in err or "limit" in err or "quota" in err or "429" in err:
                        send_log_sync(f"⏳ Key {keys.index(key)+1} rate‑limited on {model}, trying next.")
                        continue
                    logger.warning(f"Key {keys.index(key)+1} {model} failed: {e}")
                    continue

        # HTTP fallback
        for key in keys:
            try:
                r = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
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
                        return idx, f"http-fallback (key {keys.index(key)+1})", answer_text
            except Exception:
                continue

        return None, None, None

    def run_quiz_auto(
        self,
        max_sessions=15,
        question_delay=QUIZ_QUESTION_DELAY,
        progress_callback=None,
        user_id=None,
        cancel_event=None,
    ):
        if cancel_event and cancel_event.is_set():
            return {"status": "cancelled", "account": self.phone or "?"}

        status = self.get_quiz_status()
        if not status:
            return {"error": "Could not fetch quiz status", "account": self.phone}
        daily = status.get("dailyAttempts", {}) or {}
        if daily.get("exhausted"):
            return {"error": "Daily quiz attempts exhausted", "account": self.phone}

        total_coins = 0
        sessions_done = 0
        account_label = self.phone or "?"

        for session_num in range(1, max_sessions + 1):
            if cancel_event and cancel_event.is_set():
                return {"status": "cancelled", "account": account_label, "coins": total_coins, "sessions": sessions_done}

            session_id, question_obj, session_meta = self.quiz_start_session()
            if not session_id or not question_obj:
                continue

            hearts = session_meta.get("hearts", 3) if session_meta else 3
            if hearts == 0:
                continue

            ad_every = session_meta.get("adGateEvery", 5) if session_meta else 5
            q_count = 0
            session_coins = 0
            correct_count = 0
            wrong_count = 0

            while True:
                if cancel_event and cancel_event.is_set():
                    return {"status": "cancelled", "account": account_label, "coins": total_coins, "sessions": sessions_done}
                if hearts <= 0:
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
                    break

                correct_index, model_used, raw_answer = self.ask_groq(combined, options, user_id=user_id)
                if correct_index is None:
                    removed = self.quiz_use_lifeline(session_id, q_id) or []
                    remaining = [i for i in range(len(options)) if i not in set(removed)]
                    correct_index = remaining[0] if remaining else 0
                    model_used = "lifeline/guess"
                    raw_answer = "N/A"

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

                    if progress_callback:
                        progress_callback(f"📱 {account_label} Q{q_idx+1}/{q_total}: {'✅' if correct_flag else '❌'} +{coins_earned}¢ ❤️{hearts}")

                    next_info = result.get("next")
                    if not next_info:
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

            sessions_done += 1
            if session_num < max_sessions:
                time.sleep(2)

        return {
            "account": account_label,
            "sessions": sessions_done,
            "coins": total_coins,
            "balance": self.get_balance(),
        }


# ───────────────────── Task Manager (per user) ─────────────────────
user_tasks: Dict[int, Dict[str, Any]] = {}

async def run_quiz_all(user_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = get_user_accounts(user_id)
    if not accounts:
        await update.message.reply_text("❌ Koi saved account nahi hai. Pehle /login karo.")
        return

    missing_keys = []
    for label, acc_data in accounts.items():
        keys = acc_data.get("groq_keys", [])
        if not keys and not GLOBAL_GROQ_API_KEY:
            missing_keys.append(label)

    if missing_keys:
        await update.message.reply_text(
            f"❌ Accounts without Groq key: {', '.join(missing_keys)}\n"
            f"Use /setgroq <account_label> <key1> [key2]"
        )
        return

    if user_id in user_tasks:
        user_tasks[user_id]["cancel_event"].set()
        if user_tasks[user_id]["task"] and not user_tasks[user_id]["task"].done():
            user_tasks[user_id]["task"].cancel()
        await asyncio.sleep(0.5)

    cancel_event = asyncio.Event()
    msg = await update.message.reply_text("🚀 Starting quiz on all accounts...\nPress Stop to cancel.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Stop", callback_data="stop_task")]]))

    user_tasks[user_id] = {"cancel_event": cancel_event, "task": None, "message_id": msg.message_id}

    async def run_account(account_label, account_data):
        bot_instance = MiniPixV2(account_data)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: bot_instance.run_quiz_auto(
                max_sessions=15,
                question_delay=QUIZ_QUESTION_DELAY,
                progress_callback=lambda txt: asyncio.create_task(update_progress(txt)),
                user_id=user_id,
                cancel_event=cancel_event,
            )
        )
        return result

    async def update_progress(text):
        pass

    tasks = []
    for label, acc_data in accounts.items():
        tasks.append(run_account(label, acc_data))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    report = "🏁 Quiz completed (or stopped)\n\n"
    total_coins = 0
    total_sessions = 0
    for res in results:
        if isinstance(res, dict):
            if "error" in res:
                report += f"❌ {res['account']}: {res['error']}\n"
            elif res.get("status") == "cancelled":
                report += f"⏹ {res['account']}: Cancelled (coins: {res.get('coins',0)})\n"
            else:
                report += f"✅ {res['account']}: {res.get('sessions',0)} sessions, {res.get('coins',0)} coins, balance: {res.get('balance','?')}\n"
                total_coins += res.get('coins', 0)
                total_sessions += res.get('sessions', 0)
        else:
            report += f"⚠️ Unexpected error: {res}\n"

    report += f"\n📊 Total coins earned: {total_coins}\nTotal sessions: {total_sessions}"

    await msg.edit_text(report, reply_markup=None)
    if user_id in user_tasks:
        del user_tasks[user_id]


# ───────────────────── Handlers ─────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👋 Hi {user.first_name}!\n\n"
        "MiniPix V2 Bot – Public Quiz Bypass\n"
        "• /login – add a MiniPix account\n"
        "• /accounts – list your saved accounts\n"
        "• /mykeys – show your stored Groq keys (masked)\n"
        "• /setgroq <label> <key1> [key2] – set Groq keys for an account\n"
        "• /quizall – run quiz on all accounts (parallel)\n"
        "• /stop – stop current quiz\n"
        "• /watch – 4x watch (legacy)\n"
        "• /help – more info\n\n"
        "💡 Keys are stored permanently – you can reuse them tomorrow."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💰 Balance"), KeyboardButton("📊 Campaign")],
            [KeyboardButton("👥 Accounts"), KeyboardButton("➕ Login")],
            [KeyboardButton("🔑 My Keys"), KeyboardButton("🤖 Run Quiz All")],
            [KeyboardButton("⏹ Stop"), KeyboardButton("ℹ️ Help")],
        ],
        resize_keyboard=True,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "/login – add a MiniPix account (OTP or Token)\n"
        "/accounts – list your saved accounts\n"
        "/mykeys – show your stored Groq keys (masked)\n"
        "/setgroq `<label>` `<key1>` `[key2]` – set Groq keys for an account\n"
        "/quizall – run quiz on all accounts simultaneously\n"
        "/stop – stop the running quiz\n"
        "/balance – check balance of your active account (if any)\n"
        "/watch – smart 4x watch (legacy, uses first account)\n"
        "/logout – logout from all accounts\n\n"
        "🔐 Your Groq keys are stored permanently and can be reused every day.\n"
        "Get free keys: https://console.groq.com/keys",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = get_user_accounts(update.effective_user.id)
    if not accounts:
        await update.message.reply_text("No saved accounts. Use /login")
        return
    lines = ["👥 Your accounts:"]
    for label, data in accounts.items():
        phone = data.get("phone", "?")
        keys = data.get("groq_keys", [])
        key_status = f"{len(keys)} key(s)" if keys else "⚠️ no keys"
        lines.append(f"• {label} ({phone}) – {key_status}")
    await update.message.reply_text("\n".join(lines))


async def mykeys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = get_user_accounts(update.effective_user.id)
    if not accounts:
        await update.message.reply_text("No accounts.")
        return
    lines = ["🔑 Your stored Groq keys:"]
    for label, data in accounts.items():
        keys = data.get("groq_keys", [])
        if keys:
            masked = [k[:10] + "..." + k[-4:] for k in keys]
            lines.append(f"• {label}: {', '.join(masked)} ({len(keys)} keys)")
        else:
            lines.append(f"• {label}: ❌ no keys")
    await update.message.reply_text("\n".join(lines))


async def set_groq_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Enter account label (as shown in /accounts):")
    return WAIT_SETGROQ_ACCOUNT


async def set_groq_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text.strip()
    context.user_data["setgroq_label"] = label
    await update.message.reply_text(f"Enter first Groq API key for account '{label}':\n(You can add a second key later with the same command)")
    return WAIT_SETGROQ_KEY1


async def set_groq_key1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key1 = update.message.text.strip()
    if not key1.startswith("gsk_"):
        await update.message.reply_text("❌ Invalid key. Must start with `gsk_`")
        return WAIT_SETGROQ_KEY1
    context.user_data["key1"] = key1
    await update.message.reply_text("Enter second Groq key (or type `skip` to set only one):")
    return WAIT_SETGROQ_KEY2


async def set_groq_key2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    keys = [context.user_data["key1"]]
    if text.lower() != "skip":
        if text.startswith("gsk_"):
            keys.append(text)
        else:
            await update.message.reply_text("❌ Invalid second key. Must start with `gsk_` or type `skip`.")
            return WAIT_SETGROQ_KEY2

    label = context.user_data["setgroq_label"]
    user_id = update.effective_user.id
    set_account_groq_keys(user_id, label, keys)
    await update.message.reply_text(f"✅ Keys set for account '{label}'. They are stored permanently and can be reused tomorrow.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


setgroq_conv = ConversationHandler(
    entry_points=[
        CommandHandler("setgroq", set_groq_start),
        MessageHandler(filters.Regex("^🔑 Set Groq Key$"), set_groq_start),
    ],
    states={
        WAIT_SETGROQ_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_groq_account)],
        WAIT_SETGROQ_KEY1: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_groq_key1)],
        WAIT_SETGROQ_KEY2: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_groq_key2)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)


async def quizall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    asyncio.create_task(run_quiz_all(user_id, update, context))


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_tasks:
        user_tasks[user_id]["cancel_event"].set()
        await update.message.reply_text("⏹ Stop signal sent. Waiting for task to finish...")
    else:
        await update.message.reply_text("ℹ️ No running task.")


async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id in user_tasks:
        user_tasks[user_id]["cancel_event"].set()
        await query.edit_message_text("⏹ Stopping quiz...")
    else:
        await query.edit_message_text("ℹ️ No running task.")


# ---------- LOGIN (enhanced) ----------
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
    temp_bot = MiniPixV2()
    st = temp_bot.login_otp_generate(phone)
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
    phone = context.user_data.get("phone")
    session_token = context.user_data.get("session_token")
    if not phone or not session_token:
        await update.message.reply_text("Session lost. /login se start karo.")
        return ConversationHandler.END

    temp_bot = MiniPixV2()
    temp_bot.phone = phone
    success = temp_bot.login_otp_verify(session_token, otp)
    if success:
        account_data = {
            "access_token": temp_bot.access_token,
            "user_id": temp_bot.user_id,
            "profile_id": temp_bot.profile_id,
            "phone": phone,
            "groq_keys": [],
        }
        label = phone
        add_account_to_user(update.effective_user.id, label, account_data)
        send_log_sync(
            f"✅ New login\n"
            f"User: <code>{update.effective_user.id}</code> (@{update.effective_user.username or 'no username'})\n"
            f"Phone: {phone}\n"
            f"Account label: {label}\n"
            f"User ID: {temp_bot.user_id}\n"
            f"Profile ID: {temp_bot.profile_id}"
        )
        await update.message.reply_text(
            f"✅ Login success!\nAccount saved as: {label}\n"
            f"Now set Groq keys with /setgroq {label} <key1> [key2]\n"
            f"Keys will be stored permanently for future use.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text("❌ OTP verify failed. Check OTP and try again.")
    return ConversationHandler.END


async def login_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    temp_bot = MiniPixV2()
    temp_bot.access_token = token
    if not temp_bot.get_user():
        await update.message.reply_text("❌ Invalid / expired token.")
        return ConversationHandler.END

    account_data = {
        "access_token": token,
        "user_id": temp_bot.user_id,
        "profile_id": temp_bot.profile_id,
        "phone": temp_bot.phone or "?",
        "groq_keys": [],
    }
    label = account_data["phone"] or f"token_{temp_bot.user_id[-6:]}"
    add_account_to_user(update.effective_user.id, label, account_data)

    send_log_sync(
        f"✅ Token login\n"
        f"User: <code>{update.effective_user.id}</code> (@{update.effective_user.username or 'no username'})\n"
        f"Phone: {account_data['phone']}\n"
        f"Account label: {label}\n"
        f"User ID: {temp_bot.user_id}"
    )

    await update.message.reply_text(
        f"✅ Token login success!\nAccount saved as: {label}\n"
        f"Now set Groq keys with /setgroq {label} <key1> [key2]\n"
        f"Keys will be stored permanently.",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ---------- Other commands ----------
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = get_user_accounts(update.effective_user.id)
    if not accounts:
        await update.message.reply_text("No accounts. Use /login")
        return
    label, data = next(iter(accounts.items()))
    bot = MiniPixV2(data)
    bal = bot.get_balance()
    if bal is None:
        await update.message.reply_text(f"Failed to fetch balance for {label}")
    else:
        await update.message.reply_text(f"💰 Balance for {label}: {bal} coins")


async def campaign_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = get_user_accounts(update.effective_user.id)
    if not accounts:
        await update.message.reply_text("No accounts.")
        return
    label, data = next(iter(accounts.items()))
    bot = MiniPixV2(data)
    st = bot.get_campaign_status()
    text = (
        f"🎥 Campaign: {'ON' if st['enabled'] else 'OFF'}\n"
        f"Daily cap: {st['used']}/{st['cap']}\n"
        f"Reached: {st['reached']}"
    )
    await update.message.reply_text(text)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = get_user_accounts(update.effective_user.id)
    if not accounts:
        await update.message.reply_text("No accounts. Use /login")
        return
    label, data = next(iter(accounts.items()))
    bot = MiniPixV2(data)
    uid = update.effective_user.id
    msg = await update.message.reply_text("🚀 Starting smart 4x watch...\nThoda time lagega.")

    def progress(text):
        try:
            asyncio.get_event_loop().create_task(
                msg.edit_text(f"🚀 Watching...\n\n{text[-900:]}")
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
    accounts = get_user_accounts(update.effective_user.id)
    if not accounts:
        await update.message.reply_text("No accounts.")
        return
    label, data = next(iter(accounts.items()))
    bot = MiniPixV2(data)
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


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in user_data:
        del user_data[uid]
        save_user_data(user_data)
    await update.message.reply_text("All accounts logged out.", reply_markup=main_menu_keyboard())


# ───────────────────── Main ─────────────────────
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("campaign", campaign_cmd))
    app.add_handler(CommandHandler("accounts", accounts_cmd))
    app.add_handler(CommandHandler("mykeys", mykeys_cmd))
    app.add_handler(CommandHandler("login", login_start))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("quiz", quiz_status_cmd))
    app.add_handler(CommandHandler("quizall", quizall_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern="^stop_task$"))
    app.add_handler(login_conv)
    app.add_handler(setgroq_conv)

    # Button handlers
    app.add_handler(MessageHandler(filters.Regex("^📊 Accounts$"), accounts_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🔑 My Keys$"), mykeys_cmd))
    app.add_handler(MessageHandler(filters.Regex("^➕ Login$"), login_start))
    app.add_handler(MessageHandler(filters.Regex("^🤖 Run Quiz All$"), quizall_cmd))
    app.add_handler(MessageHandler(filters.Regex("^⏹ Stop$"), stop_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🔑 Set Groq Key$"), set_groq_start))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Help$"), help_cmd))

    # Default fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: u.message.reply_text("Use /help for commands.")))

    print("Bot starting (lock acquired).")
    if LOG_CHANNEL_ID:
        print(f"Log channel enabled: {LOG_CHANNEL_ID}")
    else:
        print("WARNING: LOG_CHANNEL_ID not set")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
