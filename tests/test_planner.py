"""
Planner のユニットテスト（実APIは一切叩かない）

LLM は注入したモック（`make_llm`）で差し替える。検証対象:
- 正常系: 観点と英語クエリが保持される
- 日本語→英語クエリ変換は LLM の責務 → モックの英語クエリが通過することを確認
- 防御: 空観点 / 重複観点 / クエリ欠落の除去とフォールバック
- 実行時設定: max_aspects 切り詰め / max_queries_per_aspect 切り詰め
"""

import pytest

from src.paper_survey import planner
from src.paper_survey.schemas import (
    SearchAspect,
    SurveyConfig,
    SurveyPlan,
)


# ===== モックLLMヘルパー =====
def make_llm(plan, *, record=None):
    """
    与えた plan(SurveyPlan or dict) を返すだけのモック LLM を作る。
    record にリストを渡すと、呼び出し時の引数を記録する（実API未呼び出し確認用）。
    """

    def _llm(theme, *, min_aspects, max_aspects):
        if record is not None:
            record.append(
                {"theme": theme, "min_aspects": min_aspects, "max_aspects": max_aspects}
            )
        return plan

    return _llm


def _aspect(name, intent, queries):
    return SearchAspect(name=name, intent=intent, search_queries=queries)


# ===== 正常系 =====
def test_planner_happy_path_preserves_aspects_and_english_queries():
    plan = SurveyPlan(
        theme="大規模言語モデルの効率化",
        aspects=[
            _aspect("量子化", "重みを低ビット化して省メモリ化する手法を知る",
                    ["LLM quantization", "post-training quantization"]),
            _aspect("蒸留", "大モデルから小モデルへ知識を移す手法を知る",
                    ["knowledge distillation language model"]),
        ],
        reasoning="効率化を手法カテゴリで分割した",
    )
    record = []
    result = planner.run_planner(
        "大規模言語モデルの効率化", llm=make_llm(plan, record=record)
    )

    # LLM は1回だけ（実APIではなくモック経由）
    assert len(record) == 1
    assert record[0]["theme"] == "大規模言語モデルの効率化"

    assert isinstance(result, SurveyPlan)
    assert len(result.aspects) == 2
    # 観点名は日本語、クエリは英語のまま保持
    assert result.aspects[0].name == "量子化"
    assert result.aspects[0].search_queries == [
        "LLM quantization",
        "post-training quantization",
    ]
    # 英語クエリであることの簡易確認（ASCII）
    for asp in result.aspects:
        for q in asp.search_queries:
            assert q.isascii()


def test_planner_accepts_dict_plan_from_llm():
    """LLM が dict を返しても SurveyPlan に正規化される"""
    plan_dict = {
        "theme": "拡散モデル",
        "aspects": [
            {"name": "サンプリング高速化", "intent": "生成を速くする",
             "search_queries": ["diffusion model fast sampling"]},
        ],
        "reasoning": "r",
    }
    result = planner.run_planner("拡散モデル", llm=make_llm(plan_dict))
    assert isinstance(result, SurveyPlan)
    assert result.aspects[0].name == "サンプリング高速化"


# ===== 防御: クエリ欠落 =====
def test_aspect_with_no_queries_is_dropped():
    plan = SurveyPlan(
        theme="t",
        aspects=[
            _aspect("有効", "i", ["valid query"]),
            _aspect("クエリ無し", "i", []),  # 欠落 → 除去
            _aspect("空白のみ", "i", ["   ", ""]),  # 実質空 → 除去
        ],
    )
    result = planner.run_planner("t", llm=make_llm(plan))
    assert [a.name for a in result.aspects] == ["有効"]


# ===== 防御: 空の名前/intent =====
def test_aspect_with_empty_name_or_intent_is_dropped():
    plan = SurveyPlan(
        theme="t",
        aspects=[
            _aspect("", "intent", ["q1"]),       # 名前空
            _aspect("name", "   ", ["q2"]),       # intent空白
            _aspect("ok", "intent", ["q3"]),
        ],
    )
    result = planner.run_planner("t", llm=make_llm(plan))
    assert [a.name for a in result.aspects] == ["ok"]


