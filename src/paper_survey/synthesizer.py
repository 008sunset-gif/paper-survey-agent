"""
Synthesizer - 英語論文群を観点ごとに日本語サーベイへ統合する

最重要要件は「各主張(claim)に根拠論文IDを必ず紐付ける」こと。日本語で書くが、
根拠は英語原論文の paper_id にトレースでき、翻訳しても原典へ戻れる。

完成度の肝＝防御:
  - LLM が論文ID紐付けをサボった（paper_ids 空）claim を検出して弾く
  - 収集済みリストに存在しない paper_id（幻覚）を参照していたら検出して弾く
これは後段 Verifier の前段の「機械的チェック」として機能する。検証済みの
最終 claim は型 `Claim`（paper_ids が min_length=1）で **構造的に空IDを持てない**。

【重要】実APIは叩かない。LLM は注入し、テストはモックで行う。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Protocol, Union

from src.paper_survey.schemas import (
    AspectDraft,
    AspectSynthesis,
    Claim,
    CollectionResult,
    Paper,
    SurveySynthesis,
    SurveyPlan,
    SynthesisIssue,
    SynthesisResult,
)

logger = logging.getLogger(__name__)


# 注入する LLM: 1観点を統合して AspectDraft（or dict）を返す
class SynthesizerLLM(Protocol):
    def __call__(
        self, *, aspect_name: str, intent: str, papers: List[Paper]
    ) -> Union[AspectDraft, dict]:
        ...


# === プロンプト（実LLM用。テストでは使わない）===
SYNTHESIZER_SYSTEM = """あなたは学術サーベイのライターです。
与えられた観点と、その観点で集めた英語論文（タイトル/著者/年/abstract/paper_id）を読み、
日本語で統合した主張(claim)を作成してください。

【絶対ルール】
1. 集めた論文に書かれた内容のみを根拠にする（推測・一般論・未収録の知識で補完しない）
2. 各 claim には、根拠とした論文の paper_id を必ず1つ以上付ける
3. 提示された論文の paper_id 以外を絶対に書かない（存在しないIDを創作しない）
4. claim は日本語で書く。ただし根拠は paper_id でトレースできるようにする
5. 重要な数値・固有名詞・手法名は省略しない

