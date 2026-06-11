"""
Synthesizer（日本語統合）のユニットテスト（実APIは一切叩かない）

LLM はモック注入。検証:
- 観点ごとに claim が生成され、各 claim に論文IDが紐付く
- 根拠論文IDが無い claim を検出して弾く（missing_paper_ids）
- 収集済みに存在しないID参照を検出（phantom_paper_id）、有効IDが残らなければ棄却
- Claim 型が構造的に空IDを許さない
- LLM失敗はその観点だけ隔離して継続
"""

import pytest
from pydantic import ValidationError

from src.paper_survey import synthesizer
from src.paper_survey.schemas import (
    AspectDraft,
    Claim,
    CollectedPaper,
    CollectionResult,
    Paper,
    RawClaim,
    SearchAspect,
    SurveyPlan,
)


# ===== ヘルパー =====
def _paper(pid):
    return Paper(paper_id=pid, source="arxiv", title=f"t-{pid}", authors=[], year=2020, abstract="a", url="")


def _plan(aspects):
    return SurveyPlan(
        theme="テーマ",
        aspects=[SearchAspect(name=n, intent=it, search_queries=["q"]) for n, it in aspects],
    )


def _collection(aspect_to_ids):
    all_ids = []
    for ids in aspect_to_ids.values():
        all_ids.extend(ids)
    unique = list(dict.fromkeys(all_ids))
    papers = [
        CollectedPaper(
            paper=_paper(pid),
            found_in_aspects=[a for a, ids in aspect_to_ids.items() if pid in ids],
        )
        for pid in unique
    ]
    return CollectionResult(theme="テーマ", papers=papers, by_aspect=dict(aspect_to_ids), failures=[])


def make_llm(draft_for):
    """draft_for(aspect_name) -> AspectDraft/dict/Exception"""

    def _llm(*, aspect_name, intent, papers):
        v = draft_for(aspect_name)
        if isinstance(v, Exception):
            raise v
        return v

    return _llm


# ===== 正常系: 観点ごとにclaim生成・ID紐付け =====
def test_synthesize_generates_claims_with_ids():
    plan = _plan([("A", "ia"), ("B", "ib")])
    collection = _collection({"A": ["p1", "p2"], "B": ["p3"]})

    drafts = {
        "A": AspectDraft(claims=[
            RawClaim(statement="量子化で精度をほぼ保てる", paper_ids=["p1"], quote="Table 2"),
            RawClaim(statement="4bitでも有効", paper_ids=["p1", "p2"]),
        ]),
        "B": AspectDraft(claims=[RawClaim(statement="蒸留が有効", paper_ids=["p3"])]),
    }
    result = synthesizer.synthesize(plan, collection, llm=make_llm(lambda n: drafts[n]))

    # 観点が2つ、claim にIDが紐付く
    a = result.synthesis.aspects[0]
    assert a.aspect_name == "A"
    assert len(a.claims) == 2
    assert all(len(c.paper_ids) >= 1 for c in a.claims)
    assert a.claims[0].quote == "Table 2"  # 引用が保持される
    assert result.accepted_claim_count == 3
    assert result.raw_claim_count == 3
    assert result.total_issues == 0
    assert result.has_unsupported_claims is False


# ===== ID無しclaimを検出して弾く =====
def test_missing_paper_ids_is_flagged_and_dropped():
    plan = _plan([("A", "ia")])
    collection = _collection({"A": ["p1"]})
    draft = AspectDraft(claims=[
        RawClaim(statement="根拠つき主張", paper_ids=["p1"]),
        RawClaim(statement="根拠なし主張", paper_ids=[]),       # ID無し
        RawClaim(statement="空白IDだけ", paper_ids=["  ", ""]),  # 実質ID無し
    ])
    result = synthesizer.synthesize(plan, collection, llm=make_llm(lambda n: draft))

    accepted = result.synthesis.aspects[0].claims
    assert [c.statement for c in accepted] == ["根拠つき主張"]
    missing = [i for i in result.issues if i.issue_type == "missing_paper_ids"]
    assert len(missing) == 2
    assert result.has_unsupported_claims is True
    assert result.accepted_claim_count == 1


