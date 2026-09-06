import requests
import json
import time
import sys
import os
import io
import threading
from datetime import date, datetime
from collections import defaultdict

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        ContextTypes,
        filters,
    )
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("[!] python-telegram-bot install nahi mila. Install karo: pip install python-telegram-bot==20.7")

try:
    from flask import Flask, jsonify
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    print("[i] Flask nahi mila — keep-alive server disable rahega. pip install flask")

API_BASE = "https://api.minipix.co/v4"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.json")
USERS_FILE = os.path.join(BASE_DIR, "users_data.json")
STATS_FILE = os.path.join(BASE_DIR, "usage_stats.json")

MAX_WATCHES_PER_EP = 4
REWARDS_BY_WATCH = {1: 15, 2: 8, 3: 5, 4: 3}


def _expected_reward(nth_watch):
    return REWARDS_BY_WATCH.get(int(nth_watch) if nth_watch else 1, 0)


HEADERS_BASE = {
    "user-agent": "okhttp/4.12.0",
    "accept-encoding": "gzip",
}


class BotConfig:
    def __init__(self):
        self.bot_token = ""
        self.log_channel_id = ""
        self.port = int(os.environ.get("PORT", "8080"))
        self.webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
        self.load()

    def load(self):
        env_token = os.environ.get("BOT_TOKEN", "").strip()
        env_log = os.environ.get("LOG_CHANNEL_ID", "").strip()
        if env_token:
            self.bot_token = env_token
            self.log_channel_id = env_log
            if os.environ.get("RAILWAY_ENVIRONMENT_NAME") or os.environ.get("RAILWAY_STATIC_URL"):
                print("[✅] Railway environment detected — using ENV vars for config.")
                return
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    if not self.bot_token:
                        self.bot_token = d.get("bot_token", "")
                    if not self.log_channel_id:
                        self.log_channel_id = d.get("log_channel_id", "")
            except Exception:
                pass
        if not self.bot_token and not (os.environ.get("RAILWAY_ENVIRONMENT_NAME") or os.environ.get("RAILWAY_STATIC_URL")):
            print(f"\n[⚠️] Bot config nahi mila: {CONFIG_FILE}")
            print("    Local run: config file banaye ya environment variables set kare:")
            print("      BOT_TOKEN=xyz LOG_CHANNEL_ID=-100123 python bot.py")
            print("    Railway me: Variables tab me BOT_TOKEN aur LOG_CHANNEL_ID set kare.")
            try:
                self.bot_token = input("\n    Bot token daalo (ya empty chhod do): ").strip()
                self.log_channel_id = input("    Log channel ID daalo (ya empty): ").strip()
                self.save()
            except Exception:
                pass

    def save(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"bot_token": self.bot_token, "log_channel_id": self.log_channel_id}, f, indent=2)
        except Exception as e:
            print(f"[!] Config save error: {e}")


BOT_CONFIG = BotConfig()


class UserStats:
    def __init__(self):
        self.data = defaultdict(lambda: {
            "total_watches": 0,
            "total_coins_earned": 0,
            "quizzes_solved": 0,
            "series_done": 0,
            "last_active": None,
            "joined_at": None,
        })
        self.load()

    def load(self):
        if os.path.exists(STATS_FILE):
            try:
                with open(STATS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    for k, v in raw.items():
                        self.data[k] = v
            except Exception:
                pass

    def save(self):
        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(dict(self.data), f, indent=2, default=str)
        except Exception:
            pass

    def bump(self, user_id, key, amount=1):
        uid = str(user_id)
        if uid not in self.data:
            self.data[uid]["joined_at"] = datetime.now().isoformat()
        self.data[uid][key] = self.data[uid].get(key, 0) + amount
        self.data[uid]["last_active"] = datetime.now().isoformat()
        self.save()


STATS = UserStats()


class UserStore:
    def __init__(self):
        self.users = {}
        self.load()

    def load(self):
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    self.users = json.load(f)
            except Exception:
                self.users = {}

    def save(self):
        try:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.users, f, indent=2)
        except Exception as e:
            print(f"[!] Users save error: {e}")

    def get(self, user_id):
        uid = str(user_id)
        if uid not in self.users:
            self.users[uid] = {
                "minipix_token": None,
                "minipix_user_id": None,
                "minipix_profile_id": None,
                "minipix_phone": None,
                "groq_api_key": None,
                "otp_session": None,
                "otp_phone": None,
            }
            self.save()
        return self.users[uid]

    def set(self, user_id, **kwargs):
        uid = str(user_id)
        if uid not in self.users:
            self.get(user_id)
        self.users[uid].update(kwargs)
        self.save()


USERS = UserStore()


