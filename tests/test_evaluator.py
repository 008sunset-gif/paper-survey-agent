"""
Evaluator（自己批評ループ）のユニットテスト（実APIは一切叩かない）

LLM も collect 関数も注入したモックで差し替える。検証:
- 機械チェック: 論文ゼロ/極少の観点を検出
- 質的チェック: LLMが intent 未達と言えば不足、提案クエリで再検索指示
- 統合判定とフォールバック: LLM失敗時は機械チェックのみで継続
- 再検索クエリが前回の単純反復にならない
- ループ: 全カバーで終了 / 再検索で充足 / max_rounds で必ず打ち切り（不足を明示）
"""

import pytest

from src.paper_survey import evaluator
from src.paper_survey.schemas import (
    CollectedPaper,
    CollectionResult,
    Paper,
    QualitativeVerdict,
    SearchAspect,
    SurveyConfig,
    SurveyPlan,
)


# ===== ヘルパー =====
def _paper(pid):
    return Paper(paper_id=pid, source="arxiv", title=f"t-{pid}", authors=[], year=2020, abstract="", url="")


def _plan(aspects):
    """aspects: list of (name, intent, [queries])"""
    return SurveyPlan(
        theme="テーマ",
        aspects=[SearchAspect(name=n, intent=it, search_queries=q) for n, it, q in aspects],
    )


def _collection(theme, aspect_to_ids):
    """aspect_to_ids: dict aspect_name -> [paper_ids] からCollectionResultを組み立てる"""
    all_ids = []
    for ids in aspect_to_ids.values():
        all_ids.extend(ids)
    unique = list(dict.fromkeys(all_ids))
    papers = []
    for pid in unique:
        found = [a for a, ids in aspect_to_ids.items() if pid in ids]
        papers.append(CollectedPaper(paper=_paper(pid), found_in_aspects=found))
    return CollectionResult(theme=theme, papers=papers, by_aspect=dict(aspect_to_ids), failures=[])


def make_llm(verdict_for):
    """verdict_for(aspect_name) -> QualitativeVerdict/dict/Exception を返すモックLLM"""

    def _llm(*, aspect_name, intent, papers):
        v = verdict_for(aspect_name)
        if isinstance(v, Exception):
            raise v
        return v

    return _llm


# ===== 機械チェック: 極少観点の検出 =====
def test_mechanical_detects_low_count_without_llm():
    plan = _plan([("A", "ia", ["qa"]), ("B", "ib", ["qb"])])
    collection = _collection("テーマ", {"A": ["p1", "p2", "p3"], "B": []})
    config = SurveyConfig(min_papers_per_aspect=2)

    result = evaluator.evaluate_round(plan, collection, config=config, llm=None)

    assert result.covered_aspects == ["A"]
    assert result.insufficient_aspects == ["B"]
    assert not result.is_complete
    # Bに再検索指示が出る
    assert len(result.research_instructions) == 1
    instr = result.research_instructions[0]
    assert instr.aspect_name == "B"
    assert instr.new_queries  # 何らかの新クエリ
    # 機械チェックのみ → llm_used False
    assert result.llm_used is False
    # アセスメントの可視化
    b = next(a for a in result.assessments if a.aspect_name == "B")
    assert b.mechanical_ok is False
    assert b.paper_count == 0


# ===== 質的チェック: 論文数は足りるが intent 未達 =====
def test_qualitative_marks_insufficient_and_uses_suggested_queries():
    plan = _plan([("A", "ia", ["llm quantization"])])
    collection = _collection("テーマ", {"A": ["p1", "p2", "p3"]})  # 機械OK
    config = SurveyConfig(min_papers_per_aspect=2, max_queries_per_aspect=3)

    llm = make_llm(
        lambda name: QualitativeVerdict(
            answers_intent=False,
            missing_points="実機ベンチマークが無い",
            suggested_queries=["int4 quantization benchmark", "quantization latency measurement"],
        )
    )
    result = evaluator.evaluate_round(plan, collection, config=config, llm=llm)

    assert result.insufficient_aspects == ["A"]
    assert not result.is_complete
    assert result.llm_used is True
    instr = result.research_instructions[0]
    # LLM提案クエリが採用される
    assert instr.new_queries == [
        "int4 quantization benchmark",
        "quantization latency measurement",
    ]
    a = result.assessments[0]
    assert a.mechanical_ok is True
    assert a.qualitative_ok is False


