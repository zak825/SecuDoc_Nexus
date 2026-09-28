import logging
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Optional

from app.core.config import settings
from app.schemas.document import (
    DocumentMetadata,
    DocumentProcessResponse,
    DocumentStatus,
)
from app.services.ocr_service import OCRService
from app.services.slm_service import SLMService
from app.services.validation_service import ValidationService

logger = logging.getLogger(__name__)

NEEDS_REVIEW_DIR = "NEEDS_REVIEW"
APPROVED_DIR = "APPROVED"
UNKNOWN_TYPE_DIR = "未分類"

_UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_path_segment(value: Optional[str], fallback: str) -> str:
    """フォルダ名に使えない文字を取り除き、空なら fallback を返す。"""
    if not value:
        return fallback
    cleaned = _UNSAFE_PATH_CHARS.sub("_", value).strip().strip(".")
    return cleaned or fallback


def build_destination_path(metadata: DocumentMetadata, filename: str) -> str:
    """要確認なら NEEDS_REVIEW/、確定可能なら APPROVED/<文書種別>/ に振り分ける。"""
    base = PurePosixPath(Path(settings.STORAGE_BASE_DIR).as_posix())
    safe_filename = sanitize_path_segment(Path(filename).name, "document")
    if metadata.needs_review:
        return (base / NEEDS_REVIEW_DIR / safe_filename).as_posix()
    type_dir = sanitize_path_segment(metadata.document_type, UNKNOWN_TYPE_DIR)
    return (base / APPROVED_DIR / type_dir / safe_filename).as_posix()


class DocumentPipelineService:
    """OCR → SLM → 検証 を一貫して実行し、DocumentProcessResponse を組み立てる。"""

    def __init__(
        self,
        ocr_service: Optional[OCRService] = None,
        slm_service: Optional[SLMService] = None,
        validation_service: Optional[ValidationService] = None,
    ) -> None:
        self.ocr_service = ocr_service or OCRService()
        self.slm_service = slm_service or SLMService()
        self.validation_service = validation_service or ValidationService()

    async def process_document(
        self,
        file_bytes: bytes,
        filename: str,
    ) -> DocumentProcessResponse:
        document_id = str(uuid.uuid4())
        safe_filename = Path(filename).name or "document"
        logger.info("処理開始: id=%s file=%s size=%d", document_id, safe_filename, len(file_bytes))

        try:
            ocr_text = await self.ocr_service.analyze_document(file_bytes)
        except Exception as exc:
            logger.exception("OCR段階で失敗しました: id=%s", document_id)
            return DocumentProcessResponse(
                document_id=document_id,
                filename=safe_filename,
                status=DocumentStatus.FAILED,
                metadata=None,
                destination_path=None,
                error_message=f"OCR処理に失敗しました: {exc}",
            )

        if not ocr_text.strip():
            logger.warning("OCRテキストが空でした: id=%s", document_id)

        raw_metadata = await self.slm_service.extract_metadata(ocr_text)

        metadata = self.validation_service.validate_and_score(raw_metadata, ocr_text)

        destination = build_destination_path(metadata, safe_filename)
        status = (
            DocumentStatus.NEEDS_REVIEW if metadata.needs_review else DocumentStatus.PENDING
        )

        error_message = None
        if not raw_metadata:
            error_message = "SLMからメタデータを取得できなかったため要確認として登録しました。"

        logger.info(
            "処理完了: id=%s status=%s confidence=%.2f dest=%s",
            document_id,
            status.value,
            metadata.confidence_score,
            destination,
        )
        return DocumentProcessResponse(
            document_id=document_id,
            filename=safe_filename,
            status=status,
            metadata=metadata,
            destination_path=destination,
            error_message=error_message,
        )


_pipeline_singleton: Optional[DocumentPipelineService] = None


def get_pipeline_service() -> DocumentPipelineService:
    """FastAPI の Depends から利用する共有インスタンス。"""
    global _pipeline_singleton
    if _pipeline_singleton is None:
        _pipeline_singleton = DocumentPipelineService()
    return _pipeline_singleton
