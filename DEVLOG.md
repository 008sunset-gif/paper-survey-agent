# DEVLOG — 論文サーベイエージェント

各ステップで「何が問題で、どう解決したか」を記録する。後で ES（直面した課題と解決）に流用する。

---

## ステップ1: Searcher 実装（arXiv / Semantic Scholar）

**日付:** 2026-06-11
**ゴール:** テーマから実在論文を集める Searcher を実装。実APIは叩かず、モック＋ユニットテストで「正しくパースできる」「リトライが動く」ことを検証する。

### 成果物
- `src/paper_survey/schemas.py` — ソース非依存の正規化済み論文型 `Paper`（title/authors/year/abstract/paper_id/url）。
- `src/paper_survey/searcher.py` — `search_arxiv` / `search_semantic_scholar` と、共通の `_request_with_retry`（指数バックオフ）。
- `tests/test_searcher.py` — 実APIをモックした6本のユニットテスト（全PASS）。
- `conftest.py` — pytest にプロジェクトルートを認識させ、名前空間パッケージ `src` を import 可能にする。
- `requirements-dev.txt` — テスト用 pytest を本番依存と分離。

### 直面した課題と解決

**課題1: 2つのAPIでスキーマがバラバラ。**
arXiv は Atom XML（`<entry>` の `id`/`title`/`summary`/`author/name`/`published`）、Semantic Scholar は JSON（`paperId`/`abstract`/`year`/`authors[].name`/`externalIds`/`url`）と、フィールド名も形式も別物。後段（Synthesizer/Verifier）が両者を区別なく扱えないと、主張と論文IDの紐付けが破綻する。
→ **解決:** ソース非依存の共通型 `Paper` を定義し、各 `_parse_*` 関数でそこへ詰め替えて正規化。`source` フィールドと `citation_key`（例 `arxiv:2310.06825v1`）で出典を一意に追跡できるようにした。

**課題2: arXiv の XML はタイトル/要旨に改行・インデントが混入する。**
Atom の `<title>`/`<summary>` は整形のため改行とスペースが入り、そのままだと "Mistral 7B" が "\n      Mistral 7B\n    " になる。
→ **解決:** `" ".join(text.split())` で連続空白を1つに畳んで正規化。テストで「連続空白が残っていない」ことも検証。

**課題3: arXiv の論文IDが URL 形式で返る。**
`id` が `http://arxiv.org/abs/2310.06825v1` のように来るため、そのままだと紐付けキーに使いづらい。
→ **解決:** `/abs/` で右分割して末尾の `2310.06825v1` を `paper_id` に採用。URL自体は `url` に保持。

**課題4: Semantic Scholar は無認証だと 429（レート制限）を返しやすい。**
単発GETだと実運用で頻繁に失敗する。
→ **解決:** `_request_with_retry` を実装。429 と 5xx、ネットワーク例外（`requests.exceptions.RequestException`）でリトライし、待機を `base_delay * 2**attempt`（1, 2, 4, 8…秒）の指数バックオフに。429以外の4xxは即エラー（リトライ無意味なため）。最終失敗時は `SearchError` に集約。

**課題5: 「実APIを叩かない」かつ「リトライを検証する」をどう両立するか。**
リトライのテストで `time.sleep` を本当に呼ぶとテストが何十秒も止まる。実 `requests.get` を呼べばルール違反。
→ **解決:** ① `_request_with_retry(sleeper=...)` で待機関数を注入可能にし、テストでは `sleeper=lambda d: sleeps.append(d)` を渡して実時間ゼロで「待機が何秒×何回呼ばれたか」を検証。② `unittest.mock.patch.object(searcher.requests, "get", ...)` で `requests.get` を差し替え、`side_effect` で「429×2 → 成功」「ConnectionError → 成功」「429×4 → SearchError」を再現。`call_count` で実APIに到達していないことも担保。

**課題6: `src/__init__.py` が無い構成で pytest が `import src.paper_survey` を解決できない。**
既存 deepdive は名前空間パッケージ運用（`src/__init__.py` 無し）で、pytest はテストファイルのあるディレクトリを優先 sys.path に入れるため、ルートが通らず import 失敗。
→ **解決:** プロジェクトルートに `conftest.py` を置き、pytest にルートを sys.path へ追加させて `src` を名前空間パッケージとして解決。既存コードには手を加えていない。

### テスト結果
```
6 passed in 0.19s
- test_search_arxiv_parses_entries          ... arXiv XML を正しくパース
- test_search_semantic_scholar_parses_items ... S2 JSON を正しくパース（abstract/year/url=null も防御）
- test_retry_succeeds_after_429             ... 429×2 後に成功、バックオフ [1.0, 2.0]
- test_retry_succeeds_after_network_error   ... ConnectionError 後に成功
- test_retry_exhausted_raises_search_error  ... 429×4 で SearchError、待機 [1.0, 2.0, 4.0]
- test_non_retryable_4xx_raises_immediately ... 400 は即エラー・リトライしない
```
すべて実APIを叩かずモックで検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**

### 次の候補（未着手）
Planner（観点分解）→ Searcher を複数クエリで並列実行する層。ただし指示どおり1ステップずつ進めるため、GO待ち。

---

## ステップ2: Planner 実装（観点分解 / 日本語→英語クエリ）

**日付:** 2026-06-11
**ゴール:** 日本語テーマを受け取り、テーマの広さに応じた数（目安3〜5）の観点へ分解する Planner を実装。各観点に「観点名(日本語)/intent/英語検索クエリ」を持たせる。LLM は注入可能にしてモックでテストし、実APIは叩かない。

### 成果物
- `src/paper_survey/schemas.py`（追記）— `SearchAspect` / `SurveyPlan` / 実行時設定 `SurveyConfig`。
- `src/paper_survey/planner.py` — `run_planner`（注入LLM）、`_normalize_and_validate`（防御）、`build_gemini_planner_llm`（本番用、テストでは未使用）。
- `tests/test_planner.py` — 12本のユニットテスト（全PASS、実API未呼び出し）。

### 直面した課題と解決

