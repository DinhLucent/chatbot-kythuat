from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MOJIBAKE_MARKERS = (
    "\u00c2\u00a0",
    "\u00c2\u00a9",
    "\u00c2\u00ae",
    "\u00c2\u00b0",
    "\u00c2\u00b1",
    "\u00c2\u00b5",
    "\u00c2\u00b7",
    "\u00c2\u00b8",
    "\u00c3",
    "\u00c4",
    "\u00c6",
    "\u00e1\u00ba",
    "\u00e1\u00bb",
    "\u00e2\u20ac",
    "\ufffd",
)

COMMON_MOJIBAKE_REPLACEMENTS = {
    "\u00c2\u00a0": " ",
    "\u00c2\u00a9": "\u00a9",
    "\u00c2\u00ae": "\u00ae",
    "\u00c2\u00b0": "\u00b0",
    "\u00c2\u00b1": "\u00b1",
    "\u00c2\u00b5": "\u00b5",
    "\u00c2\u00b7": "\u00b7",
    "\u00c2\u00b8": ",",
    "\u00e2\u20ac\u201c": "-",
    "\u00e2\u20ac\u201d": "-",
    "\u00e2\u20ac\u0153": "\"",
    "\u00e2\u20ac\u009d": "\"",
    "\u00e2\u20ac\u02dc": "'",
    "\u00e2\u20ac\u2122": "'",
    "\u00e2\u20ac\u00a2": "-",
    "\u00e2\u20ac\u00a6": "...",
    "\u00e2\u201e\u00a2": "TM",
    "\u00e2\u2014\u008f": "-",
    "\u00e2\u2020\u2019": "->",
    "\u00ef\u00ac\u0081": "fi",
    "\u00ef\u00ac\u0082": "fl",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:length]


def safe_filename(value: str, max_len: int = 96) -> str:
    text = normalize_ascii(value)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(" ._-")
    if not text:
        text = "item"
    if len(text) <= max_len:
        return text
    suffix = short_hash(value, 8)
    return f"{text[: max_len - 9].rstrip('-')}-{suffix}"


def normalize_ascii(value: str) -> str:
    text = unicodedata.normalize("NFD", value)
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    text = text.replace("đ", "d").replace("Đ", "D")
    return text


def normalized_text(value: object) -> str:
    text = repair_mojibake(str(value or ""))
    text = normalize_ascii(text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9./_-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(value: object) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", normalized_text(value))


def repair_mojibake(value: object) -> str:
    text = str(value or "")
    if not text:
        return text
    best_score = mojibake_score(text)
    if best_score == 0:
        return text
    best = text
    for encoding in ("latin1", "cp1252"):
        try:
            candidate = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        score = mojibake_score(candidate)
        if score < best_score:
            best = candidate
            best_score = score
    best = apply_common_mojibake_replacements(best)
    return best


def apply_common_mojibake_replacements(text: str) -> str:
    repaired = text
    for bad, good in COMMON_MOJIBAKE_REPLACEMENTS.items():
        repaired = repaired.replace(bad, good)
    repaired = re.sub(r"\u00c2(?=[\s,.;:)/-])", "", repaired)
    repaired = re.sub(r"(?<=\w)\u00b8(?=\s)", ",", repaired)
    return repaired


def mojibake_score(value: object) -> int:
    text = str(value or "")
    broad_prefixes = {"\u00c3", "\u00c4", "\u00c6"}
    marker_score = sum(text.count(marker) for marker in MOJIBAKE_MARKERS if marker not in broad_prefixes) * 4
    marker_score += len(re.findall(r"[\u00c3\u00c4\u00c6][\u0080-\u00ff]", text)) * 4
    marker_score += len(re.findall(r"\u00e1[\u00ba\u00bb][\u0080-\u00ff]?", text)) * 4
    marker_score += len(re.findall(r"\u00e2\u20ac[\u0080-\u00ff\u2018-\u201d]?", text)) * 4
    replacement_score = text.count("\ufffd") * 8
    controls = sum(1 for char in text if unicodedata.category(char) == "Cc" and char not in "\r\n\t")
    return marker_score + replacement_score + controls * 4


def hash_embedding(text: str, dim: int = 256) -> list[float]:
    vector = [0.0] * dim
    for token in tokenize(text):
        vector[int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 0:
        return vector
    return [round(item / norm, 8) for item in vector]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    size = min(len(a), len(b))
    return sum(a[index] * b[index] for index in range(size))


def keyword_codes(text: str) -> list[str]:
    patterns = [
        r"\b(?:AW|G|L|F|SC)\s*[-_]?\s*\d{3,8}[A-Z]?\b",
        r"\b[A-Z]{1,6}\d{3,8}[A-Z]?\b",
        r"\b(?:SW|software)\s*\d+(?:\.\d+|\.x|\.xx)?\b",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            code = re.sub(r"\s+", "", match.group(0).upper())
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def clamp_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."
