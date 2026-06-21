from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class BlockType(str, Enum):
    ALARM_CODE = "alarm_code"
    TROUBLESHOOTING_SYMPTOM = "troubleshooting_symptom"
    TROUBLESHOOTING_CAUSE = "troubleshooting_cause"
    TROUBLESHOOTING_FIX = "troubleshooting_fix"
    SERVICE_STEP = "service_step"
    MAINTENANCE_STEP = "maintenance_step"
    CALIBRATION_STEP = "calibration_step"
    WARNING_SAFETY = "warning_safety"
    SPEC_TABLE = "spec_table"
    PARTS_TABLE = "parts_table"
    WIRING_DIAGRAM = "wiring_diagram"
    FIGURE_CAPTION = "figure_caption"
    SOFTWARE_NOTE = "software_note"
    PROSE = "prose"


@dataclass
class DocumentRecord:
    doc_id: str
    brand: str
    business_unit: str
    machine_line: str
    model: str
    doc_type: str
    doc_code: str
    software_version: str
    revision: str
    language: str
    status: str
    source_path: str
    checksum: str
    source_package: str | None = None
    file_name: str | None = None
    file_ext: str | None = None
    size_bytes: int | None = None
    page_count: int | None = None
    embedded_text_ratio: float | None = None
    title: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SectionRecord:
    section_id: str
    doc_id: str
    title: str
    level: int
    line_start: int
    line_end: int
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChunkRecord:
    chunk_id: str
    doc_id: str
    content: str
    block_type: BlockType
    section_path: list[str]
    parent_section_id: str
    line_start: int
    line_end: int
    page_start: int | None = None
    page_end: int | None = None
    source_path: str | None = None
    source_file: str | None = None
    model: str | None = None
    doc_type: str | None = None
    software_version: str | None = None
    doc_code: str | None = None
    anchor_id: str | None = None
    bbox: list[float] | None = None
    image_refs: list[str] = field(default_factory=list)
    table_refs: list[str] = field(default_factory=list)
    alarm_codes: list[str] = field(default_factory=list)
    part_numbers: list[str] = field(default_factory=list)
    semantic_summary: str | None = None
    semantic_keywords: list[str] = field(default_factory=list)
    llm_enrichment: dict[str, Any] = field(default_factory=dict)
    low_value: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["block_type"] = self.block_type.value
        return payload


@dataclass
class Citation:
    citation_id: str
    chunk_id: str
    doc_id: str
    source_file: str
    page_start: int | None
    page_end: int | None
    section_path: list[str]
    line_start: int
    line_end: int
    display: str
    bbox: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