**課題1: LLM出力を信用するとサーベイ品質が崩れる（空観点・重複・クエリ欠落）。**
LLM は時々、名前だけで intent やクエリが空の観点、ほぼ同義の重複観点、検索クエリの無い観点を返す。これをそのまま Searcher に渡すと空検索や重複検索で品質・コストが悪化する。
→ **解決:** `_normalize_and_validate` を独立関数化し、LLM 出力に対して機械的に: ①名前/intent が空 or 有効クエリ0 の観点を除去、②観点名を「小文字化＋前後空白除去」で正規化して重複を先勝ち除去、③`max_aspects` 超過を切り詰め。各除去は `logger.warning` で可視化。検証ロジックを LLM から切り離したことで、モック出力を流すだけで全分岐をテストできる。

**課題2: 「観点が1つも残らなかったら」をどう扱うか（クラッシュ回避）。**
全観点が無効だと後段が空入力で落ちる。
→ **解決:** フォールバック観点 `全体概観` を theme から生成。ただし英語翻訳は LLM の仕事で、ここでは翻訳できないため、最終手段としてテーマ文字列をそのままクエリに使う（非クラッシュ優先）。観点不足（min未満）は**埋めずに警告のみ**——観点を捏造する方がサーベイの信頼性を損なうため、あえて足さない設計にした。

**課題3: 日本語→英語クエリ変換をどこで担保するか。**
英語論文を探すには英語クエリが要るが、変換は本質的に LLM の仕事でコードでは無理。
→ **解決:** 変換責務は LLM（プロンプトで「英語の専門用語へ翻訳」と明示）に置き、コード側は「英語クエリが欠落していないか」「空白/重複の正規化」を保証する役割分担に。テストではモックが英語クエリを返し、それが保持されること＋`q.isascii()` で英語であることを確認。

**課題4: 「実APIを叩かない」と「本番では Gemini を使う」を両立。**
`run_planner` が llm 未指定時に勝手に Gemini を生成すると、誤って実APIを叩く事故が起きうる。
→ **解決:** `run_planner(llm=...)` を**必須注入**にし、`llm=None` は親切なメッセージ付きで `ValueError`。本番用の `build_gemini_planner_llm()` は別関数として用意し、google ライブラリの import も関数内に閉じ込め（モジュール読み込み時に依存しない）。これでテストは純粋なモック、本番は明示配線、という線引きができた。

**課題5: 通し確認時の API 消費を、コード品質を落とさず絞りたい。**
→ **解決:** `SurveyConfig` に実行時パラメータを集約: `max_aspects` / `max_queries_per_aspect`（Planner が使用）、`papers_per_aspect` / `max_research_rounds`（後段の Searcher/Evaluator が参照）。`SurveyConfig.light()` プリセットで「観点2・クエリ1・論文2・再検索0」に一括縮小できる。本体ロジックは据え置きで、実行設定だけ軽量化できる構造にした。

### テスト結果
```
18 passed in 0.17s （Planner 12 + Searcher 6）

Planner:
- happy_path                         ... 観点保持＆英語クエリ(ASCII)保持
- accepts_dict_plan                  ... LLMがdictを返してもSurveyPlanへ正規化
- no_queries_is_dropped              ... クエリ欠落/空白のみ観点を除去
- empty_name_or_intent_is_dropped    ... 名前/intent 空を除去
- duplicate_aspects_deduped          ... 重複観点を先勝ちで除去
- all_invalid → fallback             ... 全無効でテーマからフォールバック生成
- empty_aspect_list → fallback       ... 観点ゼロでもフォールバック
- max_aspects_truncation             ... 7観点→上限5に切り詰め
- max_queries_per_aspect + dedup     ... クエリ大文字小文字重複除去＆上限3
- light_config_values                ... 軽量プリセット＆min/maxがLLMへ伝達
- empty_theme_raises                 ... 空テーマで ValueError
- missing_llm_raises                 ... llm未指定で ValueError（実API誤爆防止）
```
すべてモックで検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**

### 次の候補（未着手・GO待ち）
Searcher を複数クエリで並列実行する収集層、または Evaluator（網羅性の自己評価→再検索）。1ステップずつ進める。

---

## ステップ3: 並列収集層（Collector）実装

**日付:** 2026-06-11
**ゴール:** Planner の複数観点×複数クエリ（英語）を受け取り、Searcher を並列実行して観点ごとに正規化済み Paper を集める層を実装。診断で指摘された「逐次 for ループ」を解消する。完成度＝堅牢性（部分失敗の救済）を最優先。

### 成果物
- `src/paper_survey/schemas.py`（追記）— `CollectionFailure` / `CollectedPaper` / `CollectionResult`、`SurveyConfig.max_concurrent_requests`。
- `src/paper_survey/collector.py` — `collect_papers`（ThreadPoolExecutor で並列収集）、`CollectionError`。
- `tests/test_collector.py` — 8本のユニットテスト（全PASS、実API未呼び出し）。

### 直面した課題と解決

**課題1: async と スレッドプール、どちらで並列化するか。**
Searcher は `requests` ベースのブロッキング I/O。async 化するには Searcher 自体を `httpx.AsyncClient` 等へ書き換える必要があり、既存実装との二重管理になる。
→ **解決:** `concurrent.futures.ThreadPoolExecutor` を採用。ブロッキング I/O はスレッドで十分に並列化でき、Searcher を一切変更せずに注入できる。`max_workers = min(max_concurrent_requests, タスク数)` でスレッドの作り過ぎも防止。

**課題2【最重要】: 一部の検索が失敗しても全体を止めない（部分失敗の救済）。**
タイムアウト/429/例外が1つでも起きると `executor.map` 等では全体が巻き込まれて落ちうる。サーベイは「集まった分だけでも価値がある」ので全停止は最悪。
→ **解決:** タスクごとに `future.result()` を try/except で個別に捕捉。失敗は `CollectionFailure(aspect_name, query, source, error)` に記録して `failures` で可視化し、成功分はそのまま結果に含める。**全タスクが失敗したときのみ** `CollectionError` を送出（成功0件＝全滅の定義）。成功はあるが0件ヒット、は正常終了とする。

**課題3: 並列実行を「確定的に」テストする（タイミング依存を避ける）。**
`time.sleep` で重なりを期待するテストは不安定（flaky）になりがち。
→ **解決:** `threading.Barrier(タスク数)` をモック検索内で `wait(timeout=5)` させる。全タスクが同時に到達しないと Barrier を通過できないため、**逐次実行ならタイムアウト→失敗扱い**になる。よって「失敗ゼロで全件返る」ことが、真に並列で動いた確定的な証拠になる。同時数の上限テストは `max_concurrent_requests=1` で `ConcurrencyTracker.max_active == 1` を確認。

