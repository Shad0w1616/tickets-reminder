"""Public Telegram channel -> private ticket announcements and sale reminders."""
import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
MSK = timezone(timedelta(hours=3))
LOG = logging.getLogger("loko")
MONTHS = dict(zip(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(),
    range(1, 13)))
TICKET = re.compile(r"билет|продаж|касс[аыуе]|абонемент|ticket", re.I)
SALE = re.compile(
    r"(?:старт|начал|откр\w*|запуск)\w*\s+(?:онлайн[- ]?)?продаж\w*"
    r"|продаж\w*\s+(?:билет\w*\s+)?(?:старт\w*|нач\w*|откр\w*)"
    r"|билет\w*\s+(?:можно\s+будет\s+)?(?:поступ\w*|доступ\w*|появ\w*)", re.I)
DATE = re.compile(
    r"(?<!\d)(\d{1,2})(?:-?(?:го|е))?\s+(" + "|".join(MONTHS) +
    r")(?:\s+(20\d{2}))?\b|(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}))?(?!\d)", re.I)
CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")


def sale_time(text, published):
    """Only one sale clause with one explicit date and clock; otherwise abstain.

    Never infer a sale time from a match schedule elsewhere in a post.
    """
    text = text.lower().replace("\xa0", " ").replace("ё", "е")
    clauses = re.split(r"[!;?\n]|\.(?=\s|$)", text)
    candidates = []
    for clause in clauses:
        if not SALE.search(clause):
            continue
        if re.search(r"отмен|перенос|перенес|не\s+(?:нач|откр|состо)|заверш|законч", clause):
            return None
        # Multiple phases (online/cash desk/member sales) must be checked by a person.
        if re.search(r"абонемент|владельц|держател|приоритет|льгот|касс", clause):
            return None
        # Clauses mixing match schedules and sales are deliberately left to a person.
        if re.search(r"\bматч|\bигр[ауы]|\bвстреч", clause):
            return None
        dates = list(DATE.finditer(clause))
        clocks = [m for m in CLOCK.finditer(clause)
                  if not any(d.start() <= m.start() < d.end() for d in dates)]
        relatives = re.findall(r"\b(сегодня|завтра|послезавтра)\b", clause)
        if len(clocks) != 1 or len(dates) + len(relatives) != 1:
            return None
        clock = clocks[0]
        # '30.12' is a date, not an additional sale clock.
        if any(m.start() <= clock.start() < m.end() for m in dates):
            return None
        if dates:
            d = dates[0]
            day = int(d[1] or d[4])
            month = MONTHS[d[2]] if d[2] else int(d[5])
            explicit_year = d[3] or d[6]
            year = int(explicit_year) if explicit_year else published.year
            try:
                date = datetime(year, month, day, tzinfo=MSK)
                if not explicit_year and date.date() < (published - timedelta(days=7)).date():
                    date = date.replace(year=year + 1)
            except ValueError:
                return None
        else:
            date = published + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[relatives[0]])
        date = date.replace(hour=int(clock[1]), minute=int(clock[2]), second=0, microsecond=0)
        if not published - timedelta(days=1) <= date <= published + timedelta(days=90):
            return None
        candidates.append(date)
    return candidates[0] if len(candidates) == 1 else None


def parse_posts(html, channel):
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for node in soup.select(".tgme_widget_message[data-post]"):
        ref = node.get("data-post", "")
        if not ref.startswith(channel + "/"):
            continue
        stamp = node.select_one("time[datetime]")
        if not stamp:
            continue
        body = node.select_one(".tgme_widget_message_text")
        if body:
            for br in body.find_all("br"):
                br.replace_with("\n")
        text = body.get_text("", strip=False) if body else ""
        result.append({"id": int(ref.rsplit("/", 1)[1]), "text": text,
                       "published": datetime.fromisoformat(stamp["datetime"].replace("Z", "+00:00")).astimezone(MSK),
                       "url": "https://t.me/" + ref})
    if not result:
        raise RuntimeError("В публичной странице нет постов: канал недоступен или изменил разметку")
    return sorted(result, key=lambda p: p["id"])


