"""Bounded pure parsers, executed in a short-lived process by source workers."""

import io
import json
import math
import os
import subprocess
import sys
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from uuid import uuid4

from .table_sources import SourceError

FILE_LIMIT = 10 * 1024 * 1024
TEXT_LIMIT = 64 * 1024
XML_LIMIT = 32 * 1024 * 1024
MAX_CELLS = 50_000
MAX_ROWS = 5_000
MAX_COLUMNS = 200
MAX_SHEETS = 32
TABLE_TEXT_LIMIT = 8 * 1024 * 1024


def zip_guard(raw):
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        entries = archive.infolist()
        if (len(entries) > 2000 or sum(item.file_size for item in entries) > XML_LIMIT
                or any(item.flag_bits & 1 or item.file_size > XML_LIMIT for item in entries)
                or len({item.filename for item in entries}) != len(entries)):
            raise SourceError("source_too_large")
        return archive
    except zipfile.BadZipFile:
        raise SourceError("invalid_source_file") from None


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime, time)):
        result = value.isoformat()
    elif isinstance(value, bool):
        result = "Да" if value else "Нет"
    elif isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise SourceError("invalid_cell_value")
        result = str(value)
    elif isinstance(value, str):
        result = value
    else:
        raise SourceError("unsupported_cell_type")
    if "\0" in result or len(result) > 4096:
        raise SourceError("invalid_cell_value")
    result.encode("utf-8")
    return result


def check_text(text):
    if not text.strip():
        raise SourceError("empty_source")
    if "\0" in text or len(text.encode("utf-8")) > TEXT_LIMIT:
        raise SourceError("text_too_large")
    return text


def xlsx(raw):
    import openpyxl

    with zip_guard(raw):
        pass
    book = openpyxl.load_workbook(io.BytesIO(raw), read_only=False, data_only=False, keep_links=False)
    try:
        if len(book.worksheets) > MAX_SHEETS:
            raise SourceError("source_too_large")
        sheets, total = [], 0
        text_bytes = 0

        def bounded(value):
            nonlocal text_bytes
            result = cell_text(value)
            text_bytes += len(result.encode("utf-8"))
            if text_bytes > TABLE_TEXT_LIMIT:
                raise SourceError("source_too_large")
            return result
        for sheet in book.worksheets:
            rows, width = sheet.max_row, sheet.max_column
            total += rows * width
            if rows > MAX_ROWS or width > MAX_COLUMNS or total > MAX_CELLS:
                raise SourceError("source_too_large")
            columns = [{"key": uuid4().hex, "index": index,
                "header": "" if sheet.cell(1, index + 1).data_type == "f" else bounded(sheet.cell(1, index + 1).value),
                "hidden": any(dim.hidden and (dim.min or index + 1) <= index + 1 <= (dim.max or index + 1)
                              for dim in sheet.column_dimensions.values())} for index in range(width)]
            values = [[{"text": "" if cell.data_type == "f" else bounded(cell.value),
                        "formula": cell.data_type == "f"} for cell in row]
                      for row in sheet.iter_rows(min_row=2, max_row=rows, max_col=width)]
            blocked = ""
            if sheet.merged_cells.ranges or sheet.array_formulae:
                blocked = "unsupported_merged_or_array_cells"
            if any(dim.hidden for dim in sheet.row_dimensions.values()):
                blocked = "unsupported_hidden_rows"
            sheets.append({"key": uuid4().hex, "identity": sheet.title, "title": sheet.title,
                "hidden": sheet.sheet_state != "visible", "columns": columns, "rows": values,
                "blocked_reason": blocked})
        return {"schema": 1, "source_type": "xlsx", "source_id": "", "sheets": sheets}
    finally:
        book.close()


def docx(raw):
    from defusedxml.ElementTree import fromstring

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    def paragraphs(node):
        if node.tag in {namespace + "del", namespace + "delText"}:
            return ""
        if node.tag == namespace + "r" and node.find(namespace + "rPr/" + namespace + "vanish") is not None:
            return ""
        if node.tag == namespace + "t":
            return node.text or ""
        if node.tag in {namespace + "tab"}:
            return "\t"
        separator = "\n" if node.tag in {namespace + "body", namespace + "tbl", namespace + "tr"} else ""
        return separator.join(paragraphs(child) + ("\n" if child.tag == namespace + "p" else "") for child in node)
    with zip_guard(raw) as archive:
        names = archive.namelist()
        if "word/document.xml" not in names:
            raise SourceError("invalid_source_file")
        parts = ["word/document.xml", *sorted(name for name in names if name.startswith(("word/header", "word/footer")) and name.endswith(".xml"))]
        parts.extend(name for name in ("word/footnotes.xml", "word/endnotes.xml") if name in names)
        result = []
        for name in parts:
            root = fromstring(archive.read(name), forbid_dtd=True)
            if root.find(".//" + namespace + "gridSpan") is not None or root.find(".//" + namespace + "vMerge") is not None:
                raise SourceError("unsupported_merged_cells")
            result.append(paragraphs(root))
        return check_text("\n".join(result))


def pdf(raw):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted:
        raise SourceError("encrypted_source")
    if len(reader.pages) > 200:
        raise SourceError("source_too_large")
    result = []
    for page in reader.pages:
        text = page.extract_text()
        if not text or not text.strip():
            raise SourceError("pdf_text_layer_required")
        result.append(text)
        check_text("\n".join(result))
    return check_text("\n".join(result))


def extract(raw, kind):
    if len(raw) > FILE_LIMIT:
        raise SourceError("source_too_large")
    if kind == "xlsx":
        return {"table": xlsx(raw)}
    if kind == "txt":
        return {"text": check_text(raw.decode("utf-8-sig"))}
    if kind == "docx":
        return {"text": docx(raw)}
    if kind == "pdf":
        return {"text": pdf(raw)}
    raise SourceError("unsupported_source_type")


def extract_isolated(raw, kind):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    try:
        result = subprocess.run([sys.executable, "-m", "knowledge.extractors", kind], input=raw,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        raise SourceError("source_timeout") from None
    if result.returncode or len(result.stdout) > XML_LIMIT:
        raise SourceError("source_parser_failed")
    data = json.loads(result.stdout)
    if "error" in data:
        raise SourceError(data["error"])
    return data


if __name__ == "__main__":
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    try:
        value = extract(sys.stdin.buffer.read(FILE_LIMIT + 1), sys.argv[1])
    except SourceError as error:
        value = {"error": error.code}
    except Exception:
        value = {"error": "invalid_source_file"}
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) > XML_LIMIT:
        encoded = b'{"error":"source_too_large"}'
    sys.stdout.buffer.write(encoded)
