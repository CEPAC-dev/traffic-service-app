import sqlite3, unittest
from trafficlens.services.turning_movement_forms import parse_study_form
from trafficlens.services.turning_movement_studies import StudyError
from trafficlens.study_notifications import ensure_study_notifications


class Form(dict):
    def getlist(self, key):
        v = self.get(key, [])
        return v if isinstance(v, list) else [v]


def good(**extra):
    f = Form(title="Main / 5th", study_date="2026-11-01", preset="typical", period_ids=["0", "1"])
    for k, (label, ins, outs) in enumerate((("AM", [2070, 2700, 2170, 1770], [1320, 1870, 2570, 2950]),
                                            ("PM", ["1,200", "900.0", 800, 700], [900, 900, 900, 900]))):
        f[f"period_label_{k}"] = label
        for i in range(4):
            f[f"in_{k}_{i}"], f[f"out_{k}_{i}"] = ins[i], outs[i]
    f.update(extra)
    return f


class FormTests(unittest.TestCase):
    def test_good_form(self):
        r = parse_study_form(good())
        self.assertEqual(r["legs"], ["E", "N", "W", "S"])
        self.assertEqual(r["periods"][1]["inflows"], [1200, 900, 800, 700])   # "1,200" and "900.0" -> ints
        self.assertEqual(r["turn_classes"][0]["through"], 3)
        self.assertIsNone(parse_study_form(good(study_date=""))["study_date"])

    def test_custom_classes(self):
        f = good(preset="custom")
        for i in range(4):
            for kind in ("u_turn", "left", "through", "right"):
                f[f"class_{i}_{kind}"] = "2"
        self.assertEqual(parse_study_form(f)["turn_classes"][3]["left"], 2)
        f["class_2_left"] = "4"
        with self.assertRaises(StudyError): parse_study_form(f)

    def test_bad_input_is_rejected_with_a_message(self):
        for change in (dict(in_0_1=""), dict(in_0_1="abc"), dict(out_1_3="nan"), dict(in_0_0="inf"),
                       dict(period_ids=[]), dict(period_ids=["x"]), dict(preset="weird")):
            with self.subTest(change=change), self.assertRaises(StudyError):
                parse_study_form(good(**change))

    def test_too_many_periods(self):
        with self.assertRaises(StudyError): parse_study_form(good(period_ids=[str(i) for i in range(25)]))


class NotificationMigrationTests(unittest.TestCase):
    def conn(self):
        c = sqlite3.connect(":memory:", isolation_level=None)
        c.executescript("""CREATE TABLE users(id INTEGER PRIMARY KEY); CREATE TABLE requests(id INTEGER PRIMARY KEY);
        CREATE TABLE turning_movement_studies(id INTEGER PRIMARY KEY);
        CREATE TABLE notifications(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          request_id INTEGER NOT NULL REFERENCES requests(id), kind TEXT NOT NULL, title TEXT NOT NULL,
          message TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT);
        CREATE INDEX idx_notifications_user_unread ON notifications(user_id, read_at, id);
        INSERT INTO users VALUES(1); INSERT INTO requests VALUES(7); INSERT INTO turning_movement_studies VALUES(3);
        INSERT INTO notifications VALUES(5,1,7,'new_request','New request','m','2026-01-01','2026-01-02');""")
        return c

    def test_old_rows_survive_and_study_rows_work(self):
        c = self.conn()
        self.assertTrue(ensure_study_notifications(c))
        self.assertEqual(c.execute("SELECT id,request_id,study_id,read_at FROM notifications").fetchall(),
                         [(5, 7, None, "2026-01-02")])
        c.execute("INSERT INTO notifications(user_id,study_id,kind,title,message,created_at) VALUES(1,3,'tm_submitted','t','m','x')")
        with self.assertRaises(sqlite3.IntegrityError):   # must point at something
            c.execute("INSERT INTO notifications(user_id,kind,title,message,created_at) VALUES(1,'k','t','m','x')")

    def test_runs_twice_safely(self):
        c = self.conn()
        self.assertTrue(ensure_study_notifications(c))
        self.assertFalse(ensure_study_notifications(c))

    def test_works_inside_an_open_transaction(self):
        # app.init() uses a normal connection, which may already be inside a transaction when this runs
        c = sqlite3.connect(":memory:")
        c.executescript("""CREATE TABLE users(id INTEGER PRIMARY KEY); CREATE TABLE requests(id INTEGER PRIMARY KEY);
        CREATE TABLE notifications(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, request_id INTEGER NOT NULL,
          kind TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT);""")
        c.execute("INSERT INTO users VALUES(1)")            # opens an implicit transaction
        self.assertTrue(c.in_transaction)
        self.assertTrue(ensure_study_notifications(c))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_unexpected_columns_are_not_touched(self):
        c = self.conn(); c.execute("ALTER TABLE notifications ADD COLUMN extra TEXT")
        with self.assertRaises(RuntimeError): ensure_study_notifications(c)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