def fetch_posts(session, channel, cutoff, last_id):
    posts, before = {}, None
    for _ in range(100):
        response = session.get("https://t.me/s/" + channel,
                               params={"before": before} if before else {}, timeout=(10, 30))
        response.raise_for_status()
        batch = parse_posts(response.text, channel)
        for post in batch:
            posts[post["id"]] = post
        oldest = batch[0]
        if (last_id and oldest["id"] <= last_id) or (not last_id and oldest["published"] <= cutoff):
            return sorted(posts.values(), key=lambda p: p["id"])
        if before is not None and oldest["id"] >= before:
            raise RuntimeError("Не удалось загрузить историю канала; курсор не продвигается")
        before = oldest["id"]
        time.sleep(0.3)
    raise RuntimeError("История превышает 100 страниц. Уменьшите initial_history_days")


class BotError(RuntimeError):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f"Telegram API: код {code}; retry_after={retry_after}")


class Bot:
    def __init__(self, session, token, chat_id):
        self.session, self.token, self.chat_id = session, token, chat_id

    def call(self, method, **payload):
        # Do not log request URLs or raw exceptions: they contain the bot token.
        try:
            response = self.session.post(
                f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=(10, 30))
            data = response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError("Telegram API: ошибка соединения или неверный ответ") from None
        if not data.get("ok"):
            code = data.get("error_code", response.status_code)
            retry = data.get("parameters", {}).get("retry_after", 0)
            raise BotError(code, retry)
        return data["result"]

    def send(self, text, chat_id=None):
        self.call("sendMessage", chat_id=self.chat_id if chat_id is None else chat_id, text=text,
                  link_preview_options={"is_disabled": True})


