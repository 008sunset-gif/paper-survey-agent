"""
Verifier（grounding 意味的検証）のユニットテスト（実APIは一切叩かない）

LLM はモック注入。検証:
- SUPPORTED / NOT_SUPPORTED / CONTRADICTED 各判定
- 判定は削除せずフラグで残す（NOT_SUPPORTED でも claim はレポートに残る）
- 複数論文の集約（CONTRADICTED 優先 / any SUPPORTED）
- LLM失敗・不正出力は UNVERIFIED（クラッシュしない）
- 重複照合をキャッシュで回避 / max_verifications で上限
- 検証サマリの集計
"""

import pytest

from src.paper_survey import verifier
from src.paper_survey.schemas import (
    AspectSynthesis,
    Claim,
    CollectedPaper,
    CollectionResult,
    GroundingVerdict,
    Paper,
    SurveyConfig,
    SurveySynthesis,
)


# ===== ヘルパー =====
def _paper(pid, abstract="some abstract"):
    return Paper(paper_id=pid, source="arxiv", title=f"t-{pid}", authors=[], year=2020, abstract=abstract, url="")


def _collection(paper_ids):
    papers = [CollectedPaper(paper=_paper(pid), found_in_aspects=["A"]) for pid in paper_ids]
    by_aspect = {"A": list(paper_ids)}
    return CollectionResult(theme="テーマ", papers=papers, by_aspect=by_aspect, failures=[])


def _synthesis(claims_per_aspect):
    """claims_per_aspect: list of (aspect_name, [(statement, [paper_ids]), ...])"""
    aspects = []
    for name, claims in claims_per_aspect:
        aspects.append(
            AspectSynthesis(
                aspect_name=name,
                claims=[Claim(statement=s, paper_ids=ids) for s, ids in claims],
            )
        )
    return SurveySynthesis(theme="テーマ", aspects=aspects)


def make_llm(verdict_for, *, calls=None):
    """verdict_for(statement, paper_id) -> GroundingVerdict/dict/Exception"""

    def _llm(*, statement, paper):
        if calls is not None:
            calls.append((statement, paper.paper_id))
        v = verdict_for(statement, paper.paper_id)
        if isinstance(v, Exception):
            raise v
        return v

    return _llm


# ===== 各判定 =====
def test_supported():
    synth = _synthesis([("A", [("主張1", ["p1"])])])
    coll = _collection(["p1"])
    llm = make_llm(lambda s, pid: GroundingVerdict(status="SUPPORTED", reason="要旨が裏付け"))

    report = verifier.verify(synth, coll, llm=llm)

    assert len(report.claim_verifications) == 1
    cv = report.claim_verifications[0]
    assert cv.status == "SUPPORTED"
    assert cv.paper_verdicts[0].status == "SUPPORTED"
    assert cv.paper_verdicts[0].reason == "要旨が裏付け"
    assert report.summary.counts["SUPPORTED"] == 1
    assert report.summary.supported_ratio == 1.0


def test_not_supported_is_kept_not_deleted():
    synth = _synthesis([("A", [("裏取れない主張", ["p1"])])])
    coll = _collection(["p1"])
    llm = make_llm(lambda s, pid: GroundingVerdict(status="NOT_SUPPORTED", reason="要旨に記載なし"))

    report = verifier.verify(synth, coll, llm=llm)

    # 消さずにフラグで残る
    assert len(report.claim_verifications) == 1
    assert report.claim_verifications[0].status == "NOT_SUPPORTED"
    assert report.claim_verifications[0].statement == "裏取れない主張"
    assert report.summary.counts["NOT_SUPPORTED"] == 1


def test_contradicted():
    synth = _synthesis([("A", [("矛盾する主張", ["p1"])])])
    coll = _collection(["p1"])
    llm = make_llm(lambda s, pid: GroundingVerdict(status="CONTRADICTED", reason="要旨と逆"))

    report = verifier.verify(synth, coll, llm=llm)
    assert report.claim_verifications[0].status == "CONTRADICTED"
    assert report.summary.counts["CONTRADICTED"] == 1


# ===== 集約: CONTRADICTED 優先 =====
def test_aggregation_contradicted_takes_precedence():
    synth = _synthesis([("A", [("複数根拠の主張", ["p1", "p2"])])])
    coll = _collection(["p1", "p2"])

    def verdict_for(s, pid):
        return GroundingVerdict(status="SUPPORTED") if pid == "p1" else GroundingVerdict(status="CONTRADICTED")

    report = verifier.verify(synth, coll, llm=make_llm(verdict_for))
    cv = report.claim_verifications[0]
    # 1論文が支持でも、1論文が矛盾なら矛盾を表面化
    assert cv.status == "CONTRADICTED"
    # 両論文の判定はトレース用に残る
    assert {v.paper_id: v.status for v in cv.paper_verdicts} == {"p1": "SUPPORTED", "p2": "CONTRADICTED"}