**課題4: 観点をまたいだ重複論文の統合と、出典追跡の両立。**
同じ論文が複数観点で見つかるのは普通。単純結合だと重複し、逆に消すと「どの観点で出たか」を失う。
→ **解決:** `paper_id` をキーに `OrderedDict` で統合し、`CollectedPaper.found_in_aspects` に観点名を蓄積（重複なし・順序保持）。`by_aspect` には観点別の採用 paper_id を別途保持。タスクを**元の観点順・クエリ順で決定的に**後処理することで、`as_completed` の非決定性に依らず出力順を安定させた。

**課題5: API 消費の制御（過負荷/レート制限回避）。**
→ **解決:** `max_concurrent_requests` で同時リクエスト数を、`papers_per_aspect` で観点あたり取得数（`max_results` として各検索へ伝播＋観点内で上限切り詰め）、`max_queries_per_aspect` で実行クエリ数を制御。`SurveyConfig.light()` にも `max_concurrent_requests=2` を追加し、通し確認を軽量化できる。

### テスト結果
```
26 passed in 0.27s （Collector 8 + Planner 12 + Searcher 6）

Collector:
- searches_run_in_parallel              ... Barrierで真の並列を確定的に証明
- max_concurrent_requests_serializes    ... 上限1で同時実行が1を超えない
- partial_failure_returns_successes...   ... 一部失敗でも成功分を返し失敗を記録
- dedup_across_aspects_keeps_origin...   ... 同一paper_id統合＋found_in_aspects保持
- all_failure_raises_collection_error    ... 全滅で CollectionError
- papers_per_aspect_limit_truncates      ... 観点あたり取得上限で切り詰め＋max_results伝播
- max_queries_per_aspect_respected       ... 実行クエリ数の上限を尊重
- empty_plan_returns_empty_result        ... 空プランは非エラーで空結果
```
すべてモックで検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**

### 次の候補（未着手・GO待ち）
Evaluator（網羅性の自己評価→未カバーなら再検索ループ。`max_research_rounds` を消費）、または Planner→Collector を繋ぐオーケストレーション。1ステップずつ進める。

---

## ステップ4: Evaluator（自己批評ループ）実装

**日付:** 2026-06-11
**ゴール:** Collector が集めた論文が観点を十分カバーしているかを自己評価し、不足があれば「どの観点を・どんな新クエリで」再検索すべきか出力する層。診断の「看板倒れ（直線グラフ）」を解消する自己批評ループの心臓部。設計の核心なので特に丁寧に。

### 成果物
- `src/paper_survey/schemas.py`（追記）— `QualitativeVerdict` / `AspectAssessment` / `ReSearchInstruction` / `EvaluationResult` / `EvaluationLoopResult`、`SurveyConfig.min_papers_per_aspect`。
- `src/paper_survey/evaluator.py` — `evaluate_round`（単一ラウンド評価）、`run_evaluation_loop`（ループ駆動＋累積マージ）。
- `tests/test_evaluator.py` — 11本のユニットテスト（全PASS、実API未呼び出し）。

### 直面した課題と解決

**課題1: 網羅性をどう判定するか（機械だけでも、LLMだけでも不十分）。**
論文数だけ見ると「多いが的外れ」を見逃す。LLM だけだと不安定・コスト高、かつゼロ件を高価に判定するのは無駄。
→ **解決:** 二段構え。①機械的チェックで `min_papers_per_aspect` 未満を確実・無料に弾く。②機械チェックを通った観点だけ LLM に「intent に答えているか」を問う。統合規則は「機械NG→不足」「機械OK＋質的NG→不足」「機械OK＋(質的OK or LLM無/失敗)→カバー」。論文ゼロの観点に LLM を呼ばないことで無駄コストも回避。

**課題2【完成度の肝】: LLM評価が失敗/不正でもパイプラインを止めない。**
LLM はタイムアウトや構造不正（必須フィールド欠落）を起こす。
→ **解決:** `_coerce_verdict` で dict→`QualitativeVerdict` 変換（不正なら pydantic が例外）。`evaluate_round` 内で観点ごとに try/except し、失敗時は `qualitative_ok=None`＝**機械チェックのみで継続**。アセスメントに「機械チェックのみ」と理由を残して可視化。テストで「LLM例外」「必須欠落dict」両方のフォールバックを確認。

**課題3: 再検索が前回と同じクエリの単純反復になり、ループが無駄に空回りする。**
同じクエリを再投入しても同じ結果しか返らない。
→ **解決:** `_build_new_queries` で前回クエリ集合（`previous_queries`、大文字小文字無視）を除外。優先順位は「①LLMの提案クエリ（使えるものがあれば水増しせず採用）→②皆無なら元クエリ＋差別化サフィックス（survey/recent advances/benchmark…）で合成」。ループ側は各ラウンドで使ったクエリを `previous_queries` に蓄積し、次ラウンドの重複を防ぐ。
（実装時のバグ: 当初②で max まで水増しし、LLM良提案に低品質な合成クエリが混入。テストで検出し「LLM提案があれば②に進まない」へ修正。）

**課題4【無限ループ防止】: 規定回数で必ず止め、不足を正直に出す。**
自己批評ループは放置すると永久に回りうる。隠して「完了」と偽るのは最悪。
→ **解決:** `run_evaluation_loop` は `while not complete and research_round < max_research_rounds` で必ず打ち切り。打ち切り時は `completed=False`・`unmet_aspects` に**不足観点を明示**。実行可能な再検索指示が無くなった場合も break。`max_research_rounds=0` なら初回評価だけで再検索なし。

**課題5: ラウンドをまたいだ収集結果の累積をどう管理するか。**
再検索は不足観点だけを対象にした部分 plan で回すので、結果を毎回マージして「全体像」で再評価する必要がある。
→ **解決:** `_Accumulator` を導入し、`paper_id` でグローバル統合（`found_in_aspects` 保持）＋観点別 `by_aspect` を累積。各ラウンドは累積後の `CollectionResult` で再評価。Collector の出力構造をそのまま受け取り・再生成できるので層の責務が綺麗に分離。

