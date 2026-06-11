"""
Reporter（SurveyResult → Markdown）のユニットテスト（純粋整形・API不使用）

検証:
- 各検証ステータスがバッジで正しく表示される
- 裏取り率(supported_ratio)が冒頭サマリに出る
- 各 claim に根拠論文リンク（タイトル/ID/URL）が付く
- CONTRADICTED/NOT_SUPPORTED が警告付きで目立つ形で残る（隠さない）
- 不足観点・自己批評ループのプロセス透明性が出る
- 途中停止(failed)でも破綻せず整形できる
"""

from src.paper_survey import reporter
from src.paper_survey.schemas import (
    AspectSynthesis,
    ClaimVerification,
    CollectedPaper,
    CollectionResult,
    EvaluationLoopResult,
    Paper,
    PaperVerdict,
    SurveyPlan,
    SearchAspect,
    SurveyResult,
    SurveySynthesis,
    SynthesisResult,
    VerificationReport,
    VerificationSummary,
)


# ===== モック組み立て =====
def _paper(pid, title, url, year=2020, authors=None):
    return Paper(paper_id=pid, source="arxiv", title=title, authors=authors or ["Foo Bar"], year=year, abstract="a", url=url)


def _collection(papers_with_aspects):
    """papers_with_aspects: list of (Paper, [aspect_names])"""
    papers = [CollectedPaper(paper=p, found_in_aspects=asp) for p, asp in papers_with_aspects]
    by_aspect = {}
    for p, asp in papers_with_aspects:
        for a in asp:
            by_aspect.setdefault(a, []).append(p.paper_id)
    return CollectionResult(theme="テーマ", papers=papers, by_aspect=by_aspect, failures=[])


def _cv(aspect, statement, paper_ids, status, verdicts=None, quote=None):
    return ClaimVerification(
        aspect_name=aspect,
        statement=statement,
        paper_ids=paper_ids,
        quote=quote,
        status=status,
        paper_verdicts=verdicts or [PaperVerdict(paper_id=pid, status=status, reason="r") for pid in paper_ids],
    )


def _full_result():
    p1 = _paper("2310.0001", "Efficient LLM Quantization", "http://arxiv.org/abs/2310.0001")
    p2 = _paper("2310.0002", "Knowledge Distillation Survey", "http://arxiv.org/abs/2310.0002")
    p3 = _paper("2310.0003", "Contradicting Findings", "http://arxiv.org/abs/2310.0003")
    collection = _collection([(p1, ["量子化"]), (p2, ["蒸留"]), (p3, ["蒸留"])])

    claim_verifs = [
        _cv("量子化", "4bit量子化で精度をほぼ保てる", ["2310.0001"], "SUPPORTED", quote="Table 2"),
        _cv("蒸留", "蒸留で小型化できる", ["2310.0002"], "PARTIALLY_SUPPORTED"),
        _cv("蒸留", "蒸留は常に精度を上げる", ["2310.0003"], "CONTRADICTED",
            verdicts=[PaperVerdict(paper_id="2310.0003", status="CONTRADICTED", reason="要旨は精度低下を報告")]),
    ]
    summary = VerificationSummary(
        total_claims=3,
        counts={"SUPPORTED": 1, "PARTIALLY_SUPPORTED": 1, "NOT_SUPPORTED": 0, "CONTRADICTED": 1, "UNVERIFIED": 0},
        supported_ratio=1 / 3,
    )
    verification = VerificationReport(
        theme="テーマ", claim_verifications=claim_verifs, summary=summary,
        llm_calls=3, cache_hits=1, cap_reached=False,
    )
    synthesis = SynthesisResult(
        synthesis=SurveySynthesis(theme="テーマ", aspects=[
            AspectSynthesis(aspect_name="量子化", claims=[]),
            AspectSynthesis(aspect_name="蒸留", claims=[]),
        ]),
        issues=[], raw_claim_count=3, accepted_claim_count=3,
    )
    evaluation = EvaluationLoopResult(
        theme="テーマ", completed=True, rounds_used=1, unmet_aspects=[],
        final_collection=collection, evaluations=[],
    )
    return SurveyResult(
        theme="LLM効率化", status="completed",
        plan=SurveyPlan(theme="テーマ", aspects=[SearchAspect(name="量子化", intent="i", search_queries=["q"])]),
        collection=collection, evaluation=evaluation,
        synthesis=synthesis, verification=verification,
        notes=["planner: 2 観点を生成", "verifier: SUPPORTED 1/3"],
    )


