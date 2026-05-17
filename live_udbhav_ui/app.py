import json
import os
import time
import socket
import subprocess
import shutil
from datetime import datetime, timedelta
from functools import wraps
from urllib.request import Request, urlopen

import feedparser
from flask import Flask, request, redirect, render_template, url_for, session, send_from_directory
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from google.oauth2 import id_token as google_id_token
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["PREFERRED_URL_SCHEME"] = "https"
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
USERS_PATH = os.getenv("USERS_PATH", os.path.join(DATA_DIR, "users.txt"))
UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(DATA_DIR, "uploads"))
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
os.makedirs(UPLOAD_DIR, exist_ok=True)
BACKUP_MARKER = os.getenv("BACKUP_MARKER", os.path.join(DATA_DIR, "last_backup.txt"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")
GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
]

FEED_SOURCES = {
    "US Stocks": [
        {"name": "Nasdaq Markets", "url": "https://www.nasdaq.com/feed/rssoutbound?category=Markets"},
        {"name": "Investing.com Stock Market", "url": "https://www.investing.com/rss/news_25.rss"},
    ],
    "India Markets": [
        {"name": "Economic Times Markets", "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
        {"name": "Economic Times Stocks", "url": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"},
    ],
    "Crypto": [
        {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
        {"name": "Investing.com Crypto", "url": "https://www.investing.com/rss/news_301.rss"},
    ],
    "Gold & Silver": [
        {"name": "Investing.com Commodities", "url": "https://www.investing.com/rss/news_11.rss"},
        {"name": "Economic Times Commodities", "url": "https://economictimes.indiatimes.com/markets/commodities/rssfeeds/1808152121.cms"},
    ],
}

BTC_NEWS_SOURCES = [
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/.rss/full/"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
]

BTC_COMMUNITY_SOURCES = [
    {"name": "Reddit r/Bitcoin", "url": "https://www.reddit.com/r/Bitcoin/.rss"},
]

CACHE_TTL_SECONDS = 600
VAULT_CACHE_TTL_SECONDS = 60
FETCH_TIMEOUT = 6
_feed_cache = {}
_api_cache = {}

# Hyperliquid vaults
HYPERLIQUID_API_URL = os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz")

def _cache_get(key, ttl):
    entry = _api_cache.get(key)
    if not entry:
        return None
    now = int(time.time())
    if now - entry["ts"] > ttl:
        return None
    return entry["data"]

def _cache_set(key, data):
    _api_cache[key] = {"ts": int(time.time()), "data": data}

def _fetch_hl_info(payload, ttl=CACHE_TTL_SECONDS):
    cache_key = f"hl:{json.dumps(payload, sort_keys=True)}"
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached
    try:
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            f"{HYPERLIQUID_API_URL}/info",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
        _cache_set(cache_key, parsed)
        return parsed
    except Exception:
        return {}

def _fetch_vault_details(vault_address, user):
    payload = {"type": "vaultDetails", "vaultAddress": vault_address, "user": user}
    return _fetch_hl_info(payload, ttl=VAULT_CACHE_TTL_SECONDS) or {}

def _to_float(value):
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None

def _extract_vault_list(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ["vaultEquities", "vaults", "data", "equities", "vaultEquityPositions"]:
            if key in raw and isinstance(raw[key], list):
                return raw[key]
        for val in raw.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val
    return []

def _extract_portfolio_history(details):
    portfolio = details.get("portfolio") or []
    preferred = ["month", "week", "day"]
    by_label = {}
    for item in portfolio:
        if isinstance(item, list) and len(item) == 2:
            label, payload = item
            by_label[label] = payload
    payload = None
    for label in preferred:
        if label in by_label:
            payload = by_label[label]
            break
    if payload is None and portfolio:
        payload = portfolio[0][1] if isinstance(portfolio[0], list) and len(portfolio[0]) == 2 else None
    if not payload:
        return []
    history = payload.get("accountValueHistory") or []
    series = []
    for point in history:
        if isinstance(point, list) and len(point) >= 2:
            ts = int(point[0])
            val = _to_float(point[1])
            if val is not None:
                series.append((ts, val))
    return series

def _linear_series(start_ts, start_val, end_ts, end_val, points=30):
    if end_ts <= start_ts or points < 2:
        return [(end_ts, end_val)]
    series = []
    for i in range(points):
        ratio = i / (points - 1)
        ts = int(start_ts + (end_ts - start_ts) * ratio)
        val = start_val + (end_val - start_val) * ratio
        series.append((ts, val))
    return series


def _normalize_vaults(raw, user):
    vaults = []
    now_ts = int(time.time() * 1000)
    for idx, entry in enumerate(_extract_vault_list(raw)):
        if not isinstance(entry, dict):
            continue
        vault_id = entry.get("vaultAddress") or entry.get("address") or entry.get("vault") or entry.get("id") or f"vault-{idx}"
        equity = (
            _to_float(entry.get("equity"))
            or _to_float(entry.get("value"))
            or _to_float(entry.get("balance"))
            or _to_float(entry.get("currentValue"))
            or _to_float(entry.get("usdValue"))
            or 0.0
        )
        details = _fetch_vault_details(vault_id, user)
        follower = details.get("followerState") or {}
        name = details.get("name") or entry.get("name") or entry.get("vaultName") or entry.get("title") or vault_id
        apr = _to_float(details.get("apr"))
        if apr is not None and apr <= 1.5:
            apr *= 100
        user_equity = _to_float(follower.get("vaultEquity")) or equity
        pnl = _to_float(follower.get("pnl"))
        all_time_pnl = _to_float(follower.get("allTimePnl"))
        if pnl is None and all_time_pnl is not None:
            pnl = all_time_pnl
        days_following = follower.get("daysFollowing")
        lockup_until = follower.get("lockupUntil")

        entry_ts = follower.get("vaultEntryTime")
        if not entry_ts and days_following:
            entry_ts = now_ts - int(days_following) * 24 * 3600 * 1000
        if not entry_ts:
            entry_ts = now_ts - 7 * 24 * 3600 * 1000

        initial_equity = user_equity - (all_time_pnl if all_time_pnl is not None else (pnl or 0.0))
        if initial_equity < 0:
            initial_equity = 0.0
        points = 30
        if days_following:
            points = max(2, min(int(days_following), 90))
        user_series = _linear_series(entry_ts, initial_equity, now_ts, user_equity, points)
        vault_aum_current = None
        vaults.append(
            {
                "id": vault_id,
                "name": name,
                "equity": user_equity,
                "pnl": pnl,
                "all_time_pnl": all_time_pnl,
                "apr": apr,
                "days_following": days_following,
                "lockup_until": lockup_until,
                "vault_aum": vault_aum_current,
                "series": [val for _, val in user_series],
                "series_with_time": user_series,
            }
        )
    return vaults


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _build_bot_stats(entries):
    bots = {}
    daily_totals = {}

    for entry in entries:
        bot = (entry.get("strategy") or "Unassigned").strip() or "Unassigned"
        pnl = _to_float(entry.get("pnl")) or 0.0
        date = _parse_date(entry.get("date", ""))

        stats = bots.setdefault(
            bot,
            {
                "name": bot,
                "total_pnl": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "wins": 0,
                "losses": 0,
                "trades": 0,
                "daily": {},
            },
        )

        stats["trades"] += 1
        stats["total_pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1
            stats["gross_profit"] += pnl
        elif pnl < 0:
            stats["losses"] += 1
            stats["gross_loss"] += abs(pnl)

        if date:
            key = date.isoformat()
            stats["daily"][key] = stats["daily"].get(key, 0.0) + pnl
            daily_totals[key] = daily_totals.get(key, 0.0) + pnl

    bot_list = []
    for stats in bots.values():
        sorted_days = sorted(stats["daily"].keys())
        cumulative = []
        running = 0.0
        for day in sorted_days:
            running += stats["daily"][day]
            cumulative.append([day, round(running, 2)])
        profit_factor = (
            round(stats["gross_profit"] / stats["gross_loss"], 2)
            if stats["gross_loss"]
            else round(stats["gross_profit"], 2)
        )
        win_rate = round((stats["wins"] / stats["trades"]) * 100, 2) if stats["trades"] else 0.0
        bot_list.append(
            {
                "name": stats["name"],
                "total_pnl": round(stats["total_pnl"], 2),
                "win_rate": win_rate,
                "trades": stats["trades"],
                "profit_factor": profit_factor,
                "series": [point[1] for point in cumulative],
                "series_with_time": cumulative,
            }
        )

    total_series = []
    running = 0.0
    for day in sorted(daily_totals.keys()):
        running += daily_totals[day]
        total_series.append([day, round(running, 2)])

    bot_list.sort(key=lambda item: item["total_pnl"], reverse=True)
    return bot_list, total_series


def _load_users():
    users = {}
    if not os.path.exists(USERS_PATH):
        return users
    with open(USERS_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if "\t" in line:
                username, password = line.split("\t", 1)
            elif ":" in line:
                username, password = line.split(":", 1)
            else:
                continue
            users[username] = password
    return users


def _save_user(username, password):
    with open(USERS_PATH, "a", encoding="utf-8") as handle:
        handle.write(f"{username}\t{password}\n")


def _clean_username(raw):
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _format_time(entry):
    published = entry.get("published") or entry.get("updated") or ""
    if published:
        return published
    return ""


def _fetch_feed(url):
    try:
        socket.setdefaulttimeout(FETCH_TIMEOUT)
        req = Request(url, headers={"User-Agent": "UdbhavNewsBot/1.0"})
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.read()
    except Exception:
        return None


def _fetch_json(url, cache_key, ttl=60):
    now = time.time()
    cached = _api_cache.get(cache_key)
    if cached and now - cached["ts"] < ttl:
        return cached["data"]
    try:
        socket.setdefaulttimeout(FETCH_TIMEOUT)
        req = Request(url, headers={"User-Agent": "UdbhavMarketBot/1.0"})
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        _api_cache[cache_key] = {"ts": now, "data": payload}
        return payload
    except Exception:
        return None


def _fetch_feed_items(url, source_name):
    now = time.time()
    cached = _feed_cache.get(url)
    if cached and now - cached["ts"] < CACHE_TTL_SECONDS:
        return cached["items"]

    data = _fetch_feed(url)
    if not data:
        items = []
        _feed_cache[url] = {"ts": now, "items": items}
        return items

    feed = feedparser.parse(data)
    items = []
    for entry in feed.entries[:12]:
        items.append(
            {
                "title": entry.get("title", "Untitled"),
                "link": entry.get("link", "#"),
                "published": _format_time(entry),
                "source": source_name,
            }
        )

    _feed_cache[url] = {"ts": now, "items": items}
    return items


def _build_news():
    news = {}
    for category, sources in FEED_SOURCES.items():
        merged = []
        seen = set()
        for source in sources:
            for item in _fetch_feed_items(source["url"], source["name"]):
                if item["link"] in seen:
                    continue
                seen.add(item["link"])
                merged.append(item)
        news[category] = merged[:15]
    return news


def _build_btc_news():
    keywords = ("bitcoin", "btc", "btcusd", "btcusdt", "satoshi")
    merged = []
    seen = set()
    for source in BTC_NEWS_SOURCES:
        for item in _fetch_feed_items(source["url"], source["name"]):
            title = (item.get("title") or "").lower()
            if keywords and not any(keyword in title for keyword in keywords):
                continue
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            merged.append(item)
    return merged[:12]


def _build_btc_community():
    merged = []
    seen = set()
    for source in BTC_COMMUNITY_SOURCES:
        for item in _fetch_feed_items(source["url"], source["name"]):
            if item["link"] in seen:
                continue
            seen.add(item["link"])
            merged.append(item)
    return merged[:8]


def _latest_news_items(limit=8):
    cache_key = "latest_news"
    now = time.time()
    cached = _api_cache.get(cache_key)
    if cached and now - cached["ts"] < CACHE_TTL_SECONDS:
        return cached["data"]

    merged = []
    seen = set()
    for category, sources in FEED_SOURCES.items():
        for source in sources:
            for item in _fetch_feed_items(source["url"], source["name"]):
                link = item.get("link")
                if not link or link in seen:
                    continue
                seen.add(link)
                merged.append(item)
                if len(merged) >= limit:
                    break
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break

    _api_cache[cache_key] = {"ts": now, "data": merged}
    return merged


def _user_file(username, suffix):
    safe = username.replace("/", "_")
    return os.path.join(DATA_DIR, f"{safe}_{suffix}.json")


def _user_settings(username):
    return _user_file(username, "settings")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _save_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _allowed_image(filename):
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED_IMAGE_EXTENSIONS


def _save_diary_photo(file_storage):
    if not file_storage or not file_storage.filename:
        return ""
    filename = secure_filename(file_storage.filename)
    if not filename or not _allowed_image(filename):
        return ""
    stamped = f"{int(time.time())}_{filename}"
    dest = os.path.join(UPLOAD_DIR, stamped)
    file_storage.save(dest)
    return stamped


def _read_last_backup():
    try:
        with open(BACKUP_MARKER, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return ""


def _systemctl_active_since(service_name):
    try:
        output = subprocess.check_output(
            ["systemctl", "show", "-p", "ActiveEnterTimestamp", service_name],
            text=True,
            timeout=3,
        )
        _, value = output.strip().split("=", 1)
        return value.strip()
    except Exception:
        return ""


def _machine_stats():
    stats = {}
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as handle:
            parts = handle.read().strip().split()
            stats["load_1m"] = parts[0]
            stats["load_5m"] = parts[1]
            stats["load_15m"] = parts[2]
    except Exception:
        stats["load_1m"] = stats["load_5m"] = stats["load_15m"] = "n/a"

    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            meminfo = handle.read().splitlines()
        mem_total = next((line for line in meminfo if line.startswith("MemTotal:")), "")
        mem_avail = next((line for line in meminfo if line.startswith("MemAvailable:")), "")
        total_kb = int(mem_total.split()[1]) if mem_total else 0
        avail_kb = int(mem_avail.split()[1]) if mem_avail else 0
        stats["mem_total_mb"] = round(total_kb / 1024) if total_kb else 0
        stats["mem_available_mb"] = round(avail_kb / 1024) if avail_kb else 0
    except Exception:
        stats["mem_total_mb"] = stats["mem_available_mb"] = 0

    try:
        usage = shutil.disk_usage("/")
        stats["disk_total_gb"] = round(usage.total / (1024**3), 1)
        stats["disk_free_gb"] = round(usage.free / (1024**3), 1)
        stats["disk_used_pct"] = round(usage.used / usage.total * 100, 1) if usage.total else 0
    except Exception:
        stats["disk_total_gb"] = stats["disk_free_gb"] = stats["disk_used_pct"] = 0

    try:
        uptime = subprocess.check_output(["uptime", "-p"], text=True, timeout=2).strip()
    except Exception:
        uptime = "n/a"
    stats["uptime"] = uptime
    return stats


def _google_flow():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI):
        return None
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_REDIRECT_URI],
            }
        },
        scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )


def _google_token_path(username):
    return _user_file(username, "google_token")


def _load_google_credentials(username):
    path = _google_token_path(username)
    if not os.path.exists(path):
        return None
    data = _load_json(path, {})
    if not data.get("token"):
        return None
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GOOGLE_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            _save_json(
                path,
                {
                    "token": creds.token,
                    "refresh_token": creds.refresh_token,
                },
            )
        except Exception:
            return None
    return creds


def _save_google_credentials(username, creds):
    path = _google_token_path(username)
    _save_json(
        path,
        {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
        },
    )


def _new_id():
    return str(int(time.time() * 1000))


def _get_fear_greed():
    url = "https://api.alternative.me/fng/?limit=1&format=json"
    data = _fetch_json(url, "fear_greed", ttl=300)
    if not data:
        return None
    values = data.get("data") or []
    return values[0] if values else None


def _get_coingecko_bitcoin():
    url = (
        "https://api.coingecko.com/api/v3/coins/bitcoin?"
        "localization=false&tickers=false&community_data=false&developer_data=false&sparkline=true"
    )
    return _fetch_json(url, "cg_btc", ttl=120)


def _get_coingecko_chart(days=7):
    url = f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days}&interval=hourly"
    return _fetch_json(url, f"cg_btc_chart_{days}", ttl=120)


def _get_coingecko_heatmap():
    url = (
        "https://api.coingecko.com/api/v3/coins/markets?"
        "vs_currency=usd&order=market_cap_desc&per_page=50&page=1&sparkline=false&price_change_percentage=24h"
    )
    data = _fetch_json(url, "cg_heatmap", ttl=180)
    return data if isinstance(data, list) else []


def _get_coingecko_global():
    url = "https://api.coingecko.com/api/v3/global"
    return _fetch_json(url, "cg_global", ttl=300)


def _get_coinbase_stats():
    url = "https://api.exchange.coinbase.com/products/BTC-USD/stats"
    return _fetch_json(url, "cb_stats", ttl=60)


def _get_coinbase_orderbook():
    url = "https://api.exchange.coinbase.com/products/BTC-USD/book?level=2"
    return _fetch_json(url, "cb_orderbook", ttl=30)


def _get_daily_inspiration():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    quote_data = _fetch_json(
        "https://api.quotable.io/random",
        f"daily_quote_{today}",
        ttl=86400,
    ) or {}
    joke_data = _fetch_json(
        "https://v2.jokeapi.dev/joke/Any?type=single",
        f"daily_joke_{today}",
        ttl=86400,
    ) or {}

    quote = quote_data.get("content") or "Stay curious, stay kind."
    author = quote_data.get("author") or "Udbhav.uk"

    joke = joke_data.get("joke")
    if not joke and joke_data.get("type") == "twopart":
        setup = joke_data.get("setup", "").strip()
        delivery = joke_data.get("delivery", "").strip()
        joke = f"{setup} {delivery}".strip()
    if not joke:
        joke = "Time flies like an arrow; fruit flies like a banana."

    photo_url = f"https://picsum.photos/seed/{today}/900/600"

    return {
        "quote": quote,
        "author": author,
        "joke": joke,
        "photo_url": photo_url,
    }


def _hyperliquid_user_fills(address):
    if not address:
        return []
    payload = {"type": "userFills", "user": address, "aggregateByTime": True}
    try:
        req = Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "UdbhavJournal/1.0"},
        )
        with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _merge_hyperliquid_fills(entries, fills):
    existing_ids = {entry.get("id") for entry in entries}
    new_entries = []
    for fill in fills:
        fill_id = f"{fill.get('hash', '')}:{fill.get('tid', '')}"
        if fill_id in existing_ids:
            continue
        new_entries.append(
            {
                "id": fill_id,
                "date": datetime.utcfromtimestamp(fill.get("time", 0) / 1000).strftime("%Y-%m-%d"),
                "symbol": fill.get("coin", ""),
                "side": fill.get("dir", ""),
                "qty": fill.get("sz", ""),
                "entry": fill.get("px", ""),
                "exit": "",
                "pnl": fill.get("closedPnl", ""),
                "result": "",
                "strategy": "Hyperliquid",
                "notes": "Imported from Hyperliquid",
                "tags": "hyperliquid",
                "screenshot": "",
                "source": "hyperliquid",
            }
        )
    if new_entries:
        entries = new_entries + entries
    return entries


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


