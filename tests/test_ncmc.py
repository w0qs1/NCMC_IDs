"""Run with:  python -m unittest discover -s tests -v"""

import contextlib
import csv
import io
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_csv                                  # noqa: E402
import ncmc_common                                 # noqa: E402
import ncmc_db                                     # noqa: E402
import ncmc_lookup                                 # noqa: E402
from ncmc_common import (                          # noqa: E402
    MODES, add_operator, add_station, connect, find_overlaps, init_schema,
    lookup_station, normalize_mode, normalize_pattern, parse_effective_date,
    pattern_matches, suggest_pattern, to_hex, transaction_time,
)


def fresh_db():
    conn = connect(":memory:", must_exist=False)
    init_schema(conn)
    return conn


def quiet(func, *args, **kwargs):
    """Call ``func`` with stdout/stderr swallowed; return (result, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        result = func(*args, **kwargs)
    return result, out.getvalue()


class Scripted(ncmc_lookup.Prompter):
    """Feeds pre-written answers to the prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def ask(self, text, default=""):
        self.prompts.append(text)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {text!r}")
        return self.answers.pop(0).strip() or default


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


# ---------------------------------------------------------------------------

class PatternTests(unittest.TestCase):
    def test_wildcard_examples(self):
        for hit in ("123000", "123001", "123456", "123ABC", "123FFF"):
            self.assertTrue(pattern_matches("123???", hit), hit)
        for miss in ("124001", "223456"):
            self.assertFalse(pattern_matches("123???", miss), miss)

    def test_normalize(self):
        self.assertEqual(normalize_pattern("0x323???"), "323???")
        self.assertEqual(normalize_pattern("323149"), "323149")
        self.assertEqual(normalize_pattern("0x0b0a1c"), "0B0A1C")
        for bad in ("", "12345", "1234567", "12G456", None):
            self.assertIsNone(normalize_pattern(bad), bad)

    def test_x_is_accepted_as_alias_for_question_mark(self):
        self.assertEqual(normalize_pattern("0x323xxx"), "323???")
        self.assertEqual(normalize_pattern("323XXX"), "323???")
        self.assertEqual(normalize_pattern("0XXXXX"), "0?????")       # not a 0x prefix
        self.assertEqual(normalize_pattern("0x0XXXXX"), "0?????")

    def test_suggest_masks_last_three(self):
        self.assertEqual(suggest_pattern("323149"), "323???")

    def test_to_hex_ranges(self):
        self.assertEqual(to_hex(0x323149, 6), "323149")
        self.assertEqual(to_hex("0x0B177D", 6), "0B177D")
        self.assertEqual(to_hex("177D", 4, hex_default=True), "177D")
        self.assertIsNone(to_hex(0x1000000, 6))
        self.assertIsNone(to_hex(-1, 2))
        self.assertIsNone(to_hex(True, 2))

    def test_operator_id_is_acquirer_plus_operator(self):
        self.assertEqual(ncmc_common.join_operator_id("0B", "177D"), "0B177D")
        self.assertEqual(ncmc_common.split_operator_id("0B177D"), ("0B", "177D"))