# ===== 基本構成 =====
def test_report_has_header_and_fixed_date():
    md = reporter.render_markdown(_full_result(), generated_at="2026-06-11 10:00")
    assert "# 📚 サーベイレポート: LLM効率化" in md
    assert "2026-06-11 10:00" in md
    assert "テーマ" in md


def test_supported_ratio_in_summary():
    md = reporter.render_markdown(_full_result(), generated_at="X")
    # 1/3 → 33%
    assert "33%" in md
    assert "supported_ratio" in md
    assert "1/3" in md


def test_all_status_badges_render():
    md = reporter.render_markdown(_full_result(), generated_at="X")
    assert "✅ SUPPORTED" in md
    assert "⚠️ PARTIAL" in md
    assert "🚫 CONTRADICTED" in md


def test_claims_have_paper_links_with_id_and_url():
    md = reporter.render_markdown(_full_result(), generated_at="X")
    # タイトル + URL のリンク、ID がバッククォートで付く
    assert "[Efficient LLM Quantization](http://arxiv.org/abs/2310.0001)" in md
    assert "`2310.0001`" in md
    # 引用も出る
    assert "引用: Table 2" in md


def test_contradicted_is_kept_with_warning():
    md = reporter.render_markdown(_full_result(), generated_at="X")
    # 矛盾主張が消えずに本文へ
    assert "蒸留は常に精度を上げる" in md
    # 目立つ警告が付く
    assert "要注意" in md
    assert "裏取りできませんでした" in md
    # 論文側の判定理由も出る
    assert "要旨は精度低下を報告" in md
    # 冒頭サマリにも危険件数の警告
    assert "裏取りできなかった/矛盾する主張が 1 件" in md


def test_references_section_lists_all_papers():
    md = reporter.render_markdown(_full_result(), generated_at="X")
    assert "参考文献" in md
    assert "Efficient LLM Quantization" in md
    assert "Knowledge Distillation Survey" in md
    assert "Contradicting Findings" in md
    # citation_key 形式
    assert "`arxiv:2310.0001`" in md


def test_process_transparency_section():
    md = reporter.render_markdown(_full_result(), generated_at="X")
    assert "生成プロセス" in md
    assert "再検索 1 回" in md
    assert "grounding照合" in md
    assert "LLM照合 3 回" in md


# ===== 不足観点の明示 =====
def test_incomplete_coverage_shows_unmet_aspects():
    result = _full_result()
    result.status = "incomplete_coverage"
    result.evaluation.completed = False
    result.evaluation.unmet_aspects = ["ハードウェア最適化"]
    md = reporter.render_markdown(result, generated_at="X")
    assert "不足のまま終わった観点" in md
    assert "ハードウェア最適化" in md


# ===== 途中停止(failed)でも破綻しない =====
def test_failed_result_renders_without_body():
    result = SurveyResult(
        theme="失敗テーマ", status="failed",
        plan=SurveyPlan(theme="t", aspects=[
            SearchAspect(name="観点X", intent="知りたいこと", search_queries=["query x"]),
        ]),
        collection=None, evaluation=None, synthesis=None, verification=None,
        notes=["collection failed (全滅): all search task(s) failed"],
    )
    md = reporter.render_markdown(result, generated_at="X")
    assert "🛑 途中停止" in md
    assert "レポート本文は生成されませんでした" in md
    # 計画した観点は載る
    assert "観点X" in md
    assert "query x" in md
    # 診断ログも出る
    assert "collection failed" in md


# ===== UNVERIFIED バッジ =====
def test_unverified_badge_renders():
    result = _full_result()
    result.verification.claim_verifications.append(
        _cv("量子化", "検証不能な主張", ["2310.0001"], "UNVERIFIED")
    )
    md = reporter.render_markdown(result, generated_at="X")
    assert "❓ UNVERIFIED" in md
    assert "検証不能な主張" in md
