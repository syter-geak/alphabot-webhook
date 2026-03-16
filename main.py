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
# CONFIG
# =========================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
ALPHABOT_API_KEY   = os.environ.get("ALPHABOT_API_KEY", "")  # пока не используется
PORT               = int(os.environ.get("PORT", 5000))
DB_PATH            = "seen.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# =========================================================
# DB
# =========================================================

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


def was_seen(raffle_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM seen WHERE raffle_id = ?",
        (raffle_id,)
    ).fetchone()
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


init_db()


# =========================================================
# APP
# =========================================================

app = Flask(__name__)


# =========================================================
# FILTER
# =========================================================

NEGATIVE_PATTERNS = [
    r"\bwl\b",
    r"whitelist",
    r"white list",
    r"allowlist",
    r"allow list",
    r"\bfcfs\b",
    r"\bmint\b",
    r"mint spot",
    r"guaranteed mint",
    r"\bspots?\b",
    r"early access",
    r"presale access",
    r"nft access",
]

POSITIVE_PATTERNS = [
    r"\$\s?\d",
    r"\b\d+[\d,.]*\s?(?:usd|usdt|usdc)\b",
    r"\b\d+[\d,.]*\s?(?:eth|btc|sol|bnb|ton|matic|arb|op|sui|apt|avax|near|ftm|link|dot|ada)\b",
    r"\b\d[\d,.]*\s?\$[a-z]{2,20}\b",
    r"\$[a-z]{2,20}\s+(?:token|airdrop|reward|prize)",
    r"\btoken\s+(?:reward|prize)\b",
    r"\bcash\s+prize\b",
    r"\b(?:prize|reward)\s+pool\b",
    r"\bairdrop\b",
]


def passes_filter(text: str) -> bool:
    t = (text or "").lower()

    for pattern in NEGATIVE_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            return False

    for pattern in POSITIVE_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            return True

    return False


# =========================================================
# HELPERS
# =========================================================

def deep_get(obj, *paths, default="—"):
    for path in paths:
        cur = obj
        ok = True

        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break

        if ok and cur is not None:
            if isinstance(cur, (dict, list)):
                try:
                    text = json.dumps(cur, ensure_ascii=False)
                except Exception:
                    text = str(cur)
            else:
                text = str(cur)

            if text.strip():
                return text.strip()

    return default


def parse_deadline(obj: dict) -> str:
    raw = deep_get(
        obj,
        "endDate", "end_date", "endAt", "end_at",
        "closingDate", "closing_date",
        "deadline", "endsAt", "ends_at",
        default=None
    )

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

    total_seconds = int(diff.total_seconds())
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    if days > 0:
        remaining = f"{days}d {hours}h {minutes}m до конца"
    elif hours > 0:
        remaining = f"{hours}h {minutes}m до конца"
    else:
        remaining = f"{minutes}m до конца"

    return f"{date_str} ({remaining})"


def parse_raffle(obj: dict) -> dict:
    raffle_id = deep_get(obj, "id", "_id", "uuid", "slug", "raffleId", default=None)
    if not raffle_id or raffle_id == "—":
        raffle_id = hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    title = deep_get(
        obj,
        "title",
        "name",
        "project.name",
        "project.title",
        "weblinkUrlTitle",
        "reqString",
        default="Без названия"
    )

    url = deep_get(obj, "url", "link", "raffleUrl", "permalink", "weblinkUrl", default=None)
    if not url or url == "—":
        slug = deep_get(obj, "slug", "project.slug", default=None)
        if slug and slug != "—":
            if slug.startswith("http://") or slug.startswith("https://"):
                url = slug
            else:
                url = f"https://www.alphabot.app/{slug}"
        else:
            url = "—"

    reward = deep_get(
        obj,
        "reward",
        "rewardText",
        "prize",
        "prizeText",
        "rewards",
        "rewardPool",
        "reward_pool",
        "subtitle",
        "description",
        "reqString",
        default="—"
    )

    winners = deep_get(
        obj,
        "winnerCount",
        "winner_count",
        "winners",
        "numWinners",
        "num_winners",
        "maxWinners",
        "spots",
        "totalSpots",
        default="—"
    )

    participants = deep_get(
        obj,
        "entryCount",
        "entry_count",
        "entries",
        "participantCount",
        "participant_count",
        "participants",
        "registrations",
        "totalEntries",
        "numEntries",
        default="—"
    )

    filter_text = " ".join([
        str(title),
        str(reward),
        str(deep_get(obj, "type", default="")),
        str(deep_get(obj, "tags", default="")),
        str(deep_get(obj, "reqString", default="")),
        str(deep_get(obj, "weblinkUrlTitle", default="")),
        str(deep_get(obj, "description", default="")),
        str(deep_get(obj, "subtitle", default="")),
        str(deep_get(obj, "blockchain", default="")),
    ])

    return {
        "id": raffle_id,
        "title": title,
        "url": url,
        "reward": reward,
        "winners": winners,
        "participants": participants,
        "deadline": parse_deadline(obj),
        "_filter_text": filter_text,
    }