def test_aggregation_any_supported_over_unverified():
    synth = _synthesis([("A", [("主張", ["p1", "p2"])])])
    coll = _collection(["p1", "p2"])

    def verdict_for(s, pid):
        if pid == "p1":
            return RuntimeError("LLM down")  # → UNVERIFIED
        return GroundingVerdict(status="SUPPORTED")

    report = verifier.verify(synth, coll, llm=make_llm(verdict_for))
    cv = report.claim_verifications[0]
    assert cv.status == "SUPPORTED"  # UNVERIFIED より SUPPORTED を優先


# ===== LLM失敗 / 不正出力 → UNVERIFIED =====
def test_llm_failure_is_unverified():
    synth = _synthesis([("A", [("主張", ["p1"])])])
    coll = _collection(["p1"])
    llm = make_llm(lambda s, pid: RuntimeError("boom"))

    report = verifier.verify(synth, coll, llm=llm)
    assert report.claim_verifications[0].status == "UNVERIFIED"
    assert "LLM検証失敗" in report.claim_verifications[0].paper_verdicts[0].reason


def test_invalid_status_is_unverified():
    synth = _synthesis([("A", [("主張", ["p1"])])])
    coll = _collection(["p1"])
    # status が許可値以外 → pydanticで弾かれ UNVERIFIED へ
    llm = make_llm(lambda s, pid: {"status": "MAYBE", "reason": "x"})

    report = verifier.verify(synth, coll, llm=llm)
    assert report.claim_verifications[0].status == "UNVERIFIED"


def test_missing_paper_is_unverified():
    # claim が参照する論文が collection に無い（防御）
    synth = _synthesis([("A", [("主張", ["ghost"])])])
    coll = _collection(["p1"])  # ghost は無い
    called = []
    llm = make_llm(lambda s, pid: GroundingVerdict(status="SUPPORTED"), calls=called)

    report = verifier.verify(synth, coll, llm=llm)
    assert report.claim_verifications[0].status == "UNVERIFIED"
    assert called == []  # 論文が無いのでLLMは呼ばれない


# ===== 重複照合の回避（キャッシュ）=====
def test_duplicate_pairs_use_cache():
    # 2観点が同じ (statement, paper_id) を持つ → LLM呼び出しは1回
    synth = _synthesis([
        ("A", [("同じ主張", ["p1"])]),
        ("B", [("同じ主張", ["p1"])]),
    ])
    coll = _collection(["p1"])
    calls = []
    llm = make_llm(lambda s, pid: GroundingVerdict(status="SUPPORTED"), calls=calls)

    report = verifier.verify(synth, coll, llm=llm)
    assert report.llm_calls == 1
    assert report.cache_hits == 1
    assert len(calls) == 1
    # 両claimとも結果は得られている
    assert all(cv.status == "SUPPORTED" for cv in report.claim_verifications)


# ===== 照合上限（APIコスト制御）=====
def test_max_verifications_cap():
    synth = _synthesis([("A", [("主張1", ["p1"]), ("主張2", ["p2"])])])
    coll = _collection(["p1", "p2"])
    calls = []
    llm = make_llm(lambda s, pid: GroundingVerdict(status="SUPPORTED"), calls=calls)

    config = SurveyConfig(max_verifications=1)
    report = verifier.verify(synth, coll, llm=llm, config=config)

    assert report.llm_calls == 1          # 上限で打ち切り
    assert report.cap_reached is True
    assert len(calls) == 1
    statuses = [cv.status for cv in report.claim_verifications]
    assert statuses == ["SUPPORTED", "UNVERIFIED"]  # 2件目は未検証
    assert "照合上限" in report.claim_verifications[1].paper_verdicts[0].reason


# ===== サマリ集計 =====
def test_summary_counts_mixed():
    synth = _synthesis([
        ("A", [("c1", ["p1"]), ("c2", ["p2"])]),
        ("B", [("c3", ["p3"]), ("c4", ["p4"])]),
    ])
    coll = _collection(["p1", "p2", "p3", "p4"])
    mapping = {"p1": "SUPPORTED", "p2": "NOT_SUPPORTED", "p3": "CONTRADICTED", "p4": "SUPPORTED"}
    llm = make_llm(lambda s, pid: GroundingVerdict(status=mapping[pid]))

    report = verifier.verify(synth, coll, llm=llm)
    assert report.summary.total_claims == 4
    assert report.summary.counts["SUPPORTED"] == 2
    assert report.summary.counts["NOT_SUPPORTED"] == 1
    assert report.summary.counts["CONTRADICTED"] == 1
    assert report.summary.supported_ratio == 0.5


# ===== llm未指定はエラー =====
def test_missing_llm_raises():
    synth = _synthesis([("A", [("c", ["p1"])])])
    coll = _collection(["p1"])
    with pytest.raises(ValueError):
        verifier.verify(synth, coll, llm=None)