# ===== 存在しないID参照を検出 =====
def test_phantom_paper_id_detected():
    plan = _plan([("A", "ia")])
    collection = _collection({"A": ["real1", "real2"]})
    draft = AspectDraft(claims=[
        # 一部だけ幻覚 → 幻覚を除いて有効IDで残す
        RawClaim(statement="一部幻覚", paper_ids=["real1", "ghost"]),
        # 全部幻覚 → claim棄却
        RawClaim(statement="全部幻覚", paper_ids=["ghost2", "ghost3"]),
    ])
    result = synthesizer.synthesize(plan, collection, llm=make_llm(lambda n: draft))

    accepted = result.synthesis.aspects[0].claims
    # 一部幻覚claimは有効ID(real1)のみで残る
    assert len(accepted) == 1
    assert accepted[0].statement == "一部幻覚"
    assert accepted[0].paper_ids == ["real1"]

    phantom_issues = [i for i in result.issues if i.issue_type == "phantom_paper_id"]
    dropped_issues = [i for i in result.issues if i.issue_type == "claim_dropped"]
    assert len(phantom_issues) == 2  # 両claimとも幻覚IDを含む
    assert len(dropped_issues) == 1  # 全部幻覚のものは棄却
    assert result.accepted_claim_count == 1


# ===== 幻覚検出は「収集済み全体」を基準にする（観点横断OK）=====
def test_phantom_check_uses_global_collection():
    # pB は観点Bで収集されたが、観点Aのclaimが引用してもOK（全体に実在）
    plan = _plan([("A", "ia"), ("B", "ib")])
    collection = _collection({"A": ["pA"], "B": ["pB"]})
    drafts = {
        "A": AspectDraft(claims=[RawClaim(statement="横断引用", paper_ids=["pB"])]),
        "B": AspectDraft(claims=[RawClaim(statement="自前", paper_ids=["pB"])]),
    }
    result = synthesizer.synthesize(plan, collection, llm=make_llm(lambda n: drafts[n]))
    # 幻覚扱いされない
    assert result.total_issues == 0
    assert result.synthesis.aspects[0].claims[0].paper_ids == ["pB"]


# ===== Claim型が構造的に空IDを禁止 =====
def test_claim_type_forbids_empty_paper_ids():
    with pytest.raises(ValidationError):
        Claim(statement="x", paper_ids=[])
    with pytest.raises(ValidationError):
        Claim(statement="", paper_ids=["p1"])  # 空statementも禁止


# ===== 空statementの検出 =====
def test_empty_statement_flagged():
    plan = _plan([("A", "ia")])
    collection = _collection({"A": ["p1"]})
    draft = AspectDraft(claims=[
        RawClaim(statement="   ", paper_ids=["p1"]),  # 空白のみ
        RawClaim(statement="有効", paper_ids=["p1"]),
    ])
    result = synthesizer.synthesize(plan, collection, llm=make_llm(lambda n: draft))
    assert [c.statement for c in result.synthesis.aspects[0].claims] == ["有効"]
    assert any(i.issue_type == "empty_statement" for i in result.issues)


# ===== LLM失敗はその観点だけ隔離して継続 =====
def test_llm_failure_isolated_per_aspect():
    plan = _plan([("A", "ia"), ("B", "ib")])
    collection = _collection({"A": ["p1"], "B": ["p2"]})

    def draft_for(name):
        if name == "A":
            return RuntimeError("LLM down")
        return AspectDraft(claims=[RawClaim(statement="Bは成功", paper_ids=["p2"])])

    result = synthesizer.synthesize(plan, collection, llm=make_llm(draft_for))

    # Aは失敗で空、Bは成功
    a = next(x for x in result.synthesis.aspects if x.aspect_name == "A")
    b = next(x for x in result.synthesis.aspects if x.aspect_name == "B")
    assert a.claims == []
    assert len(b.claims) == 1
    assert any(i.issue_type == "llm_failed" and i.aspect_name == "A" for i in result.issues)


# ===== dict出力も受け付ける =====
def test_accepts_dict_draft():
    plan = _plan([("A", "ia")])
    collection = _collection({"A": ["p1"]})
    draft = {"claims": [{"statement": "dict主張", "paper_ids": ["p1"]}]}
    result = synthesizer.synthesize(plan, collection, llm=make_llm(lambda n: draft))
    assert result.synthesis.aspects[0].claims[0].statement == "dict主張"


# ===== llm未指定はエラー（実API誤爆防止）=====
def test_missing_llm_raises():
    plan = _plan([("A", "ia")])
    collection = _collection({"A": ["p1"]})
    with pytest.raises(ValueError):
        synthesizer.synthesize(plan, collection, llm=None)
