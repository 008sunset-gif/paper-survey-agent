"""
Evaluator - 自己批評ループ（網羅性の自己評価 → 再検索）

Collector が集めた論文群が Planner の観点を十分カバーしているかを自己評価し、
不足があれば「どの観点を・どんな新しい英語クエリで」再検索すべきかを出力する。
これが診断で指摘された「看板倒れ（直線グラフ）」を解消する自己批評ループの心臓部。

評価は二段構え（完成度の肝）:
  1) 機械的チェック … 各観点が最低論文数（config.min_papers_per_aspect）を満たすか。
     論文ゼロ/極少を機械的に検出。LLM 不要・確実。
  2) 質的チェック   … 集まった論文が観点の intent に答えているかを LLM が判定。
     LLM は注入可能（テストではモック）。失敗/不正時は機械的チェックのみで継続。

再検索クエリは前回クエリの単純反復を避ける。無限ループは
config.max_research_rounds で必ず打ち切り、打ち切り時は不足観点を隠さず明示する。

【重要】実APIは叩かない。LLM も collect 関数も注入し、テストはモックで行う。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Protocol, Union

from src.paper_survey.schemas import (
    AspectAssessment,
    CollectedPaper,
    CollectionResult,
    EvaluationLoopResult,
    EvaluationResult,
    Paper,
    QualitativeVerdict,
    ReSearchInstruction,
    SearchAspect,
    SurveyConfig,
    SurveyPlan,
)

logger = logging.getLogger(__name__)


# 注入する LLM 評価器: 1観点を評価して QualitativeVerdict（or dict）を返す
class EvaluatorLLM(Protocol):
    def __call__(
        self, *, aspect_name: str, intent: str, papers: List[Paper]
    ) -> Union[QualitativeVerdict, dict]:
        ...


# 注入する収集関数: (sub)plan とラウンド番号を受け取り CollectionResult を返す
CollectFn = Callable[[SurveyPlan, int], CollectionResult]


# 再検索クエリを差別化するための補助サフィックス（前回の単純反復を避ける）
_VARIANT_SUFFIXES = [
    "survey",
    "recent advances",
    "review",
    "benchmark",
    "state of the art",
]


# === 内部ヘルパー ===
def _coerce_verdict(raw: Union[QualitativeVerdict, dict]) -> QualitativeVerdict:
    """LLM 出力を QualitativeVerdict に正規化（不正なら pydantic が例外）"""
    if isinstance(raw, QualitativeVerdict):
        return raw
    if isinstance(raw, dict):
        return QualitativeVerdict(**raw)
    raise TypeError(f"unexpected LLM verdict type: {type(raw).__name__}")


def _papers_by_aspect(collection: CollectionResult) -> Dict[str, List[Paper]]:
    """CollectionResult から 観点名 -> Paper リスト を構築"""
    id_to_paper = {cp.paper.paper_id: cp.paper for cp in collection.papers}
    result: Dict[str, List[Paper]] = {}
    for aspect_name, paper_ids in collection.by_aspect.items():
        result[aspect_name] = [
            id_to_paper[pid] for pid in paper_ids if pid in id_to_paper
        ]
    return result


def _build_new_queries(
    aspect: SearchAspect,
    suggested: List[str],
    previous: set,
    max_queries: int,
) -> List[str]:
    """
    再検索クエリを組み立てる。

    優先順位: ① LLM の提案クエリ → ② 元クエリ＋差別化サフィックスの合成。
    いずれも前回クエリ（previous, 大文字小文字無視）との重複は除外し、
    「同じクエリの単純反復」を避ける。最低1件は返す。
    """
    prev_lower = {p.lower() for p in previous}
    out: List[str] = []
    seen: set = set()

    def _add(candidate: str) -> bool:
        normalized = " ".join((candidate or "").split())
        if not normalized:
            return False
        key = normalized.lower()
        if key in prev_lower or key in seen:
            return False
        seen.add(key)
        out.append(normalized)
        return True

    # ① LLM 提案（使えるものが1つでもあればそれを採用し、合成で水増ししない）
    for q in suggested or []:
        _add(q)
        if len(out) >= max_queries:
            break
    if out:
        return out[:max_queries]

    # ② LLM提案が皆無のときだけ、元クエリ（英語）にサフィックスを付けて差別化
    base_terms = aspect.search_queries or [aspect.name]
    for base in base_terms:
        for suffix in _VARIANT_SUFFIXES:
            _add(f"{base} {suffix}")
            if len(out) >= max_queries:
                return out[:max_queries]

    # 最終手段（理論上ここに来るのは元クエリが全て previous と衝突した極端な場合）
    if not out:
        _add(f"{aspect.name} survey latest")
        if not out:  # それすら previous なら一意化
            out.append(f"{aspect.name} survey latest {len(previous)}")

    return out[:max_queries]


# === 単一ラウンドの評価 ===
def evaluate_round(
    plan: SurveyPlan,
    collection: CollectionResult,
    *,
    config: Optional[SurveyConfig] = None,
    llm: Optional[EvaluatorLLM] = None,
    previous_queries: Optional[Dict[str, set]] = None,
    round_index: int = 0,
) -> EvaluationResult:
    """
    1ラウンド分の網羅性評価を行う。

    Args:
        plan: Planner の観点
        collection: Collector が集めた論文（観点別）
        config: 閾値などの設定
        llm: 質的評価器（注入）。None なら機械チェックのみ。
        previous_queries: 観点名 -> これまで使ったクエリ集合（再検索の重複回避用）
        round_index: ラウンド番号

    Returns:
        EvaluationResult: カバー済み/不足観点と再検索指示
    """
    config = config or SurveyConfig()
    previous_queries = previous_queries or {}
    papers_by_aspect = _papers_by_aspect(collection)

    assessments: List[AspectAssessment] = []
    covered: List[str] = []
    insufficient: List[str] = []
    instructions: List[ReSearchInstruction] = []
    llm_used_any = False

    for aspect in plan.aspects:
        papers = papers_by_aspect.get(aspect.name, [])
        count = len(papers)

        # --- ① 機械的チェック ---
        mechanical_ok = count >= config.min_papers_per_aspect

        # --- ② 質的チェック（機械チェックを通った観点のみ LLM に問う）---
        qualitative_ok: Optional[bool] = None
        qual_missing = ""
        qual_queries: List[str] = []
        if llm is not None and mechanical_ok:
            try:
                verdict = _coerce_verdict(
                    llm(aspect_name=aspect.name, intent=aspect.intent, papers=papers)
                )
                qualitative_ok = bool(verdict.answers_intent)
                qual_missing = verdict.missing_points or ""
                qual_queries = list(verdict.suggested_queries or [])
                llm_used_any = True
            except Exception as exc:  # noqa: BLE001 - LLM失敗時は機械チェックで継続
                logger.warning(
                    "qualitative eval failed for aspect %r: %s; "
                    "falling back to mechanical check",
                    aspect.name,
                    exc,
                )
                qualitative_ok = None  # フォールバック

        # --- 統合判定 ---
        if not mechanical_ok:
            is_covered = False
            reason = (
                f"論文数 {count} < 閾値 {config.min_papers_per_aspect}"
                "（機械チェックで不足）"
            )
        elif qualitative_ok is False:
            is_covered = False
            reason = f"質的評価で intent 未達: {qual_missing or '論点に不足あり'}"
        else:
            is_covered = True
            reason = (
                "カバー済み（質的評価OK）"
                if qualitative_ok
                else "カバー済み（機械チェックのみ）"
            )

        assessments.append(
            AspectAssessment(
                aspect_name=aspect.name,
                paper_count=count,
                mechanical_ok=mechanical_ok,
                qualitative_ok=qualitative_ok,
                covered=is_covered,
                reason=reason,
            )
        )

        if is_covered:
            covered.append(aspect.name)
        else:
            insufficient.append(aspect.name)
            new_queries = _build_new_queries(
                aspect,
                qual_queries,
                previous_queries.get(aspect.name, set()),
                config.max_queries_per_aspect,
            )
            instructions.append(
                ReSearchInstruction(
                    aspect_name=aspect.name, new_queries=new_queries, reason=reason
                )
            )

    return EvaluationResult(
        round_index=round_index,
        assessments=assessments,
        covered_aspects=covered,
        insufficient_aspects=insufficient,
        research_instructions=instructions,
        is_complete=(len(insufficient) == 0),
        llm_used=llm_used_any,
    )


# === ループ駆動（累積マージ）===
def _subplan_from_instructions(
    plan: SurveyPlan, instructions: List[ReSearchInstruction]
) -> SurveyPlan:
    """再検索指示から、不足観点だけの SurveyPlan を作る（intent は元プランから引く）"""
    intent_by_name = {a.name: a.intent for a in plan.aspects}
    aspects = [
        SearchAspect(
            name=instr.aspect_name,
            intent=intent_by_name.get(instr.aspect_name, ""),
            search_queries=instr.new_queries,
        )
        for instr in instructions
        if instr.new_queries
    ]
    return SurveyPlan(theme=plan.theme, aspects=aspects)


class _Accumulator:
    """ラウンドをまたいで CollectionResult をマージする可変状態"""

    def __init__(self, theme: str):
        self.theme = theme
        self.papers: "OrderedDict[str, CollectedPaper]" = OrderedDict()
        self.by_aspect: "OrderedDict[str, List[str]]" = OrderedDict()
        self.aspect_seen: Dict[str, set] = {}
        self.failures: list = []

    def merge(self, collection: CollectionResult) -> None:
        self.failures.extend(collection.failures)
        id_to_paper = {cp.paper.paper_id: cp for cp in collection.papers}
        for aspect_name, paper_ids in collection.by_aspect.items():
            self.by_aspect.setdefault(aspect_name, [])
            self.aspect_seen.setdefault(aspect_name, set())
            for pid in paper_ids:
                if pid not in self.aspect_seen[aspect_name]:
                    self.aspect_seen[aspect_name].add(pid)
                    self.by_aspect[aspect_name].append(pid)
                # グローバル統合（見つかった観点を保持）
                src = id_to_paper.get(pid)
                if src is None:
                    continue
                if pid in self.papers:
                    if aspect_name not in self.papers[pid].found_in_aspects:
                        self.papers[pid].found_in_aspects.append(aspect_name)
                else:
                    self.papers[pid] = CollectedPaper(
                        paper=src.paper, found_in_aspects=[aspect_name]
                    )

    def to_collection(self) -> CollectionResult:
        return CollectionResult(
            theme=self.theme,
            papers=list(self.papers.values()),
            by_aspect={k: list(v) for k, v in self.by_aspect.items()},
            failures=list(self.failures),
        )


def run_evaluation_loop(
    plan: SurveyPlan,
    *,
    collect_fn: CollectFn,
    config: Optional[SurveyConfig] = None,
    llm: Optional[EvaluatorLLM] = None,
    initial_collection: Optional[CollectionResult] = None,
) -> EvaluationLoopResult:
    """
    自己批評ループを駆動する。

    初回収集 → 評価 → 不足があれば再検索（collect_fn を再呼び出し）→ マージ →
    再評価、を全観点カバー or max_research_rounds 到達まで繰り返す。

    Args:
        plan: Planner の観点
        collect_fn: (sub)plan とラウンド番号から CollectionResult を返す収集関数。
            テストではモック、本番では Collector を包んだ関数を渡す（実APIはGO後）。
        config: 設定（max_research_rounds で打ち切り回数を制御）
        llm: 質的評価器（注入）。None なら機械チェックのみ。
        initial_collection: 初回収集を外で済ませている場合に渡す（無ければ collect_fn で取得）

    Returns:
        EvaluationLoopResult: 完了可否・使用ラウンド数・不足のまま残った観点・
        マージ済み最終収集・各ラウンドの評価履歴
    """
    config = config or SurveyConfig()

    accumulator = _Accumulator(plan.theme)
    collection = initial_collection if initial_collection is not None else collect_fn(plan, 0)
    accumulator.merge(collection)

    # 観点ごとに「これまで使ったクエリ」を記録（再検索の重複回避）
    previous_queries: Dict[str, set] = {
        a.name: set(a.search_queries) for a in plan.aspects
    }

    evaluations: List[EvaluationResult] = []
    evaluation = evaluate_round(
        plan,
        accumulator.to_collection(),
        config=config,
        llm=llm,
        previous_queries=previous_queries,
        round_index=0,
    )
    evaluations.append(evaluation)

    research_round = 0
    while not evaluation.is_complete and research_round < config.max_research_rounds:
        research_round += 1

        subplan = _subplan_from_instructions(plan, evaluation.research_instructions)
        if not subplan.aspects:
            # 実行可能な再検索が無ければ打ち切り（無限ループ防止）
            logger.info("no actionable re-search instructions; stopping loop")
            break

        # 再検索の収集
        new_collection = collect_fn(subplan, research_round)
        accumulator.merge(new_collection)

        # 使ったクエリを履歴に追加（次ラウンドの重複回避）
        for instr in evaluation.research_instructions:
            previous_queries.setdefault(instr.aspect_name, set()).update(
                instr.new_queries
            )

        evaluation = evaluate_round(
            plan,
            accumulator.to_collection(),
            config=config,
            llm=llm,
            previous_queries=previous_queries,
            round_index=research_round,
        )
        evaluations.append(evaluation)

    return EvaluationLoopResult(
        theme=plan.theme,
        completed=evaluation.is_complete,
        rounds_used=research_round,
        # 打ち切り時も不足観点を隠さず明示
        unmet_aspects=list(evaluation.insufficient_aspects),
        final_collection=accumulator.to_collection(),
        evaluations=evaluations,
    )