【出力】
AspectDraft の構造（claims: 各 claim は statement / paper_ids / quote）で出力してください。"""


# === 内部ヘルパー ===
def _coerce_draft(raw: Union[AspectDraft, dict]) -> AspectDraft:
    """LLM 出力を AspectDraft に正規化（不正なら例外）"""
    if isinstance(raw, AspectDraft):
        return raw
    if isinstance(raw, dict):
        return AspectDraft(**raw)
    raise TypeError(f"unexpected LLM draft type: {type(raw).__name__}")


def _clean_ids(paper_ids: Optional[List[str]]) -> List[str]:
    """論文IDを正規化: 前後空白除去・空文字除去・重複除去（順序保持）"""
    seen = set()
    cleaned: List[str] = []
    for pid in paper_ids or []:
        normalized = (pid or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)
    return cleaned


def _papers_by_aspect(collection: CollectionResult) -> Dict[str, List[Paper]]:
    """CollectionResult から 観点名 -> Paper リスト を構築（LLMへの提示用）"""
    id_to_paper = {cp.paper.paper_id: cp.paper for cp in collection.papers}
    result: Dict[str, List[Paper]] = {}
    for aspect_name, paper_ids in collection.by_aspect.items():
        result[aspect_name] = [
            id_to_paper[pid] for pid in paper_ids if pid in id_to_paper
        ]
    return result


# === メインエントリ ===
def synthesize(
    plan: SurveyPlan,
    collection: CollectionResult,
    *,
    llm: SynthesizerLLM,
    config=None,  # 予約（現状未使用。将来 claim 数上限などに使う）
) -> SynthesisResult:
    """
    観点ごとに日本語サーベイ本文（claim 群）を生成し、根拠IDを機械的に検証する。

    Args:
        plan: Planner の観点（name / intent）
        collection: Collector/Evaluator が集めた論文（観点別 + 全体）
        llm: 統合 LLM（注入）。テストではモック。

    Returns:
        SynthesisResult: 検証済み本文 + 検出した問題（ID欠落/幻覚/棄却など）
    """
    if llm is None:
        raise ValueError(
            "llm が未指定です（誤って実APIを叩かないため自動生成しません）"
        )

    # 収集済みに実在する paper_id の全体集合（幻覚IDの検出に使う）
    known_ids = {cp.paper.paper_id for cp in collection.papers}
    papers_by_aspect = _papers_by_aspect(collection)

    aspect_syntheses: List[AspectSynthesis] = []
    issues: List[SynthesisIssue] = []
    raw_count = 0
    accepted_count = 0

    for aspect in plan.aspects:
        papers = papers_by_aspect.get(aspect.name, [])

        # --- LLM 生成（失敗してもその観点だけ空にして継続）---
        try:
            draft = _coerce_draft(
                llm(aspect_name=aspect.name, intent=aspect.intent, papers=papers)
            )
        except Exception as exc:  # noqa: BLE001 - 観点単位で失敗を隔離
            logger.warning("synthesis LLM failed for aspect %r: %s", aspect.name, exc)
            issues.append(
                SynthesisIssue(
                    aspect_name=aspect.name,
                    issue_type="llm_failed",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            aspect_syntheses.append(AspectSynthesis(aspect_name=aspect.name, claims=[]))
            continue

        # --- claim ごとの機械的検証 ---
        accepted_claims: List[Claim] = []
        for raw in draft.claims:
            raw_count += 1
            statement = (raw.statement or "").strip()
            snippet = statement[:60]
            ids = _clean_ids(raw.paper_ids)

            # ① 主張文が空
            if not statement:
                issues.append(
                    SynthesisIssue(
                        aspect_name=aspect.name,
                        issue_type="empty_statement",
                        claim_statement="",
                        detail="主張文が空",
                    )
                )
                continue

            # ② 根拠IDが無い（LLMがサボった）
            if not ids:
                issues.append(
                    SynthesisIssue(
                        aspect_name=aspect.name,
                        issue_type="missing_paper_ids",
                        claim_statement=snippet,
                        detail="根拠論文IDが付いていない",
                    )
                )
                continue

            # ③ 幻覚ID（収集済みに存在しないID）を分離
            valid_ids = [pid for pid in ids if pid in known_ids]
            phantom_ids = [pid for pid in ids if pid not in known_ids]
            if phantom_ids:
                issues.append(
                    SynthesisIssue(
                        aspect_name=aspect.name,
                        issue_type="phantom_paper_id",
                        claim_statement=snippet,
                        detail=f"収集済みに存在しないID: {phantom_ids}",
                    )
                )

            # ④ 有効な根拠が残らなければ claim ごと棄却
            if not valid_ids:
                issues.append(
                    SynthesisIssue(
                        aspect_name=aspect.name,
                        issue_type="claim_dropped",
                        claim_statement=snippet,
                        detail="有効な根拠IDが残らないため主張を棄却",
                    )
                )
                continue

            # 検証通過 → 幻覚IDを除いた有効IDのみで Claim を構成
            accepted_claims.append(
                Claim(statement=statement, paper_ids=valid_ids, quote=raw.quote)
            )
            accepted_count += 1

        aspect_syntheses.append(
            AspectSynthesis(aspect_name=aspect.name, claims=accepted_claims)
        )

    synthesis = SurveySynthesis(theme=plan.theme, aspects=aspect_syntheses)
    return SynthesisResult(
        synthesis=synthesis,
        issues=issues,
        raw_claim_count=raw_count,
        accepted_claim_count=accepted_count,
    )
