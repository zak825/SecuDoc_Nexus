from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class DocumentStatus(str, Enum):
    PENDING = "PENDING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    FAILED = "FAILED"


class DocumentMetadata(BaseModel):
    """AI抽出結果のコアデータ。"""

    document_type: Optional[str] = Field(
        default=None,
        description='文書種別（例: "申請書", "通知書", "契約書", "請求書", "その他"）',
        examples=["通知書"],
    )
    issue_date: Optional[str] = Field(
        default=None,
        description="発行日（YYYY-MM-DD形式文字列）",
        examples=["2026-04-01"],
    )
    document_number: Optional[str] = Field(
        default=None,
        description="文書番号・発信番号",
        examples=["市総第1号"],
    )
    sender: Optional[str] = Field(
        default=None,
        description="差出人",
        examples=["○○市役所"],
    )
    recipient: Optional[str] = Field(
        default=None,
        description="宛先",
        examples=["△△課"],
    )
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="抽出結果の確信度（0.0〜1.0）",
    )
    needs_review: bool = Field(
        ...,
        description="確信度が閾値未満、または必須項目が欠損している場合に True",
    )

    @field_validator(
        "document_type",
        "document_number",
        "sender",
        "recipient",
        mode="before",
    )
    @classmethod
    def blank_string_to_none(cls, value: Optional[str]) -> Optional[str]:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("issue_date")
    @classmethod
    def validate_issue_date(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise ValueError("issue_date は YYYY-MM-DD 形式で指定してください。") from exc
        return value


class DocumentProcessResponse(BaseModel):
    """文書処理APIのレスポンス。"""

    document_id: str = Field(..., description="文書ID（UUID）")
    filename: str
    status: DocumentStatus
    metadata: Optional[DocumentMetadata] = None
    destination_path: Optional[str] = None
    error_message: Optional[str] = None


def evaluate_needs_review(
    metadata: DocumentMetadata,
    confidence_threshold: float,
) -> bool:
    """確信度不足、または必須項目の欠損があればレビュー対象にする。"""
    required_values = (
        metadata.document_type,
        metadata.issue_date,
        metadata.document_number,
        metadata.sender,
        metadata.recipient,
    )
    missing = any(value is None or not value.strip() for value in required_values)
    return missing or metadata.confidence_score < confidence_threshold
