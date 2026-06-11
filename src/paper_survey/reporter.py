"""
Reporter - SurveyResult を人が読む日本語 Markdown サーベイへ整形する

このツールの売り（出典の裏取り・検証）が最終成果物の見た目に明確に表れるよう、
各 claim に検証ステータスをバッジで付与し、"裏取りできなかった主張"を隠さず
警告付きで残す。自己批評ループの透明性（何ラウンド回ったか・不足観点）も載せる。

純粋な整形関数（SurveyResult → str）。LLM も API も使わない。
生成日時はテストを決定的にするため注入可能（未指定なら現在時刻）。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from src.paper_survey.schemas import Paper, SurveyResult

# 検証ステータス → 視覚バッジ
_STATUS_BADGE: Dict[str, str] = {
    "SUPPORTED": "✅ SUPPORTED",
    "PARTIALLY_SUPPORTED": "⚠️ PARTIAL",
    "NOT_SUPPORTED": "❌ NOT_SUPPORTED",
    "CONTRADICTED": "🚫 CONTRADICTED",
    "UNVERIFIED": "❓ UNVERIFIED",
}

# 目立たせる（裏取り不可・矛盾）ステータス
_WARN_STATUSES = {"NOT_SUPPORTED", "CONTRADICTED"}

# パイプライン状態 → 見出し用の表現
_SURVEY_STATUS_LABEL = {
    "completed": "✅ 完了（全観点カバー）",
    "incomplete_coverage": "⚠️ 一部の観点が不足のまま完了",
    "failed": "🛑 途中停止（収集失敗）",
}


def _badge(status: str) -> str:
    return _STATUS_BADGE.get(status, f"❓ {status}")


def _id_to_paper(result: SurveyResult) -> Dict[str, Paper]:
    if result.collection is None:
        return {}
    return {cp.paper.paper_id: cp.paper for cp in result.collection.papers}


def render_markdown(
    result: SurveyResult, *, generated_at: Optional[str] = None
) -> str:
    """
    SurveyResult を Markdown 文字列に整形する。

    Args:
        result: パイプラインの最終結果
        generated_at: 生成日時の表示文字列（未指定なら現在時刻）。
            テストでは固定値を渡して決定的にする。

    Returns:
        str: 日本語 Markdown レポート全文
    """
    if generated_at is None:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines: List[str] = []

    # === ヘッダ ===
    lines.append(f"# 📚 サーベイレポート: {result.theme}")
    lines.append("")
    lines.append(f"- **テーマ**: {result.theme}")
    lines.append(f"- **生成日時**: {generated_at}")
    lines.append(
        f"- **ステータス**: {_SURVEY_STATUS_LABEL.get(result.status, result.status)}"
    )
    lines.append("")

    # === サマリ（裏取り率を冒頭に明示）===
    lines.extend(_render_summary(result))

    # === 途中停止なら、本文の代わりに停止理由を出して終了 ===
    if result.verification is None:
        lines.append("## ⚠️ レポート本文は生成されませんでした")
        lines.append("")
        lines.append(
            "パイプラインが途中で停止したため、検証済み本文はありません。"
            "計画した観点と診断ログのみ掲載します。"
        )
        lines.append("")
        lines.extend(_render_planned_aspects(result))
        lines.extend(_render_process(result, generated_at))
        return "\n".join(lines).rstrip() + "\n"

    # === 観点ごとの本文 ===
    lines.extend(_render_body(result))

    # === 参考文献 ===
    lines.extend(_render_references(result))

    # === 生成プロセスの透明性 ===
    lines.extend(_render_process(result, generated_at))

    return "\n".join(lines).rstrip() + "\n"


def _render_summary(result: SurveyResult) -> List[str]:
    lines = ["## 📊 サマリ", ""]

    ver = result.verification
    if ver is not None:
        summary = ver.summary
        ratio_pct = f"{summary.supported_ratio * 100:.0f}%"
        supported = summary.counts.get("SUPPORTED", 0)
        lines.append(
            f"- **裏取り率 (supported_ratio)**: **{ratio_pct}** "
            f"（{supported}/{summary.total_claims} claim が論文要旨で裏付け済み）"
        )
        # ステータス内訳
        breakdown = " / ".join(
            f"{_badge(s)}: {summary.counts.get(s, 0)}"
            for s in ["SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED", "CONTRADICTED", "UNVERIFIED"]
        )
        lines.append(f"- **検証内訳**: {breakdown}")

        # 裏取りできなかった主張の警告（売り＝誠実さ）
        problematic = summary.counts.get("NOT_SUPPORTED", 0) + summary.counts.get("CONTRADICTED", 0)
        if problematic:
            lines.append(
                f"- 🚨 **裏取りできなかった/矛盾する主張が {problematic} 件あります"
                "（本文に警告付きで残しています。鵜呑みにしないでください）**"
            )

    # 不足のまま終わった観点
    if result.evaluation is not None and not result.evaluation.completed:
        unmet = "、".join(result.evaluation.unmet_aspects) or "(なし)"
        lines.append(f"- ⚠️ **不足のまま終わった観点**: {unmet}")

    # 構造検証で弾かれた主張（Synthesizer 段）
    if result.synthesis is not None and result.synthesis.has_unsupported_claims:
        lines.append(
            f"- 🧹 Synthesizer の構造検証で根拠不備の主張を除去しました"
            f"（採用 {result.synthesis.accepted_claim_count}/{result.synthesis.raw_claim_count}、"
            f"問題 {result.synthesis.total_issues} 件）"
        )

    lines.append("")
    return lines


def _group_claims_by_aspect(result: SurveyResult):
    """verification.claim_verifications を観点順にグループ化"""
    grouped: "OrderedDict[str, list]" = OrderedDict()
    for cv in result.verification.claim_verifications:
        grouped.setdefault(cv.aspect_name, []).append(cv)
    return grouped


def _render_body(result: SurveyResult) -> List[str]:
    lines: List[str] = []
    id_to_paper = _id_to_paper(result)
    grouped = _group_claims_by_aspect(result)

    if not grouped:
        lines.append("## 本文")
        lines.append("")
        lines.append("検証済みの主張がありませんでした。")
        lines.append("")
        return lines

    for aspect_name, claims in grouped.items():
        lines.append(f"## 観点: {aspect_name}")
        lines.append("")
        for cv in claims:
            # 主張＋ステータスバッジ
            lines.append(f"### {_badge(cv.status)}")
            lines.append("")
            lines.append(cv.statement)
            lines.append("")
            if cv.quote:
                lines.append(f"> 引用: {cv.quote}")
                lines.append("")

            # 裏取り不可・矛盾は目立たせる
            if cv.status in _WARN_STATUSES:
                lines.append(
                    f"> 🚨 **要注意**: この主張は根拠論文で裏取りできませんでした"
                    f"（{cv.status}）。論文の主張ではない可能性があります。"
                )
                lines.append("")

            # 根拠論文（原典トレース）
            lines.append("**根拠論文:**")
            for pid in cv.paper_ids:
                paper = id_to_paper.get(pid)
                verdict = next((v for v in cv.paper_verdicts if v.paper_id == pid), None)
                vtxt = f" — {_badge(verdict.status)}" if verdict else ""
                if paper:
                    title = paper.title or "(タイトル不明)"
                    url = paper.url or ""
                    link = f"[{title}]({url})" if url else title
                    lines.append(f"- {link} `{pid}`{vtxt}")
                else:
                    lines.append(f"- `{pid}`{vtxt}（収集リストに見つからず）")
                # 問題ある判定は理由も出す
                if verdict and verdict.status in _WARN_STATUSES and verdict.reason:
                    lines.append(f"  - 理由: {verdict.reason}")
            lines.append("")

    return lines


def _render_references(result: SurveyResult) -> List[str]:
    lines = ["## 📖 参考文献（収集論文・重複排除済み）", ""]
    if result.collection is None or not result.collection.papers:
        lines.append("（収集論文なし）")
        lines.append("")
        return lines

    for i, cp in enumerate(result.collection.papers, 1):
        p = cp.paper
        title = p.title or "(タイトル不明)"
        url = p.url or ""
        link = f"[{title}]({url})" if url else title
        authors = ", ".join(p.authors[:3]) + (" ほか" if len(p.authors) > 3 else "")
        year = p.year if p.year is not None else "n.d."
        found = "、".join(cp.found_in_aspects)
        lines.append(
            f"{i}. {link} `{p.citation_key}` — {authors or '著者不明'} ({year})"
            + (f" — 観点: {found}" if found else "")
        )
    lines.append("")
    return lines


def _render_planned_aspects(result: SurveyResult) -> List[str]:
    """途中停止時に、計画した観点だけでも掲載"""
    lines: List[str] = []
    if result.plan is None or not result.plan.aspects:
        return lines
    lines.append("### 計画した観点")
    lines.append("")
    for a in result.plan.aspects:
        queries = ", ".join(a.search_queries)
        lines.append(f"- **{a.name}**: {a.intent}（クエリ: {queries}）")
    lines.append("")
    return lines


def _render_process(result: SurveyResult, generated_at: str) -> List[str]:
    """生成プロセスの透明性（ループ回数・不足・コスト・診断ログ）"""
    lines = ["## 🔍 生成プロセス（透明性）", ""]

    if result.evaluation is not None:
        ev = result.evaluation
        lines.append(
            f"- **自己批評ループ**: 再検索 {ev.rounds_used} 回、"
            f"完了={'はい' if ev.completed else 'いいえ'}"
        )
        if not ev.completed and ev.unmet_aspects:
            lines.append(f"  - 不足のまま残った観点: {'、'.join(ev.unmet_aspects)}")

    if result.collection is not None and result.collection.failures:
        lines.append(
            f"- **収集の部分失敗**: {result.collection.total_failures} 件"
            "（成功分のみでレポート生成）"
        )

    if result.verification is not None:
        ver = result.verification
        cap = "（照合上限に到達）" if ver.cap_reached else ""
        lines.append(
            f"- **grounding照合**: LLM照合 {ver.llm_calls} 回 / "
            f"重複回避 {ver.cache_hits} 回{cap}"
        )

    if result.notes:
        lines.append("- **段階ログ**:")
        for n in result.notes:
            lines.append(f"  - {n}")

    lines.append("")
    lines.append(
        "> このレポートは収集した実在論文の要旨に対する機械＋LLM検証を経ています。"
        "✅以外のステータスが付いた主張は、原典に当たって確認してください。"
    )
    lines.append("")
    return lines