class MiniPixUserSession:
    def __init__(self, user_data):
        self.access_token = user_data.get("minipix_token")
        self.user_id = user_data.get("minipix_user_id")
        self.profile_id = user_data.get("minipix_profile_id")
        self.phone = user_data.get("minipix_phone")
        self.session = requests.Session()
        self.session.headers.update(HEADERS_BASE)
        self.device_id = "65969f0b7041fabc"
        self.device_info = "Xiaomi"
        self.watch_history = {}
        self.watch_history_raw = []
        self.runtime_watch_counts = {}
        self.groq_api_key = user_data.get("groq_api_key")
        self.quiz_qbank = {}
        if self.access_token:
            self.session.headers["authorization"] = f"Bearer {self.access_token}"

    def to_dict(self):
        return {
            "minipix_token": self.access_token,
            "minipix_user_id": self.user_id,
            "minipix_profile_id": self.profile_id,
            "minipix_phone": self.phone,
            "groq_api_key": self.groq_api_key,
        }

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
            return 0, None

    def login_otp_generate(self, phone):
        self.phone = phone
        payload = {"phone_number": phone}
        sc, data = self._req(
            "POST",
            "/login/generate-otp",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and data and data.get("message") == "OTP sent":
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
        sc, data = self._req(
            "POST",
            "/login/verify-otp",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and data and data.get("access_token"):
            self.access_token = data["access_token"]
            self.user_id = data["id"]
            self.session.headers["authorization"] = f"Bearer {self.access_token}"
            self.get_user()
            return True
        return False

    def login_with_token(self, token, user_id=None, profile_id=None):
        self.access_token = token
        self.user_id = user_id
        self.profile_id = profile_id
        self.session.headers["authorization"] = f"Bearer {self.access_token}"
        if not self.get_user():
            return False
        return True

    def get_user(self):
        if not self.user_id:
            return False
        sc, data = self._req("GET", f"/users/{self.user_id}")
        if sc == 200 and data:
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
        if sc == 200 and data and data.get("success"):
            return True
        return False

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
                obj.get("title")
                or obj.get("numberOfEpisodes") is not None
                or obj.get("totalEpisodes") is not None
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

    def get_all_series(self, page_size=100, max_pages=10):
        found = {}
        home_urls = [("GET", "/short_search?page=home", False)]
        for method, tmpl, _p in home_urls:
            try:
                sc, data = self._req(method, tmpl)
            except Exception:
                sc, data = 0, None
            if sc == 200 and isinstance(data, (dict, list)):
                self._collect_series_deep(data, found)
        endpoints = [
            ("GET", "/webseries?page={p}&pageSize={ps}", True),
            ("GET", "/discover?type=webseries&page={p}&pageSize={ps}", True),
            ("GET", "/series?page={p}&pageSize={ps}", True),
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
                inner = data.get("data") if isinstance(data.get("data"), dict) else (
                    data.get("response") if isinstance(data.get("response"), dict) else None
                )
                if inner:
                    for k in ("webseries", "series", "items", "results", "contents", "list", "data"):
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
        def _sort_key(s):
            try:
                return -int(s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0)
            except Exception:
                return 0
        series_list.sort(key=_sort_key)
        return series_list

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
        raw_history = []
        try:
            self.get_profile()
        except Exception:
            pass
        if isinstance(self.watch_history_raw, list):
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
        for (sid, ep_no), info in self.watch_history.items():
            pct = int(info.get("watchedPct") or 0) if isinstance(info, dict) else 0
            if pct >= 80:
                k = (str(sid), str(ep_no))
                if counts.get(k, 0) < 1:
                    counts[k] = max(counts.get(k, 0), 1)
        if isinstance(self.runtime_watch_counts, dict):
            for k, c in self.runtime_watch_counts.items():
                counts[k] = max(counts.get(k, 0), c)
        return counts

    def get_series(self, series_id):
        sc, data = self._req("GET", f"/webseries/{series_id}")
        if sc == 200 and data and data.get("success"):
            return data
        return None

    def get_episodes(self, series_id, page=1, page_size=50):
        sc, data = self._req("GET", f"/episodes?series_id={series_id}&page={page}&pageSize={page_size}")
        if sc == 200 and data:
            return data.get("episodes", []), data.get("total", 0)
        return [], 0

    def _update_watch_progress(self, series_id, series_title, hindi_title, episode_no,
                                tc_in_ms, tc_out_ms, detail_image, watched_pct):
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
        try:
            for path in (
                f"/users/{self.user_id}/profiles/{self.profile_id}/watch-history/update",
                "/watch-history/update",
            ):
                sc2, d2 = self._req(
                    "POST", path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps({"watched": watch_obj, "campaign": False}, ensure_ascii=False).encode("utf-8"),
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

    def _report_watch_progress_to_coins(self, series_id, episode_no, watched_pct, series_title=""):
        if not (self.user_id and self.profile_id):
            return False
        try:
            watched_pct = int(watched_pct or 0)
        except Exception:
            watched_pct = 0
        ep_str = str(episode_no)
        bodies = [
            {"series_id": series_id, "episode_no": episode_no, "episodeNo": episode_no,
             "progress": watched_pct, "watchedPct": watched_pct, "campaign": False, "task_type": "watch_ladder"},
            {"type": "watch_ladder", "seriesId": series_id, "episode": ep_str, "watched": watched_pct, "campaign": False},
            {"task_id": f"watch_ladder_{series_id}", "progress_delta": 1, "series_id": series_id,
             "episode_no": episode_no, "campaign": False},
        ]
        endpoints = [
            ("POST", "/coins/progress-report", bodies[0]),
            ("POST", "/coins/tasks/progress", bodies[0]),
            ("POST", "/coins/watch-progress", bodies[1]),
            ("POST", "/coins/report-watched", bodies[1]),
            ("POST", "/watch-ladder/progress", bodies[0]),
        ]
        any_ok = False
        for method, path, body in endpoints:
            try:
                sc, d = self._req(
                    method, path,
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
            ("POST", f"/coins/tasks/{task_id}/start", {"series_id": series_id, "campaign": False}),
            ("POST", "/watch-campaign/start", {"seriesId": series_id, "series_id": series_id, "campaign": False}),
            ("POST", "/watch-campaign/select-series", {"seriesId": series_id, "series_id": series_id, "campaign": False}),
        ]
        for method, path, body in candidates:
            try:
                sc, d = self._req(
                    method, path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
                if sc and sc < 500:
                    if isinstance(d, dict) and d.get("success") is True:
                        return True
                    if sc == 200:
                        return True
            except Exception:
                pass
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
        candidates = []
        if series_id:
            task_id = f"watch_ladder_{series_id}"
            candidates.append(("POST", f"/coins/tasks/{task_id}/claim", None))
            candidates.append(("POST", "/coins/tasks/claim", {"task_id": task_id, "campaign": False}))
            candidates.append(("POST", f"/coins/tasks/{task_id}/reward", None))
            candidates.append(("POST", "/watch-ladder/claim", {"series_id": series_id, "campaign": False}))
            candidates.append(("POST", f"/coins/claim", {"series_id": series_id, "type": "watch_ladder", "campaign": False}))
        any_ok = False
        last_coins = None
        for method, path, body in candidates:
            try:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
                sc, data = self._req(
                    method, path,
                    headers={"content-type": "application/json; charset=utf-8"} if body else {},
                    data=payload,
                )
                if sc and sc < 500 and isinstance(data, dict):
                    if data.get("success") is True:
                        last_coins = data.get("coins") or data.get("reward_coins") or data.get("reward") or 0
                        any_ok = True
                        break
                    if sc == 200 and "success" not in data:
                        any_ok = True
                        break
            except Exception:
                continue
        return any_ok, last_coins

    def unlock_episode(self, series_id, ep_id, ep_no):
        if not (series_id and ep_id):
            return False
        unlock_candidates = [
            ("POST", f"/episodes/{ep_id}/unlock", {"series_id": series_id, "episodeNo": ep_no, "campaign": False}),
            ("POST", "/episodes/unlock", {"series_id": series_id, "episode_id": ep_id, "episodeNo": ep_no, "campaign": False}),
            ("POST", "/coins/unlock-episode", {"series_id": series_id, "episode_id": ep_id, "episodeNo": ep_no}),
        ]
        for method, path, body in unlock_candidates:
            try:
                sc, d = self._req(
                    method, path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                )
                if sc and sc < 500 and isinstance(d, dict):
                    if d.get("success") is True:
                        return True
                    if sc == 200 and d.get("unlocked"):
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
        expected_coin = None
        if nth_watch is not None:
            try:
                expected_coin = _expected_reward(int(nth_watch))
            except Exception:
                pass
        if not episode.get("coinUnlocked", True):
            ep_id = episode.get("_id") or episode.get("id")
            try:
                self.unlock_episode(series_id, ep_id, ep_no)
            except Exception:
                pass
        progress_steps = [1, 50, 80, 99, 100, 100]
        reported_coin_progress = False
        for pct in progress_steps:
            if not allow_repeat and pct < current_pct:
                continue
            self._update_watch_progress(
                series_id, series_title, hindi_title, ep_no,
                tc_in_ms, tc_out_ms, detail_image, pct,
            )
            if pct >= 80 and not reported_coin_progress:
                try:
                    self._report_watch_progress_to_coins(series_id, ep_no, pct, series_title)
                    reported_coin_progress = True
                except Exception:
                    pass
            time.sleep(0.15)
        if not reported_coin_progress:
            try:
                self._report_watch_progress_to_coins(series_id, ep_no, 100, series_title)
            except Exception:
                pass
        bal_before = self.get_balance_silent()
        try:
            self.claim_reward_task(series_id=series_id)
        except Exception:
            pass
        time.sleep(0.6)
        bal_after = self.get_balance_silent()
        gained = 0
        if bal_before is not None and bal_after is not None:
            gained = (bal_after or 0) - (bal_before or 0)
        self.watch_history[history_key] = {"watchedPct": 100, "time": tc_out_ms}
        rk = (str(series_id), str(ep_no))
        self.runtime_watch_counts[rk] = self.runtime_watch_counts.get(rk, 0) + 1
        self.watch_history_raw.append({
            "id": series_id,
            "series_id": series_id,
            "episodeNo": ep_no,
            "watchedPct": 100,
            "progress": 100,
            "time": tc_out_ms,
        })
        return True, "done", gained, expected_coin

    def solve_quiz_with_groq(self, question, opts, force=False):
        opts = [str(x) for x in (opts or [])]
        if not opts or not self.groq_api_key:
            return -1, None
        qid = question.get("questionId") or ""
        qhi = str(question.get("questionHi") or "")
        qen = str(question.get("questionEn") or "")
        topic = str(question.get("topic") or "")
        qtype = question.get("type") or "unknown"
        cache = getattr(self, "groq_cache", None)
        if cache is None:
            cache = {}
            setattr(self, "groq_cache", cache)
        cache_key = (qid, tuple(opts), qhi, qen, topic)
        if not force and cache_key in cache:
            return cache[cache_key]
        prompt_lines = [
            "# MINI-QUIZ QUESTION (LEVEL-1 KIDS)",
            "",
            f"**Type**: {qtype}",
            f"**Question (Hi)**: {qhi}",
            f"**Question (En)**: {qen}",
            f"**Topic**: {topic}" if topic else "",
            "",
            "**Options (index = N from 0)**:",
        ]
        for i, o in enumerate(opts):
            prompt_lines.append(f"  N={i}  →  {o}")
        prompt_lines.extend([
            "",
            "## INSTRUCTIONS",
            "- This is a KIDS/LEVEL-1 multiple choice English question for a mini-quiz inside an Indian short-video app.",
            "- Pick the SINGLE best correct option index.",
            f"- Return ONLY a strict JSON object, exactly one line, in this shape:",
            '  {"chosenIndex": N, "reasoning": "short reasoning"}',
            f"- WHERE N MUST be an integer strictly between 0 and {len(opts) - 1} inclusive.",
            "- Strict JSON only, no markdown, no extra text.",
            "",
            "## OUTPUT (strict JSON only)",
        ])
        prompt = "\n".join(prompt_lines)
        chosen_index = -1
        reasoning = None
        try:
            api_key = self.groq_api_key.strip()
            url = "https://api.groq.com/openai/v1/chat/completions"
            body = {
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 256,
                "response_format": {"type": "json_object"},
            }
            resp = self.session.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                },
                data=json.dumps(body),
                timeout=30,
            )
            if resp.status_code == 200:
                rd = resp.json()
                choices = rd.get("choices") or []
                if choices:
                    raw_txt = (choices[0].get("message") or {}).get("content") or ""
                    chosen_index, reasoning = self._parse_quiz_json(raw_txt, len(opts))
        except Exception:
            pass
        if chosen_index is not None and 0 <= int(chosen_index) < len(opts):
            chosen_index = int(chosen_index)
            cache[cache_key] = (chosen_index, reasoning or "groq")
            return chosen_index, reasoning or "groq"
        return -1, None

    def _parse_quiz_json(self, raw_text, n_options):
        import re as _rep
        if not raw_text:
            return -1, None
        chosen_index = -1
        reasoning = None
        s = str(raw_text).strip()
        s2 = s.replace("```json", "").replace("```", "").strip()
        try:
            d = json.loads(s2)
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
            m = _rep.search(r'"chosenIndex"\s*:\s*(-?\d+)', s2)
            if m:
                try:
                    chosen_index = int(m.group(1))
                except Exception:
                    pass
        if chosen_index < 0:
            m2 = _rep.search(r"\bN\s*=\s*(\d+)\b", s2)
            if m2:
                try:
                    chosen_index = int(m2.group(1))
                except Exception:
                    pass
        if reasoning is None:
            m_reason = _rep.search(r'"reasoning"\s*:\s*"([^"]{0,120})"', s2)
            if m_reason:
                reasoning = m_reason.group(1)
        if chosen_index is not None and 0 <= int(chosen_index) < n_options:
            return int(chosen_index), reasoning
        return -1, reasoning

    def quiz_get_status(self):
        sc, data = self._req("GET", "/quiz/status")
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data
        return None

    def quiz_start_session(self):
        sc, data = self._req("POST", "/quiz/session/start",
                             headers={"content-type": "application/json; charset=utf-8"},
                             data=json.dumps({"campaign": False}).encode("utf-8"))
        if sc == 200 and isinstance(data, dict) and data.get("success"):
            return data
        return None

    def quiz_submit_answer(self, session_id, question_id, chosen_index):
        body = {
            "sessionId": session_id,
            "questionId": question_id,
            "chosenIndex": int(chosen_index),
        }
        sc, data = self._req(
            "POST", "/quiz/session/answer",
            headers={"content-type": "application/json; charset=utf-8"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        if sc == 200 and isinstance(data, dict):
            return data
        return None

    def quiz_claim_final(self, session_id):
        if not session_id:
            return False
        candidates = [
            ("POST", "/quiz/session/claim", {"sessionId": session_id, "campaign": False}),
            ("POST", "/quiz/session/complete", {"sessionId": session_id, "campaign": False}),
            ("POST", "/quiz/claim", {"sessionId": session_id, "campaign": False}),
            ("POST", "/coins/quiz-claim", {"sessionId": session_id, "type": "quiz", "campaign": False}),
        ]
        any_ok = False
        last_coins = None
        for method, path, body in candidates:
            try:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                sc, d = self._req(
                    method, path,
                    headers={"content-type": "application/json; charset=utf-8"},
                    data=payload,
                )
                if sc and sc < 500 and isinstance(d, dict):
                    if d.get("success") is True:
                        last_coins = d.get("coins") or d.get("reward_coins") or d.get("coinsEarned") or d.get("reward") or 0
                        any_ok = True
                        break
                    if sc == 200 and "success" not in d:
                        any_ok = True
                        break
            except Exception:
                continue
        return any_ok, last_coins

    def run_auto_quiz(self, max_sessions=5):
        if not self.groq_api_key:
            yield "❌ Groq API key set nahi hai! Pehle /set_groq_key use karo."
            return
        bal_start = self.get_balance_silent()
        total_coins = 0
        sessions_done = 0
        for session_num in range(1, max_sessions + 1):
            yield f"🔄 Quiz Session {session_num}/{max_sessions} start..."
            sess = self.quiz_start_session()
            if not sess:
                yield "❌ Quiz session start nahi ho paya."
                continue
            session_id = sess.get("sessionId") or sess.get("session_id")
            questions = sess.get("questions") or sess.get("data", {}).get("questions") or []
            if not questions:
                yield "⚠️ Session me questions nahi mile, skip."
                continue
            correct_count = 0
            for q_idx, q in enumerate(questions, 1):
                qid = q.get("questionId") or q.get("id")
                opts = q.get("options") or []
                chosen, reason = self.solve_quiz_with_groq(q, opts)
                if chosen < 0:
                    chosen = 0
                    reason = "fallback"
                resp = self.quiz_submit_answer(session_id, qid, chosen)
                correct = False
                if resp and isinstance(resp, dict):
                    correct = resp.get("correct") is True or resp.get("isCorrect") is True or (
                        isinstance(resp.get("success"), dict) and resp["success"].get("correct") is True
                    )
                if correct:
                    correct_count += 1
                opt_txt = str(opts[chosen])[:50] if chosen < len(opts) else "?"
                yield f"  Q{q_idx}: {'✅' if correct else '❌'} N={chosen} → {opt_txt}"
            ok, coins = self.quiz_claim_final(session_id)
            if coins:
                total_coins += int(coins)
                yield f"  💰 Session {session_num}: +{coins} coins | {correct_count}/{len(questions)} correct"
            sessions_done += 1
            time.sleep(1)
        bal_end = self.get_balance_silent()
        delta = 0
        if bal_start is not None and bal_end is not None:
            delta = (bal_end or 0) - (bal_start or 0)
        yield f"\n🏁 QUIZ COMPLETE: {sessions_done} sessions | Net delta: +{delta} coins"


def get_session_for(user_id) -> MiniPixUserSession:
    ud = USERS.get(user_id)
    return MiniPixUserSession(ud)


def save_session_for(user_id, session: MiniPixUserSession):
    USERS.set(user_id, **session.to_dict())


async def log_to_channel(application, message: str):
    ch_id = BOT_CONFIG.log_channel_id
    if not ch_id or not application:
        return
    try:
        await application.bot.send_message(chat_id=ch_id, text=message[:4000])
    except Exception:
        pass


def user_display(user):
    name = ""
    if user.first_name:
        name += user.first_name
    if user.last_name:
        name += " " + user.last_name
    if not name:
        name = str(user.id)
    if user.username:
        name += f" (@{user.username})"
    return name


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    STATS.bump(user.id, "last_active")
    STATS.data[str(user.id)]["joined_at"] = STATS.data[str(user.id)].get("joined_at") or datetime.now().isoformat()
    STATS.save()
    ud = USERS.get(user.id)
    text = (
        f"👋 *Welcome {user.first_name or ''}*\n\n"
        f"🤖 *MiniPix Automation Bot*\n\n"
        f"📋 *Commands:*\n"
        f"/login - MiniPix me login (OTP)\n"
        f"/set_token - Direct access token set karo\n"
        f"/set_groq_key - Groq AI API key set karo (quiz ke liye)\n\n"
        f"/balance - Coin balance check\n"
        f"/status - Campaign + daily cap status\n"
        f"/series_list - Available series dikhaye\n"
        f"/watch_all - Sab series smart watch start (4x rewards)\n"
        f"/watch_one <series_id> - Ek specific series dekho\n"
        f"/quiz <N> - Mini-quiz auto solve (N sessions, default 5)\n\n"
        f"/my_stats - Apna usage stats dekho\n"
        f"/help - Ye message firse\n"
    )
    status = "✅ Logged IN" if ud.get("minipix_token") else "❌ Not logged in"
    groq = "✅ Groq key set" if ud.get("groq_api_key") else "⚠️ Groq key nahi set (quiz nahi chalega)"
    text += f"\n📊 *Your Status:*\n  • MiniPix: {status}\n  • {groq}\n"
    await update.message.reply_text(text, parse_mode="Markdown")
    await log_to_channel(context.application, f"👤 User: {user_display(user)}\nID: `{user.id}`\n/start diya.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "📱 *Phone number bhejo:*\n`/login 9876543210`\n\nFormat: /login <10-digit number>"
            "\n\nYa direct message me sirf phone number bhejo (10 digit)."
            , parse_mode="Markdown"
        )
        return
    phone = args[0].strip()
    if not phone.isdigit() or len(phone) < 10:
        await update.message.reply_text("❌ Valid 10-digit phone number daalo!")
        return
    if not phone.startswith("+"):
        phone_full = "+91" + phone[-10:]
    else:
        phone_full = phone
    sess = get_session_for(user.id)
    session_token = sess.login_otp_generate(phone_full)
    if session_token:
        USERS.set(user.id, otp_session=session_token, otp_phone=phone_full)
        save_session_for(user.id, sess)
        await update.message.reply_text(
            f"✅ OTP bheja gaya `{phone_full}` par!\n\nAb OTP aise bhejo:\n`/otp 123456`\n\nYa sirf 6 digit ka OTP message me bhejo."
            , parse_mode="Markdown"
        )
        await log_to_channel(context.application, f"🔐 Login OTP request: {user_display(user)} | Phone: {phone_full}")
    else:
        await update.message.reply_text("❌ OTP generate nahi ho paya! Number check karo ya baad me try karo.")


async def otp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ud = USERS.get(user.id)
    args = context.args or []
    otp = None
    if args:
        otp = args[0].strip()
    else:
        txt = (update.message.text or "").strip()
        if txt.isdigit() and len(txt) == 6:
            otp = txt
    if not otp or not otp.isdigit():
        await update.message.reply_text("❌ Valid 6-digit OTP daalo: `/otp 123456`", parse_mode="Markdown")
        return
    session_token = ud.get("otp_session")
    phone = ud.get("otp_phone")
    if not session_token or not phone:
        await update.message.reply_text("❌ Pehle /login use karo OTP ke liye!")
        return
    sess = get_session_for(user.id)
    sess.phone = phone
    ok = sess.login_otp_verify(session_token, otp)
    if ok:
        save_session_for(user.id, sess)
        USERS.set(user.id, otp_session=None, otp_phone=None)
        info = f"Phone: {sess.phone}\nUser ID: `{sess.user_id}`\nProfile ID: `{sess.profile_id}`"
        try:
            sess.open_app()
        except Exception:
            pass
        bal = sess.get_balance()
        msg = f"✅ *Login SUCCESS!*\n\n{info}\n\n💰 Balance: `{bal or '?'}` coins"
        await update.message.reply_text(msg, parse_mode="Markdown")
        await log_to_channel(context.application, f"✅ LOGIN SUCCESS: {user_display(user)}\nPhone: {sess.phone}\nMiniPix User ID: {sess.user_id}")
    else:
        await update.message.reply_text("❌ OTP wrong ya expired! Firse /login use karo.")


async def set_token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🔑 *MiniPix access token set karo:*\n`/set_token <your_access_token>`\n\n"
            "Token format: long JWT string (app se capture karo ya login se lo)."
            , parse_mode="Markdown"
        )
        return
    token = args[0].strip()
    sess = MiniPixUserSession({"groq_api_key": USERS.get(user.id).get("groq_api_key")})
    ok = sess.login_with_token(token)
    if ok:
        save_session_for(user.id, sess)
        msg = f"✅ *Token set ho gaya!*\nUser ID: `{sess.user_id}`\nProfile ID: `{sess.profile_id}`"
        try:
            sess.open_app()
        except Exception:
            pass
        bal = sess.get_balance()
        msg += f"\n💰 Balance: `{bal or '?'}` coins"
        await update.message.reply_text(msg, parse_mode="Markdown")
        await log_to_channel(context.application, f"🔑 TOKEN LOGIN: {user_display(user)}\nMiniPix User ID: {sess.user_id}")
    else:
        await update.message.reply_text("❌ Token invalid ya expired!")


async def set_groq_key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🧠 *Groq AI API Key set karo:*\n`/set_groq_key gsk_xxxxxx`\n\n"
            "👉 Key yaha se lelo: https://console.groq.com/keys\n"
            "Ye key sirf aapke quiz solve karne ke liye use hogi (aapki hi file me save hoti hai)."
            , parse_mode="Markdown"
        )
        return
    key = args[0].strip()
    USERS.set(user.id, groq_api_key=key)
    await update.message.reply_text("✅ Groq API key set ho gayi! Ab /quiz use kar sakte ho.", parse_mode="Markdown")
    await log_to_channel(context.application, f"🧠 GROQ KEY SET: {user_display(user)} | Key length: {len(key)} chars")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sess = get_session_for(user.id)
    if not sess.access_token:
        await update.message.reply_text("❌ Pehle login karo: /login ya /set_token")
        return
    bal = sess.get_balance()
    if bal is not None:
        await update.message.reply_text(f"💰 *Coin Balance:* `{bal}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Balance fetch nahi ho paya!")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sess = get_session_for(user.id)
    if not sess.access_token:
        await update.message.reply_text("❌ Pehle login karo!")
        return
    cap = sess.get_campaign_status()
    bal = sess.get_balance()
    text = (
        f"📊 *MiniPix Status*\n\n"
        f"💰 Balance: `{bal or '?'}` coins\n"
        f"📹 Campaign: {'✅ ON' if cap['enabled'] else '❌ OFF'}\n"
        f"🎯 Daily Cap: `{cap['used']}/{cap['cap']}`"
        f"{'  *[REACHED]*' if cap['reached'] else ''}\n"
        f"🚫 Block watching: {'YES' if cap['blockWatching'] else 'NO'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def series_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sess = get_session_for(user.id)
    if not sess.access_token:
        await update.message.reply_text("❌ Pehle login karo!")
        return
    msg = await update.message.reply_text("🔍 Series load ho rahi hain...")
    try:
        sess.get_profile()
    except Exception:
        pass
    counts = sess.get_watch_counts_from_profile()
    series = sess.get_all_series(max_pages=5)
    if not series:
        await msg.edit_text("❌ Koi series nahi mili!")
        return
    lines = [f"📚 *Series List* (first 25 of {len(series)}):\n"]
    for i, s in enumerate(series[:25], 1):
        sid = s.get("_id") or s.get("id") or s.get("series_id")
        title = (s.get("title") or s.get("name") or "?")[:30]
        n_eps = s.get("numberOfEpisodes") or s.get("totalEpisodes") or 0
        slots = 0
        try:
            slots = sum(max(0, MAX_WATCHES_PER_EP - counts.get((str(sid), str(e)), 0)) for e in range(1, int(n_eps) + 1))
        except Exception:
            pass
        lines.append(f"{i}. *{title}*\n   ID: `{sid}`\n   🎬 {n_eps} eps | 🔁 {slots} slots\n")
    await msg.edit_text("\n".join(lines)[:4000], parse_mode="Markdown")


async def watch_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sess = get_session_for(user.id)
    if not sess.access_token:
        await update.message.reply_text("❌ Pehle login karo!")
        return
    args = context.args or []
    stop_after = None
    if args and args[0].isdigit():
        stop_after = int(args[0])
    msg = await update.message.reply_text(
        f"🚀 *Smart Watch Start* (max {MAX_WATCHES_PER_EP}x/ep: 15→8→5→3 coins)\n\n"
        f"📡 Series discover ho rahi hain...\n"
        + (f"⏹️ Stop after {stop_after} total watches." if stop_after else "")
        , parse_mode="Markdown"
    )
    try:
        sess.open_app()
    except Exception:
        pass
    cap = sess.get_campaign_status()
    daily_used = cap.get("used", 0)
    daily_cap = cap.get("cap", 0)
    try:
        sess.get_profile()
    except Exception:
        pass
    watch_counts = sess.get_watch_counts_from_profile()
    series_list = sess.get_all_series(max_pages=8)
    total_watched = 0
    total_gained = 0
    bal_start = sess.get_balance_silent()
    await msg.edit_text(
        f"🚀 Watch Running...\n📚 {len(series_list)} series mili\n"
        f"📹 Daily cap: {daily_used}/{daily_cap}\n"
        + (f"⏹️ Stop after: {stop_after} watches" if stop_after else ""),
        parse_mode="Markdown"
    )
    log_chunks = []

    def _ep_key(e):
        n = e.get("episodeNo")
        try:
            return int(n)
        except Exception:
            try:
                return float(n)
            except Exception:
                return 0

    for s_idx, s in enumerate(series_list, 1):
        if stop_after and total_watched >= stop_after:
            break
        if daily_cap and daily_used + total_watched >= daily_cap:
            log_chunks.append(f"\n🛑 *DAILY CAP REACHED* ({daily_used}+{total_watched} >= {daily_cap})!")
            break
        sid = s.get("_id") or s.get("id") or s.get("series_id")
        if not sid:
            continue
        s_info = sess.get_series(sid) or s
        real_sid = (s_info or {}).get("_id") or (s_info or {}).get("id") or sid
        s_title = (s_info or {}).get("title") or s.get("title") or "?"
        episodes, _ = sess.get_episodes(real_sid, page=1, page_size=300)
        if not episodes:
            continue
        episodes_sorted = sorted(episodes, key=_ep_key)
        sess.watch_campaign_select_series(real_sid)
        series_watched = 0
        series_gained = 0
        any_progress = True
        while any_progress:
            any_progress = False
            for ep in episodes_sorted:
                if stop_after and total_watched >= stop_after:
                    break
                if daily_cap and daily_used + total_watched >= daily_cap:
                    break
                ep_no = ep.get("episodeNo") or ep.get("episode_no") or 0
                kp = (str(real_sid), str(ep_no))
                cur = watch_counts.get(kp, 0)
                if cur >= MAX_WATCHES_PER_EP:
                    continue
                nth = cur + 1
                ok, st, gained, expected = sess.watch_episode(ep, s_info or s, allow_repeat=True, nth_watch=nth)
                if st == "skip":
                    continue
                if ok:
                    series_watched += 1
                    total_watched += 1
                    series_gained += gained or 0
                    total_gained += gained or 0
                    watch_counts[kp] = nth
                    any_progress = True
                    STATS.bump(user.id, "total_watches", 1)
                    if gained:
                        STATS.bump(user.id, "total_coins_earned", gained)
        if series_watched > 0:
            log_chunks.append(f"📺 *{s_title[:30]}*: ✅ {series_watched} watches | 💰 +{series_gained}")
            if len(log_chunks) % 3 == 0:
                try:
                    save_session_for(user.id, sess)
                except Exception:
                    pass
                prog_text = (
                    f"🚀 Watch Running...\n"
                    f"📚 Series {s_idx}/{len(series_list)}\n"
                    f"🎬 Total watches: {total_watched}\n"
                    f"💰 Coins gained: +{total_gained}\n\n"
                    + "\n".join(log_chunks[-10:])
                )
                try:
                    await msg.edit_text(prog_text[:4000], parse_mode="Markdown")
                except Exception:
                    pass
    bal_end = sess.get_balance_silent()
    net_delta = 0
    if bal_start is not None and bal_end is not None:
        net_delta = (bal_end or 0) - (bal_start or 0)
    STATS.data[str(user.id)]["series_done"] = STATS.data[str(user.id)].get("series_done", 0) + sum(1 for c in log_chunks if "✅" in c)
    STATS.save()
    save_session_for(user.id, sess)
    final = (
        f"🏁 *WATCH COMPLETE*\n\n"
        f"🎬 Total episodes watched: *{total_watched}*\n"
        f"💰 Coins gained (claimed so far): *+{total_gained}*\n"
        f"📊 Net balance delta: *+{net_delta}* coins\n"
        f"  (Before: {bal_start} → After: {bal_end})\n\n"
    )
    if log_chunks:
        final += "📝 *Per-series summary:*\n" + "\n".join(log_chunks[-15:])
    await msg.edit_text(final[:4000], parse_mode="Markdown")
    await log_to_channel(
        context.application,
        f"🎬 WATCH COMPLETE: {user_display(user)}\n"
        f"🎬 {total_watched} episodes | 💰 Net +{net_delta} coins\n"
        f"Before: {bal_start} → After: {bal_end}"
    )


async def watch_one_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sess = get_session_for(user.id)
    if not sess.access_token:
        await update.message.reply_text("❌ Pehle login karo!")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🎬 *Ek series dekho:*\n`/watch_one <series_id>`\n\nSeries ID ke liye /series_list use karo.",
            parse_mode="Markdown"
        )
        return
    series_id = args[0].strip()
    msg = await update.message.reply_text(f"🔍 Series `{series_id}` load ho raha hai...", parse_mode="Markdown")
    try:
        sess.open_app()
    except Exception:
        pass
    try:
        sess.get_profile()
    except Exception:
        pass
    watch_counts = sess.get_watch_counts_from_profile()
    s_info = sess.get_series(series_id)
    if not s_info:
        await msg.edit_text(f"❌ Series `{series_id}` nahi mili!")
        return
    real_sid = s_info.get("_id") or s_info.get("id") or series_id
    title = s_info.get("title") or "?"
    n_eps = s_info.get("numberOfEpisodes") or s_info.get("totalEpisodes") or 0
    episodes, _ = sess.get_episodes(real_sid, page=1, page_size=max(300, int(n_eps) or 300))
    if not episodes:
        await msg.edit_text("❌ Episodes nahi mile!")
        return
    def _ep_key(e):
        n = e.get("episodeNo")
        try:
            return int(n)
        except Exception:
            return 0
    episodes_sorted = sorted(episodes, key=_ep_key)
    sess.watch_campaign_select_series(real_sid)
    total_watched = 0
    total_gained = 0
    bal_start = sess.get_balance_silent()
    log_lines = []
    any_progress = True
    while any_progress:
        any_progress = False
        for ep in episodes_sorted:
            ep_no = ep.get("episodeNo") or ep.get("episode_no") or 0
            kp = (str(real_sid), str(ep_no))
            cur = watch_counts.get(kp, 0)
            if cur >= MAX_WATCHES_PER_EP:
                continue
            nth = cur + 1
            ok, st, gained, expected = sess.watch_episode(ep, s_info, allow_repeat=True, nth_watch=nth)
            if st == "skip":
                continue
            if ok:
                total_watched += 1
                total_gained += gained or 0
                watch_counts[kp] = nth
                any_progress = True
                STATS.bump(user.id, "total_watches", 1)
                if gained:
                    STATS.bump(user.id, "total_coins_earned", gained)
                try:
                    ep_no_f = int(ep_no)
                except Exception:
                    ep_no_f = ep_no
                log_lines.append(f"  E{ep_no_f} x{nth}: +{gained or 0} coins")
                if len(log_lines) % 5 == 0:
                    prog = (
                        f"🎬 *Watching: {title[:30]}*\n\n"
                        f"✅ Done: {total_watched} watches\n"
                        f"💰 Gained: +{total_gained}\n\n"
                        + "\n".join(log_lines[-10:])
                    )
                    try:
                        await msg.edit_text(prog[:4000], parse_mode="Markdown")
                    except Exception:
                        pass
    bal_end = sess.get_balance_silent()
    net_delta = 0
    if bal_start is not None and bal_end is not None:
        net_delta = (bal_end or 0) - (bal_start or 0)
    STATS.bump(user.id, "series_done", 1)
    save_session_for(user.id, sess)
    final = (
        f"🏁 *Series Complete: {title}*\n\n"
        f"🎬 Total watches: *{total_watched}*\n"
        f"💰 Claimed so far: *+{total_gained}*\n"
        f"📊 Net balance delta: *+{net_delta}*\n"
        f"  (Before: {bal_start} → After: {bal_end})\n\n"
    )
    if log_lines:
        final += "📝 *Recent:*\n" + "\n".join(log_lines[-20:])
    await msg.edit_text(final[:4000], parse_mode="Markdown")
    await log_to_channel(
        context.application,
        f"🎬 WATCH ONE: {user_display(user)}\n"
        f"Series: {title} ({real_sid})\n"
        f"{total_watched} watches | Net +{net_delta} coins"
    )


async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sess = get_session_for(user.id)
    if not sess.access_token:
        await update.message.reply_text("❌ Pehle MiniPix me login karo!")
        return
    if not sess.groq_api_key:
        await update.message.reply_text(
            "❌ *Groq API Key nahi set!*\n\nQuiz solve karne ke liye:\n`/set_groq_key gsk_xxxxxx`\n\n"
            "👉 Key: https://console.groq.com/keys",
            parse_mode="Markdown"
        )
        return
    args = context.args or []
    n = 5
    if args and args[0].isdigit():
        n = max(1, min(int(args[0]), 20))
    msg = await update.message.reply_text(f"🧠 *Quiz Auto-Solve* ({n} sessions)...\nUsing Groq AI (OpenAI GPT-OSS 120B)\n", parse_mode="Markdown")
    lines = []
    total_coins = 0
    sessions_done = 0
    total_q = 0
    total_correct = 0
    for out in sess.run_auto_quiz(max_sessions=n):
        lines.append(out)
        if "correct_count" in str(out):
            pass
        if "Session " in out and ":" in out:
            sessions_done += 1
        if "Net delta" in out or "Net delta" in out:
            pass
        STATS.bump(user.id, "quizzes_solved", 1)
        prog = "🧠 *Quiz Running...*\n\n" + "\n".join(lines[-15:])
        try:
            await msg.edit_text(prog[:4000], parse_mode="Markdown")
        except Exception:
            pass
    save_session_for(user.id, sess)
    final = "🏁 *QUIZ DONE*\n\n" + "\n".join(lines)
    await msg.edit_text(final[:4000], parse_mode="Markdown")
    bal = sess.get_balance()
    await log_to_channel(
        context.application,
        f"🧠 QUIZ: {user_display(user)}\n"
        f"Sessions: {sessions_done} | Balance: {bal}"
    )


async def my_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = STATS.data.get(str(user.id), {})
    ud = USERS.get(user.id)
    logged_in = "✅" if ud.get("minipix_token") else "❌"
    groq = "✅" if ud.get("groq_api_key") else "❌"
    text = (
        f"📊 *Your Usage Stats*\n\n"
        f"👤 User ID: `{user.id}`\n"
        f"🔐 MiniPix login: {logged_in}\n"
        f"🧠 Groq key: {groq}\n"
        f"📅 Joined: `{data.get('joined_at', 'N/A')[:19]}`\n"
        f"⏰ Last active: `{data.get('last_active', 'N/A')[:19]}`\n\n"
        f"🎬 Total episodes watched: *{data.get('total_watches', 0)}*\n"
        f"💰 Total coins earned: *+{data.get('total_coins_earned', 0)}*\n"
        f"🧠 Quizzes solved: *{data.get('quizzes_solved', 0)}*\n"
        f"📚 Series completed: *{data.get('series_done', 0)}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def admin_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    total_users = len(STATS.data)
    total_watches = sum(v.get("total_watches", 0) for v in STATS.data.values())
    total_coins = sum(v.get("total_coins_earned", 0) for v in STATS.data.values())
    total_quizzes = sum(v.get("quizzes_solved", 0) for v in STATS.data.values())
    active_users = sum(1 for v in STATS.data.values() if v.get("last_active"))
    lines = [
        f"📊 *GLOBAL STATS*\n",
        f"👥 Total users: {total_users}",
        f"🟢 Active (used): {active_users}",
        f"🎬 Total watches: {total_watches}",
        f"💰 Total coins: +{total_coins}",
        f"🧠 Total quizzes: {total_quizzes}",
        "",
        "👤 *Top 10 users (by watches):*",
    ]
    sorted_users = sorted(STATS.data.items(), key=lambda x: x[1].get("total_watches", 0), reverse=True)
    for i, (uid, d) in enumerate(sorted_users[:10], 1):
        w = d.get("total_watches", 0)
        c = d.get("total_coins_earned", 0)
        q = d.get("quizzes_solved", 0)
        lines.append(f"{i}. ID `{uid}`: 🎬{w} 💰+{c} 🧠{q}")
    await update.message.reply_text("\n".join(lines)[:4000], parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    txt = (update.message.text or "").strip()
    if txt.isdigit():
        if len(txt) == 10:
            context.args = [txt]
            await login_cmd(update, context)
            return
        elif len(txt) == 6:
            ud = USERS.get(user.id)
            if ud.get("otp_session"):
                context.args = [txt]
                await otp_cmd(update, context)
                return
    await update.message.reply_text(
        "❓ Command samajh nahi aaya!\n\n/help use karo saare commands dekhne ke liye."
    )


TG_APP = None
app = None

if FLASK_AVAILABLE:
    app = Flask(__name__)

    @app.route("/")
    def home():
        users = len(USERS.users)
        watches = sum(v.get("total_watches", 0) for v in STATS.data.values())
        coins = sum(v.get("total_coins_earned", 0) for v in STATS.data.values())
        quizzes = sum(v.get("quizzes_solved", 0) for v in STATS.data.values())
        return f"""
        <!DOCTYPE html>
        <html><head><title>MiniPix Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
        body {{font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: linear-gradient(135deg,#1a1a2e,#16213e); color:#fff; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0;}}
        .card {{background: rgba(255,255,255,0.05); padding: 2rem 2.5rem; border-radius: 16px; backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.1); text-align:center;}}
        h1 {{margin: 0 0 1rem; background: linear-gradient(90deg,#00c853,#29b6f6); -webkit-background-clip: text; -webkit-text-fill-color: transparent;}}
        .stats {{display: grid; grid-template-columns: repeat(2, 1fr); gap: 1rem; margin: 1rem 0;}}
        .stat {{background: rgba(0,200,83,0.08); padding: 1rem; border-radius: 10px; border: 1px solid rgba(0,200,83,0.2);}}
        .stat b {{display:block; font-size: 1.6rem; color: #00c853;}}
        .stat span {{font-size: .85rem; opacity: .7;}}
        .ok {{margin-top:1rem; padding:.6rem 1rem; background: rgba(0,200,83,0.15); border-radius:8px; display:inline-block; border:1px solid rgba(0,200,83,0.3);}}
        </style></head><body>
        <div class="card">
        <h1>🤖 MiniPix Bot</h1>
        <div class="stats">
            <div class="stat"><b>{users}</b><span>Total Users</span></div>
            <div class="stat"><b>{watches}</b><span>Episodes Watched</span></div>
            <div class="stat"><b>+{coins}</b><span>Coins Earned</span></div>
            <div class="stat"><b>{quizzes}</b><span>Quizzes Solved</span></div>
        </div>
        <span class="ok">✅ Bot is Running</span>
        </div></body></html>
        """

    @app.route("/health")
    def health():
        return jsonify({
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "bot_token_set": bool(BOT_CONFIG.bot_token),
            "log_channel_id": bool(BOT_CONFIG.log_channel_id),
            "total_users": len(USERS.users),
            "total_watches": sum(v.get("total_watches", 0) for v in STATS.data.values()),
        })


def build_tg_app():
    global TG_APP
    if not TELEGRAM_AVAILABLE:
        return None
    if not BOT_CONFIG.bot_token:
        return None
    application = ApplicationBuilder().token(BOT_CONFIG.bot_token).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("login", login_cmd))
    application.add_handler(CommandHandler("otp", otp_cmd))
    application.add_handler(CommandHandler("set_token", set_token_cmd))
    application.add_handler(CommandHandler("set_groq_key", set_groq_key_cmd))
    application.add_handler(CommandHandler("balance", balance_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("series_list", series_list_cmd))
    application.add_handler(CommandHandler("watch_all", watch_all_cmd))
    application.add_handler(CommandHandler("watch_one", watch_one_cmd))
    application.add_handler(CommandHandler("quiz", quiz_cmd))
    application.add_handler(CommandHandler("my_stats", my_stats_cmd))
    application.add_handler(CommandHandler("admin_stats", admin_stats_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    TG_APP = application
    return application


def _start_telegram_in_thread():
    application = build_tg_app()
    if not application:
        return
    import asyncio

    async def runner():
        webhook = BOT_CONFIG.webhook_url
        if webhook and BOT_CONFIG.port:
            print(f"[�] Webhook mode: {webhook} | Internal port: {BOT_CONFIG.port}")
            await application.bot.delete_webhook(drop_pending_updates=True)
            await application.bot.set_webhook(url=webhook)
            await application.start()
            await application.updater.start_polling(application.bot) if False else None
            await asyncio.Event().wait()
        else:
            print("[�🚀] Bot polling mode me start ho raha hai (thread)...")
            await application.initialize()
            await application.start()
            await application.updater.start_polling(application.bot, drop_pending_updates=True)
            await asyncio.Event().wait()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(runner())
    except Exception as e:
        print(f"[!] Telegram thread error: {e}")
    finally:
        try:
            USERS.save()
            STATS.save()
        except Exception:
            pass


def main():
    if not TELEGRAM_AVAILABLE:
        print("\n[🚫] python-telegram-bot install nahi mila!")
        print("    Install karo:\n       pip install python-telegram-bot==20.7")
        return
    if not BOT_CONFIG.bot_token:
        print("\n[🚫] Bot token nahi mila! Railway me Variables tab me BOT_TOKEN set karo ya config file daalo.")
        return
    print(f"[✅] Bot token loaded. Log channel: {BOT_CONFIG.log_channel_id or 'NOT SET'}")
    print(f"[✅] Users: {len(USERS.users)} | Stats entries: {len(STATS.data)}")
    print(f"[✅] Port: {BOT_CONFIG.port} | Webhook: {BOT_CONFIG.webhook_url or 'polling'}")

    if FLASK_AVAILABLE:
        t = threading.Thread(target=_start_telegram_in_thread, daemon=True)
        t.start()
        time.sleep(1.2)
        print(f"[🌐] Flask keep-alive server starting on port {BOT_CONFIG.port}...")
        try:
            app.run(host="0.0.0.0", port=BOT_CONFIG.port, debug=False, use_reloader=False, threaded=True)
        except KeyboardInterrupt:
            print("\n[👋] Server stop kiya gaya.")
        except Exception as e:
            print(f"[!] Flask error: {e}")
            print("[🚀] Falling back to direct polling...")
            try:
                USERS.save()
                STATS.save()
            except Exception:
                pass
            _app = build_tg_app()
            if _app:
                _app.run_polling()
    else:
        print("[🚀] Flask nahi mila — direct polling mode start...")
        application = build_tg_app()
        if application:
            try:
                application.run_polling()
            finally:
                try:
                    USERS.save()
                    STATS.save()
                except Exception:
                    pass


if __name__ == "__main__":
    main()