# ===== 防御: 重複観点 =====
def test_duplicate_aspects_are_deduped_first_wins():
    plan = SurveyPlan(
        theme="t",
        aspects=[
            _aspect("量子化", "first", ["q-first"]),
            _aspect("  量子化 ", "second", ["q-second"]),  # 前後空白違い → 重複
            _aspect("ハードウェア", "third", ["q-third"]),
        ],
    )
    result = planner.run_planner("t", llm=make_llm(plan))
    names = [a.name for a in result.aspects]
    assert names == ["量子化", "ハードウェア"]
    # 先勝ち: 最初の intent/クエリが残る
    assert result.aspects[0].search_queries == ["q-first"]


# ===== 防御: 全観点が無効 → フォールバック =====
def test_all_invalid_aspects_triggers_fallback():
    plan = SurveyPlan(
        theme="量子コンピューティングの誤り訂正",
        aspects=[
            _aspect("", "", []),
            _aspect("x", "y", []),  # クエリ欠落
        ],
    )
    result = planner.run_planner(
        "量子コンピューティングの誤り訂正", llm=make_llm(plan)
    )
    assert len(result.aspects) == 1
    fb = result.aspects[0]
    assert fb.name == "全体概観"
    # 翻訳不能の最終手段としてテーマ文字列をクエリに使う
    assert fb.search_queries == ["量子コンピューティングの誤り訂正"]


def test_empty_aspect_list_triggers_fallback():
    plan = SurveyPlan(theme="トピック", aspects=[])
    result = planner.run_planner("トピック", llm=make_llm(plan))
    assert len(result.aspects) == 1
    assert result.aspects[0].name == "全体概観"


# ===== 実行時設定: 観点数の切り詰め =====
def test_max_aspects_truncation():
    aspects = [
        _aspect(f"観点{i}", f"intent{i}", [f"query {i}"]) for i in range(7)
    ]
    plan = SurveyPlan(theme="t", aspects=aspects)
    config = SurveyConfig(max_aspects=5, min_aspects=3)
    result = planner.run_planner("t", llm=make_llm(plan), config=config)
    assert len(result.aspects) == 5
    assert result.aspects[0].name == "観点0"
    assert result.aspects[-1].name == "観点4"


# ===== 実行時設定: クエリ数の切り詰め＋重複除去 =====
def test_max_queries_per_aspect_and_query_dedup():
    plan = SurveyPlan(
        theme="t",
        aspects=[
            _aspect(
                "a",
                "i",
                [
                    "Query One",
                    "query one",     # 大文字小文字違い → 重複除去
                    "  query two  ",  # 空白畳み
                    "query three",
                    "query four",     # 上限超過で切り捨て
                ],
            )
        ],
    )
    config = SurveyConfig(max_queries_per_aspect=3)
    result = planner.run_planner("t", llm=make_llm(plan), config=config)
    assert result.aspects[0].search_queries == [
        "Query One",
        "query two",
        "query three",
    ]


# ===== 実行時設定: light プリセットが API 消費を絞る値を持つ =====
def test_light_config_values():
    light = SurveyConfig.light()
    assert light.papers_per_aspect <= 2
    assert light.max_research_rounds == 0
    assert light.max_aspects <= 2
    assert light.max_queries_per_aspect == 1
    # min/max が LLM へヒントとして渡ることも確認
    record = []
    plan = SurveyPlan(theme="t", aspects=[_aspect("a", "i", ["q"])])
    planner.run_planner("t", llm=make_llm(plan, record=record), config=light)
    assert record[0]["min_aspects"] == light.min_aspects
    assert record[0]["max_aspects"] == light.max_aspects


# ===== 入力バリデーション =====
def test_empty_theme_raises():
    with pytest.raises(ValueError):
        planner.run_planner("   ", llm=make_llm(SurveyPlan(theme="t", aspects=[])))


def test_missing_llm_raises_to_avoid_real_api():
    """llm 未指定は実APIを誤って叩かないよう例外にする"""
    with pytest.raises(ValueError):
        planner.run_planner("テーマ")
