"""
Searcher - arXiv / Semantic Scholar から論文を検索する

それぞれの公開APIを叩き、レスポンスを共通の `Paper` 型に正規化する。
Semantic Scholar は無認証だと 429 (rate limit) を返しやすいため、
リトライ + 指数バックオフで耐える。

【重要】このモジュールは実APIへのアクセス手段を提供するが、
ユニットテストでは `requests.get` をモックし、実APIは一切叩かない。
実行は利用者の明示的な GO が出てから。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, List, Optional
from xml.etree import ElementTree as ET

import requests

from src.paper_survey.schemas import Paper

logger = logging.getLogger(__name__)

# === エンドポイント ===
ARXIV_API_URL = "http://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR_API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

# arXiv が返す Atom XML の名前空間
_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# Semantic Scholar で取得するフィールド
_S2_DEFAULT_FIELDS = "title,abstract,year,authors,externalIds,url"


class SearchError(Exception):
    """検索が（リトライ後も）最終的に失敗したときに送出"""


# === リトライ付きGET ===
def _request_with_retry(
    url: str,
    params: dict,
    *,
    headers: Optional[dict] = None,
    max_retries: int = 5,
    base_delay: float = 1.0,
    timeout: float = 20.0,
    sleeper: Callable[[float], None] = time.sleep,
    session: Optional[requests.Session] = None,
) -> requests.Response:
    """
    指数バックオフ付きのGET。

    429 (rate limit) と 5xx、およびネットワーク例外でリトライする。
    待ち時間は base_delay * 2**attempt 秒（1, 2, 4, 8, ...）。
    最後の試行後は待たずに諦めて SearchError を送出する。

    Args:
        headers: 任意のHTTPヘッダー（S2 の x-api-key など）。
        sleeper: 待機関数。テストでは no-op を渡して実時間を消費しない。
        session: 任意の requests.Session（コネクション再利用用）。
    """
    get = (session or requests).get
    last_error: Optional[BaseException] = None

    for attempt in range(max_retries):
        try:
            resp = get(url, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            # ネットワーク系（タイムアウト/接続断など）→ リトライ対象
            last_error = exc
            logger.warning(
                "request error (attempt %d/%d): %s", attempt + 1, max_retries, exc
            )
        else:
            status = resp.status_code
            if status == 429 or 500 <= status < 600:
                # レート制限 / サーバ側一時障害 → リトライ対象
                last_error = SearchError(f"retryable HTTP status {status}")
                logger.warning(
                    "retryable status %d (attempt %d/%d)",
                    status,
                    attempt + 1,
                    max_retries,
                )
            else:
                # 4xx(429除く) は raise、2xx/3xx は成功として返す
                resp.raise_for_status()
                return resp

        # 最後の試行のあとは待たない
        if attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt)
            logger.debug("backing off %.1fs before retry", delay)
            sleeper(delay)

    raise SearchError(
        f"request to {url} failed after {max_retries} attempts"
    ) from last_error


# === arXiv ===
def _parse_arxiv(xml_text: str) -> List[Paper]:
    """arXiv の Atom XML を Paper のリストに変換"""
    root = ET.fromstring(xml_text)
    papers: List[Paper] = []

    for entry in root.findall("atom:entry", _ATOM_NS):
        raw_id = (entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()
        # id は http://arxiv.org/abs/2310.06825v1 形式 → 末尾のIDを取り出す
        arxiv_id = raw_id.rsplit("/abs/", 1)[-1] if "/abs/" in raw_id else raw_id

        # タイトル/要旨は改行やインデントが入るので空白を畳む
        title = " ".join(
            (entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").split()
        )
        abstract = " ".join(
            (entry.findtext("atom:summary", default="", namespaces=_ATOM_NS) or "").split()
        )

        published = entry.findtext("atom:published", default="", namespaces=_ATOM_NS) or ""
        year = int(published[:4]) if published[:4].isdigit() else None

        authors = []
        for author in entry.findall("atom:author", _ATOM_NS):
            name = (author.findtext("atom:name", default="", namespaces=_ATOM_NS) or "").strip()
            if name:
                authors.append(name)

        papers.append(
            Paper(
                paper_id=arxiv_id,
                source="arxiv",
                title=title,
                authors=authors,
                year=year,
                abstract=abstract,
                url=raw_id or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
            )
        )

    return papers


def search_arxiv(query: str, max_results: int = 10, **retry_kwargs) -> List[Paper]:
    """
    arXiv API で論文を検索する。

    Args:
        query: 検索クエリ
        max_results: 取得する最大件数
        **retry_kwargs: _request_with_retry に渡す（max_retries, base_delay,
            sleeper, session など）

    Returns:
        List[Paper]: 正規化済み論文リスト
    """
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
    }
    resp = _request_with_retry(ARXIV_API_URL, params, **retry_kwargs)
    return _parse_arxiv(resp.text)


# === Semantic Scholar ===
def _parse_semantic_scholar(payload: dict) -> List[Paper]:
    """Semantic Scholar の JSON を Paper のリストに変換"""
    papers: List[Paper] = []

    for item in payload.get("data") or []:
        paper_id = item.get("paperId") or ""
        authors = [
            (a.get("name") or "").strip()
            for a in (item.get("authors") or [])
            if (a.get("name") or "").strip()
        ]
        url = item.get("url") or (
            f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else ""
        )

        papers.append(
            Paper(
                paper_id=paper_id,
                source="semantic_scholar",
                title=item.get("title") or "",
                authors=authors,
                year=item.get("year"),  # None 可
                abstract=item.get("abstract") or "",  # S2 は abstract が null のことがある
                url=url,
            )
        )

    return papers


def search_semantic_scholar(
    query: str,
    max_results: int = 10,
    *,
    fields: str = _S2_DEFAULT_FIELDS,
    api_key: Optional[str] = None,
    **retry_kwargs,
) -> List[Paper]:
    """
    Semantic Scholar API で論文を検索する（本パイプラインでは「補助ソース」）。

    無認証だと 429（レート制限）が厳しいため、Collector 側で逐次＋間隔制御して
    礼儀正しく叩く。API キーがあれば `x-api-key` ヘッダーを付けて上限を緩和する
    （無くても動作する）。

    Args:
        query: 検索クエリ
        max_results: 取得する最大件数（API の limit）
        fields: 取得するフィールド（カンマ区切り）
        api_key: S2 API キー。未指定なら環境変数 SEMANTIC_SCHOLAR_API_KEY を見る。
            どちらも無ければ無認証で叩く。
        **retry_kwargs: _request_with_retry に渡す

    Returns:
        List[Paper]: 正規化済み論文リスト
    """
    params = {
        "query": query,
        "limit": max_results,
        "fields": fields,
    }

    # キーがあれば付ける（将来キーを入手したらそのまま使える / 今は無くても動く）
    key = api_key or os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    headers = {"x-api-key": key} if key else None

    resp = _request_with_retry(
        SEMANTIC_SCHOLAR_API_URL, params, headers=headers, **retry_kwargs
    )
    return _parse_semantic_scholar(resp.json())
