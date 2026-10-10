"""Phase 3, pass 1: the spreadsheet intake (a client's own CSV/Excel file).

Adversarial tests for Agent 1's upload path, in the same spirit as the rest of the
suite: every claim about the intake is checked against the engine's own run, and every
fail-closed rule is asserted with the exact token it is supposed to produce.

The contract being tested:
  * ONE translator (the documented alias table) and ONE normalizer - a file handed over
    by a human goes through exactly the calls a live vendor export goes through;
  * PREVIEW writes nothing; COMMIT writes only against an immutable ingestion_batches
    record that matches the previewed state;
  * nothing is coerced, invented or silently overwritten.
"""
from __future__ import annotations

import copy
import io
import json
import sqlite3
import unittest

from engine import config, ingest
from engine import db as dbmod
from tests.base import EngineTestCase

CSV_HEADERS = "Cust_ID,Cust_Name,AmountDue,Inv_Date,Email,Cust_Phone"
CSV_GOOD = ("Cust_ID,Cust_Name,AmountDue,Inv_Date,Email,Cust_Phone\n"
            "AC-1001,Cedar Lane Design Studio,4820.50,2026-06-15,ap@cedarlane.example,(212) 555-0143\n"
            "AC-1002,Northwind Freight LLC,12340.00,2026-06-16,ap@northwind.example,(303) 555-0144\n")


