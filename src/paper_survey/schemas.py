"""
paper_survey のデータスキーマ

Searcher が arXiv / Semantic Scholar から取得した論文メタデータを、
ソースに依らない共通の `Paper` 型に正規化する。
LLM が後段で出典を捏造しないよう、ここで「実在する論文ID」を必ず保持する。
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# 論文の取得元
PaperSource = Literal["arxiv", "semantic_scholar"]


class Paper(BaseModel):
    """
    1本の論文（ソース非依存の正規化済みメタデータ）

    arXiv と Semantic Scholar でフィールド名が違うため、Searcher 側で
    この共通形に詰め替える。後段の Synthesizer / Verifier は
    `paper_id` を主張と論文の紐付けキーとして使う。
    """

    paper_id: str = Field(
        description="ソース固有の論文ID（arXiv ID または Semantic Scholar paperId）"
    )
    source: PaperSource = Field(description="取得元: arxiv / semantic_scholar")
    title: str = Field(description="論文タイトル")
    authors: List[str] = Field(default_factory=list, description="著者名のリスト")
    year: Optional[int] = Field(default=None, description="出版年（取得できなければ None）")
    abstract: str = Field(default="", description="要旨（Verifier の矛盾照合に使う）")
    url: str = Field(default="", description="論文ページのURL")

    @property
    def citation_key(self) -> str:
        """主張に紐付ける一意キー（例: 'arxiv:2310.06825v1'）"""
        return f"{self.source}:{self.paper_id}"


# ===== Planner（観点分解）の型 =====
class SearchAspect(BaseModel):
    """
    サーベイの1観点

    日本語テーマを多角的にカバーするための切り口。検索クエリは英語論文を
    探すため英語で持つ（日本語→英語変換は Planner の LLM が担う）。
    """

    name: str = Field(description="観点名（日本語）")
    intent: str = Field(description="この観点で何を知りたいか（日本語）")
    search_queries: List[str] = Field(
        default_factory=list,
        description="Searcher に渡す英語の検索クエリ（複数可）",
    )


class SurveyPlan(BaseModel):
    """
    Planner の出力するサーベイ計画

    観点数は固定せず、テーマの広さに応じて Planner（LLM）が適切な数
    （目安3〜5）を決める。
    """

    theme: str = Field(description="入力された研究テーマ（日本語可）")
    aspects: List[SearchAspect] = Field(description="サーベイの観点リスト")
    reasoning: Optional[str] = Field(
        default=None, description="なぜこの観点群に分解したかの理由"
    )


# ===== 実行時設定（通し確認で API 消費を絞るためのパラメータ）=====
class SurveyConfig(BaseModel):
    """
    パイプライン全体の実行時設定

    コード品質は落とさず「実行設定だけ軽く」できるようにするための束。
    Planner は観点数・クエリ数の上限に使い、papers_per_aspect /
    max_research_rounds は後段（Searcher / Evaluator）が参照する。
    """

    # Planner が狙う観点数の目安（LLM へのヒント。厳密強制ではない）
    min_aspects: int = Field(default=3, ge=1, le=10)
    max_aspects: int = Field(default=5, ge=1, le=10, description="観点数の上限（超過は切り詰め）")
    max_queries_per_aspect: int = Field(default=3, ge=1, le=10, description="1観点あたりの検索クエリ数上限")

    # 後段が参照する実行時の絞り込みパラメータ
    papers_per_aspect: int = Field(default=10, ge=1, description="1観点あたりの取得論文数")
    max_research_rounds: int = Field(default=2, ge=0, description="Evaluator による再検索の最大回数")

    # 並列収集の同時リクエスト数（API過負荷/レート制限を避けるための上限）
    max_concurrent_requests: int = Field(
        default=4, ge=1, le=32, description="並列検索の同時リクエスト数上限（主ソース=arXiv）"
    )

    # Semantic Scholar（補助ソース）の逐次リクエスト間の最小間隔（秒）
    # 無認証のレート上限を超えないよう、礼儀正しく間隔を空けて叩く
    semantic_scholar_min_interval: float = Field(
        default=1.0, ge=0.0, description="S2 リクエスト間の最小待機秒数（逐次）"
    )

    # Evaluator: 観点を「カバー済み」と見なす最低論文数（機械チェックの閾値）
    min_papers_per_aspect: int = Field(
        default=2, ge=0, description="観点ごとの最低論文数（これ未満は機械的に不足と判定）"
    )

    # Verifier: grounding 照合（claim×論文）の LLM 呼び出し上限（APIコスト制御）
    max_verifications: int = Field(
        default=100, ge=0, description="grounding照合の最大LLM呼び出し回数（超過分はUNVERIFIED）"
    )

    @classmethod
    def light(cls) -> "SurveyConfig":
        """通し確認用の軽量プリセット（API消費を最小化）"""
        return cls(
            min_aspects=2,
            max_aspects=2,
            max_queries_per_aspect=1,
            papers_per_aspect=2,
            max_research_rounds=0,
            max_concurrent_requests=2,
            min_papers_per_aspect=1,
            max_verifications=4,
        )


# ===== 並列収集層（Collector）の型 =====
class CollectionFailure(BaseModel):
    """1件の検索（観点×クエリ×ソース）が失敗したことの記録"""

    aspect_name: str = Field(description="失敗した観点名")
    query: str = Field(description="失敗した検索クエリ")
    source: str = Field(description="失敗したソース（arxiv / semantic_scholar）")
    error: str = Field(description="エラーの種類とメッセージ")


class CollectedPaper(BaseModel):
    """
    重複排除後の1論文

    観点をまたいで同じ paper_id が見つかった場合は1つに統合し、
    どの観点で見つかったか（出典追跡用）を `found_in_aspects` に保持する。
    """

    paper: Paper
    found_in_aspects: List[str] = Field(
        default_factory=list, description="この論文が見つかった観点名のリスト"
    )


class CollectionResult(BaseModel):
    """並列収集層の出力"""

    theme: str
    papers: List[CollectedPaper] = Field(
        default_factory=list, description="重複排除済みの論文（全観点横断）"
    )
    by_aspect: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="観点名 → その観点で採用された paper_id のリスト",
    )
    failures: List[CollectionFailure] = Field(
        default_factory=list, description="失敗した検索の一覧（部分失敗の可視化）"
    )

    @property
    def total_papers(self) -> int:
        return len(self.papers)

    @property
    def total_failures(self) -> int:
        return len(self.failures)


# ===== Evaluator（自己批評ループ）の型 =====
class QualitativeVerdict(BaseModel):
    """
    1観点に対する LLM の質的評価（LLMが返す構造化出力）

    集まった論文がその観点の intent に答えているかを判定する。
    """

    answers_intent: bool = Field(description="集まった論文が観点の intent に答えているか")
    missing_points: str = Field(
        default="", description="不足している論点（再検索の手がかり）"
    )
    suggested_queries: List[str] = Field(
        default_factory=list, description="不足を埋めるための新しい英語検索クエリ案"
    )


class AspectAssessment(BaseModel):
    """1観点の評価結果（機械チェック＋質的チェックの統合）"""

    aspect_name: str
    paper_count: int = Field(description="その観点に付いた論文数")
    mechanical_ok: bool = Field(description="最低論文数を満たすか（機械チェック）")
    qualitative_ok: Optional[bool] = Field(
        default=None, description="質的チェック結果。None=LLM未実行/失敗でフォールバック"
    )
    covered: bool = Field(description="最終的にカバー済みと判定したか")
    reason: str = Field(default="", description="判定理由（可視化用）")


class ReSearchInstruction(BaseModel):
    """不足観点に対する再検索の指示"""

    aspect_name: str
    new_queries: List[str] = Field(
        description="前回と重複しない新しい英語検索クエリ"
    )
    reason: str = Field(default="", description="なぜ再検索が必要か")


class EvaluationResult(BaseModel):
    """1ラウンド分の評価結果"""

    round_index: int = Field(description="評価ラウンド番号（0=初回）")
    assessments: List[AspectAssessment] = Field(default_factory=list)
    covered_aspects: List[str] = Field(default_factory=list)
    insufficient_aspects: List[str] = Field(default_factory=list)
    research_instructions: List[ReSearchInstruction] = Field(default_factory=list)
    is_complete: bool = Field(description="全観点カバー済みか（ループ終了条件）")
    llm_used: bool = Field(
        default=False, description="このラウンドで質的評価(LLM)が1回でも成功したか"
    )


class EvaluationLoopResult(BaseModel):
    """自己批評ループ全体の最終結果"""

    theme: str
    completed: bool = Field(description="全観点カバーで正常終了したか")
    rounds_used: int = Field(description="実行した再検索ラウンド数")
    unmet_aspects: List[str] = Field(
        default_factory=list,
        description="打ち切り時に不足のまま残った観点（隠さず明示）",
    )
    final_collection: CollectionResult
    evaluations: List[EvaluationResult] = Field(
        default_factory=list, description="各ラウンドの評価履歴"
    )


# ===== Synthesizer（日本語統合）の型 =====
class RawClaim(BaseModel):
    """
    LLM が生成する主張の生データ（検証前。緩い型で受ける）

    LLM は論文ID紐付けをサボることがあるため、ここでは paper_ids が空でも
    受け取れるようにし、Synthesizer 側で検出・除去する。
    """

    statement: str = Field(default="", description="日本語の主張文")
    paper_ids: List[str] = Field(default_factory=list, description="根拠論文IDのリスト")
    quote: Optional[str] = Field(default=None, description="（任意）該当箇所/引用")


class AspectDraft(BaseModel):
    """1観点ぶんの LLM 生出力（複数 RawClaim）"""

    claims: List[RawClaim] = Field(default_factory=list)


class Claim(BaseModel):
    """
    検証済みの主張（最終成果物）

    paper_ids は min_length=1 で **構造的に空を許さない**。これにより
    「根拠論文IDの無い主張」は Claim 型として存在できない（型レベルの保証）。
    """

    statement: str = Field(min_length=1, description="日本語の主張文")
    paper_ids: List[str] = Field(
        min_length=1, description="根拠論文IDのリスト（収集済みに実在するIDのみ）"
    )
    quote: Optional[str] = Field(default=None, description="（任意）該当箇所/引用")


class AspectSynthesis(BaseModel):
    """1観点ぶんの統合結果（検証済み claim のみ）"""

    aspect_name: str
    claims: List[Claim] = Field(default_factory=list)


class SurveySynthesis(BaseModel):
    """サーベイ本文（観点 → 複数主張の階層）"""

    theme: str
    aspects: List[AspectSynthesis] = Field(default_factory=list)


# 機械的検証で検出した問題の種類
SynthesisIssueType = Literal[
    "empty_statement",     # 主張文が空
    "missing_paper_ids",   # 根拠論文IDが無い（LLMがサボった）
    "phantom_paper_id",    # 収集済みに存在しないIDを参照（幻覚）
    "claim_dropped",       # 有効な根拠が残らず棄却
    "llm_failed",          # その観点のLLM生成自体が失敗
]


class SynthesisIssue(BaseModel):
    """Synthesizer の機械的検証で検出した問題（Verifier 前段のチェック）"""

    aspect_name: str
    issue_type: SynthesisIssueType
    claim_statement: str = Field(default="", description="該当主張の冒頭（可視化用）")
    detail: str = Field(default="", description="詳細")


class SynthesisResult(BaseModel):
    """Synthesizer の出力（本文 + 検証で見つかった問題）"""

    synthesis: SurveySynthesis
    issues: List[SynthesisIssue] = Field(default_factory=list)
    raw_claim_count: int = Field(default=0, description="LLMが生成した生claim総数")
    accepted_claim_count: int = Field(default=0, description="検証を通過したclaim数")

    @property
    def total_issues(self) -> int:
        return len(self.issues)

    @property
    def has_unsupported_claims(self) -> bool:
        """根拠不備（ID欠落/幻覚/棄却）が1件でもあるか"""
        return any(
            i.issue_type in ("missing_paper_ids", "phantom_paper_id", "claim_dropped")
            for i in self.issues
        )


# ===== Verifier（grounding 意味的検証）の型 =====
# 主張が紐付け論文の要旨と整合するかの判定値
VerificationStatus = Literal[
    "SUPPORTED",            # 論文が主張を裏付ける
    "PARTIALLY_SUPPORTED",  # 一部のみ裏付け
    "NOT_SUPPORTED",        # 論文が言っていない（裏取り不可）
    "CONTRADICTED",         # 論文と矛盾
    "UNVERIFIED",           # 検証不能（LLM失敗/不正/上限超過/論文欠落）
]


class GroundingVerdict(BaseModel):
    """LLM が返す (claim, 論文) 1組の判定（生出力）"""

    status: VerificationStatus = Field(description="裏付け判定")
    reason: str = Field(default="", description="判定理由（論文要旨に基づく）")


class PaperVerdict(BaseModel):
    """1つの根拠論文に対する claim の検証結果"""

    paper_id: str
    status: VerificationStatus
    reason: str = Field(default="")


class ClaimVerification(BaseModel):
    """1 claim の検証結果（複数論文の判定を集約）。claim は消さずフラグで残す。"""

    aspect_name: str
    statement: str = Field(description="検証対象の主張文（日本語）")
    paper_ids: List[str] = Field(default_factory=list)
    quote: Optional[str] = None
    status: VerificationStatus = Field(description="claim 全体の集約ステータス")
    paper_verdicts: List[PaperVerdict] = Field(
        default_factory=list, description="根拠論文ごとの判定（トレース用）"
    )


class VerificationSummary(BaseModel):
    """検証サマリ（レポートの信頼度指標）"""

    total_claims: int = 0
    counts: Dict[str, int] = Field(
        default_factory=dict, description="ステータス別 claim 件数"
    )
    supported_ratio: float = Field(
        default=0.0, description="SUPPORTED の割合（全 claim 中）"
    )


class VerificationReport(BaseModel):
    """Verifier の最終出力"""

    theme: str
    claim_verifications: List[ClaimVerification] = Field(default_factory=list)
    summary: VerificationSummary
    llm_calls: int = Field(default=0, description="実際に行った LLM 照合回数（コスト可視化）")
    cache_hits: int = Field(default=0, description="重複照合をキャッシュで回避した回数")
    cap_reached: bool = Field(
        default=False, description="照合上限に達して未検証が残ったか"
    )


# ===== オーケストレーション（全体パイプライン）の型 =====
SurveyStatus = Literal[
    "completed",            # 全観点カバー＋全段階完走
    "incomplete_coverage",  # 完走したが一部観点が不足のまま（正直に明示）
    "failed",               # 収集全滅などで途中停止（部分結果は保持）
]


class SurveyResult(BaseModel):
    """
    パイプライン全体の最終結果

    各段階の中間結果を全て保持し、後段のレポート整形/UI から使えるようにする。
    どこかで失敗しても破綻させず、status と notes で状態を可視化する。
    """

    theme: str
    status: SurveyStatus
    plan: Optional[SurveyPlan] = Field(default=None, description="Planner の観点分解")
    collection: Optional[CollectionResult] = Field(
        default=None, description="Collector/Evaluator 後の最終収集（マージ済み）"
    )
    evaluation: Optional[EvaluationLoopResult] = Field(
        default=None, description="自己批評ループの結果（各ラウンド評価含む）"
    )
    synthesis: Optional[SynthesisResult] = Field(
        default=None, description="日本語統合（claim + 根拠ID検証）"
    )
    verification: Optional[VerificationReport] = Field(
        default=None, description="grounding 意味的検証の結果"
    )
    notes: List[str] = Field(
        default_factory=list, description="段階ごとの診断メモ（不足観点/失敗など）"
    )