# ===== 統合: 全カバーで完了 =====
def test_all_covered_completes():
    plan = _plan([("A", "ia", ["qa"]), ("B", "ib", ["qb"])])
    collection = _collection("テーマ", {"A": ["p1", "p2"], "B": ["p3", "p4"]})
    llm = make_llm(lambda name: QualitativeVerdict(answers_intent=True))

    result = evaluator.evaluate_round(plan, collection, config=SurveyConfig(min_papers_per_aspect=2), llm=llm)

    assert result.is_complete
    assert set(result.covered_aspects) == {"A", "B"}
    assert result.insufficient_aspects == []
    assert result.research_instructions == []


# ===== フォールバック: LLM失敗でも機械チェックで継続 =====
def test_llm_failure_falls_back_to_mechanical():
    plan = _plan([("A", "ia", ["qa"])])
    collection = _collection("テーマ", {"A": ["p1", "p2", "p3"]})  # 機械OK
    llm = make_llm(lambda name: RuntimeError("LLM down"))

    result = evaluator.evaluate_round(plan, collection, config=SurveyConfig(min_papers_per_aspect=2), llm=llm)

    # LLM失敗 → 機械チェックのみでカバー扱い、クラッシュしない
    assert result.is_complete
    assert result.covered_aspects == ["A"]
    a = result.assessments[0]
    assert a.qualitative_ok is None
    assert "機械チェックのみ" in a.reason
    assert result.llm_used is False


def test_llm_invalid_output_falls_back():
    plan = _plan([("A", "ia", ["qa"])])
    collection = _collection("テーマ", {"A": ["p1", "p2"]})
    # answers_intent を欠いた不正dict → pydanticで例外 → フォールバック
    llm = make_llm(lambda name: {"missing_points": "x"})

    result = evaluator.evaluate_round(plan, collection, config=SurveyConfig(min_papers_per_aspect=2), llm=llm)
    assert result.is_complete  # 機械チェックで通過
    assert result.assessments[0].qualitative_ok is None


# ===== 再検索クエリが前回の単純反復を避ける =====
def test_new_queries_avoid_repetition():
    plan = _plan([("A", "ia", ["llm quantization"])])
    collection = _collection("テーマ", {"A": []})  # 機械不足
    config = SurveyConfig(min_papers_per_aspect=2, max_queries_per_aspect=2)
    previous = {"A": {"llm quantization"}}

    # LLMが前回と同じクエリを混ぜて提案
    llm = make_llm(
        lambda name: QualitativeVerdict(
            answers_intent=False,
            suggested_queries=["LLM Quantization", "int4 quantization survey"],
        )
    )
    # ただし機械不足の観点はLLMを呼ばない設計 → 提案ではなく合成クエリで差別化される
    result = evaluator.evaluate_round(
        plan, collection, config=config, llm=llm, previous_queries=previous
    )
    new_q = result.research_instructions[0].new_queries
    # 前回クエリ "llm quantization" の単純反復は含まれない（大文字小文字無視）
    assert all(q.lower() != "llm quantization" for q in new_q)
    assert len(new_q) >= 1


def test_new_queries_uses_llm_suggestions_filtering_previous():
    # 機械OKだが質的NG → LLM提案を使うが前回分は除外
    plan = _plan([("A", "ia", ["base query"])])
    collection = _collection("テーマ", {"A": ["p1", "p2"]})
    config = SurveyConfig(min_papers_per_aspect=2, max_queries_per_aspect=3)
    previous = {"A": {"base query", "old query"}}
    llm = make_llm(
        lambda name: QualitativeVerdict(
            answers_intent=False,
            suggested_queries=["old query", "fresh query one", "fresh query two"],
        )
    )
    result = evaluator.evaluate_round(
        plan, collection, config=config, llm=llm, previous_queries=previous
    )
    new_q = result.research_instructions[0].new_queries
    assert "old query" not in [q.lower() for q in new_q]
    assert new_q == ["fresh query one", "fresh query two"]


