"""
Collector（並列収集層）のユニットテスト（実APIは一切叩かない）

Searcher はモック関数を注入して差し替える。検証:
- 複数クエリ×複数ソースが「本当に並列」で処理される（threading.Barrier で確定的に証明）
- 一部の検索が失敗しても成功分が返り、失敗が記録される（部分失敗の救済）
- 観点をまたいだ重複（同一 paper_id）が統合され、見つかった観点が保持される
- 全タスク失敗時は CollectionError
- papers_per_aspect / max_queries_per_aspect / max_concurrent_requests を尊重
"""

import threading
import time

import pytest

from src.paper_survey import collector
from src.paper_survey.schemas import (
    Paper,
    SearchAspect,
    SurveyConfig,
    SurveyPlan,
)


# ===== ヘルパー =====
def _paper(pid, source="arxiv"):
    return Paper(
        paper_id=pid, source=source, title=f"title-{pid}", authors=[],
        year=2020, abstract="", url=f"http://x/{pid}",
    )


def _plan(aspects):
    """aspects: list of (name, [queries])"""
    return SurveyPlan(
        theme="テーマ",
        aspects=[
            SearchAspect(name=n, intent="i", search_queries=q) for n, q in aspects
        ],
    )


def make_searcher(source, papers_for, calls, *, barrier=None, fail=False, tracker=None, sleep=0.0):
    """
    モック検索関数を作る。
    - papers_for(query) -> List[Paper]（max_results では切らない＝Collector側の上限を検証）
    - calls に呼び出し記録を残す
    - barrier: 渡すと wait し、並列実行を確定的に強制
    - tracker: 同時実行数を計測
    - fail: True なら例外を投げる
    """

    def fn(query, max_results=10):
        calls.append({"source": source, "query": query, "max_results": max_results})
        if tracker is not None:
            tracker.enter()
        try:
            if barrier is not None:
                barrier.wait(timeout=5)  # 並列でなければ timeout → BrokenBarrierError
            if sleep:
                time.sleep(sleep)
            if fail:
                raise TimeoutError(f"boom on {source}:{query}")
            return list(papers_for(query))
        finally:
            if tracker is not None:
                tracker.exit()

    return fn


class ConcurrencyTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter(self):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def exit(self):
        with self._lock:
            self.active -= 1


# ===== 主ソース arXiv の並列実行（Barrier で確定的に証明）=====
def test_searches_run_in_parallel():
    # 主ソース arXiv の並列性を証明（1観点 × 4クエリ = 4タスク、全て並列）
    plan = _plan([("観点A", ["q1", "q2", "q3", "q4"])])
    calls_a = []
    barrier = threading.Barrier(4)  # 4タスクが同時に到達しないと進めない

    searchers = {
        "arxiv": make_searcher("arxiv", lambda q: [_paper(f"arxiv-{q}")], calls_a, barrier=barrier),
    }
    config = SurveyConfig(max_concurrent_requests=4, papers_per_aspect=10, max_queries_per_aspect=4)

    result = collector.collect_papers(plan, config=config, searchers=searchers)

    # 逐次なら Barrier が timeout して失敗扱いになる → 失敗ゼロ＝真に並列だった証拠
    assert result.failures == []
    assert len(calls_a) == 4
    assert result.total_papers == 4


# ===== 同時リクエスト数の上限（主ソース）=====
def test_max_concurrent_requests_serializes():
    plan = _plan([("観点A", ["q1", "q2", "q3", "q4"])])  # arXiv 4タスク
    calls_a = []
    tracker = ConcurrencyTracker()
    searchers = {
        "arxiv": make_searcher("arxiv", lambda q: [_paper(f"a-{q}")], calls_a, tracker=tracker, sleep=0.02),
    }
    # 上限1 → 同時実行は決して1を超えない
    config = SurveyConfig(max_concurrent_requests=1, max_queries_per_aspect=4)
    result = collector.collect_papers(plan, config=config, searchers=searchers)

    assert tracker.max_active == 1
    assert result.total_papers == 4


# ===== 部分失敗の救済 =====
def test_partial_failure_returns_successes_and_records_failures():
    plan = _plan([("観点A", ["q1"])])  # 1クエリ × 2ソース = 2タスク
    calls_ok, calls_bad = [], []
    searchers = {
        "arxiv": make_searcher("arxiv", lambda q: [_paper("ok-1")], calls_ok),
        "semantic_scholar": make_searcher("semantic_scholar", lambda q: [], calls_bad, fail=True),
    }
    result = collector.collect_papers(plan, config=SurveyConfig(), searchers=searchers)

    # 成功分は返る
    assert result.total_papers == 1
    assert result.papers[0].paper.paper_id == "ok-1"
    # 失敗は記録され、どの観点・クエリ・ソースかが分かる
    assert result.total_failures == 1
    f = result.failures[0]
    assert f.aspect_name == "観点A"
    assert f.query == "q1"
    assert f.source == "semantic_scholar"
    assert "TimeoutError" in f.error


