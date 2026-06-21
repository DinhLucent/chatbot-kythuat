from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .models import DocumentRecord


DOC_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?:AW|G|L|F|SC|SI)\s*[-_ ]?\s*\d{3,8}[A-Z]?(?![A-Z0-9])",
    flags=re.IGNORECASE,
)


def infer_doc_code_from_values(*values: object) -> str:
    for value in values:
        text = str(value or "")
        for match in DOC_CODE_RE.finditer(text):
            return re.sub(r"[\s_-]+", "", match.group(0).upper())
    return ""


def infer_doc_code_from_record(record: DocumentRecord | dict[str, Any]) -> str:
    if isinstance(record, DocumentRecord):
        if record.doc_code:
            return record.doc_code
        extra = record.extra if isinstance(record.extra, dict) else {}
        return infer_doc_code_from_values(record.file_name, record.title, extra.get("sample_text"), extra.get("relative_path"))
    if record.get("doc_code"):
        return str(record.get("doc_code") or "")
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    return infer_doc_code_from_values(record.get("file_name"), record.get("title"), extra.get("sample_text"), extra.get("relative_path"))


def record_with_inferred_doc_code(record: DocumentRecord) -> DocumentRecord:
    code = infer_doc_code_from_record(record)
    if not code or code == record.doc_code:
        return record
    return replace(record, doc_code=code)