### テスト結果
```
37 passed in 0.24s （Evaluator 11 + Collector 8 + Planner 12 + Searcher 6）

Evaluator:
- mechanical_detects_low_count           ... 論文ゼロ/極少を機械検出・再検索指示
- qualitative_marks_insufficient         ... 数は足りるがintent未達→提案クエリで再検索
- all_covered_completes                  ... 全カバーで is_complete
- llm_failure_falls_back_to_mechanical   ... LLM例外でも機械チェックで継続
- llm_invalid_output_falls_back          ... 必須欠落dictでもフォールバック
- new_queries_avoid_repetition           ... 前回クエリの単純反復を回避
- new_queries_uses_llm_suggestions...    ... LLM提案を使い前回分のみ除外
- loop_terminates_immediately            ... 初回全カバーで再検索ゼロ
- loop_research_then_completes           ... 1回再検索で充足・不足観点のみ再検索
- loop_stops_at_max_rounds_reports_unmet ... 規定回数で打ち切り＋不足を明示
- loop_max_rounds_zero_does_no_research  ... 0回設定で再検索なし
```
すべてモックで検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**

### 次の候補（未着手・GO待ち）
Synthesizer（英語論文→日本語サーベイ統合、各主張に論文ID紐付け）、Verifier（主張を要旨と照合）、または Planner→Collector→Evaluator を繋ぐ全体オーケストレーション。1ステップずつ進める。

---

## ステップ5: Synthesizer（日本語統合）実装

**日付:** 2026-06-11
**ゴール:** 集めた英語論文を観点ごとに日本語サーベイへ統合。最重要要件は「各主張(claim)に根拠論文IDを必ず紐付ける」こと（後段 Verifier の矛盾照合の土台）。LLM のID紐付けサボり/幻覚IDを機械的に弾く。

### 成果物
- `src/paper_survey/schemas.py`（追記）— `RawClaim`/`AspectDraft`（LLM生出力・緩い型）、`Claim`/`AspectSynthesis`/`SurveySynthesis`（検証済み）、`SynthesisIssue`/`SynthesisResult`。
- `src/paper_survey/synthesizer.py` — `synthesize`（観点別生成＋ID機械検証）。
- `tests/test_synthesizer.py` — 9本のユニットテスト（全PASS、実API未呼び出し）。

### 直面した課題と解決

**課題1【最重要】: 「根拠IDの無い主張」をどう"許さない"か（文書ルールでは破られる）。**
プロンプトに「IDを付けろ」と書くだけでは LLM は守らないことがある。
→ **解決:** 型を二層に分離。LLM が返す生データは `RawClaim`（`paper_ids` 空を許す緩い型）で受け、検証を通った最終主張だけ `Claim`（`paper_ids: Field(min_length=1)`／`statement: Field(min_length=1)`）に変換。これで**「IDの無い Claim は型として存在できない」**という構造的保証になり、同時に検証段で `missing_paper_ids` として検出もできる。テストで `Claim(paper_ids=[])` が `ValidationError` になることも確認。

**課題2: LLM が存在しない論文ID（幻覚）を参照する。**
翻訳・要約の過程で、提示していない paper_id を創作することがある。
→ **解決:** 収集済み `CollectionResult` から実在 paper_id の**全体集合 `known_ids`** を作り、各 claim の ID を `valid_ids`（実在）と `phantom_ids`（幻覚）に分離。幻覚は `phantom_paper_id` として記録しつつ ID から除去。有効IDが1つも残らなければ claim ごと `claim_dropped` で棄却。幻覚チェックを「観点別ではなく収集全体」基準にしたのは、観点横断で見つかった論文を別観点の主張が引用するのは正当だから（テスト `test_phantom_check_uses_global_collection` で担保）。

**課題3: 日本語で書きつつ英語原典へ戻れるようにする。**
→ **解決:** claim 本文は日本語、根拠は英語原論文の `paper_id`（Searcher が正規化済み）で保持。`Claim.quote` に該当箇所も任意で残せる。翻訳後も `paper_ids` から `CollectionResult` の `Paper`（title/url/abstract）へ一意に戻れる。

**課題4: 1観点の生成失敗で全体を落とさない／問題を隠さない。**
→ **解決:** 観点単位で try/except し、失敗観点は `llm_failed` を記録して空 claim で継続。検証で見つかった全問題は `SynthesisResult.issues` に集約し、`has_unsupported_claims` で根拠不備の有無を一目で出せる（Verifier 前段の機械チェックとして機能）。`raw_claim_count` / `accepted_claim_count` で「何件中何件が根拠検証を通ったか」も可視化。

### テスト結果
```
46 passed in 0.29s （Synthesizer 9 + Evaluator 11 + Collector 8 + Planner 12 + Searcher 6）

Synthesizer:
- generates_claims_with_ids            ... 観点ごとにclaim生成・ID紐付け・引用保持
- missing_paper_ids_flagged_and_dropped... ID無し/空白IDのみのclaimを検出・棄却
- phantom_paper_id_detected            ... 一部幻覚は有効IDで残す/全部幻覚は棄却
- phantom_check_uses_global_collection ... 幻覚判定は収集全体基準（観点横断引用OK）
- claim_type_forbids_empty_paper_ids   ... Claim型が空ID/空statementを構造的に拒否
- empty_statement_flagged              ... 空主張文を検出
- llm_failure_isolated_per_aspect      ... 観点単位で失敗隔離・他観点は継続
- accepts_dict_draft                   ... LLMがdictを返してもAspectDraftへ正規化
- missing_llm_raises                   ... llm未指定はValueError（実API誤爆防止）
```
すべてモックで検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**

### 次の候補（未着手・GO待ち）
Verifier（各 claim を根拠論文の abstract と LLM で矛盾照合しフラグ）、または Planner→Collector→Evaluator→Synthesizer→Verifier を繋ぐ全体オーケストレーション。1ステップずつ進める。

---

## ステップ6: Verifier（レベル3 grounding 検証）実装

**日付:** 2026-06-11
**ゴール:** 各 claim（日本語主張＋根拠論文ID）について「主張が紐付け論文の abstract と整合するか」を LLM で照合しフラグする。Synthesizer の構造的裏取り（IDが実在するか）に対し、Verifier は意味的裏取り（内容が矛盾しないか）を担う二段目。「LLMを信じない設計」の最終形＝本ツールの核心。

