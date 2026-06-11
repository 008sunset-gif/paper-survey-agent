"""
Orchestrator の統合テスト（実APIは一切叩かない / 全部品モック注入）

単体テストでは見えない「配線して初めて出る」統合バグを洗い出す:
- テーマ → 最終 SurveyResult まで通る（型の受け渡しが全段で整合）
- 自己批評ループが配線レベルで回る（不足→再検索→充足）
- 途中で部品が失敗しても全体が破綻しない（収集全滅/段階LLM失敗）
- Synthesizer の出力が Verifier の入力に正しく繋がる
"""

import pytest

from src.paper_survey import orchestrator
from src.paper_survey.orchestrator import SurveyDependencies, run_survey
from src.paper_survey.schemas import (
    AspectDraft,
    GroundingVerdict,
    Paper,
    QualitativeVerdict,
    RawClaim,
    SearchAspect,
    SurveyConfig,
    SurveyPlan,
)


# ===== モック部品 =====
def make_planner_llm(aspects):
    """aspects: list of (name, intent, [queries])"""

    def _llm(theme, *, min_aspects, max_aspects):
        return SurveyPlan(
            theme=theme,
            aspects=[SearchAspect(name=n, intent=it, search_queries=q) for n, it, q in aspects],
        )

    return _llm


def make_searcher(papers_for, *, calls=None, fail=False):
    """papers_for(query) -> List[Paper]"""

    def _fn(query, max_results=10):
        if calls is not None:
            calls.append(query)
        if fail:
            raise TimeoutError(f"search failed: {query}")
        return list(papers_for(query))[:max_results]

    return _fn


def _paper(pid):
    return Paper(paper_id=pid, source="arxiv", title=f"t-{pid}", authors=[], year=2020, abstract=f"abstract of {pid}", url="")


def make_evaluator_llm(answers_for):
    def _llm(*, aspect_name, intent, papers):
        return QualitativeVerdict(answers_intent=answers_for(aspect_name))

    return _llm


def make_synth_llm(*, fail_aspect=None):
    """各観点について、渡された論文の実IDを引用する draft を返す"""

    def _llm(*, aspect_name, intent, papers):
        if fail_aspect == aspect_name:
            raise RuntimeError("synth LLM down")
        claims = [
            RawClaim(statement=f"{aspect_name}の主張", paper_ids=[papers[0].paper_id])
        ] if papers else []
        return AspectDraft(claims=claims)

    return _llm


def make_verifier_llm(status_for):
    def _llm(*, statement, paper):
        return GroundingVerdict(status=status_for(statement, paper.paper_id))

    return _llm


# ===== 正常系: テーマ→最終結果まで通る =====
def test_full_pipeline_runs_to_verification():
    deps = SurveyDependencies(
        planner_llm=make_planner_llm([
            ("量子化", "i1", ["llm quantization"]),
            ("蒸留", "i2", ["knowledge distillation"]),
        ]),
        # 各クエリ2論文（min_papers_per_aspect=2 を満たす）
        searchers={"arxiv": make_searcher(lambda q: [_paper(f"{q}-1"), _paper(f"{q}-2")])},
        evaluator_llm=make_evaluator_llm(lambda name: True),
        synthesizer_llm=make_synth_llm(),
        verifier_llm=make_verifier_llm(lambda s, pid: "SUPPORTED"),
    )
    config = SurveyConfig(min_papers_per_aspect=2, max_research_rounds=1)

    result = run_survey("LLM効率化", deps=deps, config=config)

    assert result.status == "completed"
    # 全段階の中間結果が保持されている
    assert result.plan is not None and len(result.plan.aspects) == 2
    assert result.collection is not None and result.collection.total_papers > 0
    assert result.evaluation is not None and result.evaluation.completed
    assert result.synthesis is not None and result.synthesis.accepted_claim_count == 2
    assert result.verification is not None
    # 型の受け渡し: Synthesizer の claim 数 == Verifier の検証 claim 数
    assert len(result.verification.claim_verifications) == result.synthesis.accepted_claim_count
    assert result.verification.summary.counts["SUPPORTED"] == 2


