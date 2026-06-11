"""
Verifier - レベル3 grounding 検証（意味的裏取り）

Synthesizer が「構造的裏取り（紐付けた paper_id が実在するか）」を担うのに対し、
Verifier は「意味的裏取り（主張がその論文の要旨と矛盾しないか）」を担う二段目。
「LLM を信じない設計」の最終形であり、本ツールの核心。

各 claim について、紐付いた論文の abstract と主張文を LLM に渡し、
SUPPORTED / PARTIALLY_SUPPORTED / NOT_SUPPORTED / CONTRADICTED で判定する。

設計の肝:
- 判定は **削除せずフラグで残す**。NOT_SUPPORTED/CONTRADICTED の主張も消さず
  「検証ステータス付き」で残し、最終レポートで可視化する
  （研究用途では"裏取りできなかった事実"自体が重要なため）。
- LLM 評価が失敗/不正なら **UNVERIFIED**（検証不能）として扱い、クラッシュしない。
- 同一 (claim文, 論文ID) の重複照合をキャッシュで回避し、
  `SurveyConfig.max_verifications` で LLM 照合回数の上限を設ける（APIコスト制御）。

【重要】実APIは叩かない。LLM は注入し、テストはモックで行う。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Protocol, Tuple, Union

from src.paper_survey.schemas import (
    ClaimVerification,
    CollectionResult,
    GroundingVerdict,
    Paper,
    PaperVerdict,
    SurveyConfig,
    SurveySynthesis,
    VerificationReport,
    VerificationStatus,
    VerificationSummary,
)

logger = logging.getLogger(__name__)


# 注入する LLM: (claim文, 論文) 1組を判定して GroundingVerdict（or dict）を返す
class VerifierLLM(Protocol):
    def __call__(self, *, statement: str, paper: Paper) -> Union[GroundingVerdict, dict]:
        ...


# === プロンプト（実LLM用。テストでは使わない）===
VERIFIER_SYSTEM = """あなたは厳格なファクトチェッカーです。
ある「主張（日本語）」が、提示された「論文の要旨(abstract)」によって裏付けられるかを判定してください。

【判定ルール】
- SUPPORTED: 論文の要旨が主張を明確に裏付ける
- PARTIALLY_SUPPORTED: 主張の一部のみ裏付けられ、残りは要旨からは判断できない
- NOT_SUPPORTED: 要旨は主張に触れていない（裏取りできない）
- CONTRADICTED: 要旨が主張と矛盾する

【厳格性】
1. 要旨に書かれていることだけを根拠にする。一般知識で補完しない。
2. 主張が要旨の範囲を超えていれば SUPPORTED にしない。
3. 少しでも矛盾があれば CONTRADICTED を選ぶ。
4. 必ず理由(reason)を、要旨の記述に即して書く。

