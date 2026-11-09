import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

from loko_alert import BotError, Broadcast, MSK, database, parse_posts, process, sale_time, send_due


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 12, 15, 9, tzinfo=MSK)
        self.db = database(":memory:")
        self.bot = Mock()
        self.config = {"watch_until": "2027-01-07T00:00:00+03:00",
                       "remind_minutes": [1440, 60, 10, 0]}

    def tearDown(self):
        self.db.close()

    def post(self, text):
        return {"id": 123, "text": text, "published": self.now,
                "url": "https://t.me/hclokomotiv_official/123"}

    def test_explicit_dates(self):
        for text in ["Старт продаж 20 декабря в 10:00",
                     "Начало продаж 20.12.2026 в 10:00",
                     "20 декабря в 10:00 продажи начнутся"]:
            with self.subTest(text=text):
                self.assertEqual(sale_time(text, self.now), datetime(2026, 12, 20, 10, tzinfo=MSK))

    def test_relative_dates(self):
        for word, days in [("сегодня", 0), ("завтра", 1), ("послезавтра", 2)]:
            self.assertEqual(sale_time(f"Продажи начнутся {word} в 12:00", self.now),
                             (self.now + timedelta(days=days)).replace(hour=12))

    def test_new_year(self):
        published = datetime(2026, 12, 28, 9, tzinfo=MSK)
        self.assertEqual(sale_time("Старт продаж 2 января в 10:00", published),
                         datetime(2027, 1, 2, 10, tzinfo=MSK))

    def test_ambiguous_dates_abstain(self):
        for text in ["Билеты на матч 30 декабря в 19:00",
                     "Билеты доступны на матч 30 декабря в 19:00",
                     "Старт продаж 20 декабря в 10:00, матч 30 декабря в 19:00",
                     "Старт продаж 20 декабря в 10:00 и в 12:00",
                     "Старт продаж 20 декабря", "Старт продаж в 10:00",
                     "Старт продаж отменён, 20 декабря в 10:00",
                     "Для владельцев абонементов старт продаж завтра в 10:00",
                     "Старт продаж 31 февраля в 10:00"]:
            with self.subTest(text=text):
                self.assertIsNone(sale_time(text, self.now))

    def test_match_in_different_sentence(self):
        self.assertEqual(sale_time("Матч 30 декабря в 19:00. Старт продаж завтра в 10:00", self.now),
                         datetime(2026, 12, 16, 10, tzinfo=MSK))

    def test_html_preserves_inline_words_and_breaks(self):
        html = '''<div class="tgme_widget_message" data-post="hclokomotiv_official/123">
        <div class="tgme_widget_message_text"><b>Старт продаж</b> завтра в 10:00<br>Ак Барс</div>
        <time datetime="2026-12-15T06:00:00+00:00"></time></div>'''
        p = parse_posts(html, "hclokomotiv_official")[0]
        self.assertEqual(p["text"], "Старт продаж завтра в 10:00\nАк Барс")
        self.assertEqual(p["published"], self.now)
        with self.assertRaises(RuntimeError):
            parse_posts("<html>Access denied</html>", "hclokomotiv_official")

    def test_duplicates_and_edits(self):
        p = self.post("Старт продаж 20 декабря в 10:00")
        process(self.db, [p], self.bot, self.config, self.now)
        process(self.db, [p], self.bot, self.config, self.now)
        self.assertEqual(self.bot.send.call_count, 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM reminders").fetchone()[0], 4)
        p["text"] = "Продажи перенесены. Новую дату сообщим позже."
        process(self.db, [p], self.bot, self.config, self.now)
        self.assertEqual(self.bot.send.call_count, 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM reminders").fetchone()[0], 0)

    def test_send_failure_does_not_mark_as_processed(self):
        self.bot.send.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            process(self.db, [self.post("Старт продаж завтра в 10:00")], self.bot, self.config, self.now)
        self.assertEqual(self.db.execute("SELECT count(*) FROM posts").fetchone()[0], 0)
        self.bot.send.side_effect = None
        process(self.db, [self.post("Старт продаж завтра в 10:00")], self.bot, self.config, self.now)
        self.assertEqual(self.db.execute("SELECT count(*) FROM posts").fetchone()[0], 1)

    def test_due_reminders_collapsed_after_downtime(self):
        process(self.db, [self.post("Старт продаж 20 декабря в 10:00")], self.bot, self.config, self.now)
        self.bot.reset_mock()
        now = datetime(2026, 12, 20, 10, 3, tzinfo=MSK)
        send_due(self.db, self.bot, now)
        send_due(self.db, self.bot, now)
        self.assertEqual(self.bot.send.call_count, 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM reminders WHERE done=0").fetchone()[0], 0)

    def test_failed_reminder_retried(self):
        process(self.db, [self.post("Старт продаж 20 декабря в 10:00")], self.bot, self.config, self.now)
        self.bot.send.side_effect = RuntimeError("offline")
        now = datetime(2026, 12, 20, 10, tzinfo=MSK)
        with self.assertRaises(RuntimeError):
            send_due(self.db, self.bot, now)
        self.assertEqual(self.db.execute("SELECT count(*) FROM reminders WHERE done=0").fetchone()[0], 4)

    def test_non_ticket_edit_silent(self):
        process(self.db, [self.post("Тренировка")], self.bot, self.config, self.now)
        process(self.db, [self.post("Тренировка завершена")], self.bot, self.config, self.now)
        self.bot.send.assert_not_called()


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.db = database(":memory:")
        self.api = Mock(chat_id="10")
        self.broadcast = Broadcast(self.db, self.api)
        self.addCleanup(self.db.close)

    @staticmethod
    def update(n, chat_id, text, kind="private"):
        return {"update_id": n, "message": {"chat": {"id": chat_id, "type": kind}, "text": text}}

    def test_broadcast_to_two_private_subscribers(self):
        self.api.call.return_value = [self.update(5, 20, "/start invite"),
                                      self.update(6, 30, "/start", "group"),
                                      self.update(7, 20, "/start")]
        self.broadcast.poll()
        self.broadcast.send("Ticket sale")
        self.db.commit()
        self.broadcast.flush()
        deliveries = [c.kwargs["chat_id"] for c in self.api.send.call_args_list if c.args[0] == "Ticket sale"]
        self.assertCountEqual(deliveries, [10, 20])
        self.api.call.return_value = []
        Broadcast(self.db, self.api).poll()
        self.assertEqual(self.api.call.call_args.kwargs["offset"], 8)

    def test_stop_clears_queue_and_does_not_reseed_owner(self):
        self.broadcast.send("Pending sale")
        self.api.call.return_value = [self.update(2, 10, "/stop")]
        self.broadcast.poll()
        Broadcast(self.db, self.api).send("Next sale")
        self.broadcast.flush()
        self.assertEqual(self.api.send.call_count, 1)
        self.assertIn("/start", self.api.send.call_args.args[0])
        self.assertEqual(self.db.execute("SELECT count(*) FROM subscribers").fetchone()[0], 0)

    def test_blocked_recipient_does_not_interrupt_broadcast(self):
        self.db.execute("INSERT INTO subscribers VALUES (20)")
        self.broadcast.send("Ticket sale")
        self.db.commit()
        self.api.send.side_effect = [BotError(403), None]
        self.broadcast.flush()
        self.assertEqual(self.api.send.call_count, 2)
        self.assertEqual(self.db.execute("SELECT chat_id FROM subscribers").fetchall(), [(20,)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_delivery_retry_does_not_resend_successful_copy(self):
        self.db.execute("INSERT INTO subscribers VALUES (20)")
        self.broadcast.send("Ticket sale")
        self.db.commit()
        self.api.send.side_effect = [None, RuntimeError("offline"), None]
        with self.assertRaises(RuntimeError):
            self.broadcast.flush()
        Broadcast(self.db, self.api).flush()
        self.assertEqual([c.kwargs["chat_id"] for c in self.api.send.call_args_list], [10, 20, 20])


if __name__ == "__main__":
    unittest.main()