### 成果物
- `src/paper_survey/schemas.py`（追記）— `VerificationStatus`/`GroundingVerdict`/`PaperVerdict`/`ClaimVerification`/`VerificationSummary`/`VerificationReport`、`SurveyConfig.max_verifications`。
- `src/paper_survey/verifier.py` — `verify`（claim×論文の照合・集約・キャッシュ・上限）。
- `tests/test_verifier.py` — 12本のユニットテスト（全PASS、実API未呼び出し）。

### 直面した課題と解決

**課題1【核心】: 裏取りできなかった主張を"消す"べきか"残す"べきか。**
普通の品質管理なら不確実な主張は削除する。だが研究用途では「裏取りできなかった/矛盾した」という事実そのものが価値ある情報で、隠すのは不誠実。
→ **解決:** **削除せずフラグで残す**設計に。全 claim を `ClaimVerification`（`status` 付き）としてレポートに保持し、NOT_SUPPORTED/CONTRADICTED でも消さない。最終レポートで「検証ステータス付き」で可視化できる。テスト `test_not_supported_is_kept_not_deleted` で「消えない」ことを明示的に担保。

**課題2: 1 claim が複数論文を引用するとき、claim 全体の判定をどう決めるか。**
論文Aは支持・論文Bは矛盾、のような分裂が起きる。
→ **解決:** 集約優先順位を `CONTRADICTED > SUPPORTED > PARTIALLY_SUPPORTED > NOT_SUPPORTED > UNVERIFIED` とし、**矛盾を最優先で表面化**（1論文でも矛盾すれば claim 全体を CONTRADICTED に）。「LLMを信じない＝危険信号を埋もれさせない」方針の具現化。論文ごとの判定は `paper_verdicts` に全て残してトレース可能にした。

**課題3: LLM 評価が失敗/不正でも止めない。**
ファクトチェック LLM もタイムアウトや不正 status を返す。
→ **解決:** (claim,論文) ごとに try/except し、例外も不正 status（`GroundingVerdict.status` は Literal なので許可値以外は pydantic が弾く）も **UNVERIFIED（検証不能）** に倒す。「検証できなかった」も正直なステータスとして残す。テストで「LLM例外」「status=MAYBE の不正dict」両方を確認。

**課題4: API コストの制御（同一照合の重複・件数爆発）。**
claim×論文の総当たりは件数が膨らみ、同じ (主張, 論文) を別観点で再照合する無駄も生じる。
→ **解決:** `(claim文, 論文ID)` をキーにキャッシュし重複照合を回避（`cache_hits` で可視化）。さらに `SurveyConfig.max_verifications` で LLM 照合回数に上限を設け、超過分は UNVERIFIED（理由に「照合上限」明記）として `cap_reached=True` を立てる。実 LLM 呼び出し回数 `llm_calls` も出してコストを見える化。

**課題5: レポートの信頼度をどう示すか。**
→ **解決:** `VerificationSummary` でステータス別件数と `supported_ratio`（全 claim 中 SUPPORTED の割合）を集計。これがレポート全体の"裏取り率"＝信頼度指標になる。

### テスト結果
```
58 passed in 0.31s （Verifier 12 + Synthesizer 9 + Evaluator 11 + Collector 8 + Planner 12 + Searcher 6）

Verifier:
- supported / not_supported_is_kept / contradicted ... 各判定、NOT_SUPPORTEDも消さず残す
- aggregation_contradicted_takes_precedence        ... 1論文矛盾で claim 全体を矛盾に
- aggregation_any_supported_over_unverified        ... UNVERIFIEDよりSUPPORTED優先
- llm_failure_is_unverified / invalid_status_...   ... 失敗・不正statusはUNVERIFIED
- missing_paper_is_unverified                      ... 論文欠落時はLLMを呼ばずUNVERIFIED
- duplicate_pairs_use_cache                        ... 同一(主張,論文)はキャッシュで1回
- max_verifications_cap                            ... 上限で打ち切り・超過はUNVERIFIED
- summary_counts_mixed                             ... ステータス集計・supported_ratio
- missing_llm_raises                               ... llm未指定はValueError
```
すべてモックで検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**

### 5層パイプライン部品が出揃った
Planner → Collector → Evaluator → Synthesizer → Verifier の各層が、全てモック注入でテスト可能・実API非依存の形で完成。次は **全体オーケストレーション**（5層を `SurveyConfig` 一つで繋ぎ、LLM/検索を1箇所で注入）か、最終レポート整形。GO待ち。

---

## ステップ7: 全体オーケストレーション（6部品の配線）実装

**日付:** 2026-06-11
**ゴール:** Planner → Collector → Evaluator(自己批評ループ) → Synthesizer → Verifier を1本に繋ぎ、テーマ→検証済み日本語サーベイの `SurveyResult` まで通す。全部品モックで通せる配線。

### 成果物
- `src/paper_survey/orchestrator.py` — `run_survey`（配線本体）、`SurveyDependencies`（注入束）、`build_gemini_dependencies`（本番配線・GO後）。
- `src/paper_survey/schemas.py`（追記）— `SurveyStatus` / `SurveyResult`（全中間結果を保持）。
- `tests/test_orchestrator.py` — 6本の統合テスト（全PASS、実API未呼び出し）。

### 方式の判断: LangGraph か関数合成か → **関数合成**
理由（指示によりDEVLOGに明記）:
1. パイプラインは本質的に線形で、唯一の分岐＝再検索ループは既に `run_evaluation_loop` 内に終了条件付きで実装済み。状態機械を別途グラフで組む必要がない。
2. 本タスクの肝は「全部品の LLM/検索を注入してモックで丸ごと通す」こと。関数合成の方が依存注入とテスト境界が素直。LangGraph の state/node に包むと注入点が増え検証が複雑化する。
3. 既存 deepdive の LangGraph 実装とは別モジュールとして独立させる方針なので、ここで LangGraph に依存しない方が結合度が下がる。
（将来、可視化・チェックポイント・人手介入が必要になれば LangGraph 化を再検討する余地は残す。）

### 配線して初めて出た統合課題（重視）

**統合課題1: 自己批評ループに渡す collect_fn の責務境界。**
Evaluator の `run_evaluation_loop` は「初回収集」も「再検索」も `collect_fn(subplan, round)` 経由で行う設計。オーケストレータがこれを満たす必要があった。
→ **解決:** `collect_fn` を `collect_papers(subplan, config, searchers=deps.searchers)` の薄いラッパとして1箇所に閉じ込め、Collector への依存（searchers）をループの外で注入。初回も再検索も同じ経路を通るので、ループが「配線レベルで」正しく回ることをテスト `test_self_critique_loop_research_then_completes` で確認（初回 qb で空→再検索の新クエリで充足→completed）。単体では各層を個別に検証していたが、「ループが初回収集も兼ねる」点は結合して初めて噛み合いを確認できた。