class ModeTests(unittest.TestCase):
    def test_the_thirteen_modes_in_order(self):
        self.assertEqual(MODES, (
            "BUS", "TRAIN", "TRAM", "METRO", "FERRY", "TICKET_MACHINE",
            "VENDING_MACHINE", "POS", "OTHER", "TROLLEYBUS", "TOLL_ROAD",
            "MONORAIL", "CABLECAR"))

    def test_normalize_mode(self):
        self.assertEqual(normalize_mode("metro"), "METRO")
        self.assertEqual(normalize_mode(" Ticket machine "), "TICKET_MACHINE")
        self.assertEqual(normalize_mode("toll-road"), "TOLL_ROAD")
        self.assertEqual(normalize_mode("1"), "BUS")
        self.assertEqual(normalize_mode("13"), "CABLECAR")
        for bad in ("", "0", "14", "boat", None):
            self.assertIsNone(normalize_mode(bad), bad)

    def test_schema_check_constraint_matches_python_list(self):
        conn = fresh_db()
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'operators'").fetchone()[0]
        self.assertEqual(set(re.findall(r"'([A-Z_]+)'", sql)), set(MODES))

    def test_schema_version_matches(self):
        conn = fresh_db()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         ncmc_common.SCHEMA_VERSION)


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
        add_operator(self.conn, "043630", "Hyderabad Metro", "METRO")

    def test_hierarchy_enforced(self):
        with self.assertRaises(sqlite3.IntegrityError):              # unknown operator
            add_station(self.conn, "099999", "111???", "Nowhere")

    def test_rejects_malformed_operator_ids(self):
        for bad in ("43630", "0436300", "04363G", "04363a"):        # short/long/non-hex/lowercase
            with self.assertRaises(sqlite3.IntegrityError, msg=bad):
                add_operator(self.conn, bad, "Bad", "BUS")

    def test_rejects_malformed_reader_ids(self):
        for bad in ("12345", "32XXXX", "32????x", "12g???", "32?? ?"):
            with self.assertRaises(sqlite3.IntegrityError, msg=bad):
                add_station(self.conn, "043630", bad, "Bad")

    def test_mode_is_validated_in_python_and_in_sql(self):
        with self.assertRaises(ValueError):
            add_operator(self.conn, "010001", "MTC", "BOAT")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO operators VALUES ('010001', 'MTC', 'BOAT')")
        for i, mode in enumerate(MODES):                             # every mode is accepted
            add_operator(self.conn, f"{i + 1:02X}0001", f"Op {mode}", mode.lower())
        self.assertEqual(self.conn.execute("SELECT COUNT(DISTINCT mode) FROM operators").fetchone()[0],
                         len(MODES))

    def test_operator_needs_a_name(self):
        with self.assertRaises(ValueError):
            add_operator(self.conn, "010001", "   ", "BUS")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO operators VALUES ('010001', '  ', 'BUS')")

    def test_duplicate_pattern_rejected_but_duplicate_name_allowed(self):
        add_station(self.conn, "043630", "111???", "Ameerpet")
        add_station(self.conn, "043630", "314???", "Ameerpet")       # interchange
        with self.assertRaises(sqlite3.IntegrityError):
            add_station(self.conn, "043630", "111???", "Other")

    def test_multiple_undecoded_stations_allowed(self):
        add_station(self.conn, "043630", None, "A")
        add_station(self.conn, "043630", None, "B")

    def test_operator_with_stations_cannot_be_deleted(self):
        add_station(self.conn, "043630", "111???", "Ameerpet")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM operators")

    def test_changing_operator_id_cascades_to_stations(self):
        add_station(self.conn, "043630", "111???", "Ameerpet")
        self.conn.execute("UPDATE operators SET id = '043631' WHERE id = '043630'")
        self.assertEqual(self.conn.execute("SELECT operator_id FROM stations").fetchone()[0], "043631")


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        add_operator(self.conn, "043630", "Hyderabad Metro", "METRO")

    def test_exact_overrides_mask(self):
        add_station(self.conn, "043630", "323???", "Raidurg")
        add_station(self.conn, "043630", "323149", "Raidurg Gate 1")
        self.assertEqual(lookup_station(self.conn, "043630", "323149")["stop_name"], "Raidurg Gate 1")
        self.assertEqual(lookup_station(self.conn, "043630", "323FFF")["stop_name"], "Raidurg")
        self.assertIsNone(lookup_station(self.conn, "043630", "324000"))

    def test_lookup_is_per_operator(self):
        add_station(self.conn, "043630", "323???", "Raidurg")
        add_operator(self.conn, "020001", "Bengaluru Metro", "METRO")
        self.assertIsNone(lookup_station(self.conn, "020001", "323149"))

    def test_overlaps(self):
        add_station(self.conn, "043630", "323???", "Raidurg")
        self.assertEqual(len(find_overlaps(self.conn, "043630", "3?????")), 1)
        self.assertEqual(len(find_overlaps(self.conn, "043630", "324???")), 0)


