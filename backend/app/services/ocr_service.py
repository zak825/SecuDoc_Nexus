import asyncio
import logging
from typing import Any, Optional

from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential

from app.core.config import settings

logger = logging.getLogger(__name__)


class OCRConfigurationError(RuntimeError):
    """Azure AI Document Intelligence の接続情報が設定されていない。"""


class OCRService:
    """Azure AI Document Intelligence (prebuilt-read) でテキストを抽出する。"""

    MODEL_ID = "prebuilt-read"

    def __init__(self, client: Optional[DocumentAnalysisClient] = None) -> None:
        self._client = client

    def _get_client(self) -> DocumentAnalysisClient:
        if self._client is not None:
            return self._client

        endpoint = settings.AZURE_DOC_INTELLIGENCE_ENDPOINT.strip()
        key = settings.AZURE_DOC_INTELLIGENCE_KEY.strip()
        if not endpoint or not key:
            raise OCRConfigurationError(
                "AZURE_DOC_INTELLIGENCE_ENDPOINT と AZURE_DOC_INTELLIGENCE_KEY を "
                "環境変数に設定してください。"
            )

        self._client = DocumentAnalysisClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(key),
        )
        return self._client

    def _analyze_sync(self, file_bytes: bytes) -> Any:
        client = self._get_client()
        poller = client.begin_analyze_document(self.MODEL_ID, document=file_bytes)
        return poller.result()

    @staticmethod
    def _format_result(result: Any) -> str:
        pages = getattr(result, "pages", None) or []
        page_texts: list[str] = []
        for page in pages:
            lines = getattr(page, "lines", None) or []
            line_texts = [
                line.content.strip()
                for line in lines
                if getattr(line, "content", None) and line.content.strip()
            ]
            if line_texts:
                page_texts.append("\n".join(line_texts))

        if page_texts:
            return "\n\n".join(page_texts).strip()

        content = getattr(result, "content", None)
        return content.strip() if isinstance(content, str) else ""

    async def analyze_document(self, file_bytes: bytes) -> str:
        """PDF/画像のバイト列からテキストを抽出し、ページ・行を整形して返す。"""
        if not file_bytes:
            raise ValueError("OCR対象のファイルが空です。")

        try:
            result = await asyncio.to_thread(self._analyze_sync, file_bytes)
        except OCRConfigurationError:
            logger.error("OCRサービスの設定が不足しています。")
            raise
        except Exception:
            logger.exception("Azure AI Document Intelligence の解析に失敗しました。")
            raise

        text = self._format_result(result)
        logger.info(
            "OCR完了: pages=%d, chars=%d",
            len(getattr(result, "pages", None) or []),
            len(text),
        )
        return text