def database(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, hash TEXT NOT NULL, ticket INTEGER);
        CREATE TABLE IF NOT EXISTS reminders (
            post_id INTEGER, minutes INTEGER, due REAL, sale REAL, url TEXT, done INTEGER DEFAULT 0,
            PRIMARY KEY(post_id, minutes));
        CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
            next_try REAL NOT NULL DEFAULT 0, post_id INTEGER, reminder INTEGER NOT NULL DEFAULT 0);
    """)
    return db


class Broadcast:
    def __init__(self, db, bot):
        self.db, self.bot = db, bot
        with db:
            seed = db.execute("INSERT OR IGNORE INTO bot_state VALUES ('seeded', 1)")
            if seed.rowcount and bot.chat_id:
                db.execute("INSERT OR IGNORE INTO subscribers VALUES (?)", (int(bot.chat_id),))

    def unsubscribe(self, chat_id):
        self.db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
        self.db.execute("DELETE FROM outbox WHERE chat_id=?", (chat_id,))

    def poll(self):
        row = self.db.execute("SELECT value FROM bot_state WHERE key='offset'").fetchone()
        offset = row[0] if row else 0
        updates = self.bot.call("getUpdates", offset=offset, timeout=0, limit=100,
                                allowed_updates=["message"])
        with self.db:
            for update in sorted(updates, key=lambda u: u["update_id"]):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat = message.get("chat", {})
                if chat.get("type") != "private":
                    continue
                parts = message.get("text", "").split()
                command = parts[0] if parts else ""
                chat_id = chat["id"]
                if command == "/start":
                    self.db.execute("INSERT OR IGNORE INTO subscribers VALUES (?)", (chat_id,))
                    reply = "Подписка включена. Буду присылать объявления о билетах и напоминания. Отписаться: /stop."
                elif command == "/stop":
                    self.unsubscribe(chat_id)
                    reply = "Подписка отключена. Включить снова: /start."
                else:
                    continue
                self.db.execute("INSERT INTO outbox (chat_id, body) VALUES (?,?)", (chat_id, reply))
            if updates:
                self.db.execute("INSERT OR REPLACE INTO bot_state VALUES ('offset', ?)", (offset,))
            else:
                self.db.execute("DELETE FROM bot_state WHERE key='offset'")

    def send(self, text, *, post_id=None, reminder=False):
        # Caller commits the outbox together with the processed post or reminder.
        self.db.execute("INSERT INTO outbox (chat_id, body, post_id, reminder) "
                        "SELECT chat_id, ?, ?, ? FROM subscribers", (text, post_id, reminder))

    def flush(self):
        rows = self.db.execute("SELECT id, chat_id, body FROM outbox WHERE next_try<=? "
                               "ORDER BY id LIMIT 300", (time.time(),)).fetchall()
        blocked = set()
        for msg_id, chat_id, body in rows:
            if chat_id in blocked:
                continue
            try:
                self.bot.send(body, chat_id=chat_id)
            except BotError as exc:
                if exc.code == 403:
                    with self.db:
                        self.unsubscribe(chat_id)
                    blocked.add(chat_id)
                    continue
                if exc.code == 429:
                    with self.db:
                        self.db.execute("UPDATE outbox SET next_try=? WHERE chat_id=?",
                                        (time.time() + max(1, exc.retry_after), chat_id))
                    return
                if exc.code != 400:
                    raise
                LOG.warning("Telegram отклонил сообщение из очереди: %s", exc)
            with self.db:
                self.db.execute("DELETE FROM outbox WHERE id=?", (msg_id,))
            time.sleep(0.05)


def process(db, posts, bot, config, now):
    for post in posts:
        digest = hashlib.sha256(post["text"].encode()).hexdigest()
        old = db.execute("SELECT hash, ticket FROM posts WHERE id=?", (post["id"],)).fetchone()
        if old and old[0] == digest:
            continue
        # A changed post cancels its old reminders, even if delivery then fails.
        if old:
            db.execute("DELETE FROM reminders WHERE post_id=?", (post["id"],))
            db.execute("DELETE FROM outbox WHERE post_id=?", (post["id"],))
            db.commit()
        if TICKET.search(post["text"]):
            start = sale_time(post["text"], post["published"])
            title = "Изменено билетное объявление" if old else "Билетное объявление «Локомотива»"
            if start:
                details = "Распознано время старта продаж: " + start.strftime("%d.%m.%Y в %H:%M МСК")
                details += "\nСверьте с оригиналом: у клуба могут быть отдельные этапы продаж."
                if start <= now:
                    details += "\nУказанное время уже наступило."
            else:
                details = "Дата/время старта не распознаны однозначно — проверьте оригинал. Напоминания не созданы."
            body = post["text"][:2600]
            bot.send(f"{title}\n\n{details}\n\n{body}\n\n{post['url']}\n\n"
                     "Следим за 30.12.2026 — Ак Барс и 06.01.2027 — Металлург. "
                     "Объявление может относиться ко всей серии или другим матчам.", post_id=post["id"])
            if start and now < start <= datetime.fromisoformat(config["watch_until"]):
                for minutes in config["remind_minutes"]:
                    due = start - timedelta(minutes=minutes)
                    if due >= now:
                        db.execute("INSERT OR REPLACE INTO reminders VALUES (?,?,?,?,?,0)",
                                   (post["id"], minutes, due.timestamp(), start.timestamp(), post["url"]))
        elif old and old[1]:
            bot.send("Изменён ранее проверенный пост. Его старые напоминания отменены.\n" + post["url"],
                     post_id=post["id"])
        db.execute("INSERT OR REPLACE INTO posts VALUES (?,?,?)",
                   (post["id"], digest, bool(TICKET.search(post["text"]))))
        db.commit()


def send_due(db, bot, now):
    rows = db.execute("SELECT post_id, minutes, sale, url FROM reminders "
                      "WHERE done=0 AND due<=? ORDER BY due DESC", (now.timestamp(),)).fetchall()
    sent = set()
    for pid, minutes, stamp, url in rows:
        if pid in sent:
            continue
        start = datetime.fromtimestamp(stamp, MSK)
        if now > start + timedelta(hours=6):
            message = "Пропущенное напоминание: скрипт не работал вовремя."
        elif now >= start:
            message = "Наступило объявленное время старта продаж."
        else:
            remaining = max(1, int((start - now).total_seconds() / 60))
            message = f"До объявленного старта продаж примерно {remaining} мин."
        bot.send(f"{message}\n{start:%d.%m.%Y в %H:%M} МСК\n"
                 f"Проверьте, что объявление относится к нужным матчам:\n{url}", post_id=pid, reminder=True)
        db.execute("UPDATE reminders SET done=1 WHERE post_id=? AND due<=?", (pid, now.timestamp()))
        db.commit()
        sent.add(pid)


def load_config(require_token=True):
    path = ROOT / "config.json"
    if not path.exists():
        if require_token:
            raise RuntimeError("Скопируйте config.example.json в config.json и заполните bot_token")
        path = ROOT / "config.example.json"
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if require_token and not config.get("bot_token"):
        raise RuntimeError("Заполните bot_token в config.json")
    if not re.fullmatch(r"[A-Za-z0-9_]+", config["channel"]):
        raise RuntimeError("channel должен содержать имя канала без @ и URL")
    if config["poll_seconds"] < 30 or not 1 <= config["initial_history_days"] <= 365:
        raise RuntimeError("poll_seconds >= 30; initial_history_days от 1 до 365")
    if any(not isinstance(m, int) or m < 0 for m in config["remind_minutes"]):
        raise RuntimeError("remind_minutes: целые неотрицательные числа")
    if datetime.fromisoformat(config["watch_until"]).utcoffset() is None:
        raise RuntimeError("watch_until должен включать часовой пояс +03:00")
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", action="store_true", help="Показать chat_id из личных /start")
    parser.add_argument("--test", action="store_true", help="Отправить тестовое сообщение")
    parser.add_argument("--once", action="store_true", help="Один проход мониторинга")
    parser.add_argument("--dry-run", action="store_true", help="Читать канал без отправки и записи состояния")
    parser.add_argument("--cancel", type=int, metavar="POST_ID", help="Отменить напоминания поста по его номеру")
    args = parser.parse_args()
    config = load_config(require_token=not args.dry_run)
    if args.cancel is not None:
        with database(ROOT / "state.sqlite3") as db:
            db.execute("UPDATE reminders SET done=1 WHERE post_id=?", (args.cancel,))
            db.execute("DELETE FROM outbox WHERE post_id=? AND reminder=1", (args.cancel,))
        print("Напоминания поста отменены:", args.cancel)
        return
    session = requests.Session()
    session.headers["User-Agent"] = "LokoTicketNotifier/1.0"
    bot = Bot(session, config["bot_token"], config.get("chat_id"))
    if args.chat_id:
        updates = bot.call("getUpdates", timeout=0, limit=100)
        ids = {u["message"]["chat"]["id"] for u in updates if
               u.get("message", {}).get("chat", {}).get("type") == "private" and
               u["message"].get("text", "").split(" ")[0] == "/start"}
        print("chat_id:", ", ".join(map(str, sorted(ids))) if ids else
              "не найден; отправьте /start вашему боту и повторите")
        return
    if args.test:
        if not config.get("chat_id"):
            raise RuntimeError("Для --test укажите свой chat_id или проверьте подписку командой /start")
        bot.send("Проверка: уведомления «Локомотива» работают. Время — московское.")
        print("Тест отправлен")
        return
    db = database(":memory:" if args.dry_run else ROOT / "state.sqlite3")
    if args.dry_run:
        bot.send = lambda message, **_: print(message)
        alerts = bot
    else:
        alerts = Broadcast(db, bot)
    last_id = db.execute("SELECT COALESCE(MAX(id),0) FROM posts").fetchone()[0]
    failures = 0
    while datetime.now(MSK) < datetime.fromisoformat(config["watch_until"]):
        if not args.dry_run:
            try:
                alerts.poll()
            except RuntimeError as exc:
                LOG.error("Не удалось прочитать команды подписки: %s", exc)
        try:
            now = datetime.now(MSK)
            # Check reminders even when the source channel is unreachable.
            send_due(db, alerts, now)
            cutoff = now - timedelta(days=config["initial_history_days"])
            posts = fetch_posts(session, config["channel"], cutoff, last_id)
            posts = [p for p in posts if p["published"] >= cutoff or p["id"] > last_id > 0]
            process(db, posts, alerts, config, datetime.now(MSK))
            last_id = db.execute("SELECT COALESCE(MAX(id),0) FROM posts").fetchone()[0]
            LOG.info("Проверено постов: %s; последний ID: %s", len(posts), last_id)
            failures = 0
        except (requests.RequestException, RuntimeError, sqlite3.Error, ValueError) as exc:
            failures += 1
            # Type only for requests exceptions: URLs may include secrets.
            safe = type(exc).__name__ if isinstance(exc, requests.RequestException) else str(exc)
            LOG.error("Ошибка #%s: %s", failures, safe)
            if args.once or args.dry_run:
                raise SystemExit(1)
        if not args.dry_run:
            try:
                alerts.flush()
            except RuntimeError as exc:
                LOG.error("Ошибка доставки; очередь будет обработана повторно: %s", exc)
                if args.once:
                    raise SystemExit(1)
        if args.once or args.dry_run:
            break
        time.sleep(config["poll_seconds"])
    db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        print("Остановлено")
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        LOG.error("%s", error)
        raise SystemExit(1)
