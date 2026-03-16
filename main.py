import hashlib
import html as html_lib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
# =========================================================
# Инициализация при импорте (для gunicorn)
# =========================================================

init_db()

# =========================================================
# CONFIG — все значения берём из переменных окружения
# =========================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ALPHABOT_API_KEY   = os.environ.get("ALPHABOT_API_KEY", "")

PORT    = int(os.environ.get("PORT", 5000))
DB_PATH = "seen.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            raffle_id TEXT PRIMARY KEY,
            title     TEXT,
            seen_at   TEXT
        )
    """)
    conn.commit()
    conn.close()

app = Flask(__name__)
# Инициализируем БД при старте приложения (для gunicorn)
init_db()

# =========================================================
# DB
# =========================================================

def was_seen(raffle_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM seen WHERE raffle_id = ?", (raffle_id,)).fetchone()
    conn.close()
    return row is not None


def mark_seen(raffle_id: str, title: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO seen(raffle_id, title, seen_at) VALUES (?, ?, ?)",
        (raffle_id, title, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


# =========================================================
# ФИЛЬТР
# =========================================================

NEGATIVE_PATTERNS = [
    r"\bwl\b", r"whitelist", r"white list",
    r"allowlist", r"allow list",
    r"\bfcfs\b", r"\bmint\b", r"mint spot",
    r"guaranteed mint", r"\bspots?\b",
    r"early access", r"presale access", r"nft access",
]

POSITIVE_PATTERNS = [
    r"\$\s?\d",
    r"\b\d+[\d,.]*\s?(?:usd|usdt|usdc)\b",
    r"\b\d+[\d,.]*\s?(?:eth|btc|sol|bnb|ton|matic|arb|op|sui|apt|avax|near)\b",
    r"\b\d[\d,.]*\s?\$[a-z]{2,20}\b",
    r"\$[a-z]{2,20}\s+(?:token|airdrop|reward|prize)",
    r"\btoken\s+(?:reward|prize)\b",
    r"\bcash\s+prize\b",
    r"\b(?:prize|reward)\s+pool\b",
    r"\bairdrop\b",
]


def passes_filter(text: str) -> bool:
    t = text.lower()
    for p in NEGATIVE_PATTERNS:
        if re.search(p, t, re.IGNORECASE):
            return False
    for p in POSITIVE_PATTERNS:
        if re.search(p, t, re.IGNORECASE):
            return True
    return False


# =========================================================
# PARSE
# =========================================================

def deep_get(obj, *paths, default="—"):
    for path in paths:
        cur = obj
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                cur = None
                break
        if cur is not None and str(cur).strip():
            return str(cur).strip()
    return default


def parse_deadline(obj: dict) -> str:
    raw = deep_get(obj, "endDate", "end_date", "endAt", "closingDate", "deadline", default=None)
    if not raw or raw == "—":
        return "—"

    dt = None
    try:
        ts = float(raw)
        if ts > 1e10:
            ts /= 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError):
        pass

    if dt is None:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return str(raw)

    date_str = dt.strftime("%d.%m.%Y %H:%M UTC")
    diff = dt - datetime.now(timezone.utc)

    if diff.total_seconds() <= 0:
        return f"{date_str} (завершён)"

    s = int(diff.total_seconds())
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60

    if d > 0:
        left = f"{d}d {h}h {m}m до конца"
    elif h > 0:
        left = f"{h}h {m}m до конца"
    else:
        left = f"{m}m до конца"

    return f"{date_str} ({left})"


def parse_raffle(obj: dict) -> dict:
    raffle_id = deep_get(obj, "id", "_id", "uuid", "slug", "raffleId", default=None)
    if not raffle_id:
        raffle_id = hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()

    title = deep_get(obj, "title", "name", "project.name", "project.title", default="Без названия")

    url = deep_get(obj, "url", "link", "raffleUrl", "permalink", default=None)
    if not url:
        slug = deep_get(obj, "slug", "project.slug", default=None)
        if slug and not slug.startswith("http"):
            url = f"https://www.alphabot.app/{slug}"
        else:
            url = slug or "—"

    reward       = deep_get(obj, "reward", "rewardText", "prize", "prizeText", "rewards", "description", default="—")
    winners      = deep_get(obj, "winnerCount", "winner_count", "winners", "numWinners", "maxWinners", default="—")
    participants = deep_get(obj, "entryCount", "entry_count", "entries", "participantCount", "participants", default="—")

    return {
        "id":           raffle_id,
        "title":        title,
        "url":          url,
        "reward":       reward,
        "winners":      winners,
        "participants": participants,
        "deadline":     parse_deadline(obj),
        "_filter_text": f"{title} {reward} {obj.get('type','')} {obj.get('tags','')} {obj.get('reqString','')}",
    }


# =========================================================
# TELEGRAM
# =========================================================

def format_message(r: dict) -> str:
    title        = html_lib.escape(str(r["title"]))
    reward       = html_lib.escape(str(r["reward"]))
    winners      = html_lib.escape(str(r["winners"]))
    participants = html_lib.escape(str(r["participants"]))
    deadline     = html_lib.escape(str(r["deadline"]))
    url          = str(r["url"]).replace('"', "%22")

    header = f'🎯 <b><a href="{url}">{title}</a></b>' if url != "—" else f'🎯 <b>{title}</b>'

    return "\n".join([
        header, "",
        f"🏆 <b>Награда / пул:</b> {reward}",
        f"🥇 <b>Победных мест:</b> {winners}",
        f"👥 <b>Участников:</b> {participants}",
        f"⏰ <b>Дедлайн:</b> {deadline}",
    ])


def send_telegram(text: str):
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )
    if resp.status_code >= 400:
        log.error(f"Telegram error {resp.status_code}: {resp.text[:200]}")


# =========================================================
# WEBHOOK HANDLER
# =========================================================

def extract_raffle_objects(payload) -> list:
    """
    Alphabot может прислать как одиночный объект, так и массив.
    Также событие может быть обёрнуто в {'event': ..., 'data': {...}}.
    """
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        # Формат события: {"event": "raffle.created", "data": {...}}
        data = payload.get("data") or payload.get("raffle") or payload.get("result")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        # Если сам объект рафла — вернём как есть
        if "slug" in payload or "name" in payload or "endDate" in payload:
            return [payload]

    return []


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            log.warning("Пустой или не-JSON payload")
            return jsonify({"ok": False, "error": "no json"}), 400

        log.info(f"Webhook получен: {json.dumps(payload)[:300]}")

        # Сохраняем сырой payload для отладки
        with open("last_webhook.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        objects = extract_raffle_objects(payload)
        log.info(f"Найдено объектов рафлов: {len(objects)}")

        for obj in objects:
            raffle = parse_raffle(obj)

            if was_seen(raffle["id"]):
                log.info(f"Уже видели: {raffle['title']}")
                continue

            if not passes_filter(raffle["_filter_text"]):
                log.info(f"Отфильтровано (не токен/деньги): {raffle['title']}")
                mark_seen(raffle["id"], raffle["title"])
                continue

            log.info(f"✅ Отправляем в Telegram: {raffle['title']}")
            send_telegram(format_message(raffle))
            mark_seen(raffle["id"], raffle["title"])

        return jsonify({"ok": True}), 200

    except Exception as e:
        log.error(f"Ошибка обработки webhook: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running", "service": "alphabot-webhook"}), 200


# =========================================================
# START
# =========================================================

