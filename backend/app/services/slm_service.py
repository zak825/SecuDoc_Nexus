import json
import logging
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたは日本の行政文書を解析する情報抽出エンジンです。
与えられたOCRテキストから、以下の5項目を抽出してください。

- document_type: 文書種別。「申請書」「通知書」「契約書」「請求書」「その他」のいずれか。
- issue_date: 発行年月日。必ず YYYY-MM-DD 形式（西暦）に変換する。和暦（令和・平成など）は西暦に換算する。
- document_number: 文書番号・発信番号（例: 「市総第123号」）。
- sender: 差出人（発信者・発信機関）。
- recipient: 宛先（受信者・受信機関）。

厳守事項:
1. 出力は純粋なJSONオブジェクトのみ。説明文、前置き、Markdownのコードブロックは一切含めない。
2. キーは上記5つだけを使い、すべてのキーを必ず含める。
3. テキスト中に存在しない情報は推測せず null にする。捏造は禁止。
4. 値はOCRテキストに書かれている表記を可能な限りそのまま使う（issue_dateのみ形式変換する）。

出力例:
{"document_type": "通知書", "issue_date": "2026-04-01", "document_number": "市総第12号", "sender": "○○市長", "recipient": "△△株式会社 代表取締役"}
"""

EXPECTED_KEYS = (
    "document_type",
    "issue_date",
    "document_number",
    "sender",
    "recipient",
)


class SLMService:
    """Ollama の /api/generate に問い合わせてメタデータをJSONで抽出する。"""

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        timeout: float = 120.0,
    ) -> None:
        self._client = client
        self._timeout = timeout

    @property
    def _generate_url(self) -> str:
        return f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate"

    def _build_payload(self, ocr_text: str) -> dict[str, Any]:
        prompt = (
            "次のOCRテキストからメタデータを抽出し、JSONのみを出力してください。\n\n"
            "=== OCRテキスト開始 ===\n"
            f"{ocr_text}\n"
            "=== OCRテキスト終了 ==="
        )
        return {
            "model": settings.OLLAMA_MODEL,
            "system": SYSTEM_PROMPT,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.0},
        }

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                self._generate_url,
                json=payload,
                timeout=self._timeout,
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(self._generate_url, json=payload)

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        """JSON文字列を辞書化する。前後にゴミがあれば最初の {...} を切り出して再試行する。"""
        text = raw.strip()
        if not text:
            return {}

        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                logger.error("SLM応答からJSONオブジェクトを見つけられませんでした。")
                return {}
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.error("SLM応答のJSONパースに失敗しました。")
                return {}

        if not isinstance(parsed, dict):
            logger.error("SLM応答がJSONオブジェクトではありません: %s", type(parsed).__name__)
            return {}
        return parsed

    @staticmethod
    def _normalize(parsed: dict[str, Any]) -> dict[str, Optional[str]]:
        """期待キーだけを残し、値を文字列または None に揃える。"""
        normalized: dict[str, Optional[str]] = {}
        for key in EXPECTED_KEYS:
            value = parsed.get(key)
            if value is None:
                normalized[key] = None
                continue
            if isinstance(value, (list, dict)):
                normalized[key] = None
                continue
            text = str(value).strip()
            normalized[key] = text if text and text.lower() not in {"null", "none", "不明", "なし"} else None
        return normalized

    async def extract_metadata(self, ocr_text: str) -> dict:
        """OCRテキストからメタデータ辞書を抽出する。失敗時は空辞書を返す。"""
        if not ocr_text or not ocr_text.strip():
            logger.warning("OCRテキストが空のためSLM解析をスキップします。")
            return {}

        payload = self._build_payload(ocr_text)

        try:
            response = await self._post(payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Ollama がエラー応答を返しました: status=%s body=%s",
                exc.response.status_code,
                exc.response.text[:500],
            )
            return {}
        except httpx.HTTPError as exc:
            logger.error("Ollama への接続に失敗しました: %s", exc)
            return {}

        try:
            body = response.json()
        except ValueError:
            logger.error("Ollama 応答本文がJSONではありません。")
            return {}

        raw_output = body.get("response") if isinstance(body, dict) else None
        if not isinstance(raw_output, str):
            logger.error("Ollama 応答に response フィールドがありません。")
            return {}

        parsed = self._parse_json_object(raw_output)
        if not parsed:
            return {}

        result = self._normalize(parsed)
        logger.info("SLM抽出完了: %s", {k: (v is not None) for k, v in result.items()})
        return result