def make_xlsx(rows, headers=CSV_HEADERS.split(",")):
    """A real .xlsx workbook, written with openpyxl and handed over as bytes."""
    openpyxl = __import__("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(list(headers))
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class SpreadsheetIntakeTestCase(EngineTestCase):
    """Fixtures and helpers shared by the intake tests."""

    def preview(self, body="", *, headers=CSV_HEADERS, file_name="aged.csv",
                client_id="CLI-TEST", uploader_id="dana@cedar.example", content=None):
        payload = content if content is not None else f"{headers}\n{body}"
        return self.engine.preview_spreadsheet(file_name=file_name, content=payload,
                                               client_id=client_id, uploader_id=uploader_id)

    def registered(self, preview, **kwargs):
        self.sign_off()
        options = {"uploader_id": "dana@cedar.example", "warranty_acknowledged": True,
                   "reason": "onboarding past-due file"}
        options.update(kwargs)
        return self.engine.register_ingestion_batch(preview, **options)

    def registered_and_committed(self, body=CSV_GOOD.split("\n", 1)[1]):
        preview = self.preview(body)
        batch = self.registered(preview)
        self.assertTrue(batch["ok"], batch)
        result = self.engine.commit_spreadsheet(preview, batch_id=batch["batch_id"],
                                                uploader_id="dana@cedar.example")
        return preview, batch, result

    def dropped_tokens(self, preview):
        return {d["row_index"]: d["reason_token"] for d in preview["dropped"]}


# ---------------------------------------------------------------------------
# 2. the column-alias translation matrix
# ---------------------------------------------------------------------------
class TestColumnAliasMatrix(SpreadsheetIntakeTestCase):
    def test_every_documented_alias_maps_to_its_target_field(self):
        aliases = config.load_aliases()["target_fields"]
        self.assertTrue(aliases, "the alias table is the translator; it cannot be empty")
        for target, names in aliases.items():
            for name in names:
                with self.subTest(alias=name, target=target):
                    plan = ingest.build_mapping([name], [1])
                    self.assertEqual(plan["rows"][0]["target_field"], target,
                                     f"{name!r} is documented as {target!r}")
                    self.assertTrue(plan["rows"][0]["mapped"])
        # and every target field the alias table names is accepted under its own name, as
        # the alias index has always done for the live path
        for target in list(aliases) + ingest.OPTIONAL_TARGETS:
            with self.subTest(target=target):
                self.assertEqual(ingest.build_mapping([target], ["x"])["rows"][0]["target_field"], target)
        # aging_state is DERIVED from invoice_date, not translated from a column, so a
        # source column of that name is reported unmapped rather than trusted
        aging = ingest.build_mapping(["aging_state"], ["90"])["rows"][0]
        self.assertIsNone(aging["target_field"])
        self.assertFalse(aging["mapped"])

    def test_the_documented_names_in_the_brief_all_translate(self):
        wanted = {"Cust_Name": "name", "AmountDue": "balance_owed", "Client": "client_id",
                  "Balance": "balance_owed", "Outstanding": "balance_owed",
                  "Customer Name": "name", "Amount Due": "balance_owed",
                  "Outstanding Balance": "balance_owed", "Invoice Date": "invoice_date",
                  "Email": "email", "Phone": "phone"}
        for source, target in wanted.items():
            with self.subTest(column=source):
                self.assertEqual(ingest.build_mapping([source], [1])["rows"][0]["target_field"], target)

    def test_one_mapper_the_preview_agrees_with_the_normalizer(self):
        """The mapping report and the normalizer must not drift apart: for every column
        the report says is mapped, `normalize()` must read that same target field."""
        headers = ["Cust_ID", "Cust_Name", "AmountDue", "Inv_Date", "Cust_Phone", "Terms"]
        plan = ingest.build_mapping(headers, ["AC-1", "Cedar Lane", "10.00", "2026-06-15", "", ""])
        mapping = {row["source_column"]: row["target_field"] for row in plan["rows"]}
        self.assertIsNone(mapping["Terms"])
        result = ingest.normalize([{"Cust_ID": "AC-1", "Cust_Name": "Cedar Lane", "AmountDue": "10.00",
                                    "Inv_Date": "2026-06-15", "Cust_Phone": "",
                                    "Terms": "Net 30"}],
                                  source_system="Test", endpoint="/x", client_id_hint="CLI-TEST")
        record = result.records[0]
        self.assertEqual(json.loads(record["source_columns"])["Cust_ID"], "debtor_id")
        self.assertEqual(json.loads(record["source_columns"])["Cust_Name"], "name")
        self.assertEqual(record["debtor_id"], "AC-1")
        self.assertEqual(record["name"], "Cedar Lane")

    def test_an_unmapped_column_is_reported_and_never_guessed_into_a_field(self):
        preview = self.preview("AC-1,Cedar Lane Design Studio,4820.50,2026-06-15,a@x.example,,Net 30",
                               headers=f"{CSV_HEADERS},Terms")
        mapping = {row["source_column"]: row["target_field"] for row in preview["mapping"]["rows"]}
        self.assertIsNone(mapping["Terms"], "an unknown column is not guessed at")
        self.assertIn("Terms", preview["mapping"]["unmapped_columns"])
        self.assertEqual(preview["mapping"]["unmapped_token"], "UNMAPPED_COLUMN")
        # ...and the row still imports: an unmapped trailing column is not a failure
        self.assertEqual(preview["totals"]["rows_accepted"], 1)
        self.assertEqual(preview["rows"][0]["normalized"]["debtor_id"], "AC-1")

    def test_two_columns_claiming_one_target_is_a_mapping_error_not_an_overwrite(self):
        headers = "Cust_ID,Cust_Name,Customer Name,AmountDue,Inv_Date"
        preview = self.preview("AC-1,First Name,Second Name,4820.50,2026-06-15", headers=headers)
        self.assertFalse(preview["preview_ok"])
        self.assertEqual(preview["refusal_token"], "BLOCKED_MAPPING_ERROR")
        self.assertEqual(preview["mapping"]["mapping_ok"], False)
        self.assertEqual(preview["mapping"]["mapping_error_token"], "DUPLICATE_TARGET_FIELD")
        self.assertEqual(preview["mapping"]["duplicate_targets"],
                         [{"target_field": "name", "source_columns": ["Cust_Name", "Customer Name"]}])
        # surfaced, not silently resolved: neither a register nor a commit may proceed
        self.assertEqual(self.engine.register_ingestion_batch(preview, uploader_id="u",
                                                              warranty_acknowledged=True)["refusal_token"],
                         "BLOCKED_MAPPING_ERROR")
        self.assertEqual(self.engine.commit_spreadsheet(preview, batch_id="anything")["refusal_token"],
                         "BLOCKED_MAPPING_ERROR")


# ---------------------------------------------------------------------------
# 1. file parsing: CSV (stdlib) and .xlsx (openpyxl)
# ---------------------------------------------------------------------------
class TestFileParsing(SpreadsheetIntakeTestCase):
    def test_csv_path_previews_and_commits(self):
        preview, batch, result = self.registered_and_committed()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rows_written"], 2)
        self.assertEqual(sorted(result["committed_debtor_ids"]), ["AC-1001", "AC-1002"])
        self.assertEqual(self.engine.debtor("AC-1001")["name"], "Cedar Lane Design Studio")

    def test_xlsx_path_reads_a_binary_workbook_through_openpyxl(self):
        data = make_xlsx([["AC-2001", "Sable & Rook Studio", "2210.00", "2026-06-10",
                           "ap@sable.example", "(415) 555-0170"],
                          ["AC-2002", "Kestrel Media Group", "980.00", "2026-06-11",
                           "ap@kestrel.example", "(415) 555-0171"]])
        preview = self.engine.preview_spreadsheet(file_name="aged.xlsx", content=data,
                                                  client_id="CLI-TEST", uploader_id="dana@cedar.example")
        self.assertTrue(preview["preview_ok"], preview.get("refusal_token"))
        self.assertEqual(preview["source_format"], "xlsx")
        self.assertEqual(preview["totals"], {"rows_seen": 2, "rows_accepted": 2, "rows_rejected": 0})
        batch = self.registered(preview)
        result = self.engine.commit_spreadsheet(preview, batch_id=batch["batch_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.engine.debtor("AC-2002")["balance_owed"], 980.00)

    def test_xlsx_dates_and_numbers_come_back_typed(self):
        """An Excel date cell is a date object, not a string: it must land as the same
        ISO date the CSV path produces, and a formula-free numeric cell as a number."""
        import datetime as dt
        openpyxl = __import__("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Cust_ID", "Cust_Name", "AmountDue", "Inv_Date"])
        sheet.append(["AC-3001", "Harbour Lights Agency", 1500.5, dt.datetime(2026, 6, 15)])
        buffer = io.BytesIO()
        workbook.save(buffer)
        preview = self.engine.preview_spreadsheet(file_name="dates.xlsx", content=buffer.getvalue(),
                                                  client_id="CLI-TEST", uploader_id="u")
        row = preview["rows"][0]["normalized"]
        self.assertEqual(row["invoice_date"], "2026-06-15")
        self.assertEqual(row["balance_owed"], 1500.5)
        self.assertEqual(row["aging_state"], "90")

    def test_a_file_type_we_do_not_accept_is_refused_rather_than_guessed(self):
        preview = self.engine.preview_spreadsheet(file_name="aged.pdf", content=b"%PDF-1.4 junk",
                                                  client_id="CLI-TEST", uploader_id="u")
        self.assertFalse(preview["preview_ok"])
        self.assertEqual(preview["refusal_token"], "UNSUPPORTED_FILE_TYPE")
        self.assertEqual(preview["totals"], {"rows_seen": 0, "rows_accepted": 0, "rows_rejected": 0})
        self.assertEqual(preview["accepted_rows"], [])
        self.assertEqual(preview["writes"], 0)
        self.assertEqual(len(self.engine.store.query("SELECT * FROM debtor_records")), 0)

    def test_a_row_with_the_wrong_number_of_cells_is_dropped_not_padded(self):
        preview = self.preview("AC-1,Cedar Lane Design Studio,4820.50,2026-06-15")
        self.assertEqual(self.dropped_tokens(preview)[1], ingest.DROP_ROW_SHAPE)
        self.assertEqual(preview["totals"]["rows_accepted"], 0)
        # the other direction: extra NON-empty cells are a misalignment, not a pad
        too_wide = self.preview("AC-1,Cedar Lane Design Studio,4820.50,2026-06-15,a@x.example,"
                                "(212) 555-0143,SURPRISE")
        self.assertEqual(too_wide["totals"]["rows_accepted"], 0)
        self.assertEqual(too_wide["rows"][0]["reason_token"], ingest.DROP_ROW_SHAPE)


# ---------------------------------------------------------------------------
# 3. fail-closed validation
# ---------------------------------------------------------------------------
class TestFailClosedValidation(SpreadsheetIntakeTestCase):
    def test_unparseable_date_drops_the_row_with_its_token(self):
        preview = self.preview("AC-1,Cedar Lane Design Studio,4820.50,not-a-date,a@x.example,(212) 555-0143\n"
                               "AC-2,Northwind Freight LLC,900.00,2026-06-16,b@x.example,(303) 555-0144")
        self.assertEqual(self.dropped_tokens(preview)[1], "UNPARSEABLE_DATE")
        self.assertEqual(preview["dropped"][0]["column"], "invoice_date")
        self.assertEqual(preview["dropped"][0]["value"], "not-a-date")
        self.assertEqual(preview["totals"], {"rows_seen": 2, "rows_accepted": 1, "rows_rejected": 1})

    def test_text_in_the_amount_drops_the_row_with_its_token(self):
        for value in ("pending", "12abc", '"1.234,56"', "N/A", '"$1,2x4"'):
            with self.subTest(amount=value):
                preview = self.preview(f"AC-1,Cedar Lane Design Studio,{value},2026-06-15,a@x.example,(212) 555-0143")
                self.assertEqual(self.dropped_tokens(preview)[1], "AMOUNT_NOT_NUMERIC")
                self.assertEqual(preview["totals"]["rows_accepted"], 0)

    def test_a_blank_amount_drops_the_row_as_missing_rather_than_zero(self):
        preview = self.preview("AC-1,Cedar Lane Design Studio,,2026-06-15,a@x.example,(212) 555-0143")
        self.assertEqual(self.dropped_tokens(preview)[1], "MISSING_BALANCE")

    def test_a_blank_identifier_drops_the_row(self):
        preview = self.preview(",Cedar Lane Design Studio,4820.50,2026-06-15,a@x.example,(212) 555-0143")
        self.assertEqual(self.dropped_tokens(preview)[1], "MISSING_DEBTOR_ID")

    def test_zero_and_credit_balances_are_blocked_with_the_exact_token(self):
        preview = self.preview("AC-1,Cedar Lane Design Studio,0.00,2026-06-15,a@x.example,(212) 555-0143\n"
                               "AC-2,Northwind Freight LLC,(320.75),2026-06-16,b@x.example,(303) 555-0144\n"
                               "AC-3,Sable & Rook Studio,0,2026-06-17,c@x.example,(415) 555-0170")
        tokens = self.dropped_tokens(preview)
        self.assertEqual(tokens[1], "BLOCKED_NO_BALANCE_DUE")
        self.assertEqual(tokens[2], "BLOCKED_NO_BALANCE_DUE")
        self.assertEqual(tokens[3], "BLOCKED_NO_BALANCE_DUE")
        self.assertEqual(preview["totals"]["rows_accepted"], 0)
        self.assertEqual(preview["totals"]["rows_rejected"], 3)

    def test_a_parse_failure_answers_before_the_balance_policy(self):
        """One token per row, and the documented order decides which one: a row that is
        both unparseable and a credit is reported as a parse failure."""
        preview = self.preview("AC-1,Cedar Lane Design Studio,nope,2026-06-15,a@x.example,(212) 555-0143")
        self.assertEqual(self.dropped_tokens(preview)[1], "AMOUNT_NOT_NUMERIC")

    def test_missing_phone_or_email_is_flagged_never_invented(self):
        preview, batch, result = self.registered_and_committed()
        self.assertTrue(result["ok"])
        tagged = {item["row_index"]: item["flags"] for item in preview["rows"]}
        self.assertEqual(tagged[1], [])
        self.assertEqual(tagged[2], [])

        flagged = self.preview("AC-1,Cedar Lane Design Studio,4820.50,2026-06-15,,")
        self.assertEqual(flagged["rows"][0]["flags"], ["MISSING_PHONE", "MISSING_EMAIL"])
        self.assertEqual(flagged["totals"]["rows_accepted"], 1, "a missing phone/email never drops the row")
        batch = self.registered(flagged)
        self.engine.commit_spreadsheet(flagged, batch_id=batch["batch_id"])
        row = self.engine.debtor("AC-1")
        self.assertIsNone(row["phone"], "a missing phone is stored as missing, never invented")
        self.assertIsNone(row["email"])
        self.assertEqual(row["window_decision"], "Unknown_Area_Code_Freeze",
                         "and the missing number fails closed at the calling window")

    def test_a_missing_invoice_date_imports_as_unknown_and_is_flagged(self):
        preview = self.preview("AC-1,Cedar Lane Design Studio,4820.50,,a@x.example,(212) 555-0143")
        self.assertEqual(preview["rows"][0]["flags"], ["MISSING_INVOICE_DATE"],
                         "a date we were not given is flagged, not rejected and not invented")
        self.assertEqual(preview["totals"]["rows_accepted"], 1)
        self.assertEqual(preview["rows"][0]["normalized"]["invoice_date"], None)
        self.assertEqual(preview["rows"][0]["normalized"]["aging_state"], "unknown")

    def test_aging_is_derived_from_the_uploaded_date_exactly_as_the_live_path_does(self):
        preview, _batch, _result = self.registered_and_committed()
        self.assertEqual(preview["rows"][0]["normalized"]["aging_state"], "90")
        self.assertEqual(preview["rows"][1]["normalized"]["aging_state"], "90")


# ---------------------------------------------------------------------------
# 4. the consumer-debt scanner (advisory only)
# ---------------------------------------------------------------------------
class TestConsumerDebtScanner(SpreadsheetIntakeTestCase):
    WARNING = "Warning: Data footprint resembles consumer debt. RecoverAI operates strictly on B2B commercial trade debt."

    def test_individual_person_names_trip_the_marker(self):
        preview = self.preview("AC-1,Marcus Whitfield,4820.50,2026-06-15,m@x.example,(212) 555-0143\n"
                               "AC-2,Dana Reed,1500.00,2026-06-16,d@x.example,(212) 555-0144")
        self.assertTrue(preview["consumer_debt"]["consumer_debt_marker"])
        self.assertIn("individual_name_share", preview["consumer_debt"]["signals"])
        self.assertEqual(preview["consumer_debt"]["rows_with_individual_name"], [1, 2])

    def test_balances_under_100_trip_the_marker(self):
        preview = self.preview("AC-1,Cedar Lane Design Studio,42.50,2026-06-15,a@x.example,(212) 555-0143\n"
                               "AC-2,Northwind Freight LLC,99.99,2026-06-16,b@x.example,(303) 555-0144")
        self.assertTrue(preview["consumer_debt"]["consumer_debt_marker"])
        self.assertIn("sub_100_balance_share", preview["consumer_debt"]["signals"])
        self.assertEqual(preview["consumer_debt"]["rows_under_100"], [1, 2])

    def test_business_names_at_or_above_100_do_not_trip_the_marker(self):
        preview = self.preview("AC-1,Cedar Lane Design Studio,4820.50,2026-06-15,a@x.example,(212) 555-0143\n"
                               "AC-2,Northwind Freight LLC,12340.00,2026-06-16,b@x.example,(303) 555-0144\n"
                               "AC-3,Sable & Rook Studio,100.00,2026-06-17,c@x.example,(415) 555-0170")
        self.assertFalse(preview["consumer_debt"]["consumer_debt_marker"])
        self.assertEqual(preview["consumer_debt"]["signals"], [])
        self.assertIsNone(preview["consumer_debt"]["warning"])
        self.assertEqual(preview["totals"]["rows_accepted"], 3)

    def test_the_warning_is_byte_for_byte_what_the_frontend_renders(self):
        preview = self.preview("AC-1,Marcus Whitfield,42.50,2026-06-15,m@x.example,(212) 555-0143")
        self.assertEqual(preview["consumer_debt"]["warning"], self.WARNING)
        self.assertEqual(preview["consumer_debt"]["warning"],
                         config.consumer_debt_scan_cfg()["warning_text"])
        self.assertEqual("Warning: Data footprint resembles consumer debt. "
                         "RecoverAI operates strictly on B2B commercial trade debt.",
                         preview["consumer_debt"]["warning"])

    def test_the_scanner_never_blocks_rejects_or_alters_a_row(self):
        rows = "".join(
            f"AC-{n},Marcus Whitfield,{n}.50,2026-06-1{n},m{n}@x.example,(212) 555-014{n}\n"
            for n in range(1, 5))
        preview = self.preview(rows)
        self.assertTrue(preview["consumer_debt"]["consumer_debt_marker"])
        self.assertEqual(preview["totals"]["rows_seen"], 4)
        self.assertEqual(preview["totals"]["rows_accepted"], 4, "a marker blocks nothing")
        self.assertEqual(preview["totals"]["rows_rejected"], 0)
        self.assertTrue(preview["consumer_debt"]["blocks_nothing"])
        batch = self.registered(preview)
        result = self.engine.commit_spreadsheet(preview, batch_id=batch["batch_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rows_written"], 4)
        self.assertEqual(len(self.engine.store.query("SELECT * FROM debtor_records")), 4)

    def test_the_entity_identifier_list_is_the_documented_one(self):
        cfg = config.consumer_debt_scan_cfg()
        for name in ("Northwind Freight LLC", "Cedar Lane Design Studio", "Sable & Rook Studio",
                     "Kestrel Media Group", "Halcyon Design Co", "Orbit Labs Inc",
                     "Bright Partners LLP", "Vantage Designs"):
            with self.subTest(name=name):
                self.assertFalse(ingest.looks_individual(name), f"{name} reads as an entity")
        self.assertIn("llc", cfg["entity_identifiers"])
        self.assertIn("studio", cfg["entity_identifiers"])
        self.assertIn("designs", cfg["entity_identifiers"])
        self.assertIn("media", cfg["entity_identifiers"])
        for name in ("Marcus Whitfield", "Dana Reed", "Ana-Maria Costa", "Whitfield, Marcus"):
            with self.subTest(name=name):
                self.assertTrue(ingest.looks_individual(name), f"{name} reads as an individual")


# ---------------------------------------------------------------------------
# 5. PREVIEW writes nothing / COMMIT needs the record
# ---------------------------------------------------------------------------
class TestPreviewAndCommit(SpreadsheetIntakeTestCase):
    def test_preview_inserts_nothing_into_the_working_queue_or_the_store(self):
        before = {table: len(self.engine.store.query(f"SELECT * FROM {table}"))
                  for table in ("debtor_records", "ingestion_batches", "outbound_contact_logs")}
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        self.assertEqual(preview["writes"], 0)
        self.assertEqual(preview["totals"], {"rows_seen": 2, "rows_accepted": 2, "rows_rejected": 0})
        after = {table: len(self.engine.store.query(f"SELECT * FROM {table}"))
                 for table in ("debtor_records", "ingestion_batches", "outbound_contact_logs")}
        self.assertEqual(before, after, "PREVIEW must not write anything")
        # twice for good measure: preview is idempotent and side-effect free
        self.preview(CSV_GOOD.split("\n", 1)[1])
        self.assertEqual(len(self.engine.store.query("SELECT * FROM debtor_records")), 0)

    def test_the_preview_payload_is_frontend_ready(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1].rstrip("\n")
                               + "\nAC-1003,Globex Media Inc,pending,2026-06-17,x@x.example,")
        self.assertIn("plain_english", preview)
        self.assertEqual(preview["mapping"]["target_fields"], ingest.TARGET_FIELDS)
        self.assertEqual({row["source_column"] for row in preview["mapping"]["rows"]},
                         {"Cust_ID", "Cust_Name", "AmountDue", "Inv_Date", "Email", "Cust_Phone"})
        statuses = {item["row_index"]: item["status"] for item in preview["rows"]}
        self.assertEqual(statuses, {1: "accepted", 2: "accepted", 3: "dropped"})
        self.assertEqual(preview["rows"][2]["reason_token"], "AMOUNT_NOT_NUMERIC")
        self.assertIn("normalized", preview["rows"][0])
        self.assertEqual(json.loads(json.dumps(preview))["preview_fingerprint"],
                         preview["preview_fingerprint"], "the payload survives a JSON round trip")

    def test_commit_without_a_batch_record_is_refused(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        result = self.engine.commit_spreadsheet(preview, batch_id="no-such-batch")
        self.assertFalse(result["ok"])
        self.assertEqual(result["refusal_token"], "BLOCKED_NO_INGESTION_BATCH")
        self.assertEqual(len(self.engine.store.query("SELECT * FROM debtor_records")), 0,
                         "a refused commit imports nothing")

    def test_register_refuses_without_the_step3_warranty_tick(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        self.sign_off()
        result = self.engine.register_ingestion_batch(preview, uploader_id="dana@cedar.example",
                                                      warranty_acknowledged=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["refusal_token"], "BLOCKED_WARRANTY_NOT_ACKNOWLEDGED")
        self.assertEqual(len(self.engine.ingestion_batches()), 0, "no record, no batch")

    def test_register_refuses_without_a_valid_signature_to_bind(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        result = self.engine.register_ingestion_batch(preview, uploader_id="dana@cedar.example",
                                                      warranty_acknowledged=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["refusal_token"], "BLOCKED_NO_WARRANTY_SIGNATURE")

    def test_commit_is_refused_when_the_record_does_not_match_the_preview(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        batch = self.registered(preview)

        edited_rows = copy.deepcopy(preview)
        edited_rows["accepted_rows"][0]["raw"]["AmountDue"] = "999999.00"
        self.assertEqual(self.engine.commit_spreadsheet(edited_rows, batch_id=batch["batch_id"])["refusal_token"],
                         "BATCH_PREVIEW_STATE_CHANGED")

        edited_counts = copy.deepcopy(preview)
        edited_counts["totals"]["rows_accepted"] = 1
        self.assertEqual(self.engine.commit_spreadsheet(edited_counts, batch_id=batch["batch_id"])["refusal_token"],
                         "BATCH_PREVIEW_STATE_CHANGED")

        edited_file = copy.deepcopy(preview)
        edited_file["file_sha256"] = "0" * 64
        edited_file["preview_fingerprint"] = ingest.recompute_preview_fingerprint(edited_file)
        self.assertEqual(self.engine.commit_spreadsheet(edited_file, batch_id=batch["batch_id"])["refusal_token"],
                         "BATCH_PREVIEW_STATE_CHANGED")

        other_uploader = self.engine.commit_spreadsheet(preview, batch_id=batch["batch_id"],
                                                        uploader_id="someone-else@example.com")
        self.assertEqual(other_uploader["refusal_token"], "BATCH_PREVIEW_STATE_CHANGED")

        self.assertEqual(len(self.engine.store.query("SELECT * FROM debtor_records")), 0,
                         "every drift refusal imports nothing at all")

    def test_a_preview_edited_after_it_was_produced_cannot_even_register(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        tampered = copy.deepcopy(preview)
        tampered["accepted_rows"][0]["raw"]["AmountDue"] = "9.00"
        self.sign_off()
        result = self.engine.register_ingestion_batch(tampered, uploader_id="dana@cedar.example",
                                                      warranty_acknowledged=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["refusal_token"], "BATCH_PREVIEW_STATE_CHANGED")

    def test_a_batch_commits_once(self):
        preview, batch, first = self.registered_and_committed()
        self.assertTrue(first["ok"])
        second = self.engine.commit_spreadsheet(preview, batch_id=batch["batch_id"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["refusal_token"], "BATCH_ALREADY_COMMITTED")
        self.assertEqual(len(self.engine.store.query("SELECT * FROM debtor_records")), 2)


# ---------------------------------------------------------------------------
# 5b. the immutable, cryptographically bound batch record
# ---------------------------------------------------------------------------
class TestIngestionBatchRecord(SpreadsheetIntakeTestCase):
    def test_the_record_carries_every_owner_named_field(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        batch = self.registered(preview, reason="handed over at onboarding")
        self.assertTrue(batch["ok"], batch)
        record = batch["record"]
        self.assertEqual(record["file_name"], "aged.csv")
        self.assertEqual(record["file_sha256"], preview["file_sha256"])
        self.assertEqual((record["rows_seen"], record["rows_accepted"], record["rows_rejected"]), (2, 2, 0))
        self.assertEqual(record["uploader_id"], "dana@cedar.example")
        self.assertEqual(record["client_id"], "CLI-TEST")
        self.assertTrue(record["recorded_at_ms"].endswith("Z"))
        self.assertIn("T", record["recorded_at_ms"])
        self.assertEqual(record["recorded_at_ms"], "2026-10-06T14:00:00.000Z",
                         "the batch is stamped in millisecond UTC")
        self.assertEqual(record["preview_fingerprint"], preview["preview_fingerprint"])
        self.assertEqual(record["reason"], "handed over at onboarding")

    def test_the_record_is_cryptographically_bound_to_the_step3_warranty_signature(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        signoff = self.sign_off()
        batch = self.registered(preview)
        record = batch["record"]
        self.assertEqual(record["warranty_acknowledged"], 1)
        self.assertEqual(record["warranty_signature_reference"], signoff["signature_reference"])
        self.assertEqual(record["warranty_document_version"], config.step3_warranty()["version"])
        self.assertEqual(record["warranty_text_sha256"],
                         ingest.sha256_text(config.step3_warranty()["text"]))
        expected_binding = ingest.warranty_binding(
            client_id="CLI-TEST", signature_reference=signoff["signature_reference"],
            document_version=config.step3_warranty()["version"],
            text_sha256=record["warranty_text_sha256"], uploader_id="dana@cedar.example",
            acknowledged_at_ms=record["recorded_at_ms"])
        self.assertEqual(record["warranty_binding_sha256"], expected_binding)
        self.assertEqual(record["batch_sha256"], ingest.batch_sha256(record))
        self.assertEqual(record["chain_hash"],
                         ingest.sha256_text("|".join([record["prev_chain_hash"], record["batch_sha256"],
                                                      record["batch_id"]])))
        self.assertEqual(record["prev_chain_hash"], ingest.GENESIS_BATCH_CHAIN_HASH,
                         "the first batch links back to the documented genesis constant")
        self.assertTrue(self.engine.verify_ingestion_batches()["ok"])

    def test_the_record_is_append_only(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        batch = self.registered(preview)
        with self.assertRaises(sqlite3.IntegrityError) as update_ctx:
            self.engine.store.execute("UPDATE ingestion_batches SET rows_seen = 99")
        self.assertIn("append-only", str(update_ctx.exception))
        with self.assertRaises(sqlite3.IntegrityError) as delete_ctx:
            self.engine.store.execute("DELETE FROM ingestion_batches")
        self.assertIn("append-only", str(delete_ctx.exception))
        self.assertEqual(self.engine.ingestion_batches()[0]["batch_id"], batch["batch_id"])
        self.assertEqual(self.engine.ingestion_batches()[0]["rows_seen"], 2)

    def test_the_chain_of_record_detects_a_forged_row(self):
        preview = self.preview(CSV_GOOD.split("\n", 1)[1])
        self.registered(preview)
        self.assertTrue(self.engine.verify_ingestion_batches()["ok"])
        columns = ("batch_id", "client_id", "uploader_id", "file_name", "file_sha256", "source_format",
                   "rows_seen", "rows_accepted", "rows_rejected", "preview_fingerprint", "mapping_json",
                   "rejected_json", "consumer_debt_marker", "consumer_debt_signals", "recorded_at_ms",
                   "recorded_at", "warranty_acknowledged", "warranty_signature_reference",
                   "warranty_document_version", "warranty_text_sha256", "warranty_binding_sha256",
                   "batch_sha256", "prev_chain_hash", "chain_hash", "recorded_by", "reason", "note")
        forged = ("forged", "CLI-TEST", "attacker", "forged.csv", "a" * 64, "csv", 0, 0, 0, "b" * 64,
                  "[]", "[]", 0, "[]", "2026-10-06T14:00:00.000Z", "2026-10-06T14:00:00Z", 1,
                  "sha256:forged", "WAR-1.0", "c" * 64, "d" * 64, "e" * 64, "f" * 64, "0" * 64,
                  "attacker", "", "")
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                f"INSERT INTO ingestion_batches ({','.join(columns)}) "
                f"VALUES ({','.join('?' * len(forged))})", forged)
        verification = self.engine.verify_ingestion_batches()
        self.assertFalse(verification["ok"])
        self.assertEqual(verification["count"], 2)
        self.assertEqual({br["batch_id"] for br in verification["breaks"]}, {"forged"})
        self.assertEqual(self.engine.commit_spreadsheet(preview, batch_id="forged")["refusal_token"],
                         "BATCH_RECORD_TAMPERED")

    def test_the_genesis_constant_is_a_hash_over_a_fixed_label(self):
        self.assertEqual(ingest.GENESIS_BATCH_CHAIN_HASH,
                         ingest.sha256_text("recoverai:ingestion_batches:genesis:v1"))
        self.assertEqual(len(ingest.GENESIS_BATCH_CHAIN_HASH), 64)
        self.assertNotEqual(ingest.GENESIS_BATCH_CHAIN_HASH,
                            ingest.sha256_text("recoverai:billing_route_verifications:genesis:v1"),
                            "the batch chain has its own genesis, not the receipt chain's")


# ---------------------------------------------------------------------------
# 5c. the committed rows are the previewed rows, through the shared normalizer
# ---------------------------------------------------------------------------
class TestCommitUsesTheOneNormalizationPath(SpreadsheetIntakeTestCase):
    def test_committed_rows_match_the_preview_exactly(self):
        body = ("AC-1001,Cedar Lane Design Studio,4820.50,2026-06-15,ap@cedar.example,(212) 555-0143\n"
                "AC-1002,Northwind Freight LLC,12340.00,2026-06-16,ap@northwind.example,(303) 555-0144\n"
                "AC-1003,Globex Media Inc,pending,2026-06-17,x@x.example,(415) 555-0170")
        preview = self.preview(body)
        batch = self.registered(preview)
        result = self.engine.commit_spreadsheet(preview, batch_id=batch["batch_id"])
        self.assertTrue(result["ok"])
        previewed = {item["row_index"]: item["normalized"] for item in preview["rows"]
                     if item["status"] == "accepted"}
        self.assertEqual(result["rows_written"], len(previewed))
        for item in preview["rows"]:
            if item["status"] != "accepted":
                continue
            row = self.engine.debtor(item["normalized"]["debtor_id"])
            for field in ("client_id", "name", "email", "phone", "balance_owed", "invoice_date",
                          "aging_state", "window_decision", "area_code", "tz_name"):
                self.assertEqual(row[field], item["normalized"][field],
                                 f"{item['normalized']['debtor_id']}.{field} differs from the preview")

    def test_committed_rows_enter_the_working_queue_through_the_live_normalizer(self):
        preview, batch, result = self.registered_and_committed()
        row = self.engine.debtor("AC-1001")
        self.assertEqual(row["approval_status"], "Pending", "committed rows queue for review")
        self.assertEqual(row["source_ref"], result["source_ref"])
        self.assertEqual(row["source_ref"], f"ingestion_batch:{batch['batch_id']}")
        self.assertIn("Spreadsheet intake", row["source_system"])
        self.assertIn("aged.csv", row["source_system"])
        # the same two fields every live-imported row carries
        self.assertEqual(json.loads(row["source_columns"])["Cust_Name"], "name")
        self.assertEqual(json.loads(row["raw_record"])["Cust_ID"], "AC-1001")
        self.assertEqual(row["window_decision"], "In_Window")

    def test_a_committed_row_is_a_first_class_record_the_pipeline_can_use(self):
        preview, batch, result = self.registered_and_committed()
        draft = self.engine.queue_draft("AC-1001", "email")
        self.assertEqual(draft["disclaimer_occurrences"], 1)
        sent = self.engine.send("AC-1001", "email", who="reviewer")
        self.assertEqual(sent["decision"], "BLOCKED_MODE_HITL",
                         "an uploaded row is still governed by the client's mode and every guardrail")

    def test_the_audit_log_records_the_registration_and_the_commit(self):
        preview, batch, result = self.registered_and_committed()
        actions = [entry["action"] for entry in self.engine.audit.entries]
        self.assertIn("ingestion_batch_registered", actions)
        self.assertIn("spreadsheet_committed", actions)
        committed = self.engine.audit.since("spreadsheet_committed")[0]
        self.assertEqual(committed["batch_id"], batch["batch_id"])
        self.assertEqual(committed["rows_written"], 2)
        self.assertEqual(committed["file_sha256"], preview["file_sha256"])


# ---------------------------------------------------------------------------
# vocabulary parity: the tokens in the rulebook are the tokens the engine emits
# ---------------------------------------------------------------------------
class TestIntakeVocabulary(SpreadsheetIntakeTestCase):
    def test_the_rulebook_lists_the_tokens_the_engine_uses(self):
        cfg = config.spreadsheet_intake()
        tokens = cfg["reason_tokens"]
        emitted = {
            "row_dropped": [ingest.DROP_MISSING_DEBTOR_ID, ingest.DROP_AMOUNT_NOT_NUMERIC,
                            ingest.DROP_MISSING_BALANCE, ingest.DROP_UNPARSEABLE_DATE,
                            ingest.DROP_BLOCKED_NO_BALANCE, ingest.DROP_ROW_SHAPE],
            "row_flagged": [ingest.FLAG_MISSING_PHONE, ingest.FLAG_MISSING_EMAIL,
                            ingest.FLAG_MISSING_INVOICE_DATE],
            "mapping": [ingest.MAPPING_UNMAPPED_COLUMN, ingest.MAPPING_DUPLICATE_TARGET],
            "commit_refusal": [ingest.REFUSAL_NO_BATCH, ingest.REFUSAL_PREVIEW_STATE_CHANGED,
                               ingest.REFUSAL_BATCH_TAMPERED, ingest.REFUSAL_MAPPING_ERROR,
                               ingest.REFUSAL_WARRANTY_NOT_ACKNOWLEDGED,
                               ingest.REFUSAL_NO_WARRANTY_SIGNATURE],
            "file_refusal": [ingest.REFUSAL_UNSUPPORTED_FILE],
        }
        for group, names in emitted.items():
            self.assertEqual(sorted(tokens[group]), sorted(names), group)
        self.assertEqual(cfg["evaluation_order_per_row"],
                         [ingest.DROP_MISSING_DEBTOR_ID, ingest.DROP_AMOUNT_NOT_NUMERIC,
                          ingest.DROP_MISSING_BALANCE, ingest.DROP_UNPARSEABLE_DATE,
                          ingest.DROP_BLOCKED_NO_BALANCE])
        # `BATCH_ALREADY_COMMITTED` is documented on the schema table rather than here,
        # so it is the one refusal token the rulebook list does not carry.
        self.assertNotIn(ingest.REFUSAL_BATCH_ALREADY_COMMITTED, tokens["commit_refusal"])

    def test_the_scan_configuration_is_policy_and_documented_as_such(self):
        cfg = config.consumer_debt_scan_cfg()
        self.assertTrue(cfg["advisory_only"])
        self.assertIn("POLICY, NOT LAW", cfg["policy_note"])
        self.assertEqual(cfg["sub_100_balance_threshold_cents"], 10000)
        self.assertTrue(config.step3_warranty()["text"])
        self.assertFalse(config.step3_warranty()["checkbox_default"],
                         "the Step 3 checkbox is unselected by default")


# ---------------------------------------------------------------------------
# the preview payload is a plain dict: no engine object may leak into it
# ---------------------------------------------------------------------------
class TestPayloadHygiene(SpreadsheetIntakeTestCase):
    def test_the_whole_preview_is_json_serialisable(self):
        payload = self.preview(CSV_GOOD.split("\n", 1)[1])
        encoded = json.dumps(payload, sort_keys=True)
        self.assertEqual(json.loads(encoded)["file_sha256"], payload["file_sha256"])

    def test_the_mock_database_path_is_untouched_by_these_tests(self):
        """A canary: the suite never writes to data/recoverai.db."""
        self.assertNotIn(str(dbmod.default_db_path()), str(self.db_path))


if __name__ == "__main__":
    unittest.main()
