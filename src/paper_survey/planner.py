"""
Planner - 研究テーマをサーベイの観点に分解する

日本語のテーマを受け取り、テーマの広さに応じた適切な数（目安3〜5）の観点へ
分解する。各観点は「観点名（日本語）」「intent（何を知りたいか）」
「英語の検索クエリ（Searcher 用、複数可）」を持つ。日本語→英語クエリ変換は
LLM（Gemini）が担う。

設計方針:
- LLM 呼び出しは **注入可能**（`llm` 引数）。テストではモックを渡し、実APIは叩かない。
- LLM 出力をそのまま信用せず、`_normalize_and_validate` で
  空観点 / 重複観点 / 検索クエリ欠落を検出し、除去・フォールバックする。
- 観点数や1観点あたりクエリ数の上限は `SurveyConfig` で実行時に絞れる。
"""

from __future__ import annotations

import logging
import os
from typing import Callable, List, Optional, Protocol, Union

from src.paper_survey.schemas import SearchAspect, SurveyConfig, SurveyPlan

logger = logging.getLogger(__name__)


# 注入する LLM の型。theme と観点数の目安を受け取り SurveyPlan を返す。
# 戻り値は SurveyPlan でも dict でもよい（_normalize_and_validate が吸収する）。
class PlannerLLM(Protocol):
    def __call__(
        self, theme: str, *, min_aspects: int, max_aspects: int
    ) -> Union[SurveyPlan, dict]:
        ...


# === プロンプト（実LLM用。テストでは使わない）===
PLANNER_SYSTEM = """あなたは学術サーベイの設計者です。
与えられた研究テーマを、論文サーベイとして体系的に調べるための「観点」に分解してください。

【方針】
1. 観点数は固定せず、テーマの広さに応じて適切な数（目安 {min_aspects}〜{max_aspects} 個）を自分で判断する
2. 各観点は互いに重複せず、全体でテーマを多角的にカバーすること
3. 各観点に以下を付ける:
   - name: 観点名（日本語、簡潔に）
   - intent: その観点で何を明らかにしたいか（日本語）
   - search_queries: 英語論文を探すための英語検索クエリ（1〜3個）。
     日本語テーマを適切な英語の専門用語に翻訳・言い換えること。
4. 検索クエリは必ず英語で、具体的な技術用語を含めること

【出力】
SurveyPlan の構造（theme / aspects / reasoning）で出力してください。"""


def build_gemini_planner_llm(
    model: str = "gemini-2.5-flash-lite",
    api_key: Optional[str] = None,
    temperature: float = 0.3,
) -> PlannerLLM:
    """
    実 Gemini を使う PlannerLLM を組み立てる（呼ばない限り API は叩かない）。

    アプリ配線時にこれを `run_planner(llm=...)` へ渡す。テストでは使用しない。
    """
    # import はここで行い、モジュール読み込み時点では google ライブラリに依存しない
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_google_genai import ChatGoogleGenerativeAI

    chat = ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key or os.getenv("GOOGLE_API_KEY"),
        temperature=temperature,
    )
    structured = chat.with_structured_output(SurveyPlan)
    prompt = ChatPromptTemplate.from_messages(
        [("system", PLANNER_SYSTEM), ("human", "{theme}")]
    )
    chain = prompt | structured

    def _call(theme: str, *, min_aspects: int, max_aspects: int) -> SurveyPlan:
        return chain.invoke(
            {"theme": theme, "min_aspects": min_aspects, "max_aspects": max_aspects}
        )

    return _call


# === 防御的バリデーション / 正規化 ===
def _clean_queries(queries: Optional[List[str]], max_queries: int) -> List[str]:
    """検索クエリを正規化: 空白畳み・空文字除去・重複除去・上限切り詰め"""
    seen = set()
    cleaned: List[str] = []
    for q in queries or []:
        normalized = " ".join((q or "").split())
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(normalized)
        if len(cleaned) >= max_queries:
            break
    return cleaned


def _fallback_aspect(theme: str) -> SearchAspect:
    """有効な観点が1つも残らなかったときの最終フォールバック"""
    safe_theme = theme.strip()
    return SearchAspect(
        name="全体概観",
        intent=f"「{safe_theme}」全体を概観する",
        # 翻訳できないため最終手段としてテーマ文字列をそのままクエリにする
        search_queries=[safe_theme or "survey"],
    )


def _normalize_and_validate(
    theme: str,
    raw_plan: Union[SurveyPlan, dict],
    config: SurveyConfig,
) -> SurveyPlan:
    """
    LLM 出力を検証・正規化する。

    - dict が来たら SurveyPlan に変換
    - 各観点: 名前/intent が空、または有効クエリが0 → 除去
    - 観点名の重複（大文字小文字・前後空白を無視）→ 先勝ちで除去
    - 観点数が max_aspects を超えたら切り詰め
    - 有効観点が0になったら theme からフォールバック観点を1つ生成
    """
    if isinstance(raw_plan, dict):
        raw_plan = SurveyPlan(**raw_plan)

    valid_aspects: List[SearchAspect] = []
    seen_names = set()

    for aspect in raw_plan.aspects:
        name = (aspect.name or "").strip()
        intent = (aspect.intent or "").strip()
        queries = _clean_queries(aspect.search_queries, config.max_queries_per_aspect)

        if not name or not intent or not queries:
            logger.warning(
                "invalid aspect dropped (name=%r, intent_empty=%s, queries=%d)",
                name,
                not intent,
                len(queries),
            )
            continue

        dedup_key = name.lower()
        if dedup_key in seen_names:
            logger.warning("duplicate aspect dropped: %r", name)
            continue
        seen_names.add(dedup_key)

        valid_aspects.append(
            SearchAspect(name=name, intent=intent, search_queries=queries)
        )

        if len(valid_aspects) >= config.max_aspects:
            logger.debug("max_aspects=%d reached; truncating", config.max_aspects)
            break

    if not valid_aspects:
        logger.warning("no valid aspects after validation; using theme fallback")
        valid_aspects = [_fallback_aspect(theme)]

    if len(valid_aspects) < config.min_aspects:
        # 不足はフォールバックで埋めず、警告のみ（観点を捏造しない方が安全）
        logger.info(
            "aspect count %d below min_aspects %d (kept as-is)",
            len(valid_aspects),
            config.min_aspects,
        )

    return SurveyPlan(theme=theme, aspects=valid_aspects, reasoning=raw_plan.reasoning)


# === メインエントリ ===
def run_planner(
    theme: str,
    *,
    llm: Optional[PlannerLLM] = None,
    config: Optional[SurveyConfig] = None,
) -> SurveyPlan:
    """
    Planner を実行する。

    Args:
        theme: 研究テーマ（日本語可）
        llm: 注入する PlannerLLM。テストではモックを渡す。
            None の場合は **意図的に例外**（実 API を誤って叩かないため）。
            本番では `build_gemini_planner_llm()` の戻り値を渡すこと。
        config: 実行時設定（観点数・クエリ数の上限など）

    Returns:
        SurveyPlan: 検証・正規化済みのサーベイ計画
    """
    if not theme or not theme.strip():
        raise ValueError("theme が空です")
    if llm is None:
        raise ValueError(
            "llm が未指定です。テストではモック、本番では "
            "build_gemini_planner_llm() の戻り値を渡してください"
            "（誤って実APIを叩かないため自動生成しません）"
        )

    config = config or SurveyConfig()

    raw_plan = llm(
        theme, min_aspects=config.min_aspects, max_aspects=config.max_aspects
    )
    return _normalize_and_validate(theme, raw_plan, config)