# ===== 自己批評ループが配線レベルで回る =====
def test_self_critique_loop_research_then_completes():
    # 観点B の元クエリ "qb" は空、再検索の新クエリでは論文が返る
    def papers_for(query):
        if query == "qb":
            return []  # 初回 B は不足
        return [_paper(f"{query}-1"), _paper(f"{query}-2")]

    calls = []
    deps = SurveyDependencies(
        planner_llm=make_planner_llm([
            ("A", "ia", ["qa"]),
            ("B", "ib", ["qb"]),
        ]),
        searchers={"arxiv": make_searcher(papers_for, calls=calls)},
        evaluator_llm=None,  # 機械チェックのみでループを駆動
        synthesizer_llm=make_synth_llm(),
        verifier_llm=make_verifier_llm(lambda s, pid: "SUPPORTED"),
    )
    config = SurveyConfig(min_papers_per_aspect=2, max_research_rounds=2, max_queries_per_aspect=2)

    result = run_survey("テーマ", deps=deps, config=config)

    # ループが実際に回って充足した
    assert result.evaluation.rounds_used >= 1
    assert result.evaluation.completed is True
    assert result.status == "completed"
    # 再検索クエリが初回の "qb" と異なる（単純反復していない）
    assert "qb" in calls               # 初回
    assert any(c != "qb" and c != "qa" for c in calls)  # 再検索の新クエリ


# ===== 収集全滅でも破綻しない =====
def test_pipeline_survives_total_collection_failure():
    deps = SurveyDependencies(
        planner_llm=make_planner_llm([("A", "ia", ["qa"])]),
        searchers={"arxiv": make_searcher(lambda q: [], fail=True)},  # 全検索失敗
        evaluator_llm=None,
        synthesizer_llm=make_synth_llm(),
        verifier_llm=make_verifier_llm(lambda s, pid: "SUPPORTED"),
    )
    result = run_survey("テーマ", deps=deps, config=SurveyConfig())

    # クラッシュせず failed で返り、plan は保持される
    assert result.status == "failed"
    assert result.plan is not None
    assert result.synthesis is None
    assert any("collection failed" in n for n in result.notes)


# ===== 不足のまま完走（再検索0回設定）でも合成・検証まで通す =====
def test_incomplete_coverage_still_synthesizes_and_verifies():
    def papers_for(query):
        return [] if query == "qb" else [_paper(f"{query}-1"), _paper(f"{query}-2")]

    deps = SurveyDependencies(
        planner_llm=make_planner_llm([("A", "ia", ["qa"]), ("B", "ib", ["qb"])]),
        searchers={"arxiv": make_searcher(papers_for)},
        evaluator_llm=None,
        synthesizer_llm=make_synth_llm(),
        verifier_llm=make_verifier_llm(lambda s, pid: "SUPPORTED"),
    )
    config = SurveyConfig(min_papers_per_aspect=2, max_research_rounds=0)  # 再検索しない

    result = run_survey("テーマ", deps=deps, config=config)

    assert result.status == "incomplete_coverage"
    assert result.evaluation.completed is False
    assert result.evaluation.unmet_aspects == ["B"]
    # 不足でも A については合成・検証まで通っている
    assert result.synthesis.accepted_claim_count == 1  # A のみ
    assert len(result.verification.claim_verifications) == 1
    assert any("不足" in n for n in result.notes)


# ===== 段階LLMの部分失敗が全体を壊さない =====
def test_stage_llm_failures_isolated():
    deps = SurveyDependencies(
        planner_llm=make_planner_llm([("A", "ia", ["qa"]), ("B", "ib", ["qb"])]),
        searchers={"arxiv": make_searcher(lambda q: [_paper(f"{q}-1"), _paper(f"{q}-2")])},
        evaluator_llm=make_evaluator_llm(lambda name: True),
        synthesizer_llm=make_synth_llm(fail_aspect="A"),       # A の合成が失敗
        verifier_llm=make_verifier_llm(lambda s, pid: (_ for _ in ()).throw(RuntimeError("verify down"))),  # 検証も失敗
    )
    config = SurveyConfig(min_papers_per_aspect=2, max_research_rounds=0)

    result = run_survey("テーマ", deps=deps, config=config)

    # 破綻せず最後まで到達。A合成失敗は llm_failed、B合成は成功、検証は UNVERIFIED
    assert result.status == "completed"
    assert any(i.issue_type == "llm_failed" and i.aspect_name == "A" for i in result.synthesis.issues)
    assert result.synthesis.accepted_claim_count == 1  # B のみ
    assert result.verification.summary.counts["UNVERIFIED"] == 1


# ===== Planner だけ注入すれば evaluator_llm 無しでも通る（機械チェックのみ）=====
def test_runs_without_evaluator_llm():
    deps = SurveyDependencies(
        planner_llm=make_planner_llm([("A", "ia", ["qa"])]),
        searchers={"arxiv": make_searcher(lambda q: [_paper(f"{q}-1"), _paper(f"{q}-2")])},
        evaluator_llm=None,
        synthesizer_llm=make_synth_llm(),
        verifier_llm=make_verifier_llm(lambda s, pid: "SUPPORTED"),
    )
    result = run_survey("テーマ", deps=deps, config=SurveyConfig(min_papers_per_aspect=2, max_research_rounds=0))
    assert result.status == "completed"
    assert result.synthesis.accepted_claim_count == 1
