import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from app.core.config import settings
from app.schemas.document import (
    DocumentMetadata,
    DocumentProcessResponse,
    DocumentStatus,
    evaluate_needs_review,
)

router = APIRouter()

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
    ".webp",
}
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/tiff",
    "image/bmp",
    "image/gif",
    "image/webp",
}

# 永続化は services 層へ移すまでのインメモリ置き場。
_documents: dict[str, DocumentProcessResponse] = {}


def _is_allowed_document(upload: UploadFile) -> bool:
    filename = upload.filename or ""
    extension = Path(filename).suffix.lower()
    content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    return extension in ALLOWED_EXTENSIONS or content_type in ALLOWED_CONTENT_TYPES


def _get_document_or_404(document_id: str) -> DocumentProcessResponse:
    document = _documents.get(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="指定された文書が見つかりません。")
    return document


@router.post("/upload", response_model=DocumentProcessResponse)
async def upload_document(file: UploadFile) -> DocumentProcessResponse:
    """PDFまたは画像を受け取り、処理結果のモックを返す。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="ファイル名がありません。")
    if not _is_allowed_document(file):
        raise HTTPException(
            status_code=400,
            detail="PDFまたは画像ファイルのみ受け付けます。",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="空のファイルは受け付けません。")

    filename = Path(file.filename).name
    confidence_score = 0.72
    metadata = DocumentMetadata(
        document_type="通知書",
        issue_date="2026-04-01",
        document_number=None,
        sender="○○市役所",
        recipient="△△課",
        confidence_score=confidence_score,
        needs_review=True,
    )
    metadata.needs_review = evaluate_needs_review(
        metadata,
        settings.CONFIDENCE_THRESHOLD,
    )
    status = (
        DocumentStatus.NEEDS_REVIEW
        if metadata.needs_review
        else DocumentStatus.PENDING
    )
    response = DocumentProcessResponse(
        document_id=str(uuid.uuid4()),
        filename=filename,
        status=status,
        metadata=metadata,
        destination_path=None,
        error_message=None,
    )
    _documents[response.document_id] = response
    return response


@router.get("/{document_id}", response_model=DocumentProcessResponse)
async def get_document(document_id: str) -> DocumentProcessResponse:
    """文書IDに対応する処理状態とメタデータを返す。"""
    return _get_document_or_404(document_id)


@router.post("/{document_id}/approve", response_model=DocumentProcessResponse)
async def approve_document(
    document_id: str,
    metadata: DocumentMetadata,
) -> DocumentProcessResponse:
    """人手で修正したメタデータを受け取り、確定結果のモックを返す。"""
    current = _get_document_or_404(document_id)
    metadata.needs_review = evaluate_needs_review(
        metadata,
        settings.CONFIDENCE_THRESHOLD,
    )
    destination = (
        Path(settings.STORAGE_BASE_DIR)
        / "approved"
        / document_id
        / Path(current.filename).name
    )
    approved = DocumentProcessResponse(
        document_id=document_id,
        filename=current.filename,
        status=DocumentStatus.APPROVED,
        metadata=metadata,
        destination_path=destination.as_posix(),
        error_message=None,
    )
    _documents[document_id] = approved
    return approved