@app.context_processor
def inject_latest_news():
    return {"latest_news": _latest_news_items()}


@app.route("/account/markets-layout", methods=["GET", "POST"])
def account_markets_layout():
    username = session.get("user")
    if not username:
        return {"authenticated": False, "error": "Login required"}, 401

    settings_path = _user_settings(username)
    settings = _load_json(settings_path, {})

    if request.method == "GET":
        return {
            "authenticated": True,
            "layout": settings.get("markets_layout"),
            "saved_at": settings.get("markets_layout_saved_at"),
        }

    payload = request.get_json(silent=True) or {}
    layout = payload.get("layout")
    if not isinstance(layout, dict):
        return {"authenticated": True, "error": "Invalid layout payload"}, 400

    panels = layout.get("panels")
    panel_order = layout.get("panelOrder")
    spans = layout.get("spans")
    active_layout_id = layout.get("activeLayoutId")

    if panels is not None and not isinstance(panels, list):
        return {"authenticated": True, "error": "Invalid panels payload"}, 400
    if panel_order is not None and not isinstance(panel_order, list):
        return {"authenticated": True, "error": "Invalid panel order payload"}, 400
    if spans is not None and not isinstance(spans, dict):
        return {"authenticated": True, "error": "Invalid spans payload"}, 400
    if active_layout_id is not None and not isinstance(active_layout_id, str):
        return {"authenticated": True, "error": "Invalid active layout id"}, 400

    sanitized_layout = {
        "activeLayoutId": active_layout_id or None,
        "panels": panels or [],
        "panelOrder": panel_order or [],
        "spans": spans or {},
    }
    saved_at = datetime.utcnow().isoformat() + "Z"
    settings["markets_layout"] = sanitized_layout
    settings["markets_layout_saved_at"] = saved_at
    _save_json(settings_path, settings)
    return {"authenticated": True, "ok": True, "saved_at": saved_at}


