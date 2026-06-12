"""비동기 OpenDART 클라이언트 v2.

추가점: EB/RCPS 정형 엔드포인트, corp_code 지정 공시검색,
공시 원본(document.xml zip) 다운로드·텍스트 추출.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from typing import Any, Optional

import httpx

from config import DART_ENDPOINTS, DART_VIEWER, SETTINGS, Settings

logger = logging.getLogger("mezzanine.dart")

_NO_DATA_STATUS = {"013"}
_FATAL_STATUS = {"010", "011", "012", "020", "021", "100", "101", "800", "901"}
_TAG_RE = re.compile(r"<[^>]+>")

STRUCTURED_KINDS = ("cb", "bw", "eb", "rcps")


class DartAPIError(RuntimeError):
    def __init__(self, status: str, message: str, endpoint: str) -> None:
        super().__init__(f"[{endpoint}] status={status} message={message}")
        self.status = status
        self.message = message


def viewer_url(rcept_no: str) -> str:
    """DART 공시뷰어 PC URL."""
    return DART_VIEWER.format(rcept_no=rcept_no)


class DartClient:
    """OpenDART 비동기 클라이언트 (세마포어 + 지수백오프 재시도)."""

    def __init__(self, settings: Settings = SETTINGS) -> None:
        self._s = settings
        self._s.validate()
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "DartClient":
        self._client = httpx.AsyncClient(
            timeout=self._s.request_timeout,
            headers={"User-Agent": "mezzanine-radar/2.0"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ------------------------------------------------------------------ #
    async def _get_raw(self, url: str, params: dict[str, Any]) -> httpx.Response:
        """재시도 포함 저수준 GET. HTTP 레벨 오류만 처리(본문 해석은 호출측)."""
        assert self._client is not None
        payload = {**params, "crtfc_key": self._s.dart_api_key}
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._s.max_retries + 1):
            try:
                async with self._sem:
                    await asyncio.sleep(self._s.inter_request_delay)
                    resp = await self._client.get(url, params=payload)
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                sleep_for = self._s.backoff_base * (2 ** (attempt - 1))
                logger.warning("retry %s/%s %s err=%s", attempt,
                               self._s.max_retries, url, exc)
                await asyncio.sleep(sleep_for)
        raise DartAPIError("HTTP_RETRY_EXHAUSTED", str(last_exc), url)

    async def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """JSON 엔드포인트 GET + DART status 처리."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._s.max_retries + 1):
            try:
                resp = await self._get_raw(url, params)
                data = resp.json()
                status = str(data.get("status", "000"))
                if status == "000":
                    return data
                if status in _NO_DATA_STATUS:
                    return {"status": status, "list": []}
                if status in _FATAL_STATUS:
                    raise DartAPIError(status, data.get("message", ""), url)
                last_exc = DartAPIError(status, data.get("message", ""), url)
            except ValueError as exc:  # JSON 파싱 실패
                last_exc = exc
            await asyncio.sleep(self._s.backoff_base * (2 ** (attempt - 1)))
        raise DartAPIError("RETRY_EXHAUSTED", str(last_exc), url)

    # ------------------------------------------------------------------ #
    # 공시검색
    # ------------------------------------------------------------------ #
    async def search_disclosures(
        self,
        bgn_de: str,
        end_de: str,
        pblntf_ty: Optional[str] = None,
        corp_code: Optional[str] = None,
        page_count: int = 100,
        max_pages: int = 1000,
    ) -> list[dict[str, Any]]:
        """기간(±회사) 공시 목록 전체 페이지 수집.

        Parameters
        ----------
        bgn_de, end_de : str
            YYYYMMDD.
        pblntf_ty : str, optional
            'B' 주요사항보고, 'I' 거래소공시 등.
        corp_code : str, optional
            특정 회사로 한정(컴플라이언스 단건 점검에 사용).
        """
        out: list[dict[str, Any]] = []
        page_no = 1
        while page_no <= max_pages:
            params: dict[str, Any] = {
                "bgn_de": bgn_de, "end_de": end_de,
                "page_no": page_no, "page_count": page_count,
            }
            if pblntf_ty:
                params["pblntf_ty"] = pblntf_ty
            if corp_code:
                params["corp_code"] = corp_code
            data = await self._get_json(DART_ENDPOINTS["list"], params)
            out.extend(data.get("list", []))
            total_page = int(data.get("total_page", 1) or 1)
            if page_no >= total_page:
                break
            page_no += 1
        return out

    # ------------------------------------------------------------------ #
    # 정형 주요사항보고 (CB/BW/EB/RCPS)
    # ------------------------------------------------------------------ #
    async def fetch_major_report(
        self, kind: str, corp_code: str, bgn_de: str, end_de: str
    ) -> list[dict[str, Any]]:
        """단일 회사 정형 발행결정 조회.

        kind : {'cb','bw','eb','rcps'}
        """
        if kind not in STRUCTURED_KINDS:
            raise ValueError(f"unsupported kind: {kind}")
        params = {"corp_code": corp_code, "bgn_de": bgn_de, "end_de": end_de}
        data = await self._get_json(DART_ENDPOINTS[kind], params)
        rows = data.get("list", [])
        for r in rows:
            r["_sec_kind"] = kind
        return rows

    async def fetch_major_reports_bulk(
        self, kind: str, corp_codes: list[str], bgn_de: str, end_de: str
    ) -> list[dict[str, Any]]:
        """여러 회사 동시 수집 — 개별 실패는 건너뛴다."""
        async def _one(cc: str) -> list[dict[str, Any]]:
            try:
                return await self.fetch_major_report(kind, cc, bgn_de, end_de)
            except DartAPIError as exc:
                logger.error("fetch fail kind=%s corp=%s err=%s", kind, cc, exc)
                return []
        results = await asyncio.gather(*(_one(cc) for cc in corp_codes))
        flat = [row for sub in results for row in sub]
        logger.info("bulk %s corps=%d rows=%d", kind, len(corp_codes), len(flat))
        return flat

    # ------------------------------------------------------------------ #
    # 공시 원본 다운로드 (zip → 텍스트)
    # ------------------------------------------------------------------ #
    async def fetch_document_text(self, rcept_no: str) -> Optional[str]:
        """document.xml(zip) 다운로드 후 평문 텍스트로 추출.

        풋옵션·정기 리픽싱·콜옵션·대상자 등 본문 전용 항목 파싱에 사용.
        실패 시 None (파이프라인은 정형 데이터만으로 계속 진행).
        """
        try:
            resp = await self._get_raw(
                DART_ENDPOINTS["document"], {"rcept_no": rcept_no}
            )
            content = resp.content
            if not content.startswith(b"PK"):   # zip 아님 → 오류 응답(XML)
                logger.warning("document not zip rcept=%s head=%r",
                               rcept_no, content[:80])
                return None
            texts: list[str] = []
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for name in zf.namelist():
                    raw = zf.read(name)
                    for enc in ("utf-8", "cp949", "euc-kr"):
                        try:
                            texts.append(raw.decode(enc))
                            break
                        except UnicodeDecodeError:
                            continue
            full = "\n".join(texts)
            return _TAG_RE.sub(" ", full)
        except Exception as exc:
            logger.error("document fetch fail rcept=%s err=%s", rcept_no, exc)
            return None