**統合課題2: Synthesizer 出力 → Verifier 入力の型の段差。**
Synthesizer が返すのは `SynthesisResult`（本文 + issues のラッパ）だが、Verifier が食うのは中身の `SurveySynthesis`。そのまま渡すと型が合わない。
→ **解決:** オーケストレータで `verify(synthesis.synthesis, ...)` と一段アンラップして接続。さらに「Synthesizer が採用した claim 数 == Verifier が検証した claim 数」を統合テストで突き合わせ、段差が無いことを保証（`test_full_pipeline_runs_to_verification`）。これは単体テストでは見えない受け渡しバグの典型。

**統合課題3: 途中失敗時に「破綻させない」境界をどこに引くか。**
部品内の部分失敗（収集の一部失敗・合成/検証のLLM失敗）は各層が既に救済済み。だが収集"全滅"（`CollectionError`）はループから例外で上がってくる。
→ **解決:** オーケストレータで `CollectionError` だけを捕捉し、`status="failed"` + plan 保持 + notes で理由を残して正常 return（クラッシュさせない）。一方、合成/検証の段階失敗は層内で `llm_failed`/`UNVERIFIED` に倒れるので、パイプラインは最後まで到達し `status="completed"` のまま問題を可視化。テストで「全滅→failed」「段階LLM失敗→完走しつつフラグ」を別々に確認。

**統合課題4: 不足のまま完走するケースの扱い（正直さ）。**
`max_research_rounds` 打ち切りで観点が埋まらなくても、集まった分で合成・検証する価値はある。
→ **解決:** `status="incomplete_coverage"` を用意し、不足でも Synthesizer/Verifier まで通す。`unmet_aspects` を notes に明示。隠して completed にしない（研究用途の誠実さ）。`test_incomplete_coverage_still_synthesizes_and_verifies` で担保。

**統合課題5: 依存の束ね方（callable は pydantic に載らない）。**
5種の LLM/検索を1つの設定で共有しつつ注入したい。
→ **解決:** `SurveyDependencies` を **dataclass** にして callable を保持（pydantic はバリデーションで callable を扱いにくい）。`SurveyConfig` は全部品に同一オブジェクトを渡して共有。`evaluator_llm` は `None` 許容（機械チェックのみで駆動）にし、最小構成でも回るようにした（`test_runs_without_evaluator_llm`）。

### 未検証部分（正直な記録）
`build_gemini_dependencies`（本番配線）は実装したが、実 API を叩くため **GO まで未実行**。langchain 1.x / langchain-google-genai 4.x 系での `with_structured_output` の挙動は GO 後の初回実行で要確認。チェーンは遅延構築で、呼ぶまで API には触れない。

### テスト結果
```
64 passed in 0.28s （Orchestrator 6 + Verifier 12 + Synthesizer 9 + Evaluator 11 + Collector 8 + Planner 12 + Searcher 6）

Orchestrator（統合）:
- full_pipeline_runs_to_verification        ... テーマ→最終結果まで全段通過・型整合
- self_critique_loop_research_then_completes... 不足→再検索→充足が配線で回る
- pipeline_survives_total_collection_failure... 収集全滅でも failed で破綻せず返る
- incomplete_coverage_still_synthesizes...   ... 不足でも合成・検証まで通し正直に明示
- stage_llm_failures_isolated               ... 合成/検証のLLM失敗を隔離し完走
- runs_without_evaluator_llm                ... evaluator_llm 無し（機械のみ）でも通る
```
すべてモックで検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**

### 6部品の配線が完成
テーマ1つから `SurveyResult`（観点・収集・評価・claim・検証を全保持）までが、実API非依存・全モックで通る。次は最終レポート整形（`SurveyResult`→Markdown/UI）や、GO後の実API通し確認（`SurveyConfig.light()` で軽量実行）が候補。GO待ち。

---

## ステップ8: 最終レポート整形（SurveyResult → Markdown）実装

**日付:** 2026-06-11
**ゴール:** `SurveyResult` を人が読む日本語 Markdown サーベイへ整形。本ツールの売り（出典の裏取り・検証）が見た目に明確に表れるようにする。純粋整形関数で LLM/API 不使用。

### 成果物
- `src/paper_survey/reporter.py` — `render_markdown(result, *, generated_at=None) -> str`。
- `tests/test_reporter.py` — 10本のユニットテスト（全PASS、API不使用）。

### 直面した課題と解決

**課題1【売りの体現】: "裏取りできなかった主張"をどう見せるか。**
普通のレポートなら不確実な主張は削るが、本ツールの誠実さ＝売りはむしろ「検証できなかった事実を正直に出す」こと。
→ **解決:** 各 claim に検証ステータスをバッジ表示（✅SUPPORTED / ⚠️PARTIAL / ❌NOT_SUPPORTED / 🚫CONTRADICTED / ❓UNVERIFIED）。CONTRADICTED/NOT_SUPPORTED は消さず、`> 🚨 要注意…` のブロック警告＋論文側の判定理由を併記して**目立たせて残す**。冒頭サマリにも「裏取りできなかった/矛盾する主張が N 件」を出し、鵜呑み防止メッセージを添える。テストで「矛盾主張が本文に残る」「警告が付く」「理由が出る」を担保。

**課題2: 検証データ(verification)と論文メタ(collection)が別オブジェクトに分かれている。**
claim のステータス・文・IDは `verification.claim_verifications` にあるが、論文のタイトル/URLは `collection` 側。レポートでは両者を結合して「主張→根拠論文リンク」を描く必要がある。
→ **解決:** `collection.papers` から `paper_id → Paper` の辞書を作り、claim の各 paper_id を解決してタイトル/URL/citation_key を付与。verification を観点順にグループ化して本文を構成。原典トレース（タイトル＋ID＋URL）を全 claim と参考文献の両方で実現。