【出力】GroundingVerdict（status / reason）で出力してください。"""


# claim 全体ステータスの集約優先順位（上にあるほど優先）
# 矛盾・裏取り不可を強く可視化する「LLMを信じない」方針: CONTRADICTED を最優先で表面化。
_AGGREGATION_PRECEDENCE: List[VerificationStatus] = [
    "CONTRADICTED",
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "NOT_SUPPORTED",
    "UNVERIFIED",
]

_ALL_STATUSES: List[VerificationStatus] = [
    "SUPPORTED",
    "PARTIALLY_SUPPORTED",
    "NOT_SUPPORTED",
    "CONTRADICTED",
    "UNVERIFIED",
]


def _coerce_verdict(raw: Union[GroundingVerdict, dict]) -> GroundingVerdict:
    """LLM 出力を GroundingVerdict に正規化（不正な status は pydantic が弾く）"""
    if isinstance(raw, GroundingVerdict):
        return raw
    if isinstance(raw, dict):
        return GroundingVerdict(**raw)
    raise TypeError(f"unexpected LLM verdict type: {type(raw).__name__}")


def _aggregate_status(statuses: List[VerificationStatus]) -> VerificationStatus:
    """複数論文の判定を claim 全体の1ステータスへ集約"""
    for candidate in _AGGREGATION_PRECEDENCE:
        if candidate in statuses:
            return candidate
    return "UNVERIFIED"


def _summarize(verifications: List[ClaimVerification]) -> VerificationSummary:
    """検証サマリ（ステータス別件数・SUPPORTED割合）を作る"""
    counts: Dict[str, int] = {s: 0 for s in _ALL_STATUSES}
    for v in verifications:
        counts[v.status] = counts.get(v.status, 0) + 1
    total = len(verifications)
    supported_ratio = (counts["SUPPORTED"] / total) if total else 0.0
    return VerificationSummary(
        total_claims=total, counts=counts, supported_ratio=supported_ratio
    )


def verify(
    synthesis: SurveySynthesis,
    collection: CollectionResult,
    *,
    llm: VerifierLLM,
    config: Optional[SurveyConfig] = None,
) -> VerificationReport:
    """
    各 claim を紐付け論文の abstract と LLM で照合し、grounding を検証する。

    Args:
        synthesis: Synthesizer の本文（観点→claim）
        collection: 収集済み論文（abstract の取得元）
        llm: 照合 LLM（注入）。テストではモック。
        config: 設定（max_verifications で照合回数の上限）

    Returns:
        VerificationReport: claim ごとの検証ステータス（削除せずフラグ）＋サマリ＋
        コスト情報（llm_calls / cache_hits / cap_reached）
    """
    if llm is None:
        raise ValueError(
            "llm が未指定です（誤って実APIを叩かないため自動生成しません）"
        )
    config = config or SurveyConfig()

    id_to_paper = {cp.paper.paper_id: cp.paper for cp in collection.papers}

    # (claim文, 論文ID) → PaperVerdict のキャッシュ（重複照合の回避）
    cache: Dict[Tuple[str, str], PaperVerdict] = {}
    llm_calls = 0
    cache_hits = 0
    cap_reached = False

    verifications: List[ClaimVerification] = []

    for aspect in synthesis.aspects:
        for claim in aspect.claims:
            paper_verdicts: List[PaperVerdict] = []

            for pid in claim.paper_ids:
                key = (claim.statement, pid)

                # --- 重複照合の回避 ---
                if key in cache:
                    cache_hits += 1
                    paper_verdicts.append(cache[key])
                    continue

                paper = id_to_paper.get(pid)

                # --- 論文が収集済みに無い（防御。通常 Synthesizer が弾く）---
                if paper is None:
                    verdict = PaperVerdict(
                        paper_id=pid,
                        status="UNVERIFIED",
                        reason="収集済みに論文が見つからず照合不能",
                    )
                    cache[key] = verdict
                    paper_verdicts.append(verdict)
                    continue

                # --- 照合上限（APIコスト制御）---
                if llm_calls >= config.max_verifications:
                    cap_reached = True
                    verdict = PaperVerdict(
                        paper_id=pid,
                        status="UNVERIFIED",
                        reason=f"照合上限({config.max_verifications})に達したため未検証",
                    )
                    cache[key] = verdict
                    paper_verdicts.append(verdict)
                    continue

                # --- LLM 照合（失敗/不正は UNVERIFIED でクラッシュさせない）---
                llm_calls += 1
                try:
                    gv = _coerce_verdict(
                        llm(statement=claim.statement, paper=paper)
                    )
                    verdict = PaperVerdict(
                        paper_id=pid, status=gv.status, reason=gv.reason
                    )
                except Exception as exc:  # noqa: BLE001 - 検証不能は UNVERIFIED に倒す
                    logger.warning(
                        "grounding LLM failed for claim=%r paper=%s: %s",
                        claim.statement[:40],
                        pid,
                        exc,
                    )
                    verdict = PaperVerdict(
                        paper_id=pid,
                        status="UNVERIFIED",
                        reason=f"LLM検証失敗: {type(exc).__name__}: {exc}",
                    )

                cache[key] = verdict
                paper_verdicts.append(verdict)

            # --- claim 全体ステータスを集約（claim は消さずフラグで残す）---
            status = _aggregate_status([v.status for v in paper_verdicts])
            verifications.append(
                ClaimVerification(
                    aspect_name=aspect.aspect_name,
                    statement=claim.statement,
                    paper_ids=list(claim.paper_ids),
                    quote=claim.quote,
                    status=status,
                    paper_verdicts=paper_verdicts,
                )
            )

    return VerificationReport(
        theme=synthesis.theme,
        claim_verifications=verifications,
        summary=_summarize(verifications),
        llm_calls=llm_calls,
        cache_hits=cache_hits,
        cap_reached=cap_reached,
    )
