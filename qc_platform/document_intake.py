import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from xml.etree.ElementTree import ParseError

import regex
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from qc_platform.schemas import IntakeSource

MAX_DOCUMENT_BYTES = 786_432
MAX_PDF_PAGES = 100
MAX_PDF_TEXT_CHARACTERS = 500_000
MAX_XLSX_UNCOMPRESSED_BYTES = 10_000_000
MAX_XLSX_ENTRIES = 500
MAX_XLSX_COLUMNS = 500


@dataclass(frozen=True)
class ParsedSource:
    values: dict[str, object]
    extracted_text_characters: int


def parse_source(source: IntakeSource, content: bytes) -> ParsedSource:
    if not content:
        raise ValueError(f"{source.filename}: file is empty")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"{source.filename}: file exceeds the 768 KiB per-file limit")
    if source.format in {"json", "fhir_json"}:
        return _parse_json(source, content)
    if source.format == "pdf":
        return _parse_pdf(source, content)
    if source.format == "xlsx":
        return _parse_xlsx(source, content)
    raise ValueError(f"{source.filename}: unsupported format")


def _parse_json(source: IntakeSource, content: bytes) -> ParsedSource:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{source.filename}: invalid UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise ValueError(f"{source.filename}: top-level JSON value must be an object")
    if source.format == "fhir_json" and (
        document.get("resourceType") != "Bundle"
        or not isinstance(document.get("entry"), list)
    ):
        raise ValueError(f"{source.filename}: expected a FHIR JSON Bundle")
    values = {}
    for target, pointer in source.field_map.items():
        found, value = _json_pointer(document, pointer)
        if found:
            values[target] = value
    return ParsedSource(values=values, extracted_text_characters=0)


def _json_pointer(document: object, pointer: str) -> tuple[bool, object]:
    if pointer == "":
        return True, document
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or begin with '/'")
    current = document
    for raw_part in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_part):
            raise ValueError("JSON Pointer contains an invalid escape")
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdecimal():
            index = int(part)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _parse_pdf(source: IntakeSource, content: bytes) -> ParsedSource:
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ValueError(f"{source.filename}: encrypted PDFs are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"{source.filename}: PDF exceeds the {MAX_PDF_PAGES}-page limit")
        text_parts = []
        total_characters = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            total_characters += len(page_text)
            if total_characters > MAX_PDF_TEXT_CHARACTERS:
                raise ValueError(f"{source.filename}: extracted PDF text exceeds the size limit")
            text_parts.append(page_text)
    except (PdfReadError, OSError, KeyError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith(source.filename):
            raise
        raise ValueError(f"{source.filename}: could not read PDF") from error

    text = "\n".join(text_parts)
    if not text.strip():
        raise ValueError(
            f"{source.filename}: PDF has no extractable text; scanned PDFs require OCR, which is not enabled"
        )
    values = {}
    for target, pattern in source.field_map.items():
        try:
            compiled = regex.compile(pattern)
            if compiled.groups < 1:
                raise ValueError(
                    f"{source.filename}: PDF field patterns must include a capture group"
                )
            match = compiled.search(text, timeout=0.05)
        except (regex.error, TimeoutError) as error:
            raise ValueError(f"{source.filename}: invalid or over-time PDF field pattern") from error
        if match is not None:
            values[target] = match.group(1).strip()
    return ParsedSource(values=values, extracted_text_characters=total_characters)


def _validate_xlsx_archive(content: bytes, filename: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as error:
        raise ValueError(f"{filename}: invalid XLSX workbook") from error
    if len(entries) > MAX_XLSX_ENTRIES or sum(item.file_size for item in entries) > MAX_XLSX_UNCOMPRESSED_BYTES:
        raise ValueError(f"{filename}: XLSX archive exceeds safety limits")
    if any(item.flag_bits & 0x1 for item in entries):
        raise ValueError(f"{filename}: encrypted XLSX entries are not supported")


def _parse_xlsx(source: IntakeSource, content: bytes) -> ParsedSource:
    _validate_xlsx_archive(content, source.filename)
    workbook = None
    try:
        workbook = load_workbook(
            io.BytesIO(content),
            read_only=True,
            data_only=False,
            keep_links=False,
        )
        if source.worksheet:
            if source.worksheet not in workbook.sheetnames:
                raise ValueError(f"{source.filename}: worksheet {source.worksheet!r} was not found")
            sheet = workbook[source.worksheet]
        else:
            sheet = workbook.active
        rows = sheet.iter_rows(
            min_row=1,
            max_row=source.row_index + 2,
            max_col=MAX_XLSX_COLUMNS + 1,
            values_only=True,
        )
        headers = next(rows, None)
        record = None
        for row_number, row in enumerate(rows, start=2):
            if row_number == source.row_index + 2:
                record = row
                break
        if headers is None:
            raise ValueError(f"{source.filename}: worksheet has no header row")
        if any(isinstance(value, str) and value.startswith("=") for value in headers):
            raise ValueError(f"{source.filename}: formulas are not allowed in the header row")
        if any(value is not None for value in headers[MAX_XLSX_COLUMNS:]):
            raise ValueError(f"{source.filename}: worksheet exceeds the column limit")
        normalized_headers = [
            str(value).strip() if value is not None else ""
            for value in headers
        ]
        if not any(normalized_headers):
            raise ValueError(f"{source.filename}: worksheet header row is empty")
        if len([value for value in normalized_headers if value]) != len(set(
            value for value in normalized_headers if value
        )):
            raise ValueError(f"{source.filename}: worksheet column headers must be unique")
        if record is None:
            raise ValueError(
                f"{source.filename}: requested data row {source.row_index + 1} was not found"
            )
        values = {}
        columns = {header: index for index, header in enumerate(normalized_headers) if header}
        for target, column in source.field_map.items():
            if column not in columns:
                continue
            value = record[columns[column]] if columns[column] < len(record) else None
            if value is not None:
                if isinstance(value, str) and value.startswith("="):
                    raise ValueError(
                        f"{source.filename}: formulas are not allowed in mapped cells"
                    )
                values[target] = _json_safe_cell(value, source.filename)
        return ParsedSource(values=values, extracted_text_characters=0)
    except (
        InvalidFileException,
        KeyError,
        zipfile.BadZipFile,
        OSError,
        ValueError,
        ParseError,
    ) as error:
        if isinstance(error, ValueError) and str(error).startswith(source.filename):
            raise
        raise ValueError(f"{source.filename}: could not read XLSX workbook") from error
    finally:
        if workbook is not None:
            workbook.close()


def _json_safe_cell(value: object, filename: str) -> object:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{filename}: XLSX contains a non-finite numeric value")
        return float(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"{filename}: XLSX contains an unsupported cell value")
