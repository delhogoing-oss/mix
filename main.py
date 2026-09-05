#!/usr/bin/env python3
"""
MiniPix V2 Telegram Bot - Public Multi-User
- Har user apna MiniPix account + 2 Groq keys use karega
- Key-1 limit pe → Key-2 automatically
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
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ───────────────────────── Config ─────────────────────────
API_BASE = "https://api.minipix.co/v4"
ACCOUNTS_FILE = "minipix_accounts.json"
USER_GROQ_FILE = "user_groq_keys.json"
LOCK_FILE = "bot.lock"

MAX_WATCHES_PER_EP = 4
QUIZ_QUESTION_DELAY = 10
DEFAULT_SESSIONS = 15
MIN_SESSIONS = 10
MAX_SESSIONS = 25

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
    level=logging.INFO
)
logger = logging.getLogger(__name__)

(WAIT_PHONE, WAIT_OTP, WAIT_TOKEN, WAIT_QUIZ_SESSIONS) = range(4)

# ───────────────────── Stop flags ─────────────────────
stop_flags: Dict[int, bool] = {}

def request_stop(uid: int):
    stop_flags[uid] = True

def clear_stop(uid: int):
    stop_flags[uid] = False

def is_stopped(uid: int) -> bool:
    return stop_flags.get(uid, False)


# ───────────────────── Lock ─────────────────────
def acquire_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        print("Another instance running. Exit.")
        sys.exit(1)

    def remove_lock():
        try:
            os.unlink(LOCK_FILE)
        except Exception:
            pass
    atexit.register(remove_lock)


# ───────────────────── Log ─────────────────────
def send_log(text: str):
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
        logger.warning(f"Log error: {e}")


# ───────────────────── Groq Keys (2 per user) ─────────────────────
def load_keys() -> dict:
    if os.path.exists(USER_GROQ_FILE):
        try:
            with open(USER_GROQ_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_keys(data: dict):
    try:
        with open(USER_GROQ_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(e)

user_groq_keys = load_keys()   # { "telegram_id": {"key1": "...", "key2": "..."} }

def get_user_keys(uid: int) -> List[str]:
    data = user_groq_keys.get(str(uid), {})
    keys = []
    if data.get("key1"):
        keys.append(data["key1"])
    if data.get("key2"):
        keys.append(data["key2"])
    return keys

def set_user_key(uid: int, which: int, key: str):
    uid = str(uid)
    if uid not in user_groq_keys:
        user_groq_keys[uid] = {}
    if which == 1:
        user_groq_keys[uid]["key1"] = key
    else:
        user_groq_keys[uid]["key2"] = key
    save_keys(user_groq_keys)


# ───────────────────── MiniPix Core ─────────────────────
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
        try:
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump({"accounts": self.accounts, "saved_at": date.today().isoformat()}, f, indent=2, ensure_ascii=False)
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
            return False, "Account not found"
        acc = self.accounts[label]
        if not acc.get("access_token"):
            return False, "No token"
        self._reset_state()
        self.access_token = acc["access_token"]
        self.user_id = acc.get("user_id")
        self.profile_id = acc.get("profile_id")
        self.phone = acc.get("phone")
        self.session.headers["authorization"] = f"Bearer {self.access_token}"
        self.current_account_label = label
        if self.get_user():
            self._store_current_account(label)
            return True, f"Switched to {label}"
        return False, "Token expired"

    def remove_account(self, label):
        if label not in self.accounts:
            return False
        del self.accounts[label]
        self._save_accounts()
        if self.current_account_label == label:
            self._reset_state()
        return True

    def _reset_state(self):
        self.access_token = self.user_id = self.profile_id = self.phone = None
        self.current_account_label = None
        self.watch_history = {}
        self.watch_history_raw = []
        self.runtime_watch_counts = {}
        self.last_profile = {}
        self.session.headers.pop("authorization", None)

    def _req(self, method, path, **kwargs):
        try:
            r = self.session.request(method, f"{API_BASE}{path}", timeout=30, **kwargs)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, r.text
        except Exception as e:
            return 0, str(e)

    def login_otp_generate(self, phone):
        self.phone = phone
        sc, data = self._req("POST", "/login/generate-otp",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps({"phone_number": phone}).encode())
        if sc == 200 and isinstance(data, dict) and (data.get("message") == "OTP sent" or data.get("success")):
            return data.get("session_token") or data.get("sessionToken")
        return None

    def login_otp_verify(self, session_token, otp, save_label=None):
        payload = {
            "client_id": "android", "device_id": self.device_id, "device_info": self.device_info,
            "otp": otp, "phone_number": self.phone, "session_token": session_token
        }
        sc, data = self._req("POST", "/login/verify-otp",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload).encode())
        if sc == 200 and isinstance(data, dict) and data.get("access_token"):
            self.access_token = data["access_token"]
            self.user_id = data.get("id") or data.get("_id")
            self.session.headers["authorization"] = f"Bearer {self.access_token}"
            self.get_user()
            self._store_current_account(save_label)
            return True
        return False

    def login_with_token(self, token, label=None):
        self.access_token = token
        self.session.headers["authorization"] = f"Bearer {token}"
        if self.get_user():
            self._store_current_account(label)
            return True
        return False

    def get_user(self):
        if not self.user_id:
            return False
        sc, data = self._req("GET", f"/users/{self.user_id}")
        if sc == 200 and isinstance(data, dict):
            self.user_id = data.get("_id", self.user_id)
            self.profile_id = data.get("master_profile", self.profile_id)
            if data.get("mobile") and not self.phone:
                self.phone = data["mobile"]
            return True
        return False

    def open_app(self):
        if not (self.user_id and self.profile_id):
            return False
        payload = {"openApp": {"_id": self.user_id, "date": date.today().isoformat()}}
        sc, data = self._req("PATCH", f"/users/{self.user_id}/profiles/{self.profile_id}/open_app",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload).encode())
        return sc == 200

    def get_balance(self):
        sc, data = self._req("GET", "/coins/balance")
        if sc == 200 and isinstance(data, dict):
            coins = data.get("coins", 0)
            return coins.get("coins", 0) if isinstance(coins, dict) else coins
        if sc == 200 and isinstance(data, (int, float)):
            return int(data)
        return None

    def get_balance_silent(self):
        return self.get_balance()

    def get_campaign_status(self):
        sc, data = self._req("GET", "/watch-campaign/status")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            cap = data.get("dailyVideoCap", {}) or {}
            return {"enabled": data.get("enabled"), "used": cap.get("used", 0), "cap": cap.get("cap", 0)}
        return {"enabled": False, "used": 0, "cap": 0}

    def _collect_series_deep(self, obj, out):
        if obj is None:
            return
        if isinstance(obj, dict):
            if (obj.get("_id") or obj.get("id") or obj.get("series_id")) and (
                obj.get("title") or obj.get("numberOfEpisodes") is not None or
                obj.get("totalEpisodes") is not None or obj.get("cardImage")
            ):
                sid = obj.get("_id") or obj.get("id") or obj.get("series_id")
                if sid and sid not in out:
                    out[sid] = obj
            for v in obj.values():
                self._collect_series_deep(v, out)
        elif isinstance(obj, list):
            for item in obj:
                self._collect_series_deep(item, out)

    def get_all_series(self, page_size=80, max_pages=8):
        found = {}
        try:
            sc, data = self._req("GET", "/short_search?page=home")
            if sc == 200:
                self._collect_series_deep(data, found)
        except Exception:
            pass
        for tmpl in ["/webseries?page={p}&pageSize={ps}", "/discover?type=webseries&page={p}&pageSize={ps}"]:
            for page in range(1, max_pages + 1):
                try:
                    sc, data = self._req("GET", tmpl.format(p=page, ps=page_size))
                    if sc == 200 and isinstance(data, dict):
                        self._collect_series_deep(data, found)
                except Exception:
                    continue
        series = list(found.values())
        series.sort(key=lambda s: -int(s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0))
        return series

    def get_episodes(self, series_id, page=1, page_size=50):
        sc, data = self._req("GET", f"/episodes?series_id={series_id}&page={page}&pageSize={page_size}")
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
                if isinstance(wh, dict):
                    key = (wh.get("id"), wh.get("episodeNo"))
                    self.watch_history[key] = {"watchedPct": wh.get("watchedPct", 0) or 0}
            return profile
        return None

    def get_watch_counts_from_profile(self):
        counts = {}
        try:
            self.get_profile()
        except Exception:
            pass
        for item in getattr(self, "watch_history_raw", []):
            if isinstance(item, dict):
                sid = item.get("id") or item.get("series_id")
                ep = item.get("episodeNo")
                pct = int(item.get("watchedPct") or item.get("progress") or 0)
                if sid and ep and pct >= 80:
                    k = (str(sid), str(ep))
                    counts[k] = counts.get(k, 0) + 1
        for k, c in getattr(self, "runtime_watch_counts", {}).items():
            counts[k] = max(counts.get(k, 0), c)
        return counts

    def _update_watch_progress(self, series_id, series_title, hindi_title, episode_no,
                               tc_in_ms, tc_out_ms, detail_image, watched_pct):
        if not (self.user_id and self.profile_id):
            return False
        try:
            watched_pct = int(watched_pct or 0)
        except Exception:
            watched_pct = 0
        if not tc_out_ms or tc_out_ms <= tc_in_ms:
            tc_out_ms = tc_in_ms + 60000
        duration = tc_out_ms - tc_in_ms
        current_time_ms = tc_out_ms if watched_pct >= 100 else int(tc_in_ms + (duration * watched_pct / 100))
        stored_pct = 100 if watched_pct >= 100 else watched_pct

        watch_obj = {
            "id": series_id, "title": series_title, "hindiTitle": hindi_title,
            "episodeNo": episode_no, "tcInMs": tc_in_ms, "tcOutMs": tc_out_ms,
            "detailImage": detail_image, "type": "episode",
            "progress": stored_pct, "time": current_time_ms,
            "watchedPct": stored_pct, "campaign": False,
        }
        try:
            sc, d = self._req("PATCH", f"/users/{self.user_id}/profiles/{self.profile_id}",
                headers={"content-type": "application/json; charset=utf-8"},
                data=json.dumps({"watched": watch_obj}).encode())
            return sc == 200
        except Exception:
            return False

    def _report_watch_progress_to_coins(self, series_id, episode_no, watched_pct):
        body = {
            "series_id": series_id, "episode_no": episode_no, "episodeNo": episode_no,
            "progress": watched_pct, "watchedPct": watched_pct, "campaign": False,
            "task_type": "watch_ladder",
        }
        for path in ["/coins/progress-report", "/coins/tasks/progress", "/watch-ladder/progress"]:
            try:
                sc, d = self._req("POST", path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body).encode())
                if sc and sc < 500:
                    return True
            except Exception:
                pass
        return False

    def claim_reward_task(self, series_id=None):
        if not series_id:
            return False
        for path in [f"/coins/tasks/watch_ladder_{series_id}/claim", "/watch-ladder/claim"]:
            try:
                sc, d = self._req("POST", path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps({"series_id": series_id, "campaign": False}).encode())
                if sc == 200:
                    return True
            except Exception:
                pass
        return False

    def watch_episode(self, episode, series_info):
        series_id = series_info.get("_id") or series_info.get("id")
        if not series_id:
            return False, "no_id"
        ep_no = episode.get("episodeNo") or episode.get("number") or 0
        series_title = series_info.get("title") or ""
        hindi_title = series_info.get("hindiTitle") or series_title
        detail_image = series_info.get("cardImage") or ""

        try:
            tc_in = int(episode.get("tcIn") or 0)
            tc_out = int(episode.get("tcOut") or (tc_in + 60))
        except Exception:
            tc_in, tc_out = 0, 60
        if tc_out <= tc_in:
            tc_out = tc_in + 60
        tc_in_ms, tc_out_ms = tc_in * 1000, tc_out * 1000

        for pct in [1, 50, 80, 99, 100]:
            self._update_watch_progress(series_id, series_title, hindi_title, ep_no,
                                        tc_in_ms, tc_out_ms, detail_image, pct)
            if pct >= 80:
                self._report_watch_progress_to_coins(series_id, ep_no, pct)
            time.sleep(0.1)

        self.claim_reward_task(series_id)
        rk = (str(series_id), str(ep_no))
        self.runtime_watch_counts[rk] = self.runtime_watch_counts.get(rk, 0) + 1
        return True, "done"

    def browse_and_watch_all_smart_repeat(self, progress_callback=None, max_watches=180, telegram_user_id=None):
        def log(msg):
            if progress_callback:
                progress_callback(msg)

        clear_stop(telegram_user_id)
        log("Checking campaign...")
        all_series = self.get_all_series()
        if not all_series:
            return {"error": "No series found"}

        watch_counts = self.get_watch_counts_from_profile()
        total_watched = total_skipped = total_failed = 0
        balance_before = self.get_balance_silent()

        for si, s in enumerate(all_series, 1):
            if is_stopped(telegram_user_id):
                log("🛑 Stopped by user")
                break
            if total_watched >= max_watches:
                break
            sid = s.get("_id") or s.get("id")
            if not sid:
                continue
            title = s.get("title") or "?"
            episodes, _ = self.get_episodes(sid, page=1, page_size=250)
            if not episodes:
                continue
            episodes = sorted(episodes, key=lambda e: int(e.get("episodeNo") or 0) if str(e.get("episodeNo") or "").isdigit() else 0)

            log(f"[{si}/{len(all_series)}] {title}")
            done = 0
            for ep in episodes:
                if is_stopped(telegram_user_id) or total_watched >= max_watches:
                    break
                ep_no = ep.get("episodeNo")
                kp = (str(sid), str(ep_no))
                cnt = watch_counts.get(kp, 0) + self.runtime_watch_counts.get(kp, 0)
                if cnt >= MAX_WATCHES_PER_EP:
                    total_skipped += 1
                    continue
                ok, _ = self.watch_episode(ep, s)
                if ok:
                    done += 1
                    total_watched += 1
                else:
                    total_failed += 1
            if done:
                log(f"  → {done} watches")

        bal_end = self.get_balance_silent()
        delta = (bal_end - balance_before) if (bal_end is not None and balance_before is not None) else None
        return {
            "watched": total_watched, "skipped": total_skipped, "failed": total_failed,
            "balance_before": balance_before, "balance_after": bal_end, "delta": delta,
            "stopped": is_stopped(telegram_user_id),
        }

    # ── QUIZ ──
    def get_quiz_status(self):
        sc, data = self._req("GET", "/quiz/status")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data
        return None

    def quiz_start_session(self):
        sc, data = self._req("POST", "/quiz/session/start",
            headers={"content-type": "application/json; charset=utf-8"},
            data=b"{}")
        send_log(f"📡 start_session | {sc}\n<code>{str(data)[:450]}</code>")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            session_obj = data.get("session") or {}
            question_obj = data.get("question")
            sid = session_obj.get("sessionId") or data.get("sessionId") or data.get("_id")
            if sid and question_obj:
                return sid, question_obj, session_obj
        return None, None, None

    def quiz_submit_answer(self, session_id, question_id, chosen_index):
        payload = {"sessionId": session_id, "questionId": question_id, "chosenIndex": chosen_index}
        sc, data = self._req("POST", "/quiz/session/answer",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload).encode())
        if sc == 200 and isinstance(data, dict):
            return data
        return None

    def quiz_ad_ack(self, session_id):
        sc, data = self._req("POST", "/quiz/session/ad-ack",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps({"sessionId": session_id}).encode())
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data.get("question")
        return None

    def _build_prompt(self, question, options):
        p = "Multiple choice. Reply ONLY with the number 0, 1, 2 or 3.\n\nQuestion:\n" + question + "\n\nOptions:\n"
        for i, o in enumerate(options):
            p += f"{i}. {o}\n"
        p += "\nAnswer number:"
        return p

    def _parse(self, text, options):
        if not text:
            return None
        m = re.search(r"\b(\d+)\b", text.strip())
        if m:
            idx = int(m.group(1))
            if 0 <= idx < len(options):
                return idx
        return None

    def ask_groq(self, question, options, telegram_user_id: int = None):
        keys = get_user_keys(telegram_user_id) if telegram_user_id else []
        if not keys:
            send_log(f"❌ No Groq keys for user <code>{telegram_user_id}</code>")
            return None

        prompt = self._build_prompt(question, options)

        for key_idx, api_key in enumerate(keys, 1):
            for model in GROQ_MODELS:
                try:
                    from groq import Groq
                    client = Groq(api_key=api_key)
                    completion = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": "Reply with ONLY a single integer (0, 1, 2 or 3). No other text."},
                            {"role": "user", "content": prompt}
                        ],
                        temperature=0.0,
                        max_tokens=10,
                    )
                    raw = (completion.choices[0].message.content or "").strip()
                    idx = self._parse(raw, options)
                    send_log(f"<b>🤖 Key{key_idx} | {model}</b>\nRaw: <code>{raw}</code> → {idx}")
                    if idx is not None:
                        return idx
                except Exception as e:
                    err = str(e).lower()
                    if any(x in err for x in ["rate", "limit", "quota", "429"]):
                        send_log(f"⏳ Key{key_idx} {model} rate-limited → next")
                        continue
                    logger.warning(f"Key{key_idx} {model}: {e}")
                    continue
        return None

    def run_quiz_auto(self, max_sessions=15, question_delay=10, progress_callback=None, telegram_user_id=None):
        def log(msg):
            if progress_callback:
                progress_callback(msg)

        clear_stop(telegram_user_id)

        if not self.user_id and not self.get_user():
            return {"error": "Not authenticated"}

        status = self.get_quiz_status()
        if not status:
            return {"error": "Quiz status failed"}
        daily = status.get("dailyAttempts", {}) or {}
        if daily.get("exhausted"):
            return {"error": "Daily attempts exhausted"}

        total_coins = sessions_done = 0
        failed = 0

        send_log(f"🧠 QUIZ START | User <code>{telegram_user_id}</code> | {max_sessions} sessions")

        for snum in range(1, max_sessions + 1):
            if is_stopped(telegram_user_id):
                log("🛑 Stopped")
                break

            log(f"Session {snum}/{max_sessions}...")
            sid, qobj, meta = None, None, None
            for _ in range(2):
                if is_stopped(telegram_user_id):
                    break
                sid, qobj, meta = self.quiz_start_session()
                if sid and qobj:
                    break
                time.sleep(2)

            if not sid or not qobj:
                failed += 1
                if failed >= 2:
                    break
                continue

            hearts = meta.get("hearts", 3) if meta else 3
            if hearts == 0:
                failed += 1
                if failed >= 2:
                    break
                continue

            failed = 0
            ad_every = meta.get("adGateEvery", 5) if meta else 5
            q_count = correct_c = 0
            session_coins = 0

            while hearts > 0 and q_count < 25:
                if is_stopped(telegram_user_id):
                    break
                if not isinstance(qobj, dict):
                    break

                qid = qobj.get("questionId")
                q_hi = qobj.get("questionHi") or ""
                q_en = qobj.get("questionEn") or ""
                options = qobj.get("options") or []
                if not qid or len(options) < 2:
                    break

                q_count += 1
                combined = q_hi or q_en
                if q_en and q_hi and q_en != q_hi:
                    combined = f"{q_hi}\n[EN: {q_en}]"

                log(f"S{snum} Q{q_count}: {(q_hi or q_en)[:45]}...")

                chosen = self.ask_groq(combined, options, telegram_user_id)
                if chosen is None:
                    chosen = 0
                chosen = max(0, min(chosen, len(options)-1))

                time.sleep(question_delay)
                if is_stopped(telegram_user_id):
                    break

                result = self.quiz_submit_answer(sid, qid, chosen)
                if not result or not result.get("success"):
                    break

                is_correct = result.get("correct", False)
                coins = int(result.get("coinsEarned") or 0)
                session_coins = result.get("coinsSoFar", session_coins)
                hearts = int(result.get("hearts", hearts))
                total_coins += coins
                if is_correct:
                    correct_c += 1

                send_log(
                    f"<b>Q{q_count}</b> S{snum}\n"
                    f"{q_hi or q_en}\n"
                    f"Chose: [{chosen}] {options[chosen]}\n"
                    f"{'✅' if is_correct else '❌'} +{coins} | ❤️{hearts}"
                )

                nxt = result.get("next")
                if not nxt:
                    break
                if isinstance(nxt, dict):
                    if "question" in nxt and isinstance(nxt.get("question"), dict):
                        qobj = nxt["question"]
                        sid = result.get("sessionId") or sid
                        continue
                    if "result" in nxt:
                        break
                if ad_every and q_count % ad_every == 0:
                    nq = self.quiz_ad_ack(sid)
                    if nq:
                        qobj = nq
                        continue
                    break
                break

            sessions_done += 1
            send_log(f"<b>📊 Session {snum}</b>\nQ: {q_count} | Correct: {correct_c} | Coins: {session_coins}")
            if is_stopped(telegram_user_id):
                break
            time.sleep(1.5)

        send_log(
            f"<b>🏁 QUIZ DONE</b>\n"
            f"User: <code>{telegram_user_id}</code>\n"
            f"Sessions: {sessions_done}\n"
            f"Coins: ~{total_coins}\n"
            f"Balance: {self.get_balance()}"
        )
        return {
            "sessions": sessions_done,
            "total_coins": total_coins,
            "balance": self.get_balance(),
            "stopped": is_stopped(telegram_user_id),
        }


# ───────────────────── Bot Layer ─────────────────────
user_bots: Dict[int, MiniPixV2] = {}

def get_bot(uid: int) -> MiniPixV2:
    if uid not in user_bots:
        user_bots[uid] = MiniPixV2()
    return user_bots[uid]

def menu():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💰 Balance"), KeyboardButton("📊 Campaign")],
        [KeyboardButton("👥 Accounts"), KeyboardButton("➕ Login")],
        [KeyboardButton("🎬 Watch All (4x)"), KeyboardButton("🧠 Quiz Status")],
        [KeyboardButton("🤖 Run Quiz"), KeyboardButton("🛑 Stop Task")],
        [KeyboardButton("🔑 Set Groq Keys"), KeyboardButton("ℹ️ Help")],
    ], resize_keyboard=True)


# ───────────────────── Handlers ─────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = get_bot(update.effective_user.id)
    txt = (
        f"👋 Hi {update.effective_user.first_name}!\n\n"
        "Public MiniPix Bot\n\n"
        "1. /setgroq1 + /setgroq2 se 2 Groq keys set karo\n"
        "2. /login se apna MiniPix account add karo\n"
        "3. Phir Watch / Quiz use karo"
    )
    if b.access_token:
        txt += f"\n\n✅ Logged in: {b.current_account_label or b.phone}"
    await update.message.reply_text(txt, reply_markup=menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "`/setgroq1 gsk_xxx` – 1st Groq key\n"
        "`/setgroq2 gsk_yyy` – 2nd Groq key\n"
        "`/mygroq` – keys check\n"
        "`/login` – MiniPix account\n"
        "`/watch` – 4x watch\n"
        "`/quiz` – status\n"
        "`/stop` – running task stop\n"
        "`/logout`",
        parse_mode="Markdown",
        reply_markup=menu(),
    )


async def set_groq1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/setgroq1 gsk_xxxxxxxx`", parse_mode="Markdown")
        return
    key = context.args[0].strip()
    if not key.startswith("gsk_"):
        await update.message.reply_text("Key must start with gsk_")
        return
    set_user_key(update.effective_user.id, 1, key)
    await update.message.reply_text("✅ Key 1 saved")


async def set_groq2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/setgroq2 gsk_xxxxxxxx`", parse_mode="Markdown")
        return
    key = context.args[0].strip()
    if not key.startswith("gsk_"):
        await update.message.reply_text("Key must start with gsk_")
        return
    set_user_key(update.effective_user.id, 2, key)
    await update.message.reply_text("✅ Key 2 saved")


async def my_groq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = get_user_keys(update.effective_user.id)
    if not keys:
        await update.message.reply_text("❌ Koi key set nahi hai\n\n`/setgroq1` aur `/setgroq2` use karo", parse_mode="Markdown")
        return
    lines = []
    data = user_groq_keys.get(str(update.effective_user.id), {})
    if data.get("key1"):
        k = data["key1"]
        lines.append(f"Key1: `{k[:10]}...{k[-4:]}`")
    if data.get("key2"):
        k = data["key2"]
        lines.append(f"Key2: `{k[:10]}...{k[-4:]}`")
    await update.message.reply_text("✅ Keys:\n" + "\n".join(lines), parse_mode="Markdown")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = get_bot(update.effective_user.id)
    if not b.access_token:
        await update.message.reply_text("Not logged in. /login")
        return
    await update.message.reply_text(f"💰 Balance: *{b.get_balance()}*", parse_mode="Markdown")


async def campaign_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = get_bot(update.effective_user.id)
    if not b.access_token:
        await update.message.reply_text("Not logged in")
        return
    s = b.get_campaign_status()
    await update.message.reply_text(f"🎥 {'ON' if s['enabled'] else 'OFF'} | {s['used']}/{s['cap']}")


async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = get_bot(update.effective_user.id)
    accs = b.list_accounts()
    if not accs:
        await update.message.reply_text("No accounts. /login se add karo")
        return
    lines = [f"👥 Your Accounts ({len(accs)})"]
    kb = []
    for a in accs:
        lines.append(f"• {a}")
        kb.append([
            InlineKeyboardButton(f"Switch {a}", callback_data=f"sw:{a}"),
            InlineKeyboardButton("❌", callback_data=f"rm:{a}"),
        ])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def account_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    b = get_bot(q.from_user.id)
    if q.data.startswith("sw:"):
        ok, msg = b.switch_account(q.data[3:])
        if ok:
            b.open_app()
            await q.edit_message_text(f"✅ {msg}\nBalance: {b.get_balance()}")
        else:
            await q.edit_message_text(f"❌ {msg}")
    elif q.data.startswith("rm:"):
        if b.remove_account(q.data[3:]):
            await q.edit_message_text("Removed")
        else:
            await q.edit_message_text("Failed")


async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📱 Phone + OTP", callback_data="login:otp")],
        [InlineKeyboardButton("🔑 Bearer Token", callback_data="login:token")],
    ]
    await update.message.reply_text("Login method:", reply_markup=InlineKeyboardMarkup(kb))


async def login_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "login:otp":
        await q.edit_message_text("Phone number bhejo (+91...):")
        return WAIT_PHONE
    if q.data == "login:token":
        await q.edit_message_text("Bearer token bhejo:")
        return WAIT_TOKEN
    return ConversationHandler.END


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        phone = "+91" + phone.lstrip("0")
    b = get_bot(update.effective_user.id)
    st = b.login_otp_generate(phone)
    if not st:
        await update.message.reply_text("OTP fail. Number check karo.")
        return ConversationHandler.END
    context.user_data["st"] = st
    await update.message.reply_text("OTP bhejo:")
    return WAIT_OTP


async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = get_bot(update.effective_user.id)
    if b.login_otp_verify(context.user_data.get("st"), update.message.text.strip()):
        b.open_app()
        await update.message.reply_text(f"✅ Login OK\nBalance: {b.get_balance()}", reply_markup=menu())
    else:
        await update.message.reply_text("❌ OTP wrong")
    return ConversationHandler.END


async def login_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tok = update.message.text.strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:]
    b = get_bot(update.effective_user.id)
    if b.login_with_token(tok):
        b.open_app()
        await update.message.reply_text(f"✅ Login OK\nBalance: {b.get_balance()}", reply_markup=menu())
    else:
        await update.message.reply_text("❌ Invalid token")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled", reply_markup=menu())
    return ConversationHandler.END


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request_stop(update.effective_user.id)
    await update.message.reply_text("🛑 Stop signal sent")


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = get_bot(update.effective_user.id)
    if not b.access_token:
        await update.message.reply_text("Login first")
        return
    uid = update.effective_user.id
    clear_stop(uid)
    msg = await update.message.reply_text("🚀 Watch starting...\nStop: 🛑 Stop Task")

    def prog(t):
        try:
            asyncio.get_event_loop().create_task(msg.edit_text(f"🚀 {t[-800:]}"))
        except Exception:
            pass

    res = await asyncio.get_event_loop().run_in_executor(
        None, lambda: b.browse_and_watch_all_smart_repeat(prog, telegram_user_id=uid)
    )
    if "error" in res:
        await msg.edit_text(f"❌ {res['error']}")
        return
    text = f"{'🛑 Stopped' if res.get('stopped') else '🏁 Done'}\nWatched: {res['watched']}\nSkipped: {res['skipped']}"
    if res.get("delta") is not None:
        text += f"\n💰 {res['balance_before']} → {res['balance_after']} ({res['delta']:+d})"
    await msg.edit_text(text)


async def quiz_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = get_bot(update.effective_user.id)
    if not b.access_token:
        await update.message.reply_text("Login first")
        return
    d = b.get_quiz_status()
    if not d:
        await update.message.reply_text("Failed")
        return
    daily = d.get("dailyAttempts", {})
    await update.message.reply_text(f"Level: {d.get('currentLevel')}\nDaily: {daily.get('used')}/{daily.get('limit')}")


async def quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    b = get_bot(update.effective_user.id)
    if not b.access_token:
        await update.message.reply_text("Login first")
        return ConversationHandler.END
    if len(get_user_keys(update.effective_user.id)) < 1:
        await update.message.reply_text(
            "❌ Pehle kam se kam 1 Groq key set karo:\n\n"
            "`/setgroq1 gsk_xxx`\n"
            "`/setgroq2 gsk_yyy` (recommended)",
            parse_mode="Markdown",
        )
        return ConversationHandler.END
    await update.message.reply_text(f"Sessions? ({MIN_SESSIONS}-{MAX_SESSIONS}, default {DEFAULT_SESSIONS})")
    return WAIT_QUIZ_SESSIONS


async def quiz_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip() or DEFAULT_SESSIONS)
        n = max(MIN_SESSIONS, min(MAX_SESSIONS, n))
    except Exception:
        n = DEFAULT_SESSIONS

    b = get_bot(update.effective_user.id)
    uid = update.effective_user.id
    clear_stop(uid)
    msg = await update.message.reply_text(f"🤖 {n} sessions...\nStop: 🛑 Stop Task")

    def prog(t):
        try:
            asyncio.get_event_loop().create_task(msg.edit_text(f"🤖 {t[-900:]}"))
        except Exception:
            pass

    res = await asyncio.get_event_loop().run_in_executor(
        None, lambda: b.run_quiz_auto(n, QUIZ_QUESTION_DELAY, prog, uid)
    )
    if "error" in res:
        await msg.edit_text(f"❌ {res['error']}")
    else:
        status = "🛑 Stopped" if res.get("stopped") else "🏁 Finished"
        await msg.edit_text(
            f"{status}\n"
            f"Sessions: {res.get('sessions')}\n"
            f"Coins: ~{res.get('total_coins')}\n"
            f"Balance: {res.get('balance')}"
        )
    return ConversationHandler.END


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_bot(update.effective_user.id)._reset_state()
    await update.message.reply_text("Logged out", reply_markup=menu())


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    mapping = {
        "💰 Balance": balance_cmd,
        "📊 Campaign": campaign_cmd,
        "👥 Accounts": accounts_cmd,
        "➕ Login": login_start,
        "🎬 Watch All (4x)": watch_cmd,
        "🧠 Quiz Status": quiz_status,
        "🤖 Run Quiz": quiz_start,
        "🛑 Stop Task": stop_cmd,
        "🔑 Set Groq Keys": lambda u, c: u.message.reply_text(
            "Do keys set karo:\n`/setgroq1 gsk_xxx`\n`/setgroq2 gsk_yyy`", parse_mode="Markdown"
        ),
        "ℹ️ Help": help_cmd,
    }
    h = mapping.get(t)
    if h:
        return await h(update, context)
    await update.message.reply_text("Use menu buttons")


async def error_handler(update, context):
    logger.error("Exception:", exc_info=context.error)
    if "Conflict" in str(context.error):
        logger.error("Multiple instances!")


def main():
    acquire_lock()
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN missing")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    login_c = ConversationHandler(
        entry_points=[CallbackQueryHandler(login_cb, pattern="^login:")],
        states={
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            WAIT_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp)],
            WAIT_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_token)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    quiz_c = ConversationHandler(
        entry_points=[
            CommandHandler("quizrun", quiz_start),
            MessageHandler(filters.Regex("^🤖 Run Quiz$"), quiz_start),
        ],
        states={WAIT_QUIZ_SESSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_run)]},
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
    app.add_handler(CommandHandler("quiz", quiz_status))
    app.add_handler(CommandHandler("setgroq1", set_groq1))
    app.add_handler(CommandHandler("setgroq2", set_groq2))
    app.add_handler(CommandHandler("mygroq", my_groq))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(account_cb, pattern="^(sw|rm):"))
    app.add_handler(login_c)
    app.add_handler(quiz_c)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)

    print("Public bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