# =========================================================
# TELEGRAM
# =========================================================

def format_message(r: dict) -> str:
    title = html_lib.escape(str(r["title"]))
    reward = html_lib.escape(str(r["reward"]))
    winners = html_lib.escape(str(r["winners"]))
    participants = html_lib.escape(str(r["participants"]))
    deadline = html_lib.escape(str(r["deadline"]))
    url = str(r["url"]).replace('"', "%22").strip()

    if url and url != "—":
        header = f'🎯 <b><a href="{url}">{title}</a></b>'
    else:
        header = f"🎯 <b>{title}</b>"

    return "\n".join([
        header,
        "",
        f"🏆 <b>Награда / пул:</b> {reward}",
        f"🥇 <b>Победных мест:</b> {winners}",
        f"👥 <b>Участников:</b> {participants}",
        f"⏰ <b>Дедлайн:</b> {deadline}",
    ])


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )

    if resp.status_code >= 400:
        log.error(f"Telegram error {resp.status_code}: {resp.text[:300]}")
    else:
        log.info("Сообщение отправлено в Telegram")


# =========================================================
# WEBHOOK PARSE
# =========================================================

def extract_raffle_objects(payload) -> list:
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    if "slug" in payload or "name" in payload or "title" in payload or "endDate" in payload:
        return [payload]

    data = payload.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("raffle"), dict):
            return [data["raffle"]]
        if isinstance(data.get("raffles"), list):
            return data["raffles"]
        if "slug" in data or "name" in data or "title" in data or "endDate" in data:
            return [data]

    raffle = payload.get("raffle")
    if isinstance(raffle, dict):
        return [raffle]

    result = payload.get("result")
    if isinstance(result, dict):
        if isinstance(result.get("raffle"), dict):
            return [result["raffle"]]
        return [result]
    if isinstance(result, list):
        return result

    return []


def process_webhook_payload(payload):
    with open("last_webhook.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    objects = extract_raffle_objects(payload)
    log.info(f"Найдено объектов рафлов: {len(objects)}")

    for obj in objects:
        if not isinstance(obj, dict):
            log.info("Пропуск: объект не dict")
            continue

        log.info(f"Ключи raffle object: {list(obj.keys())[:50]}")

        raffle = parse_raffle(obj)

        log.info(
            "Разобрано: "
            f"title={raffle['title']}, "
            f"reward={raffle['reward']}, "
            f"winners={raffle['winners']}, "
            f"participants={raffle['participants']}, "
            f"deadline={raffle['deadline']}"
        )

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


# =========================================================
# ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running", "service": "alphabot-webhook"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True, silent=True)

        if payload is None:
            log.warning("Пустой или не-JSON payload")
            return jsonify({"ok": False, "error": "no json"}), 400

        log.info(f"Webhook получен: {json.dumps(payload, ensure_ascii=False)[:500]}")
        process_webhook_payload(payload)

        return jsonify({"ok": True}), 200

    except Exception as e:
        log.error(f"Ошибка обработки webhook: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    log.info(f"Сервер запущен на порту {PORT}")
    app.run(host="0.0.0.0", port=PORT)