class CsvTests(unittest.TestCase):
    # Exactly the format requested.
    OPERATORS = ("id,name,mode\n"
                 "0x0B177D,Chennai Metro,METRO\n"
                 "0x043630,Hyderabad Metro,METRO\n"
                 "0x020001,Bengaluru Metro,METRO\n"
                 "0x010001,MTC Chennai,BUS\n")
    STATIONS = ("reader_id,stop_name,operator_id\n"
                "0x001140,Airport,0x0B177D\n"
                "0x002140,Meenambakkam,0x0B177D\n"
                "0x003140,OTA - Nanganallur Road,0x0B177D\n"
                "0x126???,Victoria Memorial,0x043630\n"
                "0x127???,L B Nagar,0x043630\n"
                "0x201???,JBS Parade Ground,0x043630\n"
                "0x202???,Secunderabad West,0x043630\n"
                "0x203???,Gandhi Hospital,0x043630\n")

    def write(self, directory, operators=None, stations=None):
        Path(directory, "operators.csv").write_text(operators or self.OPERATORS, encoding="utf-8")
        Path(directory, "stations.csv").write_text(stations or self.STATIONS, encoding="utf-8")

    def test_requested_files_survive_import_then_export_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp, "src"), Path(tmp, "out")
            src.mkdir()
            self.write(src)
            conn = fresh_db()
            stats, _ = quiet(ncmc_db.import_csv, conn, src)
            self.assertEqual((stats["operators"], stats["stations"], stats["warnings"]), (4, 8, []))
            result = export_csv.export(conn, out)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["operators.csv", "stations.csv"])
            self.assertEqual((out / "operators.csv").read_text(), self.OPERATORS)
            self.assertEqual((out / "stations.csv").read_text(), self.STATIONS)
            self.assertEqual((result["skipped_undecoded"], result["skipped_unnamed"]), (0, 0))

    def test_only_two_files_are_written(self):
        conn = fresh_db()
        add_operator(conn, "0B177D", "Chennai Metro", "METRO")
        add_station(conn, "0B177D", "001140", "Airport")
        with tempfile.TemporaryDirectory() as tmp:
            export_csv.export(conn, Path(tmp))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                             ["operators.csv", "stations.csv"])

    def test_unusable_rows_and_comments_are_not_exported(self):
        conn = fresh_db()
        add_operator(conn, "0B177D", "Chennai Metro", "METRO")
        add_station(conn, "0B177D", "001140", "Airport", "? secret maintainer note")
        add_station(conn, "0B177D", None, "Mannadi", "?")            # undecoded
        add_station(conn, "0B177D", "0608D6", "")                    # unnamed
        with tempfile.TemporaryDirectory() as tmp:
            result = export_csv.export(conn, Path(tmp))
            text = Path(tmp, "stations.csv").read_text()
        self.assertEqual(text, "reader_id,stop_name,operator_id\n0x001140,Airport,0x0B177D\n")
        self.assertNotIn("secret", text)
        self.assertEqual((result["stations"], result["skipped_undecoded"], result["skipped_unnamed"]),
                         (1, 1, 1))

    def test_names_with_commas_and_quotes_round_trip(self):
        conn = fresh_db()
        add_operator(conn, "0B177D", 'Chennai, "Metro"', "METRO")
        add_station(conn, "0B177D", "001???", "Anna Nagar, East")
        with tempfile.TemporaryDirectory() as tmp:
            export_csv.export(conn, Path(tmp))
            self.assertEqual(read_rows(Path(tmp, "operators.csv"))[1][1], 'Chennai, "Metro"')
            self.assertEqual(read_rows(Path(tmp, "stations.csv"))[1][1], "Anna Nagar, East")

    def test_export_order_follows_operator_then_insertion(self):
        conn = fresh_db()
        add_operator(conn, "043630", "Hyderabad Metro", "METRO")
        add_operator(conn, "0B177D", "Chennai Metro", "METRO")
        add_station(conn, "0B177D", "001140", "Airport")
        add_station(conn, "043630", "126???", "Victoria Memorial")
        add_station(conn, "0B177D", "002140", "Meenambakkam")
        with tempfile.TemporaryDirectory() as tmp:
            export_csv.export(conn, Path(tmp))
            names = [r[1] for r in read_rows(Path(tmp, "stations.csv"))[1:]]
        self.assertEqual(names, ["Victoria Memorial", "Airport", "Meenambakkam"])

    def test_import_accepts_legacy_x_wildcards_and_bare_hex(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, "id,name,mode\n0B177D,Chennai Metro,metro\n",
                       "reader_id,stop_name,operator_id\n0x123XXX,Test,0B177D\n")
            conn = fresh_db()
            stats, _ = quiet(ncmc_db.import_csv, conn, Path(tmp))
        self.assertEqual(stats["warnings"], [])
        row = conn.execute("SELECT * FROM stations").fetchone()
        self.assertEqual(row["reader_id"], "123???")
        self.assertEqual(conn.execute("SELECT mode FROM operators").fetchone()[0], "METRO")

    def test_bad_rows_are_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(
                tmp,
                self.OPERATORS + "0x0B177D,Duplicate,METRO\n"           # duplicate id
                                 "0xZZ0001,Broken,BUS\n"                # bad id
                                 "0x050001,Kochi Metro,BOAT\n",         # bad mode
                "reader_id,stop_name,operator_id\n"
                "0x001140,A,0x0B177D\n"
                "0x001140,Dup,0x0B177D\n"                              # duplicate reader
                "0xBAD,Bad,0x0B177D\n"                                 # bad reader
                ",Blank,0x0B177D\n"                                    # no reader id
                "0x001140,Orphan,0x999999\n")                          # unknown operator
            conn = fresh_db()
            stats, _ = quiet(ncmc_db.import_csv, conn, Path(tmp))
        self.assertEqual((stats["operators"], stats["stations"]), (4, 1))
        self.assertEqual(len(stats["warnings"]), 3 + 4)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name, "t.db"))
        self.addCleanup(self.tmp.cleanup)
        self.run_cli("init")

    def run_cli(self, *args):
        return quiet(ncmc_db.main, ["--db", self.db, *args])

    def test_full_workflow(self):
        self.run_cli("add-operator", "0x0B177D", "Chennai Metro", "metro")
        self.run_cli("add-station", "0x0B177D", "0x001???", "Airport", "-c", "? estimated")
        self.run_cli("add-station", "chennai", "0x003XXX", "Nanganallur")      # X alias
        self.run_cli("add-station", "0B:177D", "-", "Mannadi")                 # legacy id form
        _, listing = self.run_cli("stations", "chennai")
        self.assertIn("0x001???", listing)
        self.assertIn("0x003???", listing)
        self.assertIn("? estimated", listing)
        _, found = self.run_cli("find", "chennai", "0x001999")
        self.assertIn("Airport", found)

        self.run_cli("edit-operator", "chennai", "--mode", "TRAIN")
        self.run_cli("edit-station", "1", "--name", "Chennai Airport", "--reader-id", "0x001140")
        _, ops = self.run_cli("operators")
        self.assertIn("TRAIN", ops)
        _, found = self.run_cli("find", "0x0B177D", "0x001140")
        self.assertIn("Chennai Airport", found)

    def test_bad_input_is_rejected(self):
        self.run_cli("add-operator", "0x0B177D", "Chennai Metro", "METRO")
        for args in (("add-operator", "0x0B177D", "Again", "BUS"),             # duplicate
                     ("add-operator", "0x123", "Short", "BUS"),                # bad id
                     ("add-operator", "0x0C0001", "Boaty", "BOAT"),            # bad mode
                     ("add-station", "chennai", "0x12", "Bad"),                # bad reader id
                     ("edit-operator", "chennai", "--mode", "BOAT"),
                     ("add-station", "nomatch", "0x001???", "X")):
            with self.assertRaises(SystemExit, msg=args):
                self.run_cli(*args)

    def test_ambiguous_operator_name(self):
        self.run_cli("add-operator", "0x0B177D", "Chennai Metro", "METRO")
        self.run_cli("add-operator", "0x043630", "Hyderabad Metro", "METRO")
        with self.assertRaises(SystemExit):
            self.run_cli("stations", "metro")

    def test_check_flags_ambiguous_equal_specificity(self):
        self.run_cli("add-operator", "0x043630", "Hyderabad Metro", "METRO")
        self.run_cli("add-station", "hyderabad", "1???00", "A")
        self.run_cli("add-station", "hyderabad", "100???", "B")      # both match 100?00
        code, out = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", out)

    def test_old_schema_version_is_refused_with_a_clear_message(self):
        old = str(Path(self.tmp.name, "v1.db"))
        raw = sqlite3.connect(old)
        raw.execute("CREATE TABLE operators (acquirer_id TEXT, operator_id TEXT)")
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
        raw.close()
        with self.assertRaises(SystemExit) as caught:
            quiet(ncmc_db.main, ["--db", old, "operators"])
        self.assertIn("schema version 1", str(caught.exception))