# ===== 重複排除（観点をまたぐ）=====
def test_dedup_across_aspects_keeps_origin_aspects():
    # 2観点が同じ paper_id "P1" を返す（arxivのみ）
    plan = _plan([("観点A", ["qa"]), ("観点B", ["qb"])])
    calls = []
    searchers = {"arxiv": make_searcher("arxiv", lambda q: [_paper("P1")], calls)}

    result = collector.collect_papers(plan, config=SurveyConfig(), searchers=searchers)

    # 統合されて1件
    assert result.total_papers == 1
    collected = result.papers[0]
    assert collected.paper.paper_id == "P1"
    # 両方の観点で見つかったことが保持される
    assert collected.found_in_aspects == ["観点A", "観点B"]
    # 観点別マップにも両方に出現
    assert result.by_aspect["観点A"] == ["P1"]
    assert result.by_aspect["観点B"] == ["P1"]


# ===== 全滅 → CollectionError =====
def test_all_failure_raises_collection_error():
    plan = _plan([("観点A", ["q1", "q2"])])
    calls_a, calls_s = [], []
    searchers = {
        "arxiv": make_searcher("arxiv", lambda q: [], calls_a, fail=True),
        "semantic_scholar": make_searcher("semantic_scholar", lambda q: [], calls_s, fail=True),
    }
    with pytest.raises(collector.CollectionError):
        collector.collect_papers(
            plan, config=SurveyConfig(), searchers=searchers, sleeper=lambda d: None
        )


# ===== papers_per_aspect の上限 =====
def test_papers_per_aspect_limit_truncates():
    plan = _plan([("観点A", ["q1"])])  # 1クエリ × 1ソース
    calls = []
    five = [_paper(f"P{i}") for i in range(5)]
    searchers = {"arxiv": make_searcher("arxiv", lambda q: five, calls)}

    config = SurveyConfig(papers_per_aspect=2)
    result = collector.collect_papers(plan, config=config, searchers=searchers)

    # 観点の取得上限2に切り詰め
    assert len(result.by_aspect["観点A"]) == 2
    assert result.total_papers == 2
    # max_results にも papers_per_aspect が渡る
    assert calls[0]["max_results"] == 2


# ===== max_queries_per_aspect の尊重 =====
def test_max_queries_per_aspect_respected():
    plan = _plan([("観点A", ["q1", "q2", "q3", "q4", "q5"])])
    calls = []
    searchers = {"arxiv": make_searcher("arxiv", lambda q: [_paper(f"P-{q}")], calls)}

    config = SurveyConfig(max_queries_per_aspect=2)
    result = collector.collect_papers(plan, config=config, searchers=searchers)

    # 先頭2クエリだけが実行される
    assert [c["query"] for c in calls] == ["q1", "q2"]
    assert result.total_papers == 2


# ===== 空プランは非エラーで空結果 =====
def test_empty_plan_returns_empty_result():
    plan = SurveyPlan(theme="t", aspects=[])
    result = collector.collect_papers(plan, config=SurveyConfig(), searchers={"arxiv": lambda q, max_results=10: []})
    assert result.total_papers == 0
    assert result.total_failures == 0


# ===== 補助ソース(S2)は逐次 + 間隔制御で叩かれる =====
def test_semantic_scholar_runs_sequentially_with_interval():
    plan = _plan([("観点A", ["q1", "q2", "q3"])])  # S2: 3タスク
    calls = []
    tracker = ConcurrencyTracker()
    sleeps = []
    searchers = {
        "semantic_scholar": make_searcher(
            "semantic_scholar",
            lambda q: [_paper(f"s-{q}", "semantic_scholar")],
            calls,
            tracker=tracker,
        )
    }
    config = SurveyConfig(max_queries_per_aspect=3, semantic_scholar_min_interval=0.5)
    result = collector.collect_papers(
        plan, config=config, searchers=searchers, sleeper=lambda d: sleeps.append(d)
    )

    # 逐次 = 同時実行は1を超えない
    assert tracker.max_active == 1
    # 3タスク → 先頭以外の2回、設定どおりの間隔で待機
    assert sleeps == [0.5, 0.5]
    # 順序も保たれる
    assert [c["query"] for c in calls] == ["q1", "q2", "q3"]
    assert result.total_papers == 3


# ===== arXiv並列 と S2逐次 が共存する =====
def test_arxiv_parallel_and_s2_sequential_coexist():
    plan = _plan([("観点A", ["q1", "q2"])])  # arxiv 2(並列) + s2 2(逐次)
    calls_a, calls_s = [], []
    a_barrier = threading.Barrier(2)  # arXiv は並列でないと通過できない
    s_tracker = ConcurrencyTracker()
    sleeps = []
    searchers = {
        "arxiv": make_searcher("arxiv", lambda q: [_paper(f"a-{q}")], calls_a, barrier=a_barrier),
        "semantic_scholar": make_searcher(
            "semantic_scholar", lambda q: [_paper(f"s-{q}", "semantic_scholar")], calls_s, tracker=s_tracker
        ),
    }
    config = SurveyConfig(max_concurrent_requests=2, max_queries_per_aspect=2, semantic_scholar_min_interval=0.1)
    result = collector.collect_papers(
        plan, config=config, searchers=searchers, sleeper=lambda d: sleeps.append(d)
    )

    # arXiv は並列（Barrier(2) 通過＝失敗ゼロ）
    assert result.failures == []
    # S2 は逐次（同時1以下）＋間隔1回
    assert s_tracker.max_active == 1
    assert sleeps == [0.1]
    assert result.total_papers == 4  # arxiv 2 + s2 2