**課題3: 純粋関数なのに「生成日時」が必要＝非決定性が入る。**
`datetime.now()` を内部で呼ぶとテストが不安定になる。
→ **解決:** `generated_at` を**注入可能な引数**にし、未指定時のみ現在時刻を使う。テストは固定文字列を渡して決定的に検証。純粋整形（入力→出力が安定）を保ちつつ実用性も確保。

**課題4: 途中停止(failed)・不足完走(incomplete_coverage)でも壊れず整形したい。**
verification や collection が None のケースがある。
→ **解決:** None ガードを全箇所に入れ、`verification is None`（停止時）は本文の代わりに「計画した観点＋診断ログ」を出す分岐に。不足観点は冒頭サマリと末尾プロセス両方に明示。どの status でもクラッシュしないことをテスト（failed / incomplete / UNVERIFIED追加）で確認。

**課題5: プロセスの透明性（売りの裏付け）。**
→ **解決:** 末尾に「🔍 生成プロセス」節を設け、自己批評ループの再検索回数・完了可否・不足観点、収集の部分失敗件数、grounding 照合の LLM 回数/キャッシュ回避/照合上限到達、段階ログ(notes)を列挙。レポート自体が「どう検証したか」を語る構造にした。

（補足: Windows コンソールは cp932 で絵文字を直接 print できないが、レポートは UTF-8 文字列を返すだけなので問題なし。ファイル出力/UI 表示は UTF-8 で正常。）

### テスト結果
```
74 passed in 0.30s （Reporter 10 + Orchestrator 6 + Verifier 12 + Synthesizer 9 + Evaluator 11 + Collector 8 + Planner 12 + Searcher 6）

Reporter:
- header_and_fixed_date          ... タイトル/テーマ/注入日時
- supported_ratio_in_summary     ... 裏取り率 33% を冒頭表示
- all_status_badges_render       ... ✅/⚠️/🚫 バッジ
- claims_have_paper_links...     ... タイトル+URL+ID リンク、引用表示
- contradicted_is_kept_with_warning ... 矛盾主張を消さず警告＋理由で残す
- references_section_lists...    ... 全論文を citation_key 付きで列挙
- process_transparency_section   ... ループ回数/照合回数の透明性
- incomplete_coverage_shows_unmet... 不足観点を明示
- failed_result_renders...       ... 途中停止でも計画観点＋診断で整形
- unverified_badge_renders       ... ❓UNVERIFIED 表示
```
すべてモック SurveyResult で検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**
サンプル出力を目視確認済み（裏取り率・バッジ・矛盾警告・根拠リンク・参考文献・プロセス透明性が意図どおり表示）。

### サーベイツールが一通り完成
Planner → Collector → Evaluator → Synthesizer → Verifier → Orchestrator → Reporter まで、テーマ入力から検証済み日本語 Markdown レポート出力まで、**実API非依存・全モックで通る**（合計74テスト）。残るは GO 後の実API通し確認（`SurveyConfig.light()` で軽量実行）と、必要なら Streamlit UI への接続。GO待ち。

---

## ステップ9: 実API初通し（GO・最小消費）

**日付:** 2026-06-11
**ゴール:** 初めて実 API（Gemini + arXiv + Semantic Scholar）を叩き、テーマ「RAGのハルシネーション低減手法」を最終 Markdown まで通す。消費は最小化（`max_aspects=3 / papers_per_aspect=3 / max_queries_per_aspect=1 / max_research_rounds=1 / max_verifications=4 / max_concurrent_requests=2`）。

### 結果: 完走（status=incomplete_coverage、クラッシュなし）
- 観点 3、収集論文 14（arXiv）、収集失敗 6（Semantic Scholar）、claim 11/11 採用、検証 SUPPORTED 1 / UNVERIFIED 10、裏取り率 9%。

### 直面した課題と解決（実APIで初めて出たもの）

**課題1【予想と違った】: langchain 1.x の `with_structured_output` はエラーにならなかった。**
事前に「エラーが出る可能性が高い」と見込んでいたが、`langchain-google-genai 4.2.5` + `langchain-core 1.4.6` の組み合わせで Planner/Evaluator/Synthesizer の構造化出力（Pydantic スキーマ強制）はそのまま動作。修正不要だった。→ **記録:** 予想は外れ。事前に注入境界を分け、`build_gemini_dependencies` を遅延構築にしておいたため、仮に失敗してもモック側は無傷の構成にできていた点は有効だった。

**課題2【最大の実問題】: Semantic Scholar が無認証で 429 連発、全 S2 検索が失敗。**
無認証の S2 は厳しくレート制限され、6 クエリすべてが 5 回リトライ後に失敗。
→ **解決(設計が効いた):** Collector の**部分失敗救済**が機能し、arXiv 側 14 件だけで全体は破綻せず継続。`failures` に 6 件記録され、レポートの「収集の部分失敗 6 件」に可視化。指数バックオフも想定通り発火（429→1,2,4,8s）。**改善案(次):** S2 は API キー（`x-api-key`）を付ける／呼び出し間隔を空ける／arXiv 優先にフォールバック。

**課題3: Gemini が一時的に 503 UNAVAILABLE を1回返した（Verifier照合中）。**
"high demand" の一時障害。
→ **解決(設計が効いた):** Verifier の try/except が捕捉し、その claim を **UNVERIFIED** に倒して継続。1件の503で全体は止まらなかった。

**課題4: arXiv の関連性が低い（`all:` クエリの取りこぼし）。**
観点「データの前処理とチャンキング戦略」に、ビザンチン分散最適化やダークエネルギーの論文が混入（"data" 等に広くマッチ）。
→ **挙動(ツールの誠実さが作動):** Synthesizer は正直に「これらの論文は当該観点の内容を含まない」と書き、Verifier はその"正直な主張"を SUPPORTED と判定。さらに Evaluator の質的チェックが3観点すべてを「intent 未達」と判断し `incomplete_coverage` として正直に報告（看板倒れせず自己批評が作動した証拠）。**改善案(次):** arXiv クエリを `all:` から `ti:/abs:` 限定やフレーズ検索にする／S2 を主検索にする／関連度スコアで足切り。

**課題5: `max_verifications=4` で 11 claim 中 4 のみ照合 → 10 が UNVERIFIED。**
コスト最小化の意図どおりだが、裏取り率が見かけ上低くなる。
→ **記録:** これは設計どおりの打ち切り（`cap_reached=True`）。本番運用では `max_verifications` を claim 数に見合う値へ上げる前提。最小消費確認としては正しい挙動。

