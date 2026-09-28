import logging
import re
import unicodedata
from datetime import date
from typing import Any, Optional

from pydantic import ValidationError

from app.core.config import settings
from app.schemas.document import DocumentMetadata

logger = logging.getLogger(__name__)

# 和暦の元年（西暦）。元年 = その年。
ERA_START_YEAR = {
    "令和": 2019,
    "平成": 1989,
    "昭和": 1926,
    "大正": 1912,
    "明治": 1868,
}
ERA_ABBREVIATIONS = {"R": "令和", "H": "平成", "S": "昭和", "T": "大正", "M": "明治"}

_ISO_PATTERN = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$")
_COMPACT_PATTERN = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_ERA_PATTERN = re.compile(
    r"^(令和|平成|昭和|大正|明治|[RHSTM])\s*(元|\d{1,2})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})\s*日?$"
)

PENALTY_REQUIRED_MISSING = 0.3
PENALTY_DATE_UNPARSABLE = 0.2
PENALTY_OPTIONAL_MISSING = 0.1
PENALTY_HALLUCINATION = 0.2


class ValidationService:
    """SLM出力を DocumentMetadata に変換し、確信度と要確認フラグを決める。"""

    def __init__(self, confidence_threshold: Optional[float] = None) -> None:
        self._threshold = confidence_threshold

    @property
    def threshold(self) -> float:
        if self._threshold is not None:
            return self._threshold
        return settings.CONFIDENCE_THRESHOLD

    @staticmethod
    def _clean(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (list, dict)):
            return None
        text = unicodedata.normalize("NFKC", str(value)).strip()
        if not text or text.lower() in {"null", "none", "不明", "なし", "n/a"}:
            return None
        return text

    @classmethod
    def normalize_issue_date(cls, value: Optional[str]) -> Optional[str]:
        """西暦・和暦・区切り違いを YYYY-MM-DD に正規化する。できなければ None。"""
        if value is None:
            return None
        text = unicodedata.normalize("NFKC", value).strip().replace(" ", "")
        if not text:
            return None

        match = _ISO_PATTERN.match(text)
        if match:
            year, month, day = (int(g) for g in match.groups())
            return cls._to_iso(year, month, day)

        match = _COMPACT_PATTERN.match(text)
        if match:
            year, month, day = (int(g) for g in match.groups())
            return cls._to_iso(year, month, day)

        match = _ERA_PATTERN.match(text)
        if match:
            era_token, era_year_token, month_token, day_token = match.groups()
            era = ERA_ABBREVIATIONS.get(era_token.upper(), era_token)
            start = ERA_START_YEAR.get(era)
            if start is None:
                return None
            era_year = 1 if era_year_token == "元" else int(era_year_token)
            year = start + era_year - 1
            return cls._to_iso(year, int(month_token), int(day_token))

        return None

    @staticmethod
    def _to_iso(year: int, month: int, day: int) -> Optional[str]:
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _compact(text: str) -> str:
        """比較用に全角/半角を揃え、空白を除去する。"""
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))

    def _text_present(self, needle: Optional[str], haystack: str) -> bool:
        if not needle:
            return True
        compact_needle = self._compact(needle)
        if not compact_needle:
            return True
        return compact_needle in haystack

    def _date_present(self, raw_date: Optional[str], iso_date: Optional[str], haystack: str) -> bool:
        """日付は表記ゆれが大きいため、複数表現のいずれかがOCRテキストにあれば存在とみなす。"""
        candidates: list[str] = []
        if raw_date:
            candidates.append(raw_date)
        if iso_date:
            year, month, day = (int(part) for part in iso_date.split("-"))
            candidates.extend(
                [
                    iso_date,
                    f"{year}/{month:02d}/{day:02d}",
                    f"{year}/{month}/{day}",
                    f"{year}.{month:02d}.{day:02d}",
                    f"{year}年{month}月{day}日",
                    f"{year}年{month:02d}月{day:02d}日",
                    f"{year}{month:02d}{day:02d}",
                ]
            )
            for era, start in ERA_START_YEAR.items():
                era_year = year - start + 1
                if era_year >= 1:
                    era_label = "元" if era_year == 1 else str(era_year)
                    candidates.append(f"{era}{era_label}年{month}月{day}日")
                    candidates.append(f"{era}{era_label}年{month:02d}月{day:02d}日")
        if not candidates:
            return True
        return any(self._compact(candidate) in haystack for candidate in candidates)

    def validate_and_score(self, raw_data: dict, ocr_text: str) -> DocumentMetadata:
        """SLMの辞書出力を検証し、確信度を計算した DocumentMetadata を返す。"""
        data = raw_data if isinstance(raw_data, dict) else {}
        haystack = self._compact(ocr_text or "")

        document_type = self._clean(data.get("document_type"))
        raw_issue_date = self._clean(data.get("issue_date"))
        document_number = self._clean(data.get("document_number"))
        sender = self._clean(data.get("sender"))
        recipient = self._clean(data.get("recipient"))

        issue_date = self.normalize_issue_date(raw_issue_date)

        score = 1.0
        reasons: list[str] = []

        if document_type is None:
            score -= PENALTY_REQUIRED_MISSING
            reasons.append("document_type欠損")
        if raw_issue_date is None:
            score -= PENALTY_REQUIRED_MISSING
            reasons.append("issue_date欠損")
        elif issue_date is None:
            score -= PENALTY_DATE_UNPARSABLE
            reasons.append(f"issue_date正規化不可({raw_issue_date})")

        for name, value in (
            ("document_number", document_number),
            ("sender", sender),
            ("recipient", recipient),
        ):
            if value is None:
                score -= PENALTY_OPTIONAL_MISSING
                reasons.append(f"{name}欠損")

        if haystack:
            for name, value in (
                ("document_number", document_number),
                ("sender", sender),
                ("recipient", recipient),
            ):
                if value is not None and not self._text_present(value, haystack):
                    score -= PENALTY_HALLUCINATION
                    reasons.append(f"{name}がOCRテキストに存在しない")
            if raw_issue_date is not None and not self._date_present(
                raw_issue_date, issue_date, haystack
            ):
                score -= PENALTY_HALLUCINATION
                reasons.append("issue_dateがOCRテキストに存在しない")

        score = round(min(1.0, max(0.0, score)), 4)
        needs_review = (
            score < self.threshold or document_type is None or issue_date is None
        )

        if reasons:
            logger.info(
                "確信度=%.2f needs_review=%s 減点理由=%s",
                score,
                needs_review,
                ", ".join(reasons),
            )

        try:
            return DocumentMetadata(
                document_type=document_type,
                issue_date=issue_date,
                document_number=document_number,
                sender=sender,
                recipient=recipient,
                confidence_score=score,
                needs_review=needs_review,
            )
        except ValidationError:
            logger.exception("DocumentMetadata の生成に失敗したため要確認として扱います。")
            return DocumentMetadata(
                document_type=None,
                issue_date=None,
                document_number=None,
                sender=None,
                recipient=None,
                confidence_score=0.0,
                needs_review=True,
            )
