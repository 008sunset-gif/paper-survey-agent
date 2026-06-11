"""
Searcher のユニットテスト（実APIは一切叩かない）

`src.paper_survey.searcher.requests.get` をモックして、
- arXiv の Atom XML が正しく Paper にパースされる
- Semantic Scholar の JSON が正しく Paper にパースされる
- 429 / ネットワーク例外でリトライし、最終的に成功 or SearchError になる
ことを検証する。
"""

from unittest import mock

import pytest
import requests

from src.paper_survey import searcher
from src.paper_survey.schemas import Paper


# ===== モックレスポンス =====
class FakeResponse:
    """requests.Response の最小モック"""

    def __init__(self, *, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


# ===== モックデータ =====
ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2310.06825v1</id>
    <title>
      Mistral 7B
    </title>
    <summary>  We introduce Mistral 7B, a 7-billion-parameter language
      model engineered for superior performance.  </summary>
    <published>2023-10-10T17:54:00Z</published>
    <author><name>Albert Q. Jiang</name></author>
    <author><name>Alexandre Sablayrolles</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1706.03762v5</id>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models are based on
      complex recurrent or convolutional neural networks.</summary>
    <published>2017-06-12T17:57:34Z</published>
    <author><name>Ashish Vaswani</name></author>
  </entry>
</feed>
"""

S2_JSON = {
    "total": 2,
    "data": [
        {
            "paperId": "df2b0e26d0599ce3e70df8a9da02e51594e0e992",
            "title": "BERT: Pre-training of Deep Bidirectional Transformers",
            "abstract": "We introduce a new language representation model called BERT.",
            "year": 2019,
            "authors": [
                {"name": "Jacob Devlin"},
                {"name": "Ming-Wei Chang"},
            ],
            "externalIds": {"ArXiv": "1810.04805", "DBLP": "conf/naacl/DevlinCLT19"},
            "url": "https://www.semanticscholar.org/paper/df2b0e26",
        },
        {
            # abstract が null のケース（S2 ではよくある）
            "paperId": "abc123",
            "title": "A Paper Without Abstract",
            "abstract": None,
            "year": None,
            "authors": [],
            "externalIds": {},
            "url": None,
        },
    ],
}


# ===== arXiv パース =====
def test_search_arxiv_parses_entries():
    with mock.patch.object(
        searcher.requests, "get", return_value=FakeResponse(text=ARXIV_XML)
    ) as mocked_get:
        papers = searcher.search_arxiv("mistral", max_results=2)

    # 実APIは叩いていない（モック経由）こと
    assert mocked_get.call_count == 1

    assert len(papers) == 2
    assert all(isinstance(p, Paper) for p in papers)

    first = papers[0]
    assert first.paper_id == "2310.06825v1"
    assert first.source == "arxiv"
    assert first.title == "Mistral 7B"  # 前後の空白/改行が畳まれている
    assert first.year == 2023
    assert first.authors == ["Albert Q. Jiang", "Alexandre Sablayrolles"]
    assert first.abstract.startswith("We introduce Mistral 7B")
    assert "  " not in first.abstract  # 連続空白が畳まれている
    assert first.url == "http://arxiv.org/abs/2310.06825v1"
    assert first.citation_key == "arxiv:2310.06825v1"

    assert papers[1].paper_id == "1706.03762v5"
    assert papers[1].authors == ["Ashish Vaswani"]


# ===== Semantic Scholar パース =====
def test_search_semantic_scholar_parses_items():
    with mock.patch.object(
        searcher.requests, "get", return_value=FakeResponse(json_data=S2_JSON)
    ) as mocked_get:
        papers = searcher.search_semantic_scholar("bert", max_results=2)

    assert mocked_get.call_count == 1
    assert len(papers) == 2

    bert = papers[0]
    assert bert.source == "semantic_scholar"
    assert bert.paper_id == "df2b0e26d0599ce3e70df8a9da02e51594e0e992"
    assert bert.title.startswith("BERT")
    assert bert.year == 2019
    assert bert.authors == ["Jacob Devlin", "Ming-Wei Chang"]
    assert bert.abstract.startswith("We introduce a new language")

    # abstract=null / year=null / url=null の防御的処理
    empty = papers[1]
    assert empty.abstract == ""
    assert empty.year is None
    assert empty.authors == []
    # url が null のとき paperId からフォールバック生成
    assert empty.url == "https://www.semanticscholar.org/paper/abc123"


# ===== リトライ: 429 が続いたあと成功 =====
def test_retry_succeeds_after_429():
    responses = [
        FakeResponse(status_code=429),
        FakeResponse(status_code=429),
        FakeResponse(json_data=S2_JSON),  # 3回目で成功
    ]
    sleeps = []  # 実時間を消費せず、待機が呼ばれたかだけ記録

    with mock.patch.object(searcher.requests, "get", side_effect=responses) as mocked_get:
        papers = searcher.search_semantic_scholar(
            "bert",
            max_results=2,
            sleeper=lambda d: sleeps.append(d),
            base_delay=1.0,
        )

    assert mocked_get.call_count == 3
    # 2回リトライ → 指数バックオフ 1s, 2s
    assert sleeps == [1.0, 2.0]
    assert len(papers) == 2


# ===== リトライ: ネットワーク例外のあと成功 =====
def test_retry_succeeds_after_network_error():
    side_effects = [
        requests.exceptions.ConnectionError("boom"),
        FakeResponse(text=ARXIV_XML),  # 2回目で成功
    ]
    sleeps = []

    with mock.patch.object(searcher.requests, "get", side_effect=side_effects) as mocked_get:
        papers = searcher.search_arxiv(
            "mistral", max_results=2, sleeper=lambda d: sleeps.append(d)
        )

    assert mocked_get.call_count == 2
    assert sleeps == [1.0]  # 1回リトライ
    assert len(papers) == 2


# ===== リトライ: 上限まで失敗 → SearchError =====
def test_retry_exhausted_raises_search_error():
    sleeps = []

    with mock.patch.object(
        searcher.requests, "get", return_value=FakeResponse(status_code=429)
    ) as mocked_get:
        with pytest.raises(searcher.SearchError):
            searcher.search_semantic_scholar(
                "bert",
                max_results=2,
                max_retries=4,
                sleeper=lambda d: sleeps.append(d),
            )

    # 4回試行し、試行間の待機は 3 回
    assert mocked_get.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]


# ===== 4xx(429以外) は即座にエラー（リトライしない）=====
def test_non_retryable_4xx_raises_immediately():
    sleeps = []

    with mock.patch.object(
        searcher.requests, "get", return_value=FakeResponse(status_code=400)
    ) as mocked_get:
        with pytest.raises(requests.exceptions.HTTPError):
            searcher.search_arxiv(
                "x", max_results=1, sleeper=lambda d: sleeps.append(d)
            )

    assert mocked_get.call_count == 1  # リトライしていない
    assert sleeps == []


# ===== S2: APIキーがあれば x-api-key ヘッダーが付く =====
def test_semantic_scholar_adds_api_key_header_when_present():
    with mock.patch.object(
        searcher.requests, "get", return_value=FakeResponse(json_data=S2_JSON)
    ) as mocked_get:
        searcher.search_semantic_scholar("bert", max_results=2, api_key="SECRET_KEY")

    headers = mocked_get.call_args.kwargs.get("headers")
    assert headers == {"x-api-key": "SECRET_KEY"}


# ===== S2: キーが無ければヘッダーは付かない（無認証で動く）=====
def test_semantic_scholar_no_header_without_key(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    with mock.patch.object(
        searcher.requests, "get", return_value=FakeResponse(json_data=S2_JSON)
    ) as mocked_get:
        searcher.search_semantic_scholar("bert", max_results=2)

    # headers は None（x-api-key を付けない）
    assert mocked_get.call_args.kwargs.get("headers") is None


# ===== S2: 環境変数のキーも拾う =====
def test_semantic_scholar_uses_env_key(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "ENV_KEY")
    with mock.patch.object(
        searcher.requests, "get", return_value=FakeResponse(json_data=S2_JSON)
    ) as mocked_get:
        searcher.search_semantic_scholar("bert", max_results=2)

    assert mocked_get.call_args.kwargs.get("headers") == {"x-api-key": "ENV_KEY"}