### API 消費量（実測 + 概算）
- **Gemini（実測の内訳が取れたもの）**: Planner 1 回、Verifier 4 回（=`llm_calls`、上限到達）。
- **Gemini（未計装・概算）**: Evaluator 質的評価 ≤ 3観点 × 2ラウンド = ≤6 回、Synthesizer 3 回（観点ごと1回）。→ **Gemini 合計 ≈ 9〜14 回**。
- **arXiv**: 成功検索（初回3観点 + 再検索分）。キー不要。
- **Semantic Scholar**: 6 クエリ全失敗（各5リトライ=最大30 HTTPリクエスト、全 429）。キー不要だが要レート対策。
- 改善: Evaluator/Synthesizer の LLM 呼び出し回数も `SurveyResult` に集計フィールドを足すと、消費可視化がさらに正確になる（次の小改善候補）。

### テスト影響
モックのユニット/統合テストは不変（74 passed のまま）。実通しは別経路（`build_gemini_dependencies`）で、テストは引き続き実APIを叩かない。

### 次の改善候補（GO都度）
1. Semantic Scholar の 429 対策（APIキー/間隔/フォールバック）。2. arXiv クエリの関連性向上。3. Evaluator/Synthesizer の LLM 呼び出し回数の計装。4. Streamlit UI 接続。いずれも1ステップずつ。

---

## ステップ10: Semantic Scholar 429 対策（キー無し・間隔制御 / arXiv主・S2補助）

**日付:** 2026-06-11
**ゴール:** ステップ9で露呈した「無認証 S2 の 429 全滅」を、キー無しのまま"礼儀正しい叩き方"で緩和。arXiv を主ソース、S2 を補助と位置づけ、S2 が失敗しても全体が成立する役割分担を明確化。（実APIは叩かずモックで検証）

### 成果物
- `src/paper_survey/searcher.py` — `_request_with_retry(headers=...)`、`search_semantic_scholar(api_key=...)`＝`x-api-key` 分岐（環境変数 `SEMANTIC_SCHOLAR_API_KEY` も参照、無くても動く）。
- `src/paper_survey/collector.py` — 主/補助の実行分離（arXiv 並列 / S2 逐次＋間隔）、`SEQUENTIAL_SOURCES`、`sleeper` 注入。
- `src/paper_survey/schemas.py` — `SurveyConfig.semantic_scholar_min_interval`。
- `.env.example` / `.env` — `SEMANTIC_SCHOLAR_API_KEY=` の枠（空でOK）。
- テスト更新/追加: `tests/test_collector.py`（S2逐次・arXiv並列共存）、`tests/test_searcher.py`（ヘッダー分岐）。

### 設計判断（記録）
**無認証APIのレート制限には「間隔制御＋逐次化」で対応し、「主ソース/補助ソース」の役割分担で堅牢性を確保した。**
- arXiv はキー不要で寛容なので**主ソース**として従来通り並列収集。
- Semantic Scholar は無認証だと 429 が厳しいので**補助ソース**に格下げし、並列にせず**逐次＋最小間隔**（`semantic_scholar_min_interval`）で叩く。S2 が全滅しても arXiv だけでサーベイは成立する（部分失敗の救済）。
- 実行分離の工夫: arXiv 並列プールを起動した「同じブロック内」で S2 逐次ループを主スレッドで回すことで、**arXiv の並列収集と S2 の逐次収集を時間的に重ねて**無駄を消した（直列に並べない）。

### 直面した課題と解決
**課題1: 既存の collector テストが「S2も並列」前提だった。**
S2 を逐次化したことで、arXiv+S2 を Barrier(4) で「全部同時」と検証していたテストが壊れる。
→ **解決:** 並列性の証明テストを**主ソース arXiv 単独**（1観点×4クエリ＝4並列）に作り替え。S2 用には新規に「逐次（同時実行1以下）＋間隔（sleeper 記録）」を確定的に検証するテストを追加。さらに「arXiv 並列 と S2 逐次 が共存する」テストで両立を担保。仕様変更に伴うテスト意図の作り替えを、設計の意図どおりに反映できた。

**課題2: テストで実時間を消費せず間隔制御を検証する。**
逐次ループに `time.sleep` を直書きするとテストが遅くなる。
→ **解決:** `collect_papers(sleeper=...)` で待機関数を注入可能にし、テストは `sleeper=lambda d: sleeps.append(d)` で「何秒×何回待ったか」を実時間ゼロで検証。本番は既定の `time.sleep`。

**課題3: `os` 未 import（実装ミス）。**
`search_semantic_scholar` で `os.getenv` を使ったが searcher.py に `import os` が無く `NameError`。
→ **解決:** テストが即座に検出（5 failed）→ `import os` を追加して解消。モックテストがランタイムエラーを配線前に捕まえた好例。

**課題4: 将来キーを入手したら使えるように。**
→ **解決:** `.env` に `SEMANTIC_SCHOLAR_API_KEY=` の空枠を用意し、`search_semantic_scholar(api_key=...)` か環境変数があれば `x-api-key` ヘッダーを付与する分岐を実装。**無ければ無認証で従来通り動く**。テストで「キーあり→ヘッダー付与」「キー無し→ヘッダー None」「環境変数キーも拾う」を確認。

### テスト結果
```
79 passed in 0.32s （+5: Searcher ヘッダー3 + Collector S2逐次2）
- semantic_scholar_runs_sequentially_with_interval ... 逐次(同時1)＋間隔[0.5,0.5]
- arxiv_parallel_and_s2_sequential_coexist         ... arXiv並列(Barrier)とS2逐次が共存
- semantic_scholar_adds_api_key_header_when_present... キーあり→x-api-key付与
- semantic_scholar_no_header_without_key           ... キー無し→ヘッダーNone
- semantic_scholar_uses_env_key                    ... 環境変数キーも拾う
```
すべてモックで検証。**実API（arXiv/Semantic Scholar/Gemini/Tavily）は未呼び出し。**

### 効果（次の実通しでの期待）
S2 が逐次＋1秒間隔で叩かれるため、ステップ9の「即429全滅」よりは緩和が期待できる（無認証の上限は厳しいので完全回避は保証されないが、失敗しても arXiv 主ソースで成立）。将来 S2 キーを入れれば `x-api-key` で大幅緩和。GO後の実通しで効果測定するのが次の確認候補。
