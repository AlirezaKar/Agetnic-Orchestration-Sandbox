"""Attachments: accept a file, get text an agent can reason over.

Uploads arrive as JSON (``{name, data_b64}``) so the API needs no multipart
dependency; the page base64-encodes the file it already has in memory.

Supported without any extra package:

* text-like — ``.txt .md .markdown .rst .log .csv .tsv .json .yaml .yml .xml
  .html .py .js .ts .sh .sql .ini .env``
* ``.docx`` — Word is a zip; ``word/document.xml`` is parsed directly
* ``.xlsx`` — read straight from ``xl/worksheets`` + ``xl/sharedStrings``

``.pdf`` needs ``pypdf`` (``pip install pypdf``); without it the file is rejected
with that instruction instead of silently returning nothing.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import io
import json
import re
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".tsx",
    ".jsx", ".css", ".sh", ".sql", ".ini", ".toml", ".cfg", ".env", ".srt",
}
TABULAR_SUFFIXES = {".csv", ".tsv", ".json", ".xlsx"}
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".doc"}


class FileError(ValueError):
    """The upload could not be accepted or parsed."""

    def __init__(self, message: str, *, hint: str = "", status: int = 400):
        self.hint = hint
        self.status = status
        super().__init__(message)


@dataclass
class Attachment:
    id: str
    name: str
    suffix: str
    size: int
    text: str
    kind: str = "text"  # text | table | document | code
    truncated: bool = False
    rows: list[list[str]] | None = None
    header: list[str] | None = None
    pages: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def is_table(self) -> bool:
        return bool(self.rows) and len(self.rows) > 1

    def fingerprint(self) -> str:
        digest = hashlib.sha1()
        digest.update(self.name.encode("utf-8"))
        digest.update(self.text.encode("utf-8"))
        return digest.hexdigest()[:16]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attachment":
        """Inverse of :meth:`to_dict` — used by the store and by tests."""
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name") or "file"),
            suffix=str(data.get("suffix") or ""),
            size=int(data.get("size") or 0),
            text=str(data.get("text") or ""),
            kind=str(data.get("kind") or "text"),
            truncated=bool(data.get("truncated")),
            rows=(
                [list(map(str, row)) for row in data["rows"]]
                if isinstance(data.get("rows"), list)
                else None
            ),
            header=list(data["header"]) if data.get("header") else None,
            pages=data.get("pages"),
            meta=dict(data.get("meta") or {}),
            created=str(data.get("created") or ""),
        )

    def preview(self, limit: int = 400) -> str:
        collapsed = re.sub(r"\s+", " ", self.text).strip()
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[: max(limit - 1, 0)] + "…"

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "suffix": self.suffix,
            "size": self.size,
            "chars": self.chars,
            "truncated": self.truncated,
            "pages": self.pages,
            "columns": self.header or [],
            "rows": len(self.rows) - 1 if self.rows else 0,
            "preview": self.preview(),
        }

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        """Full record (``summary`` keeps only counts, this keeps the data)."""
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "suffix": self.suffix,
            "size": self.size,
            "kind": self.kind,
            "truncated": self.truncated,
            "rows": self.rows,
            "header": self.header,
            "pages": self.pages,
            "meta": self.meta,
            "created": self.created,
            "chars": self.chars,
        }
        if include_text:
            payload["text"] = self.text
        return payload

    def excerpt(self, limit: int | None = None) -> str:
        if limit and len(self.text) > limit:
            head = self.text[: int(limit * 0.7)]
            tail = self.text[-int(limit * 0.2) :]
            return f"{head}\n\n[…truncated for the prompt…]\n\n{tail}"
        return self.text


# ------------------------------------------------------------------ parsing


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _parse_pdf(data: bytes) -> tuple[str, int | None]:
    try:
        from pypdf import PdfReader  # noqa: PLC0415 - optional dependency
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore[no-redef]  # noqa: PLC0415
        except ImportError as exc:
            raise FileError(
                "PDF support needs the 'pypdf' package.",
                hint="pip install pypdf   (then re-upload)",
                status=415,
            ) from exc

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # pragma: no cover - malformed page
            pages.append("")
    return _clean("\n\n".join(pages)), len(reader.pages)


_DOCX_TEXT = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.DOTALL)
_DOCX_PARA = re.compile(r"</w:p>")


def _parse_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise FileError(
            "That .docx could not be read (not a Word file, or a legacy .doc).",
            hint="Save it as .docx or .txt and try again",
        ) from exc
    paragraphs = [
        "".join(_DOCX_TEXT.findall(chunk)) for chunk in _DOCX_PARA.split(xml)
    ]
    text = "\n".join(part for part in paragraphs if part.strip())
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )
    return _clean(text)


def _parse_xlsx(data: bytes) -> tuple[list[list[str]], list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = re.findall(
                    r"<t[^>]*>(.*?)</t>",
                    archive.read("xl/sharedStrings.xml").decode("utf-8", "replace"),
                    re.DOTALL,
                )
            sheet = next(
                (n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")),
                None,
            )
            if sheet is None:
                raise KeyError(sheet)
            xml = archive.read(sheet).decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise FileError("That .xlsx could not be read.", hint="Export it as CSV instead") from exc

    rows: list[list[str]] = []
    for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.DOTALL):
        values: list[str] = []
        for cell in re.findall(r"<c[^>]*?(?:/>|>.*?</c>)", row_xml, re.DOTALL):
            kind = re.search(r't="([^"]+)"', cell)
            value = re.search(r"<v>(.*?)</v>", cell, re.DOTALL)
            inline = re.search(r"<is>.*?<t[^>]*>(.*?)</t>", cell, re.DOTALL)
            if inline:
                values.append(inline.group(1))
            elif value is None:
                values.append("")
            elif kind and kind.group(1) == "s":
                try:
                    values.append(shared[int(value.group(1))])
                except (ValueError, IndexError):
                    values.append("")
            else:
                values.append(value.group(1))
        rows.append(values)
    header = rows[0] if rows else []
    return rows, header


def _tabular_rows(text: str, suffix: str) -> tuple[list[list[str]], list[str]]:
    import csv

    if suffix == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [], []
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list) or not parsed:
            return [], []
        if all(isinstance(item, dict) for item in parsed):
            columns: list[str] = []
            for item in parsed:
                for key in item:
                    if key not in columns:
                        columns.append(key)
            rows = [columns] + [[str(item.get(c, "")) for c in columns] for item in parsed]
            return rows, columns
        return [], []

    delimiter = "\t" if suffix == ".tsv" else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    header = rows[0] if rows else []
    return rows, header


def _kind_for(suffix: str) -> str:
    if suffix in TABULAR_SUFFIXES:
        return "table"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if suffix in {".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".sh", ".sql"}:
        return "code"
    return "text"


def extract(name: str, data: bytes, *, max_chars: int = 200_000, max_mb: float = 10.0) -> Attachment:
    """Turn raw bytes into an :class:`Attachment` with parsed text."""
    if not name:
        raise FileError("A file needs a name.")
    size = len(data)
    if size == 0:
        raise FileError(f"{name} is empty.")
    if size > max_mb * 1024 * 1024:
        raise FileError(
            f"{name} is {size / 1024 / 1024:.1f} MB; the limit is {max_mb:.0f} MB.",
            hint="Split it or paste the relevant part.",
            status=413,
        )

    suffix = Path(name).suffix.lower()
    pages: int | None = None
    rows: list[list[str]] | None = None
    header: list[str] | None = None
    meta: dict[str, Any] = {}

    if suffix == ".pdf":
        text, pages = _parse_pdf(data)
    elif suffix == ".docx":
        text = _parse_docx(data)
    elif suffix == ".xlsx":
        rows, header = _parse_xlsx(data)
        text = "\n".join(", ".join(row) for row in rows)
        meta["sheet_rows"] = len(rows)
    elif suffix in TEXT_SUFFIXES or suffix == "":
        text = _clean(_decode(data))
        if suffix in TABULAR_SUFFIXES:
            rows, header = _tabular_rows(text, suffix)
    elif suffix in {".doc"}:
        raise FileError(
            "Legacy .doc is not supported.",
            hint="Open it in Word and save as .docx or .txt",
            status=415,
        )
    else:
        # Last resort: if it decodes as text, treat it as text.
        candidate = _decode(data)
        printable = sum(ch.isprintable() or ch in "\n\t" for ch in candidate[:2000])
        if candidate and printable / max(len(candidate[:2000]), 1) > 0.9:
            text = _clean(candidate)
            meta["guessed_type"] = True
        else:
            raise FileError(
                f"Unsupported file type '{suffix or name}'.",
                hint="Supported: text, .md, .csv, .tsv, .json, .xlsx, .docx, .pdf (pypdf)",
                status=415,
            )

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]

    if rows and len(rows) > 5000:
        rows = rows[:5000]
        meta["rows_truncated"] = True

    return Attachment(
        id=uuid.uuid4().hex[:12],
        name=Path(name).name,
        suffix=suffix,
        size=size,
        text=text,
        kind=_kind_for(suffix),
        truncated=truncated,
        rows=rows,
        header=header,
        pages=pages,
        meta=meta,
        created=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    )


def decode_payload(
    payload: dict[str, Any],
    *,
    max_chars: int = 200_000,
    max_mb: float = 10.0,
) -> Attachment:
    """Accept ``{name, data_b64}`` or ``{name, text}`` from the API/UI."""
    name = str(payload.get("name") or "").strip() or "pasted.txt"
    if payload.get("text") is not None:
        data = str(payload["text"]).encode("utf-8")
    else:
        raw = str(payload.get("data_b64") or "")
        if "," in raw[:128] and raw.strip().startswith("data:"):  # data URL
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw, validate=False)
        except (ValueError, TypeError) as exc:
            raise FileError(f"{name}: the payload is not valid base64.") from exc
    return extract(name, data, max_chars=max_chars, max_mb=max_mb)


# ------------------------------------------------------------------- store


class AttachmentStore:
    """Small on-disk store so an upload survives between requests.

    The text is written next to a JSON sidecar; ``get`` falls back to memory if
    the directory is not writable, so read-only checkouts still work.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._memory: dict[str, Attachment] = {}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------- internals
    def _text_path(self, file_id: str) -> Path:
        return self.directory / f"{file_id}.txt"

    def _meta_path(self, file_id: str) -> Path:
        return self.directory / f"{file_id}.json"

    def _rows_path(self, file_id: str) -> Path:
        return self.directory / f"{file_id}.rows.json"

    # ---------------------------------------------------------------- public
    def save(self, attachment: Attachment) -> Attachment:
        self._memory[attachment.id] = attachment
        try:
            self._text_path(attachment.id).write_text(attachment.text, encoding="utf-8")
            meta = attachment.summary()
            meta["created"] = attachment.created
            meta["has_rows"] = bool(attachment.rows)
            self._meta_path(attachment.id).write_text(json.dumps(meta), encoding="utf-8")
            if attachment.rows:
                self._rows_path(attachment.id).write_text(
                    json.dumps(attachment.rows), encoding="utf-8"
                )
        except OSError:
            pass  # memory-only
        return attachment

    def get(self, file_id: str) -> Attachment | None:
        if file_id in self._memory:
            return self._memory[file_id]
        meta_path = self._meta_path(file_id)
        text_path = self._text_path(file_id)
        if not meta_path.exists() or not text_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            attachment = Attachment(
                id=meta.get("id", file_id),
                name=meta.get("name", "file"),
                suffix=meta.get("suffix", ""),
                size=int(meta.get("size", 0)),
                text=text_path.read_text(encoding="utf-8"),
                kind=meta.get("kind", "text"),
                truncated=bool(meta.get("truncated")),
                rows=self._read_rows(file_id) if meta.get("has_rows") else None,
                header=meta.get("columns") or None,
                pages=meta.get("pages"),
                created=meta.get("created", ""),
            )
        except (OSError, ValueError):  # pragma: no cover - corrupt store
            return None
        self._memory[file_id] = attachment
        return attachment

    def _read_rows(self, file_id: str) -> list[list[str]] | None:
        path = self._rows_path(file_id)
        if not path.exists():
            return None
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover - corrupt store
            return None
        return [list(map(str, row)) for row in rows] if isinstance(rows, list) else None

    def list(self) -> list[Attachment]:
        found: dict[str, Attachment] = dict(self._memory)
        try:
            for meta_path in sorted(self.directory.glob("*.json")):
                file_id = meta_path.stem
                if file_id not in found:
                    attachment = self.get(file_id)
                    if attachment:
                        found[file_id] = attachment
        except OSError:  # pragma: no cover
            pass
        return sorted(found.values(), key=lambda item: item.created, reverse=True)

    def delete(self, file_id: str) -> bool:
        existed = file_id in self._memory
        self._memory.pop(file_id, None)
        for path in (
            self._text_path(file_id),
            self._meta_path(file_id),
            self._rows_path(file_id),
        ):
            try:
                if path.exists():
                    path.unlink()
                    existed = True
            except OSError:  # pragma: no cover
                pass
        return existed

    def fingerprint(self, attachments: list[Attachment]) -> str:
        digest = hashlib.sha1()
        for item in attachments:
            digest.update(item.name.encode())
            digest.update(Attachment.fingerprint(item).encode())
        return digest.hexdigest()[:12]