# ---------------------------------------------------------------------------

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
            t(0x05, 0x0042, 0x123456, 4000),                          # new operator 0x050042
        ]}]]}


class PiiFilterTests(unittest.TestCase):
    def test_unlisted_fields_are_never_kept(self):
        dump = ncmc_lookup.sanitize(ncmc_lookup.find_ncmc_application(build_dump()))
        for sentinel in PII_SENTINELS:                               # values must never survive
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

    def test_acquirer_and_operator_combine_into_one_id(self):
        dump = ncmc_lookup.sanitize(ncmc_lookup.find_ncmc_application(build_dump()))
        self.assertEqual(dump.transactions[0].location.operator_id, "043630")
        self.assertEqual(dump.transactions[2].location.operator_id, "050042")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        add_operator(self.conn, "043630", "Hyderabad Metro", "METRO")
        add_station(self.conn, "043630", "301???", "Nagole")
        self.conn.commit()
        self.dump = ncmc_lookup.sanitize(ncmc_lookup.find_ncmc_application(build_dump()))

    def run_flow(self, interactive, prompter=None):
        report, out = quiet(ncmc_lookup.run, self.conn, self.dump, interactive, prompter)
        return report, out

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_history_shows_datetimes_and_no_pii(self):
        _, out = self.run_flow(False)
        self.assertIn("2026-01-02 10:15", out)        # 1440+615 min after 2026-01-01
        self.assertIn("2026-01-02 09:20", out)        # entry, 55 min earlier
        self.assertIn("Nagole", out)                  # entry resolved via 301???
        for sentinel in PII_SENTINELS:
            self.assertNotIn(sentinel, out)

    def test_report_only_mode_never_writes(self):
        report, _ = self.run_flow(False)
        self.assertEqual((self.count("operators"), self.count("stations")), (1, 1))
        self.assertFalse(report.changed)
        self.assertEqual(len(report.unresolved), 3)   # 323149, 323FFF, 123456

    def test_new_operator_prompts_for_mode_then_masked_stations_once_per_station(self):
        prompter = Scripted(
            "Raidurg", "", "",                        # name, accept 323???, no comment
            "Test Metro", "banana", "tram",           # new operator; invalid mode, then a valid one
            "Central Test", "0x123???", "? guess",    # station, own pattern, comment
            "y")                                      # save
        report, out = self.run_flow(True, prompter)
        self.assertEqual(prompter.answers, [])        # nothing left over -> 323FFF not re-asked
        op = self.conn.execute("SELECT * FROM operators WHERE id = '050042'").fetchone()
        self.assertEqual((op["name"], op["mode"]), ("Test Metro", "TRAM"))
        self.assertEqual(report.new_operators, [("050042", "Test Metro", "TRAM")])
        rows = {r["reader_id"]: r for r in self.conn.execute("SELECT * FROM stations")}
        self.assertEqual(rows["323???"]["stop_name"], "Raidurg")
        self.assertEqual(rows["123???"]["comments"], "? guess")
        self.assertIn("Enter a number 1-13", out)     # the invalid answer was rejected
        for sentinel in PII_SENTINELS:
            self.assertNotIn(sentinel, out)

    def test_mode_menu_lists_all_modes_and_accepts_numbers(self):
        prompter = Scripted("13")
        mode, out = quiet(ncmc_lookup.ask_mode, prompter)
        self.assertEqual(mode, "CABLECAR")
        for name in MODES:
            self.assertIn(name, out)
        self.assertIn(" 1. BUS", out)
        self.assertIn("13. CABLECAR", out)

    def test_mode_is_required_no_silent_default(self):
        prompter = Scripted("", "", "metro")           # two empty answers are refused
        mode, _ = quiet(ncmc_lookup.ask_mode, prompter)
        self.assertEqual(mode, "METRO")
        self.assertEqual(len(prompter.prompts), 3)

    def test_skipping_the_operator_name_never_asks_for_a_mode(self):
        prompter = Scripted("", "", "")               # skip 323149, 323FFF, then the operator
        report, _ = self.run_flow(True, prompter)
        self.assertEqual(prompter.answers, [])
        self.assertFalse(any("Select mode" in p for p in prompter.prompts))
        self.assertEqual(self.count("operators"), 1)

    def test_declining_save_rolls_back_the_operator_too(self):
        prompter = Scripted("Raidurg", "", "", "Test Metro", "5", "Central", "", "", "n")
        report, out = self.run_flow(True, prompter)
        self.assertTrue(report.changed)
        self.assertIn("Discarded", out)
        self.assertEqual((self.count("operators"), self.count("stations")), (1, 1))

    def test_skipping_names_adds_nothing(self):
        prompter = Scripted("", "", "")
        report, _ = self.run_flow(True, prompter)
        self.assertFalse(report.changed)
        self.assertEqual((self.count("operators"), self.count("stations")), (1, 1))
        self.assertEqual([r[2] for r in report.unresolved],
                         ["name skipped", "name skipped", "operator skipped"])

    def test_pattern_must_match_observed_reader(self):
        prompter = Scripted("Raidurg", "0x324???", "0x323???", "", "", "y")
        _, out = self.run_flow(True, prompter)
        self.assertIn("does not match the observed 0x323149", out)
        self.assertEqual(lookup_station(self.conn, "043630", "323149")["reader_id"], "323???")

    def test_x_wildcards_typed_by_the_user_are_stored_as_question_marks(self):
        prompter = Scripted("Raidurg", "0x323XXX", "", "", "y")
        self.run_flow(True, prompter)
        self.assertEqual(lookup_station(self.conn, "043630", "323149")["reader_id"], "323???")

    def test_naming_unnamed_exact_id_can_widen_it_to_a_mask(self):
        add_station(self.conn, "043630", "0608D6", "")
        self.conn.commit()
        dump = ncmc_lookup.sanitize({"effectiveDate": "260101", "transactions": [
            dict(acquirerId=4, operatorId=0x3630, terminalId=0x0608D6, minutesElapsed=1)]})
        prompter = Scripted("Baiyappanahalli", "", "y")           # name, accept 060???, save
        quiet(ncmc_lookup.run, self.conn, dump, True, prompter)
        row = lookup_station(self.conn, "043630", "0608FF")
        self.assertEqual((row["stop_name"], row["reader_id"]), ("Baiyappanahalli", "060???"))

    def test_second_run_finds_everything(self):
        prompter = Scripted("Raidurg", "", "", "Test Metro", "metro", "Central Test", "", "", "y")
        self.run_flow(True, prompter)
        report, _ = self.run_flow(False)
        self.assertEqual(report.unresolved, [])

    def test_saved_data_exports_in_the_two_file_format(self):
        prompter = Scripted("Raidurg", "", "", "Test Metro", "bus", "Central Test", "", "", "y")
        self.run_flow(True, prompter)
        with tempfile.TemporaryDirectory() as tmp:
            export_csv.export(self.conn, Path(tmp))
            self.assertEqual(read_rows(Path(tmp, "operators.csv")),
                             [["id", "name", "mode"],
                              ["0x043630", "Hyderabad Metro", "METRO"],
                              ["0x050042", "Test Metro", "BUS"]])
            self.assertEqual(read_rows(Path(tmp, "stations.csv")),
                             [["reader_id", "stop_name", "operator_id"],
                              ["0x301???", "Nagole", "0x043630"],
                              ["0x323???", "Raidurg", "0x043630"],
                              ["0x123???", "Central Test", "0x050042"]])


if __name__ == "__main__":
    unittest.main()