@app.route("/uploads/<path:filename>")
@login_required
def uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/health.json", methods=["GET"])
@login_required
def health_json():
    stats = _machine_stats()
    last_backup = _read_last_backup()
    last_restart = _systemctl_active_since("udbhav-ui")

    healthy = True
    try:
        if stats.get("mem_available_mb", 0) < 100:
            healthy = False
        if stats.get("disk_free_gb", 0) < 2:
            healthy = False
        if float(stats.get("load_1m", "0") or 0) > 3:
            healthy = False
    except Exception:
        healthy = False

    return {
        "healthy": healthy,
        "stats": stats,
        "last_backup": last_backup,
        "last_restart": last_restart,
        "checked_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


@app.route("/google/login", methods=["GET"])
def google_login():
    flow = _google_flow()
    if not flow:
        return redirect(url_for("login", error="Google login is not configured."))
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["google_oauth_state"] = state
    session["google_oauth_purpose"] = "login"
    return redirect(authorization_url)


@app.route("/google/callback", methods=["GET"])
def google_callback():
    flow = _google_flow()
    if not flow:
        return redirect(url_for("diary"))
    state = session.get("google_oauth_state", "")
    if state:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        purpose = session.get("google_oauth_purpose", "login")
        if purpose == "login":
            try:
                token_info = google_id_token.verify_oauth2_token(
                    creds.id_token, GoogleRequest(), GOOGLE_CLIENT_ID
                )
                email = (token_info.get("email") or "").lower()
            except Exception:
                email = ""
            if not email:
                return redirect(url_for("login", error="Google login failed."))
            session["user"] = email
            _save_google_credentials(email, creds)
            return redirect(url_for("dashboard"))

        _save_google_credentials(session.get("user"), creds)
    return redirect(url_for("diary"))


@app.route("/", methods=["GET"])
def root():
    return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template(
            "auth.html",
            title="Welcome to Udbhav.uk",
            subtitle="Sign in with Google, use a guest session, or log in with your existing account.",
            message=request.args.get("msg", ""),
            error=request.args.get("error", ""),
        )

    action = request.form.get("action")
    if action == "guest":
        session["user"] = "guest"
        return redirect(url_for("dashboard"))

    if action == "legacy":
        username = _clean_username(request.form.get("username", ""))
        password = request.form.get("password", "").strip()
        if not username or not password:
            return redirect(url_for("login", error="Username and password are required."))
        users = _load_users()
        if users.get(username) != password:
            return redirect(url_for("login", error="Invalid username or password."))
        session["user"] = username
        return redirect(url_for("dashboard"))

    if action == "email":
        email = (request.form.get("email") or "").strip().lower()
        if not email or "@" not in email or "." not in email:
            return redirect(url_for("login", error="Enter a valid email address."))
        return redirect(
            url_for(
                "login",
                msg="Email verification is not enabled yet. We can turn on OTP once email sending is configured.",
            )
        )

    return redirect(url_for("login", error="Please sign in with Google, use guest, or log in with an account."))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    news = _build_news()
    username = session.get("user")
    diary_path = _user_file(username, "diary")
    diary_entries = _load_json(diary_path, [])
    journal_path = _user_file(username, "journal")
    journal_entries = _load_json(journal_path, [])
    links_path = _user_file(username, "links")
    link_entries = _load_json(links_path, [])
    moods = {}
    happiness_vals = []
    recent_entries = []
    for entry in diary_entries:
        if entry.get("mood"):
            moods[entry["mood"]] = moods.get(entry["mood"], 0) + 1
        try:
            happiness_vals.append(float(entry.get("happiness", 0) or 0))
        except Exception:
            pass
        recent_entries.append(entry)
    dominant_mood = max(moods.items(), key=lambda pair: pair[1])[0] if moods else "Neutral"
    avg_happiness = round(sum(happiness_vals) / len(happiness_vals), 2) if happiness_vals else 0
    latest_entry = recent_entries[0] if recent_entries else {}

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today_entry = next((entry for entry in diary_entries if entry.get("date") == today_str), None)

    last_trade = journal_entries[0] if journal_entries else {}
    last_link = link_entries[0] if link_entries else {}

    next_event = {}

    stats = _machine_stats()
    last_backup = _read_last_backup()
    last_restart = _systemctl_active_since("udbhav-ui")
    healthy = True
    try:
        if stats.get("mem_available_mb", 0) < 100:
            healthy = False
        if stats.get("disk_free_gb", 0) < 2:
            healthy = False
        if float(stats.get("load_1m", "0") or 0) > 3:
            healthy = False
    except Exception:
        healthy = False
    return render_template(
        "dashboard.html",
        username=username,
        news=news,
        dominant_mood=dominant_mood,
        avg_happiness=avg_happiness,
        latest_headline=latest_entry.get("headline", ""),
        latest_mood=latest_entry.get("mood", ""),
        today_entry=today_entry,
        next_event=next_event,
        last_trade=last_trade,
        last_link=last_link,
        health={
            "healthy": healthy,
            "stats": stats,
            "last_backup": last_backup,
            "last_restart": last_restart,
        },
        generated=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.route("/bitcoin", methods=["GET"])
@login_required
def bitcoin():
    username = session.get("user")
    fear_greed = _get_fear_greed()
    btc = _get_coingecko_bitcoin() or {}
    chart = _get_coingecko_chart(7) or {}
    heatmap = _get_coingecko_heatmap()
    global_data = _get_coingecko_global() or {}
    coinbase_stats = _get_coinbase_stats() or {}
    orderbook = _get_coinbase_orderbook() or {}
    btc_news = _build_btc_news()
    btc_community = _build_btc_community()

    prices = chart.get("prices") or []
    chart_labels = [datetime.utcfromtimestamp(p[0] / 1000).strftime("%m-%d %H:%M") for p in prices]
    chart_values = [p[1] for p in prices]

    market = btc.get("market_data", {}) if btc else {}
    global_market = global_data.get("data", {}) if global_data else {}
    if not chart_values:
        spark = market.get("sparkline_7d", {}).get("price", []) if market else []
        if spark:
            chart_values = spark
            chart_labels = [f"Point {idx + 1}" for idx in range(len(spark))]

    bids = orderbook.get("bids") or []
    asks = orderbook.get("asks") or []
    top_bids = [{"price": b[0], "size": b[1]} for b in bids[:10]] if bids else []
    top_asks = [{"price": a[0], "size": a[1]} for a in asks[:10]] if asks else []

    price_now = market.get("current_price", {}).get("usd") if market else None
    change_24h = market.get("price_change_percentage_24h") if market else None
    change_7d = market.get("price_change_percentage_7d") if market else None
    change_1h = None
    if chart_values and len(chart_values) > 1:
        try:
            change_1h = ((chart_values[-1] - chart_values[-2]) / chart_values[-2]) * 100
        except Exception:
            change_1h = None

    key_high = market.get("high_24h", {}).get("usd") if market else None
    key_low = market.get("low_24h", {}).get("usd") if market else None
    if chart_values:
        key_high = max(chart_values) if key_high is None else key_high
        key_low = min(chart_values) if key_low is None else key_low

    market_pulse = {
        "price": price_now,
        "volume_24h": market.get("total_volume", {}).get("usd") if market else None,
        "market_cap": market.get("market_cap", {}).get("usd") if market else None,
        "change_1h": change_1h,
        "change_24h": change_24h,
        "change_7d": change_7d,
        "high": key_high,
        "low": key_low,
    }

    return render_template(
        "bitcoin.html",
        username=username,
        fear_greed=fear_greed,
        btc=btc,
        market=market,
        global_market=global_market,
        coinbase_stats=coinbase_stats,
        top_bids=top_bids,
        top_asks=top_asks,
        btc_news=btc_news,
        btc_community=btc_community,
        chart_labels=chart_labels,
        chart_values=chart_values,
        heatmap=heatmap,
        market_pulse=market_pulse,
        generated=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


@app.route("/notes", methods=["GET"])
def notes_redirect():
    return redirect(url_for("strategies"))


@app.route("/strategies", methods=["GET", "POST"])
@login_required
def strategies():
    username = session.get("user")
    path = _user_file(username, "strategies")
    notes_data = _load_json(path, {"notes": []})
    journal_path = _user_file(username, "journal")
    journal_entries = _load_json(journal_path, [])

    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "delete":
            note_id = request.form.get("id", "")
            notes_data["notes"] = [note for note in notes_data["notes"] if note.get("id") != note_id]
            _save_json(path, notes_data)
            return redirect(url_for("strategies"))

        if action == "update":
            note_id = request.form.get("id", "")
            for note in notes_data["notes"]:
                if note.get("id") == note_id:
                    note["title"] = request.form.get("title", "")
                    note["body"] = request.form.get("body", "")
                    note["status"] = request.form.get("status", note.get("status", "Backtesting"))
                    note["progress"] = request.form.get("progress", note.get("progress", "0"))
                    note["win_rate"] = request.form.get("win_rate", note.get("win_rate", ""))
                    note["avg_r"] = request.form.get("avg_r", note.get("avg_r", ""))
                    note["max_dd"] = request.form.get("max_dd", note.get("max_dd", ""))
                    note["updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                    break
            _save_json(path, notes_data)
            return redirect(url_for("strategies"))

        note = {
            "id": _new_id(),
            "title": request.form.get("title", ""),
            "body": request.form.get("body", ""),
            "status": request.form.get("status", "Backtesting"),
            "progress": request.form.get("progress", "0"),
            "win_rate": request.form.get("win_rate", ""),
            "avg_r": request.form.get("avg_r", ""),
            "max_dd": request.form.get("max_dd", ""),
            "created": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "updated": "",
        }
        if note["title"] or note["body"]:
            notes_data["notes"].insert(0, note)
            _save_json(path, notes_data)
        return redirect(url_for("strategies"))

    strategy_stats = {}
    for entry in journal_entries:
        strategy = (entry.get("strategy") or "").strip()
        if not strategy:
            continue
        key = strategy.lower()
        strategy_stats.setdefault(key, []).append(entry)

    def _pnl_value(item):
        try:
            return float(item.get("pnl", 0) or 0)
        except Exception:
            return 0.0

    def _sorted_entries(items):
        with_dates = []
        for idx, item in enumerate(items):
            date_str = item.get("date") or ""
            try:
                parsed = datetime.strptime(date_str, "%Y-%m-%d")
            except Exception:
                parsed = datetime.min
            with_dates.append((parsed, idx, item))
        with_dates.sort(key=lambda pair: (pair[0], pair[1]))
        return [item for _, _, item in with_dates]

    updated_stats = False
    for note in notes_data.get("notes", []):
        title = (note.get("title") or "").strip()
        if not title:
            continue
        stats_entries = strategy_stats.get(title.lower())
        if not stats_entries:
            continue
        ordered = _sorted_entries(stats_entries)
        pnl_values = [_pnl_value(item) for item in ordered]
        total = len(pnl_values)
        total_pnl = sum(pnl_values)
        wins = sum(1 for val in pnl_values if val > 0)
        win_rate = f"{(wins / total * 100):.1f}%" if total else "0%"
        avg_r = round((total_pnl / total) / 100, 2) if total else 0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for pnl in pnl_values:
            cumulative += pnl
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        progress_pct = round((total_pnl / 100) * 100, 2)
        progress_bar = max(0.0, min(100.0, progress_pct))

        note["win_rate"] = win_rate
        note["avg_r"] = f"{avg_r:.2f}"
        note["max_dd"] = f"{max_dd:.2f}"
        note["progress"] = f"{progress_pct:.2f}"
        note["progress_bar"] = f"{progress_bar:.2f}"
        note["updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        updated_stats = True

    if updated_stats:
        _save_json(path, notes_data)

    return render_template("strategies.html", username=username, notes=notes_data["notes"])


@app.route("/diary", methods=["GET", "POST"])
@login_required
def diary():
    username = session.get("user")
    path = _user_file(username, "diary")
    entries = _load_json(path, [])

    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "delete":
            entry_id = request.form.get("id", "")
            entries = [entry for entry in entries if entry.get("id") != entry_id]
            _save_json(path, entries)
            return redirect(url_for("diary"))
        if action == "update":
            entry_id = request.form.get("id", "")
            photo_name = _save_diary_photo(request.files.get("photo"))
            for entry in entries:
                if entry.get("id") == entry_id:
                    entry["date"] = request.form.get("date", entry.get("date", ""))
                    entry["mood"] = request.form.get("mood", entry.get("mood", ""))
                    entry["happiness"] = request.form.get("happiness", entry.get("happiness", ""))
                    entry["headline"] = request.form.get("headline", entry.get("headline", ""))
                    entry["gratitude"] = request.form.get("gratitude", entry.get("gratitude", ""))
                    entry["win_one"] = request.form.get("win_one", entry.get("win_one", ""))
                    entry["win_two"] = request.form.get("win_two", entry.get("win_two", ""))
                    entry["win_three"] = request.form.get("win_three", entry.get("win_three", ""))
                    entry["notes"] = request.form.get("notes", entry.get("notes", ""))
                    if photo_name:
                        entry["photo"] = photo_name
                    entry["updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                    break
            _save_json(path, entries)
            return redirect(url_for("diary"))

        photo_name = _save_diary_photo(request.files.get("photo"))
        entry = {
            "id": _new_id(),
            "date": request.form.get("date", ""),
            "mood": request.form.get("mood", ""),
            "happiness": request.form.get("happiness", ""),
            "headline": request.form.get("headline", ""),
            "gratitude": request.form.get("gratitude", ""),
            "win_one": request.form.get("win_one", ""),
            "win_two": request.form.get("win_two", ""),
            "win_three": request.form.get("win_three", ""),
            "notes": request.form.get("notes", ""),
            "photo": photo_name,
            "created": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }
        entries.insert(0, entry)
        _save_json(path, entries)

        return redirect(url_for("diary"))

    happiness_dates = []
    happiness_values = []
    mood_map = {}
    entries_by_date = {}
    parsed_entries = []
    for entry in entries:
        date = entry.get("date") or ""
        mood_map[date] = entry.get("mood", "")
        if date and date not in entries_by_date:
            if entry.get("photo"):
                entry["photo_url"] = url_for("uploads", filename=entry.get("photo"))
            entries_by_date[date] = entry
        happiness_dates.append(date)
        try:
            happiness = float(entry.get("happiness", 0) or 0)
        except Exception:
            happiness = 0
        happiness_values.append(happiness)
        try:
            parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            parsed_date = None
        if parsed_date:
            parsed_entries.append({"date": parsed_date, "mood": entry.get("mood", ""), "happiness": happiness})

    parsed_entries.sort(key=lambda item: item["date"])
    unique_dates = []
    date_map = {}
    for item in parsed_entries:
        if item["date"] not in date_map:
            unique_dates.append(item["date"])
            date_map[item["date"]] = item

    current_streak = 0
    longest_streak = 0
    if unique_dates:
        latest = unique_dates[-1]
        streak = 1
        for idx in range(len(unique_dates) - 2, -1, -1):
            if (latest - unique_dates[idx]).days == streak:
                streak += 1
            else:
                break
        current_streak = streak
        streak = 1
        for idx in range(1, len(unique_dates)):
            if (unique_dates[idx] - unique_dates[idx - 1]).days == 1:
                streak += 1
            else:
                longest_streak = max(longest_streak, streak)
                streak = 1
        longest_streak = max(longest_streak, streak)

    best_week_avg = 0
    best_week_start = ""
    for start_idx, start_date in enumerate(unique_dates):
        window_end = start_date + timedelta(days=6)
        window_entries = [item for item in parsed_entries if start_date <= item["date"] <= window_end]
        if len(window_entries) < 4:
            continue
        avg = sum(item["happiness"] for item in window_entries) / len(window_entries)
        if avg > best_week_avg:
            best_week_avg = avg
            best_week_start = start_date.strftime("%Y-%m-%d")

    monthly_summary = []
    month_groups = {}
    for item in parsed_entries:
        month_key = item["date"].strftime("%Y-%m")
        month_groups.setdefault(month_key, []).append(item)

    for month_key, group in sorted(month_groups.items()):
        avg = sum(item["happiness"] for item in group) / len(group)
        moods = {}
        for item in group:
            mood = item["mood"] or "Neutral"
            moods[mood] = moods.get(mood, 0) + 1
        dominant_mood = max(moods.items(), key=lambda pair: pair[1])[0] if moods else "Neutral"
        monthly_summary.append(
            {
                "month": month_key,
                "avg": round(avg, 2),
                "dominant": dominant_mood,
            }
        )

    inspiration = _get_daily_inspiration()

    return render_template(
        "diary.html",
        username=username,
        entries=entries,
        happiness_dates=happiness_dates,
        happiness_values=happiness_values,
        mood_map=mood_map,
        current_streak=current_streak,
        longest_streak=longest_streak,
        best_week_start=best_week_start,
        best_week_avg=round(best_week_avg, 2),
        monthly_summary=monthly_summary,
        entries_by_date=entries_by_date,
        daily_quote=inspiration.get("quote"),
        daily_author=inspiration.get("author"),
        daily_joke=inspiration.get("joke"),
        daily_photo_url=inspiration.get("photo_url"),
    )


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(error):
    return redirect(url_for("diary", error="Image too large. Please upload an image under 8 MB."))


@app.route("/collage", methods=["GET"])
@login_required
def collage():
    username = session.get("user")
    path = _user_file(username, "diary")
    entries = _load_json(path, [])
    mood_map = {}
    for entry in entries:
        date = entry.get("date") or ""
        if date:
            mood_map[date] = entry.get("mood", "")
    months = {}
    for entry in entries:
        if not entry.get("photo"):
            continue
        date = entry.get("date") or ""
        month_key = date[:7] if len(date) >= 7 else "Unknown"
        months.setdefault(month_key, []).append(entry)
    month_keys = sorted(months.keys())
    return render_template(
        "collage.html",
        username=username,
        months=months,
        month_keys=month_keys,
        mood_map=mood_map,
    )


@app.route("/journal", methods=["GET", "POST"])
@login_required
def journal():
    username = session.get("user")
    path = _user_file(username, "journal")
    entries = _load_json(path, [])
    settings_path = _user_settings(username)
    settings = _load_json(settings_path, {})
    strategies_path = _user_file(username, "strategies")
    strategies_data = _load_json(strategies_path, {"notes": []})
    strategy_options = [
        note.get("title")
        for note in strategies_data.get("notes", [])
        if note.get("title")
    ]
    updated = False
    for entry in entries:
        if not entry.get("id"):
            entry["id"] = _new_id()
            updated = True
        if entry.get("strategy") is None:
            entry["strategy"] = ""
        if entry.get("screenshot") is None:
            entry["screenshot"] = ""
        if not entry.get("result"):
            try:
                pnl_val = float(entry.get("pnl", 0) or 0)
            except Exception:
                pnl_val = 0
            if pnl_val > 0:
                entry["result"] = "Win"
            elif pnl_val < 0:
                entry["result"] = "Loss"
            else:
                entry["result"] = "Breakeven"
    if updated:
        _save_json(path, entries)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "save_hl":
            settings["hyperliquid_address"] = request.form.get("hyperliquid_address", "").strip()
            settings["hl_last_sync"] = 0
            _save_json(settings_path, settings)
            return redirect(url_for("journal"))

        if action == "update":
            entry_id = request.form.get("id", "")
            screenshot_name = _save_diary_photo(request.files.get("screenshot"))
            for entry in entries:
                if entry.get("id") == entry_id:
                    entry["date"] = request.form.get("date", entry.get("date", ""))
                    entry["symbol"] = request.form.get("symbol", entry.get("symbol", ""))
                    entry["side"] = request.form.get("side", entry.get("side", ""))
                    entry["qty"] = request.form.get("qty", entry.get("qty", ""))
                    entry["entry"] = request.form.get("entry", entry.get("entry", ""))
                    entry["exit"] = request.form.get("exit", entry.get("exit", ""))
                    entry["pnl"] = request.form.get("pnl", entry.get("pnl", ""))
                    entry["result"] = request.form.get("result", entry.get("result", ""))
                    entry["strategy"] = request.form.get("strategy", entry.get("strategy", ""))
                    entry["notes"] = request.form.get("notes", entry.get("notes", ""))
                    entry["tags"] = request.form.get("tags", entry.get("tags", ""))
                    if screenshot_name:
                        entry["screenshot"] = screenshot_name
                    entry["updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                    break
            _save_json(path, entries)
            return redirect(url_for("journal"))

        if action == "delete":
            entry_id = request.form.get("id", "")
            entries = [entry for entry in entries if entry.get("id") != entry_id]
            _save_json(path, entries)
            return redirect(url_for("journal"))

        screenshot_name = _save_diary_photo(request.files.get("screenshot"))
        result = request.form.get("result", "").strip()
        pnl_raw = request.form.get("pnl", "")
        if not result:
            try:
                pnl_val = float(pnl_raw or 0)
            except Exception:
                pnl_val = 0
            if pnl_val > 0:
                result = "Win"
            elif pnl_val < 0:
                result = "Loss"
            else:
                result = "Breakeven"

        entry = {
            "id": _new_id(),
            "date": request.form.get("date", ""),
            "symbol": request.form.get("symbol", ""),
            "side": request.form.get("side", ""),
            "qty": request.form.get("qty", ""),
            "entry": request.form.get("entry", ""),
            "exit": request.form.get("exit", ""),
            "pnl": pnl_raw,
            "result": result,
            "strategy": request.form.get("strategy", ""),
            "notes": request.form.get("notes", ""),
            "tags": request.form.get("tags", ""),
            "screenshot": screenshot_name,
        }
        entries.insert(0, entry)
        _save_json(path, entries)
        return redirect(url_for("journal"))

    hl_address = settings.get("hyperliquid_address", "")
    last_sync = settings.get("hl_last_sync", 0)
    now = int(time.time())
    if hl_address and now - last_sync > 60:
        fills = _hyperliquid_user_fills(hl_address)
        if fills:
            entries = _merge_hyperliquid_fills(entries, fills)
            _save_json(path, entries)
        settings["hl_last_sync"] = now
        _save_json(settings_path, settings)

    total_trades = len(entries)
    wins = sum(
        1
        for entry in entries
        if str(entry.get("pnl", "")).startswith("-") is False and entry.get("pnl")
    )
    win_rate = f"{(wins / total_trades * 100):.1f}%" if total_trades else "0%"

    pnl_by_date = {}
    pnl_by_symbol = {}
    gross_profit = 0.0
    gross_loss = 0.0
    pnl_values = []
    tag_counts = {}
    for entry in entries:
        try:
            pnl = float(entry.get("pnl", 0) or 0)
        except Exception:
            pnl = 0
        pnl_values.append(pnl)
        if pnl >= 0:
            gross_profit += pnl
        else:
            gross_loss += abs(pnl)
        date = entry.get("date") or ""
        symbol = entry.get("symbol") or "Unknown"
        if date:
            pnl_by_date[date] = pnl_by_date.get(date, 0) + pnl
        pnl_by_symbol[symbol] = pnl_by_symbol.get(symbol, 0) + pnl
        tags = entry.get("tags", "")
        for tag in [t.strip() for t in tags.split(",") if t.strip()]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    sorted_dates = sorted(pnl_by_date.keys())
    cumulative = []
    running = 0.0
    for date in sorted_dates:
        running += pnl_by_date[date]
        cumulative.append(running)

    symbol_labels = list(pnl_by_symbol.keys())
    symbol_values = [pnl_by_symbol[label] for label in symbol_labels]

    avg_pnl = round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else 0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else gross_profit
    best_day = ""
    worst_day = ""
    if pnl_by_date:
        best_day = max(pnl_by_date.items(), key=lambda pair: pair[1])[0]
        worst_day = min(pnl_by_date.items(), key=lambda pair: pair[1])[0]

    return render_template(
        "journal.html",
        username=username,
        entries=entries,
        hyperliquid_address=hl_address,
        strategy_options=strategy_options,
        total_trades=total_trades,
        win_rate=win_rate,
        avg_pnl=avg_pnl,
        profit_factor=profit_factor,
        best_day=best_day,
        worst_day=worst_day,
        tag_counts=tag_counts,
        pnl_dates=sorted_dates,
        pnl_cumulative=cumulative,
        symbol_labels=symbol_labels,
        symbol_values=symbol_values,
    )


@app.route("/bot", methods=["GET", "POST"])
@login_required
def bot():
    username = session.get("user")
    settings_path = _user_settings(username)
    settings = _load_json(settings_path, {})

    message = ""
    error = ""

    if request.method == "POST":
        address = request.form.get("account_address", "").strip()
        if not address:
            error = "Account address is required."
        elif not (address.startswith("0x") and len(address) == 42):
            error = "Account address must be a 42-character 0x address."
        else:
            settings["hyperliquid_address"] = address
            _save_json(settings_path, settings)
            message = "Account saved."

    address = settings.get("hyperliquid_address", "")
    vaults = []
    total_equity = 0.0
    total_pnl = 0.0
    total_series = []
    total_return_pct = 0.0

    if address and not error:
        raw = _fetch_hl_info({"type": "userVaultEquities", "user": address}, ttl=VAULT_CACHE_TTL_SECONDS)
        vaults = _normalize_vaults(raw, address)
        total_equity = sum(v["equity"] for v in vaults)
        total_pnl = sum((v.get("pnl") or 0.0) for v in vaults)
        initial_equity = total_equity - total_pnl
        if initial_equity > 0:
            total_return_pct = (total_pnl / initial_equity) * 100
        for v in vaults:
            v["share"] = (v["equity"] / total_equity * 100) if total_equity else 0.0
        daily = {}
        for v in vaults:
            for ts, val in v.get("series_with_time", []):
                date_key = datetime.utcfromtimestamp(ts / 1000).date().isoformat()
                daily[date_key] = daily.get(date_key, 0.0) + val
        total_series = sorted([[day, round(daily[day], 2)] for day in daily.keys()], key=lambda x: x[0])
        if total_equity:
            today_key = datetime.utcnow().date().isoformat()
            if not total_series or total_series[-1][0] != today_key:
                total_series.append([today_key, round(total_equity, 2)])
            else:
                total_series[-1][1] = round(total_equity, 2)

    return render_template(
        "bot.html",
        username=username,
        account_address=address,
        vaults=vaults,
        total_equity=total_equity,
        total_pnl=total_pnl,
        total_return_pct=total_return_pct,
        total_series=total_series,
        message=message,
        error=error,
    )


@app.route("/webhook", methods=["GET"])
def webhook_page():
    if "user" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("bot"))


@app.route("/webhook", methods=["POST"])
def webhook_receiver():
    return "ok"


@app.route("/links", methods=["GET", "POST"])
@login_required
def links():
    username = session.get("user")
    path = _user_file(username, "links")
    items = _load_json(path, [])

    if request.method == "POST":
        action = request.form.get("action", "add")
        if action == "delete":
            item_id = request.form.get("id", "")
            items = [item for item in items if item.get("id") != item_id]
            _save_json(path, items)
            return redirect(url_for("links"))

        if action == "update":
            item_id = request.form.get("id", "")
            for item in items:
                if item.get("id") == item_id:
                    item["title"] = request.form.get("title", "")
                    item["url"] = request.form.get("url", "")
                    item["note"] = request.form.get("note", "")
                    item["group"] = request.form.get("group", "")
                    item["tags"] = request.form.get("tags", "")
                    break
            _save_json(path, items)
            return redirect(url_for("links"))

        item = {
            "id": _new_id(),
            "title": request.form.get("title", ""),
            "url": request.form.get("url", ""),
            "note": request.form.get("note", ""),
            "group": request.form.get("group", ""),
            "tags": request.form.get("tags", ""),
        }
        if item["url"]:
            items.insert(0, item)
            _save_json(path, items)
        return redirect(url_for("links"))

    return render_template("links.html", username=username, items=items)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    app.run(host=host, port=port)