# ===== ループ: 初回で全カバー → 再検索なし =====
def test_loop_terminates_immediately_when_complete():
    plan = _plan([("A", "ia", ["qa"])])
    calls = []

    def collect_fn(p, round_index):
        calls.append(round_index)
        return _collection(p.theme, {"A": ["p1", "p2"]})

    llm = make_llm(lambda name: QualitativeVerdict(answers_intent=True))
    config = SurveyConfig(min_papers_per_aspect=2, max_research_rounds=3)

    result = evaluator.run_evaluation_loop(plan, collect_fn=collect_fn, config=config, llm=llm)

    assert result.completed is True
    assert result.rounds_used == 0
    assert result.unmet_aspects == []
    assert calls == [0]  # 初回のみ、再検索していない


# ===== ループ: 1回再検索して充足 =====
def test_loop_research_then_completes():
    plan = _plan([("A", "ia", ["qa"]), ("B", "ib", ["qb"])])
    calls = []

    def collect_fn(p, round_index):
        calls.append((round_index, [a.name for a in p.aspects]))
        if round_index == 0:
            return _collection(p.theme, {"A": ["p1", "p2"], "B": []})  # B不足
        # 再検索ラウンド: B にだけ論文が付く
        return _collection(p.theme, {"B": ["p3", "p4"]})

    config = SurveyConfig(min_papers_per_aspect=2, max_research_rounds=3)
    result = evaluator.run_evaluation_loop(plan, collect_fn=collect_fn, config=config, llm=None)

    assert result.completed is True
    assert result.rounds_used == 1
    assert result.unmet_aspects == []
    # 初回は全観点、再検索は不足のBのみ
    assert calls[0][1] == ["A", "B"]
    assert calls[1][1] == ["B"]
    # マージ結果に A,B 両方の論文がある
    assert result.final_collection.by_aspect["A"] == ["p1", "p2"]
    assert result.final_collection.by_aspect["B"] == ["p3", "p4"]


# ===== ループ: max_research_rounds で必ず打ち切り、不足を明示 =====
def test_loop_stops_at_max_rounds_and_reports_unmet():
    plan = _plan([("A", "ia", ["qa"]), ("B", "ib", ["qb"])])
    calls = []

    def collect_fn(p, round_index):
        calls.append(round_index)
        if round_index == 0:
            return _collection(p.theme, {"A": ["p1", "p2"], "B": []})
        return _collection(p.theme, {"B": []})  # 何度再検索してもBは埋まらない

    config = SurveyConfig(min_papers_per_aspect=2, max_research_rounds=2)
    result = evaluator.run_evaluation_loop(plan, collect_fn=collect_fn, config=config, llm=None)

    assert result.completed is False
    assert result.rounds_used == 2  # 規定回数で打ち切り
    assert result.unmet_aspects == ["B"]  # 隠さず明示
    # 初回 + 再検索2回 = 3回
    assert calls == [0, 1, 2]
    # 評価履歴も3ラウンド分
    assert len(result.evaluations) == 3


def test_loop_max_rounds_zero_does_no_research():
    plan = _plan([("A", "ia", ["qa"])])
    calls = []

    def collect_fn(p, round_index):
        calls.append(round_index)
        return _collection(p.theme, {"A": []})  # 不足

    config = SurveyConfig(min_papers_per_aspect=2, max_research_rounds=0)
    result = evaluator.run_evaluation_loop(plan, collect_fn=collect_fn, config=config, llm=None)

    assert result.completed is False
    assert result.rounds_used == 0
    assert result.unmet_aspects == ["A"]
    assert calls == [0]  # 再検索しない
