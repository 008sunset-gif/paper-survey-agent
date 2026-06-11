"""
Orchestrator - 6部品を1本のパイプラインに配線する

  Planner → Collector → Evaluator(自己批評ループ) → Synthesizer → Verifier

研究テーマを入れると、検証済み日本語サーベイの `SurveyResult` までを通す。

【方式の判断: LangGraph ではなく単純な関数合成】
- パイプラインは本質的に線形で、唯一の分岐＝再検索ループは既に
  `evaluator.run_evaluation_loop` の中に終了条件付きで実装済み。グラフの状態機械を
  改めて組む必要がない。
- 各部品の LLM/検索を「注入」してモックで丸ごと通せることが本タスクの肝で、
  関数合成の方が依存注入とテストが素直（LangGraph の状態/ノードに包むと
  注入境界が増えて検証が複雑化する）。
- 既存 deepdive の LangGraph 実装とは別モジュールとして独立させる方針なので、
  ここで LangGraph に依存しない方が結合度が下がる。
（詳細な理由は DEVLOG ステップ7参照）

【重要】実APIは叩かない。全部品の LLM/検索は `SurveyDependencies` で注入し、
テストはモックで行う。本番配線は `build_gemini_dependencies`（GO後に使用）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.paper_survey.collector import CollectionError, collect_papers
from src.paper_survey.evaluator import EvaluatorLLM, run_evaluation_loop
from src.paper_survey.planner import PlannerLLM, run_planner
from src.paper_survey.schemas import (
    CollectionResult,
    Paper,
    SurveyConfig,
    SurveyPlan,
    SurveyResult,
)
from src.paper_survey.synthesizer import SynthesizerLLM, synthesize
from src.paper_survey.verifier import VerifierLLM, verify

logger = logging.getLogger(__name__)


@dataclass
class SurveyDependencies:
    """
    パイプラインが必要とする外部依存（LLM/検索）の束。

    すべて注入可能。テストではモックを、本番では Gemini/実検索を渡す。
    （callable を含むため pydantic ではなく dataclass にしている）
    """

    planner_llm: PlannerLLM
    searchers: Dict[str, object]  # source 名 -> 検索関数（collector.SearchFn）
    evaluator_llm: Optional[EvaluatorLLM] = None  # None なら機械チェックのみ
    synthesizer_llm: SynthesizerLLM = None
    verifier_llm: VerifierLLM = None


def run_survey(
    theme: str,
    *,
    deps: SurveyDependencies,
    config: Optional[SurveyConfig] = None,
) -> SurveyResult:
    """
    テーマ → 検証済み日本語サーベイ（SurveyResult）まで通す。

    SurveyConfig は全部品で共有する。途中で部品が失敗しても破綻させず、
    可能な範囲の中間結果を保持して status/notes で状態を返す。

    Args:
        theme: 研究テーマ（日本語可）
        deps: 注入する LLM/検索（モック or 本番）
        config: 全部品共有の設定

    Returns:
        SurveyResult: 各段階の中間結果を含む最終結果
    """
    config = config or SurveyConfig()
    notes: List[str] = []

    # === 1. Planner: テーマ → 観点分解 ===
    plan: SurveyPlan = run_planner(theme, llm=deps.planner_llm, config=config)
    notes.append(f"planner: {len(plan.aspects)} 観点を生成")

    # === 2+3. Collector + Evaluator（自己批評ループ）===
    # 初回収集も再検索も同じ collect_fn を通す（Collector を1箇所に閉じ込める）
    def collect_fn(subplan: SurveyPlan, round_index: int) -> CollectionResult:
        return collect_papers(subplan, config=config, searchers=deps.searchers)

    try:
        evaluation = run_evaluation_loop(
            plan,
            collect_fn=collect_fn,
            config=config,
            llm=deps.evaluator_llm,
        )
    except CollectionError as exc:
        # 収集全滅 → 破綻させず部分結果（plan のみ）で返す
        notes.append(f"collection failed (全滅): {exc}")
        logger.warning("survey aborted at collection: %s", exc)
        return SurveyResult(theme=theme, status="failed", plan=plan, notes=notes)

    collection = evaluation.final_collection
    notes.append(
        f"evaluator: {evaluation.rounds_used} 回再検索, "
        f"完了={evaluation.completed}, 収集論文={collection.total_papers}"
    )
    if not evaluation.completed:
        notes.append(f"不足のまま残った観点: {evaluation.unmet_aspects}")

    # === 4. Synthesizer: 英語論文 → 日本語 claim（根拠ID検証つき）===
    synthesis = synthesize(
        plan, collection, llm=deps.synthesizer_llm, config=config
    )
    notes.append(
        f"synthesizer: claim {synthesis.accepted_claim_count}/{synthesis.raw_claim_count} 採用, "
        f"問題 {synthesis.total_issues} 件"
    )

    # === 5. Verifier: 各 claim を論文要旨と意味照合 ===
    verification = verify(
        synthesis.synthesis, collection, llm=deps.verifier_llm, config=config
    )
    notes.append(
        f"verifier: SUPPORTED {verification.summary.counts.get('SUPPORTED', 0)}"
        f"/{verification.summary.total_claims} "
        f"(照合 {verification.llm_calls} 回, cache {verification.cache_hits})"
    )

    status = "completed" if evaluation.completed else "incomplete_coverage"
    return SurveyResult(
        theme=theme,
        status=status,
        plan=plan,
        collection=collection,
        evaluation=evaluation,
        synthesis=synthesis,
        verification=verification,
        notes=notes,
    )


# === 本番配線（GO後に使用。実APIを叩くのでテストでは使わない）===
_EVALUATOR_SYSTEM = """あなたはサーベイの網羅性評価者です。
ある観点について集めた論文が、その観点の intent に十分答えているかを判定してください。
- answers_intent: 十分なら true、不足なら false
- missing_points: 不足している論点
- suggested_queries: 不足を埋める新しい英語検索クエリ案（前回と変える）
QualitativeVerdict の構造で出力してください。"""


def _papers_to_text(papers: List[Paper]) -> str:
    """論文リストを LLM 提示用テキストに整形"""
    if not papers:
        return "（論文なし）"
    lines = []
    for p in papers:
        lines.append(
            f"- [{p.paper_id}] {p.title} ({p.year})\n  {(p.abstract or '')[:500]}"
        )
    return "\n".join(lines)


def build_gemini_dependencies(
    *,
    model: str = "gemini-2.5-flash-lite",
    api_key: Optional[str] = None,
    temperature: float = 0.2,
) -> SurveyDependencies:
    """
    本番用の SurveyDependencies を Gemini + 実検索で組み立てる。

    【注意】これを使うと実 API を叩く。GO が出るまで呼ばないこと。
    呼び出し時まで実 API には触れない（チェーンは遅延構築）。
    langchain 1.x / langchain-google-genai 4.x 系での構造化出力 API は
    GO 後の初回実行で要動作確認（DEVLOG ステップ7「未検証部分」参照）。
    """
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_google_genai import ChatGoogleGenerativeAI

    from src.paper_survey.planner import build_gemini_planner_llm
    from src.paper_survey.schemas import (
        AspectDraft,
        GroundingVerdict,
        QualitativeVerdict,
    )
    from src.paper_survey.searcher import search_arxiv, search_semantic_scholar
    from src.paper_survey.synthesizer import SYNTHESIZER_SYSTEM
    from src.paper_survey.verifier import VERIFIER_SYSTEM

    key = api_key or os.getenv("GOOGLE_API_KEY")

    def _chat():
        return ChatGoogleGenerativeAI(
            model=model, google_api_key=key, temperature=temperature
        )

    # Evaluator 用
    eval_chain = (
        ChatPromptTemplate.from_messages(
            [
                ("system", _EVALUATOR_SYSTEM),
                ("human", "観点: {aspect_name}\nintent: {intent}\n論文:\n{papers_text}"),
            ]
        )
        | _chat().with_structured_output(QualitativeVerdict)
    )

    def evaluator_llm(*, aspect_name, intent, papers):
        return eval_chain.invoke(
            {"aspect_name": aspect_name, "intent": intent, "papers_text": _papers_to_text(papers)}
        )

    # Synthesizer 用
    synth_chain = (
        ChatPromptTemplate.from_messages(
            [
                ("system", SYNTHESIZER_SYSTEM),
                ("human", "観点: {aspect_name}\nintent: {intent}\n論文:\n{papers_text}"),
            ]
        )
        | _chat().with_structured_output(AspectDraft)
    )

    def synthesizer_llm(*, aspect_name, intent, papers):
        return synth_chain.invoke(
            {"aspect_name": aspect_name, "intent": intent, "papers_text": _papers_to_text(papers)}
        )

    # Verifier 用
    verify_chain = (
        ChatPromptTemplate.from_messages(
            [
                ("system", VERIFIER_SYSTEM),
                ("human", "主張: {statement}\n論文要旨:\n[{paper_id}] {abstract}"),
            ]
        )
        | _chat().with_structured_output(GroundingVerdict)
    )

    def verifier_llm(*, statement, paper):
        return verify_chain.invoke(
            {"statement": statement, "paper_id": paper.paper_id, "abstract": paper.abstract or ""}
        )

    return SurveyDependencies(
        planner_llm=build_gemini_planner_llm(model=model, api_key=key, temperature=temperature),
        searchers={"arxiv": search_arxiv, "semantic_scholar": search_semantic_scholar},
        evaluator_llm=evaluator_llm,
        synthesizer_llm=synthesizer_llm,
        verifier_llm=verifier_llm,
    )
