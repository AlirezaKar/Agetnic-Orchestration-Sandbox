"""Orchestrator: attachment parsing + file store."""

from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.orchestrator.files import (
    Attachment,
    AttachmentStore,
    FileError,
    decode_payload,
    extract,
)


def _docx_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


class ExtractTests(unittest.TestCase):
    def test_plain_text(self):
        result = extract("notes.md", b"# Title\n\nHello world")
        self.assertEqual(result.kind, "text")
        self.assertIn("Hello world", result.text)
        self.assertFalse(result.truncated)

    def test_csv_becomes_table(self):
        result = extract("sales.csv", b"month,revenue\nJan,10\nFeb,20\n")
        self.assertEqual(result.kind, "table")
        self.assertEqual(result.header, ["month", "revenue"])
        self.assertEqual(len(result.rows), 3)
        self.assertEqual(result.rows[1], ["Jan", "10"])

    def test_tsv(self):
        result = extract("t.tsv", b"a\tb\n1\t2\n")
        self.assertEqual(result.header, ["a", "b"])

    def test_json_records(self):
        payload = json.dumps([{"city": "Sofia", "n": 1}, {"city": "Varna", "n": 2}]).encode()
        result = extract("rows.json", payload)
        self.assertEqual(result.header, ["city", "n"])
        self.assertEqual(result.rows[1], ["Sofia", "1"])

    def test_docx_paragraphs(self):
        result = extract("contract.docx", _docx_bytes(["Hello World", "Second para"]))
        self.assertEqual(result.kind, "document")
        self.assertEqual(result.text, "Hello World\nSecond para")

    def test_xlsx_sheet(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "xl/sharedStrings.xml",
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<si><t>month</t></si><si><t>Jan</t></si><si><t>rev</t></si></sst>",
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>2</v></c></row>'
                '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>10</v></c></row>'
                "</sheetData></worksheet>",
            )
        result = extract("book.xlsx", buffer.getvalue())
        self.assertEqual(result.header, ["month", "rev"])
        self.assertEqual(result.rows[1], ["Jan", "10"])

    def test_legacy_doc_is_rejected_with_hint(self):
        with self.assertRaises(FileError) as caught:
            extract("old.doc", b"\xd0\xcf\x11\xe0binary")
        self.assertEqual(caught.exception.status, 415)
        self.assertIn(".docx", caught.exception.hint)

    def test_unknown_binary_rejected(self):
        with self.assertRaises(FileError) as caught:
            extract("image.png", b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
        self.assertEqual(caught.exception.status, 415)

    def test_pdf_without_library_is_explicit(self):
        with self.assertRaises(FileError) as caught:
            extract("paper.pdf", b"%PDF-1.4\n%fake")
        self.assertIn("pypdf", caught.exception.hint)

    def test_truncation_is_reported(self):
        result = extract("long.txt", b"x" * 5000, max_chars=1000)
        self.assertTrue(result.truncated)
        self.assertEqual(result.chars, 1000)

    def test_empty_file_rejected(self):
        with self.assertRaises(FileError) as caught:
            extract("empty.txt", b"")
        self.assertEqual(caught.exception.status, 400)


class DecodePayloadTests(unittest.TestCase):
    def test_text_payload(self):
        attachment = decode_payload({"name": "a.md", "text": "hi there"})
        self.assertEqual(attachment.text, "hi there")

    def test_base64_payload(self):
        payload = {
            "name": "a.csv",
            "data_b64": base64.b64encode(b"x,y\n1,2\n").decode(),
        }
        attachment = decode_payload(payload)
        self.assertEqual(attachment.header, ["x", "y"])

    def test_data_url_prefix_is_stripped(self):
        encoded = base64.b64encode(b"hello").decode()
        attachment = decode_payload({"name": "a.txt", "data_b64": f"data:text/plain;base64,{encoded}"})
        self.assertEqual(attachment.text, "hello")

    def test_missing_content(self):
        with self.assertRaises(FileError) as caught:
            decode_payload({"name": "a.txt"})
        self.assertEqual(caught.exception.status, 400)

    def test_bad_base64(self):
        with self.assertRaises(FileError) as caught:
            decode_payload({"name": "a.txt", "data_b64": "!!!not base64!!!"})
        self.assertEqual(caught.exception.status, 400)

    def test_size_limit(self):
        with self.assertRaises(FileError) as caught:
            decode_payload({"name": "big.txt", "text": "x" * 5000}, max_mb=0.001)
        self.assertEqual(caught.exception.status, 413)

    def test_char_limit_truncates_instead_of_failing(self):
        attachment = decode_payload({"name": "long.txt", "text": "x" * 5000}, max_chars=1000)
        self.assertTrue(attachment.truncated)
        self.assertEqual(len(attachment.text), 1000)

    def test_name_defaults(self):
        attachment = decode_payload({"name": "   ", "text": "x"})
        self.assertEqual(attachment.name, "pasted.txt")


class AttachmentTests(unittest.TestCase):
    def test_summary_and_preview(self):
        attachment = extract("policy.md", b"Line one\nLine two")
        summary = attachment.summary()
        self.assertEqual(summary["name"], "policy.md")
        self.assertEqual(summary["kind"], "text")
        self.assertEqual(summary["chars"], len("Line one\nLine two"))
        self.assertIn("Line one", attachment.preview(50))
        self.assertLessEqual(len(attachment.preview(10)), 10)

    def test_table_summary_reports_shape(self):
        attachment = extract("t.csv", b"a,b\n1,2\n")
        self.assertTrue(attachment.is_table)
        summary = attachment.summary()
        self.assertEqual(summary["columns"], ["a", "b"])
        self.assertEqual(summary["rows"], 1)

    def test_fingerprint_stable(self):
        a = extract("t.txt", b"same")
        b = extract("t.txt", b"same")
        c = extract("t.txt", b"other")
        self.assertEqual(a.fingerprint(), b.fingerprint())
        self.assertNotEqual(a.fingerprint(), c.fingerprint())

    def test_id_is_short_and_unique(self):
        a = extract("t.txt", b"same")
        b = extract("t.txt", b"other")
        self.assertEqual(len(a.id), 12)
        self.assertNotEqual(a.id, b.id)

    def test_to_dict_roundtrip(self):
        original = extract("t.csv", b"a,b\n1,2\n")
        revived = Attachment.from_dict(original.to_dict())
        self.assertEqual(revived.name, original.name)
        self.assertEqual(revived.rows, original.rows)
        self.assertEqual(revived.id, original.id)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttachmentStore(Path(self.tmp.name) / "files")

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_get_list_delete(self):
        attachment = extract("a.txt", b"alpha")
        self.store.save(attachment)
        self.assertIsNotNone(self.store.get(attachment.id))
        self.assertEqual(len(self.store.list()), 1)
        self.assertTrue(self.store.delete(attachment.id))
        self.assertIsNone(self.store.get(attachment.id))
        self.assertFalse(self.store.delete(attachment.id))

    def test_persistence_across_instances(self):
        attachment = extract("b.csv", b"x,y\n1,2\n")
        self.store.save(attachment)
        again = AttachmentStore(Path(self.tmp.name) / "files")
        revived = again.get(attachment.id)
        self.assertEqual(revived.rows, attachment.rows)

    def test_identical_uploads_get_distinct_ids(self):
        first = extract("a.txt", b"same bytes")
        second = extract("a.txt", b"same bytes")
        self.store.save(first)
        self.store.save(second)
        self.assertEqual(len(self.store.list()), 2)

    def test_fingerprint_matches_content_not_id(self):
        first = extract("a.txt", b"same bytes")
        second = extract("a.txt", b"same bytes")
        self.assertEqual(self.store.fingerprint([first]), self.store.fingerprint([second]))

    def test_list_is_newest_first(self):
        one = extract("1.txt", b"one")
        two = extract("2.txt", b"two")
        one.created = "2026-01-01T00:00:00+00:00"
        two.created = "2026-02-01T00:00:00+00:00"
        self.store.save(one)
        self.store.save(two)
        self.assertEqual(self.store.list()[0].name, "2.txt")

    def test_delete_missing_is_false(self):
        self.assertFalse(self.store.delete("nope"))

    def test_get_missing_is_none(self):
        self.assertIsNone(self.store.get("nope"))
        self.assertIsNone(self.store.get("../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
