"""Local OCR behind an injectable interface; no carrier-specific parsing."""
from __future__ import annotations

from importlib.metadata import version
from typing import Protocol

from .models import OCRRegion


class OCREngine(Protocol):
    @property
    def cache_key(self) -> str: ...

    def recognize(self, image: bytes) -> list[OCRRegion]: ...


class RapidOCREngine:
    """Load bundled CPU models only when a page actually needs OCR."""

    def __init__(self) -> None:
        self._engine = None

    @property
    def cache_key(self) -> str:
        return f"rapidocr-onnxruntime:{version('rapidocr-onnxruntime')}:v1"

    def recognize(self, image: bytes) -> list[OCRRegion]:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        result, _ = self._engine(image)
        return [
            OCRRegion(
                text=str(text), confidence=float(score),
                box=[[float(x), float(y)] for x, y in box],
            )
            for box, text, score in (result or []) if str(text).strip()
        ]


def reading_order(regions: list[OCRRegion]) -> tuple[str, list[OCRRegion]]:
    """Group boxes by baseline, then read cells left to right without changing text."""
    def center(region: OCRRegion) -> float:
        return sum(point[1] for point in region.box) / len(region.box)

    def height(region: OCRRegion) -> float:
        return max(point[1] for point in region.box) - min(point[1] for point in region.box)

    lines: list[list[OCRRegion]] = []
    for region in sorted(regions, key=lambda r: (center(r), min(p[0] for p in r.box))):
        if lines:
            baseline = sum(center(r) for r in lines[-1]) / len(lines[-1])
            tolerance = max(2.0, min(height(region), height(lines[-1][0])) * 0.5)
            if abs(center(region) - baseline) <= tolerance:
                lines[-1].append(region)
                continue
        lines.append([region])
    ordered_lines = [sorted(line, key=lambda r: min(p[0] for p in r.box)) for line in lines]
    return (
        "\n".join(" ".join(r.text for r in line) for line in ordered_lines),
        [r for line in ordered_lines for r in line],
    )
