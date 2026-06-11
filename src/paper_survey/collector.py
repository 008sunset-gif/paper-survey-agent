"""
Collector - 並列収集層

Planner が出した複数観点 × 複数クエリ（英語）を受け取り、Searcher（arXiv /
Semantic Scholar）を **並列実行** して論文を集め、重複排除した
`CollectionResult` を返す。診断で指摘された「逐次 for ループ」を解消する中核。

設計の肝:
- **役割分担（arXiv主・S2補助）**: arXiv はキー不要で寛容なので主ソースとして
  スレッドプールで並列収集。Semantic Scholar は無認証だと 429 が厳しいため
  **補助ソース**と位置づけ、並列にせず逐次＋最小間隔で礼儀正しく叩く
  （`SurveyConfig.semantic_scholar_min_interval`）。S2 が失敗しても arXiv 主ソースで
  全体は成立する。
- **部分失敗の救済**: 一部のクエリ/観点が失敗しても全体を止めず、成功分を返す。
  失敗は `failures` に記録して可視化。全タスクが失敗した場合のみ `CollectionError`。
- **重複排除**: 観点をまたいで同一 paper_id を統合し、見つかった観点は保持。
- **同時リクエスト数の上限**を `SurveyConfig.max_concurrent_requests` で制御し、
  API 過負荷/レート制限を避ける。

Searcher の関数は **注入可能**（`searchers` 引数）。テストではモックを渡し、
実APIは一切叩かない。
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from src.paper_survey import searcher as searcher_module
from src.paper_survey.schemas import (
    CollectedPaper,
    CollectionFailure,
    CollectionResult,
    Paper,
    SurveyConfig,
    SurveyPlan,
)

logger = logging.getLogger(__name__)

# 注入する検索関数の型: fn(query, max_results) -> List[Paper]
SearchFn = Callable[..., List[Paper]]

# 補助ソース: 並列にせず逐次＋間隔制御で叩くソース名（無認証レート制限対策）
# arXiv は主ソースとして並列。ここに無いソースは並列扱い。
SEQUENTIAL_SOURCES = frozenset({"semantic_scholar"})


class CollectionError(Exception):
    """収集が全滅（全タスク失敗）したときに送出"""


def _default_searchers() -> Dict[str, SearchFn]:
    """本番のデフォルト検索関数（テストでは使わない）"""
    return {
        "arxiv": searcher_module.search_arxiv,
        "semantic_scholar": searcher_module.search_semantic_scholar,
    }


def collect_papers(
    plan: SurveyPlan,
    *,
    config: Optional[SurveyConfig] = None,
    searchers: Optional[Dict[str, SearchFn]] = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> CollectionResult:
    """
    観点×クエリ×ソースの検索を収集し、重複排除して返す。

    主ソース（arXiv 等）は並列、補助ソース（Semantic Scholar）は逐次＋間隔制御。

    Args:
        plan: Planner の出力（観点と英語クエリ）
        config: 実行時設定。papers_per_aspect / max_queries_per_aspect /
            max_concurrent_requests / semantic_scholar_min_interval を尊重する。
        searchers: ソース名→検索関数のマップ。テストではモックを渡す。
            None なら本番の arXiv / Semantic Scholar を使う。
        sleeper: S2 の間隔制御に使う待機関数。テストでは no-op を渡す。

    Returns:
        CollectionResult: 重複排除済み論文 + 観点別 paper_id + 失敗一覧

    Raises:
        CollectionError: スケジュールした全タスクが失敗した（全滅）場合のみ。
    """
    config = config or SurveyConfig()
    searchers = searchers if searchers is not None else _default_searchers()

    # === タスク列挙（観点順・クエリ順・ソース順を保持 → 出力を決定的に）===
    tasks: List[tuple] = []  # (aspect_name, query, source_name, fn)
    for aspect in plan.aspects:
        queries = aspect.search_queries[: config.max_queries_per_aspect]
        for query in queries:
            for source_name, fn in searchers.items():
                tasks.append((aspect.name, query, source_name, fn))

    if not tasks:
        logger.warning("no search tasks to run (empty plan?)")
        return CollectionResult(theme=plan.theme)

    # === 役割分担: arXiv等=主(並列) / Semantic Scholar=補助(逐次+間隔) ===
    parallel_indices = [i for i, t in enumerate(tasks) if t[2] not in SEQUENTIAL_SOURCES]
    sequential_indices = [i for i, t in enumerate(tasks) if t[2] in SEQUENTIAL_SOURCES]

    results_by_index: Dict[int, List[Paper]] = {}
    failures: List[CollectionFailure] = []

    def _exec(index: int) -> List[Paper]:
        _, query, _, fn = tasks[index]
        return fn(query, max_results=config.papers_per_aspect)

    def _record_failure(index: int, exc: Exception) -> None:
        aspect_name, query, source_name, _ = tasks[index]
        logger.warning(
            "search failed [aspect=%s, source=%s, query=%r]: %s",
            aspect_name,
            source_name,
            query,
            exc,
        )
        failures.append(
            CollectionFailure(
                aspect_name=aspect_name,
                query=query,
                source=source_name,
                error=f"{type(exc).__name__}: {exc}",
            )
        )

    # 主ソースを並列プールに投入し、そのブロック内で補助ソースを逐次実行する。
    # こうすると arXiv の並列収集と S2 の逐次収集が時間的に重なり、無駄がない。
    max_workers = min(config.max_concurrent_requests, max(1, len(parallel_indices)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {executor.submit(_exec, i): i for i in parallel_indices}

        # --- 補助: Semantic Scholar を逐次 + 間隔制御（礼儀正しく叩く）---
        for n, index in enumerate(sequential_indices):
            if n > 0:
                # リクエスト間に最小間隔を空け、無認証のレート上限超過を避ける
                sleeper(config.semantic_scholar_min_interval)
            try:
                results_by_index[index] = _exec(index)
            except Exception as exc:  # noqa: BLE001 - 部分失敗を救済
                _record_failure(index, exc)

        # --- 主: arXiv 並列分を回収 ---
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results_by_index[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - 部分失敗を救済
                _record_failure(index, exc)

    # === 全滅判定（成功タスクが0件なら全滅）===
    if not results_by_index:
        raise CollectionError(
            f"all {len(tasks)} search task(s) failed; "
            f"first error: {failures[0].error if failures else 'unknown'}"
        )

    # === 重複排除 + 観点別集計（タスク順に決定的に処理）===
    global_dedup: "OrderedDict[str, CollectedPaper]" = OrderedDict()
    by_aspect: "OrderedDict[str, List[str]]" = OrderedDict()
    aspect_seen: Dict[str, set] = {}

    for index in range(len(tasks)):
        papers = results_by_index.get(index)
        if papers is None:  # 失敗タスク
            continue
        aspect_name = tasks[index][0]
        by_aspect.setdefault(aspect_name, [])
        aspect_seen.setdefault(aspect_name, set())

        for paper in papers:
            pid = paper.paper_id
            if not pid:
                logger.debug("skipping paper with empty paper_id: %r", paper.title)
                continue

            # --- 観点内: 重複除去 + papers_per_aspect の上限 ---
            if pid not in aspect_seen[aspect_name]:
                if len(by_aspect[aspect_name]) >= config.papers_per_aspect:
                    # この観点の取得上限に達したので、新規論文は採用しない
                    continue
                aspect_seen[aspect_name].add(pid)
                by_aspect[aspect_name].append(pid)

            # --- 観点横断: 同一 paper_id を統合し、見つかった観点を保持 ---
            if pid in global_dedup:
                collected = global_dedup[pid]
                if aspect_name not in collected.found_in_aspects:
                    collected.found_in_aspects.append(aspect_name)
            else:
                global_dedup[pid] = CollectedPaper(
                    paper=paper, found_in_aspects=[aspect_name]
                )

    return CollectionResult(
        theme=plan.theme,
        papers=list(global_dedup.values()),
        by_aspect=dict(by_aspect),
        failures=failures,
    )
