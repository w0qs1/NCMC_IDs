"""Run with:  python -m unittest discover -s tests -v"""

import contextlib
import csv
import io
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_csv                                  # noqa: E402
import ncmc_db                                     # noqa: E402
import ncmc_lookup                                 # noqa: E402
from ncmc_common import (                          # noqa: E402
    add_operator, add_station, connect, find_overlaps, init_schema,
    lookup_station, normalize_pattern, parse_effective_date, pattern_matches,
    suggest_pattern, to_hex, transaction_time,
)


def fresh_db():
    conn = connect(":memory:", must_exist=False)
    init_schema(conn)
    return conn


class Scripted(ncmc_lookup.Prompter):
    """Feeds pre-written answers to the prompts (and records the prompts)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def ask(self, text, default=""):
        self.prompts.append(text)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {text!r}")
        return self.answers.pop(0).strip() or default


class PatternTests(unittest.TestCase):
    def test_readme_wildcard_examples(self):
        for hit in ("123000", "123001", "123456", "123ABC", "123FFF"):
            self.assertTrue(pattern_matches("123XXX", hit), hit)
        for miss in ("124001", "223456"):
            self.assertFalse(pattern_matches("123XXX", miss), miss)

    def test_normalize(self):
        self.assertEqual(normalize_pattern("0x323xxx"), "323XXX")
        self.assertEqual(normalize_pattern("323149"), "323149")
        self.assertEqual(normalize_pattern("0XXXXX"), "0XXXXX")      # not a 0x prefix
        self.assertEqual(normalize_pattern("0x0XXXXX"), "0XXXXX")
        for bad in ("", "12345", "1234567", "12G456", None):
            self.assertIsNone(normalize_pattern(bad), bad)

    def test_suggest_masks_last_three(self):
        self.assertEqual(suggest_pattern("323149"), "323XXX")

    def test_to_hex_ranges(self):
        self.assertEqual(to_hex(0x323149, 6), "323149")
        self.assertEqual(to_hex("0x0B", 2), "0B")
        self.assertEqual(to_hex("177D", 4, hex_default=True), "177D")
        self.assertIsNone(to_hex(0x1000000, 6))
        self.assertIsNone(to_hex(-1, 2))
        self.assertIsNone(to_hex(True, 2))


class TimeTests(unittest.TestCase):
    def test_epoch_plus_minutes(self):
        epoch = parse_effective_date("260101")
        self.assertEqual(epoch, datetime(2026, 1, 1))
        self.assertEqual(transaction_time(epoch, 100000), datetime(2026, 3, 11, 10, 40))
        self.assertEqual(transaction_time(epoch, 0), datetime(2026, 1, 1))

    def test_bad_input(self):
        self.assertIsNone(parse_effective_date("garbage"))
        self.assertIsNone(parse_effective_date("261345"))            # month 13
        self.assertIsNone(transaction_time(None, 5))
        self.assertIsNone(transaction_time(datetime(2026, 1, 1), -5))
        self.assertEqual(parse_effective_date(260101), datetime(2026, 1, 1))


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        add_operator(self.conn, "04", "3630", "Hyderabad Metro", "hmrl")

    def test_hierarchy_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):              # unknown operator
            add_station(self.conn, "09", "9999", "111XXX", "Nowhere")

    def test_rejects_malformed_ids(self):
        with self.assertRaises(sqlite3.IntegrityError):
            add_operator(self.conn, "4", "3630", "Bad")               # 1 digit
        with self.assertRaises(sqlite3.IntegrityError):
            add_operator(self.conn, "0g", "3630", "Bad")
        with self.assertRaises(sqlite3.IntegrityError):
            add_station(self.conn, "04", "3630", "12345", "Short")
        with self.assertRaises(sqlite3.IntegrityError):
            add_station(self.conn, "04", "3630", "32xxxx", "Lowercase")

    def test_duplicate_pattern_rejected_but_duplicate_name_allowed(self):
        add_station(self.conn, "04", "3630", "111XXX", "Ameerpet")
        add_station(self.conn, "04", "3630", "314XXX", "Ameerpet")   # interchange
        with self.assertRaises(sqlite3.IntegrityError):
            add_station(self.conn, "04", "3630", "111XXX", "Other")

    def test_multiple_undecoded_stations_allowed(self):
        add_station(self.conn, "04", "3630", None, "A")
        add_station(self.conn, "04", "3630", None, "B")

    def test_station_file_rules(self):
        with self.assertRaises(ValueError):
            add_operator(self.conn, "01", "0001", "X", "../evil")
        with self.assertRaises(ValueError):
            add_operator(self.conn, "01", "0001", "X", "operators")
        with self.assertRaises(sqlite3.IntegrityError):               # NOCASE unique
            add_operator(self.conn, "01", "0001", "X", "HMRL.csv")

    def test_operator_with_stations_cannot_be_deleted(self):
        add_station(self.conn, "04", "3630", "111XXX", "Ameerpet")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM operators")


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        add_operator(self.conn, "04", "3630", "Hyderabad Metro", "hmrl")

    def test_exact_overrides_mask(self):
        add_station(self.conn, "04", "3630", "323XXX", "Raidurg")
        add_station(self.conn, "04", "3630", "323149", "Raidurg Gate 1")
        self.assertEqual(lookup_station(self.conn, "04", "3630", "323149")["name"], "Raidurg Gate 1")
        self.assertEqual(lookup_station(self.conn, "04", "3630", "323FFF")["name"], "Raidurg")
        self.assertIsNone(lookup_station(self.conn, "04", "3630", "324000"))

    def test_lookup_is_per_operator(self):
        add_station(self.conn, "04", "3630", "323XXX", "Raidurg")
        add_operator(self.conn, "02", "0001", "Bengaluru Metro", "bmrcl")
        self.assertIsNone(lookup_station(self.conn, "02", "0001", "323149"))

    def test_overlaps(self):
        add_station(self.conn, "04", "3630", "323XXX", "Raidurg")
        self.assertEqual(len(find_overlaps(self.conn, "04", "3630", "3XXXXX")), 1)
        self.assertEqual(len(find_overlaps(self.conn, "04", "3630", "324XXX")), 0)

    def test_check_flags_ambiguous_equal_specificity(self):
        add_station(self.conn, "04", "3630", "1XXX00", "A")
        add_station(self.conn, "04", "3630", "100XXX", "B")          # both overlap at 100X00
        self.conn.commit()                       # backup() needs no open transaction
        db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db.close()
        disk = sqlite3.connect(db.name)
        self.conn.backup(disk)
        disk.close()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = ncmc_db.main(["--db", db.name, "check"])
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", out.getvalue())
        Path(db.name).unlink()


class CsvRoundTripTests(unittest.TestCase):
    OPERATORS = "acquirer_id,operator_id,operator_name,terminal_info\r\n0x0B,0x177D,Chennai Metro,cmrl.csv\r\n"
    STATIONS = ("\ufeffterminal_id,station_name,comments\r\n"
                "0x001140,Airport,\r\n"
                ",Mannadi,?\r\n"                                      # undecoded ID
                "0x004140,Alandur,? Name shortened\r\n"
                "0x012140,Alandur,\"? a, comma\"\r\n"                 # interchange + quoting
                "0x0608d6,,\r\n")                                     # lowercase, unnamed

    def test_import_then_export_is_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp, "src"), Path(tmp, "out")
            src.mkdir()
            (src / "operators.csv").write_bytes(self.OPERATORS.encode())
            (src / "cmrl.csv").write_bytes(self.STATIONS.encode("utf-8"))

            conn = fresh_db()
            with contextlib.redirect_stdout(io.StringIO()):
                stats = ncmc_db.import_csv(conn, src)
            self.assertEqual((stats["operators"], stats["stations"]), (1, 5))
            self.assertEqual(stats["warnings"], [])
            export_csv.export(conn, out)

            def rows(path):
                with open(path, encoding="utf-8-sig", newline="") as fh:
                    return list(csv.reader(fh))
            expected = rows(src / "cmrl.csv")
            expected[-1][0] = "0x0608D6"                              # normalised to uppercase
            self.assertEqual(rows(out / "cmrl.csv"), expected)
            self.assertEqual(rows(out / "operators.csv"), rows(src / "operators.csv"))

    def test_bad_rows_are_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "operators.csv").write_text(self.OPERATORS + "zz,0x0001,Broken,b.csv\n")
            (src / "cmrl.csv").write_text(
                "terminal_id,station_name,comments\n0x001140,A,\n0x001140,Dup,\n0xBAD,Bad,\n")
            conn = fresh_db()
            with contextlib.redirect_stdout(io.StringIO()):
                stats = ncmc_db.import_csv(conn, src)
            self.assertEqual(stats["stations"], 1)
            self.assertEqual(len(stats["warnings"]), 3)


PII_SENTINELS = ("SENTINEL-CARDNO", "SENTINEL-HOLDER", "SENTINEL-UID", "SENTINEL-BALANCE")


def build_dump():
    def t(a, o, term, minutes, **extra):
        return dict(acquirerId=a, operatorId=o, terminalId=term, minutesElapsed=minutes, **extra)
    return {"applications": [["ncmc", {
        "effectiveDate": "260101",
        "cardNumber": PII_SENTINELS[0],
        "holderName": PII_SENTINELS[1],
        "transactions": [
            t(0x04, 0x3630, 0x323149, 1440 + 615, transactionSequence=7, amountUnits=30,
              statusCode=1, rfu=0, balanceUnits=PII_SENTINELS[3], cardUid=PII_SENTINELS[2],
              entry=t(0x04, 0x3630, 0x301001, 1440 + 560)),
            t(0x04, 0x3630, 0x323FFF, 3000),                          # same station, other gate
            t(0x05, 0x0042, 0x123456, 4000),                          # new operator
        ]}]]}


class PiiFilterTests(unittest.TestCase):
    def test_unlisted_fields_are_never_kept(self):
        app = ncmc_lookup.find_ncmc_application(build_dump())
        dump = ncmc_lookup.sanitize(app)
        for sentinel in PII_SENTINELS:          # values must never survive
            self.assertNotIn(sentinel, repr(dump))
        self.assertEqual(set(dump.ignored_app_keys), {"cardNumber", "holderName"})
        self.assertEqual(set(dump.ignored_txn_keys), {"balanceUnits", "cardUid"})

    def test_nested_values_in_allowed_fields_are_dropped(self):
        app = {"effectiveDate": "260101",
               "transactions": [{"acquirerId": 1, "operatorId": 1, "terminalId": 1,
                                 "amountUnits": {"holder": "SENTINEL"}}]}
        self.assertNotIn("SENTINEL", repr(ncmc_lookup.sanitize(app)))

    def test_finds_app_in_dict_and_list_layouts(self):
        inner = {"effectiveDate": "260101"}
        self.assertIs(ncmc_lookup.find_ncmc_application({"x": [["ncmc", inner]]}), inner)
        self.assertIs(ncmc_lookup.find_ncmc_application({"apps": {"ncmc": inner}}), inner)
        self.assertIsNone(ncmc_lookup.find_ncmc_application({"apps": [["other", {}]]}))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        add_operator(self.conn, "04", "3630", "Hyderabad Metro", "hmrl")
        add_station(self.conn, "04", "3630", "301XXX", "Nagole")
        self.conn.commit()
        self.dump = ncmc_lookup.sanitize(
            ncmc_lookup.find_ncmc_application(build_dump()))

    def run_flow(self, interactive, prompter=None):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report = ncmc_lookup.run(self.conn, self.dump, interactive, prompter)
        return report, buffer.getvalue()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_history_shows_datetimes_and_no_pii(self):
        _, out = self.run_flow(False)
        self.assertIn("2026-01-02 10:15", out)        # 1440+615 min after 2026-01-01
        self.assertIn("2026-01-02 09:20", out)        # entry, 55 min earlier
        self.assertIn("Nagole", out)                  # entry resolved via 301XXX
        for sentinel in PII_SENTINELS:
            self.assertNotIn(sentinel, out)

    def test_report_only_mode_never_writes(self):
        report, out = self.run_flow(False)
        self.assertEqual((self.count("operators"), self.count("stations")), (1, 1))
        self.assertFalse(report.changed)
        self.assertEqual(len(report.unresolved), 3)   # 323149, 323FFF, 123456

    def test_prompts_add_operator_and_masked_stations_once_per_station(self):
        prompter = Scripted(
            "Raidurg", "", "",                        # name, accept 323XXX, no comment
            "Test Metro", "testmetro",                # new operator + file
            "Central Test", "0x123XXX", "? guess",    # station, own pattern, comment
            "y")                                      # save
        report, out = self.run_flow(True, prompter)
        self.assertEqual(prompter.answers, [])        # nothing left over -> 323FFF not re-asked
        self.assertEqual(self.count("operators"), 2)
        rows = {r["terminal_pattern"]: r for r in self.conn.execute("SELECT * FROM stations")}
        self.assertEqual(rows["323XXX"]["name"], "Raidurg")
        self.assertEqual(rows["123XXX"]["comments"], "? guess")
        self.assertEqual(self.conn.execute(
            "SELECT station_file FROM operators WHERE operator_id = '0042'").fetchone()[0],
            "testmetro.csv")
        for sentinel in PII_SENTINELS:
            self.assertNotIn(sentinel, out)

    def test_declining_save_rolls_back(self):
        # 323149: name, accept mask, no comment.  Operator 05/0042: skipped.  Save? no.
        prompter = Scripted("Raidurg", "", "", "", "n")
        report, out = self.run_flow(True, prompter)
        self.assertTrue(report.changed)
        self.assertIn("Discarded", out)
        self.assertEqual(self.count("stations"), 1)   # Raidurg was rolled back

    def test_skipping_names_adds_nothing(self):
        # 323149 skipped, 323FFF (not covered by anything) skipped, operator skipped.
        prompter = Scripted("", "", "")
        report, _ = self.run_flow(True, prompter)
        self.assertFalse(report.changed)
        self.assertEqual((self.count("operators"), self.count("stations")), (1, 1))
        self.assertEqual([r[3] for r in report.unresolved],
                         ["name skipped", "name skipped", "operator skipped"])

    def test_pattern_must_match_observed_terminal(self):
        prompter = Scripted("Raidurg", "0x324XXX", "0x323XXX", "", "", "y")
        _, out = self.run_flow(True, prompter)
        self.assertIn("does not match the observed 0x323149", out)
        self.assertEqual(lookup_station(self.conn, "04", "3630", "323149")["terminal_pattern"],
                         "323XXX")

    def test_naming_unnamed_exact_id_can_widen_it_to_a_mask(self):
        add_station(self.conn, "04", "3630", "0608D6", "")
        self.conn.commit()
        dump = ncmc_lookup.sanitize({"effectiveDate": "260101", "transactions": [
            dict(acquirerId=4, operatorId=0x3630, terminalId=0x0608D6, minutesElapsed=1)]})
        prompter = Scripted("Baiyappanahalli", "", "y")           # name, accept 060XXX, save
        with contextlib.redirect_stdout(io.StringIO()):
            ncmc_lookup.run(self.conn, dump, True, prompter)
        row = lookup_station(self.conn, "04", "3630", "0608FF")
        self.assertEqual((row["name"], row["terminal_pattern"]), ("Baiyappanahalli", "060XXX"))

    def test_second_run_finds_everything(self):
        prompter = Scripted("Raidurg", "", "", "Test Metro", "", "Central Test", "", "", "y")
        self.run_flow(True, prompter)
        report, _ = self.run_flow(False)
        self.assertEqual(report.unresolved, [])


if __name__ == "__main__":
    unittest.main()
