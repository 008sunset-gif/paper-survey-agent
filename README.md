# Paper Survey Agent

研究テーマから実在論文を集め、各主張に根拠論文を紐付けた日本語サーベイを生成するツール。LLMが出典を捏造する問題を、生成後の機械的・意味的検証で抑える。

個人開発。テーマ設計・アーキテクチャ設計・全実装・79本のテスト作成までを単独で担当。

## 解決する課題

汎用のAIリサーチは「それらしいが裏が取れない」出力を出す。

具体的には2つの問題がある。

- 出典の捏造。存在しない論文やURLを根拠として示す。
- 論文が言っていないことの主張。実在する論文を引くが、その論文に書かれていない内容を断定する。

どちらも出力を読むだけでは気づけない。読者が一次資料に当たって初めて誤りが分かる。

## 設計の核: LLMの出力を検証する

このツールはLLMの出力をそのまま信用せず、二段階で検証する。

- 各主張に論文IDを紐付ける。IDの無い主張は型レベルで存在できない（`Claim.paper_ids` は最低1件を要求する）。
- 紐付けたIDが収集済みリストに実在するか機械的に照合する。実在しないID（幻覚）は弾く。これが構造的検証。
- 主張が論文の要旨と矛盾しないかLLMで照合する。これが意味的検証。判定は SUPPORTED / PARTIALLY_SUPPORTED / NOT_SUPPORTED / CONTRADICTED / UNVERIFIED の5種。
- 裏が取れない主張は消さない。検証ステータスを付けてレポートに残す。研究では「裏取りできなかった」という事実自体に意味があるため。
- 観点の網羅性を自己評価する。論文が不足する観点は、前回と違うクエリで再検索する。回数は上限で打ち切り、不足が残れば正直に明示する。

## アーキテクチャ

6つの部品を関数合成でつなぐ。

```mermaid
flowchart LR
    T[テーマ] --> P[Planner]
    P --> C[Collector]
    C --> E[Evaluator]
    E -->|不足なら再検索| C
    E --> S[Synthesizer]
    S --> V[Verifier]
    V --> R[Reporter]
    R --> M[Markdownレポート]
```

各部品の役割は次のとおり。

- Planner: テーマを観点に分解し、各観点の英語検索クエリを作る。
- Collector: arXiv（並列）とSemantic Scholar（逐次）から論文を集め、重複を排除する。
- Evaluator: 観点ごとに論文が足りるか自己評価し、不足なら再検索を指示する。
- Synthesizer: 論文を日本語の主張に統合し、各主張に根拠論文IDを付ける。
- Verifier: 各主張を論文要旨とLLMで照合し、裏付け状況を判定する。
- Reporter: 検証ステータス付きのMarkdownレポートに整形する。

Evaluatorは不足時にCollectorへ戻る。これが自己批評ループになる。オーケストレーションはLangGraphを使わず、関数合成にした。経路が線形で、唯一の分岐（再検索ループ）はEvaluator内に終了条件付きで実装済みのため。

なお Searcher は Collector が内部で使う検索部品、Orchestrator は上記6部品を順につなぐ進行役であり、テストではこの2つを加えた8モジュール単位で検証している。

## 動作の実例

検索が観点と無関係な論文を返したとき、ツールは嘘を書かなかった。

テーマ「RAGのハルシネーション低減手法」で実行した。観点「データの前処理とチャンキング戦略」に対し、arXivはビザンチン分散最適化やダークエネルギーの論文を返した。クエリが "data" などに広くマッチしたため。

このときSynthesizerは「これらの論文は当該観点の内容を含まない」と書いた。Evaluatorの質的評価はこの観点を「intent未達」と判定し、再検索でも埋まらず、結果を `incomplete_coverage`（不足のまま完了）として報告した。無関係な論文を根拠に、それらしい主張を作ることはなかった。

検証層が無ければ、混入した論文を引いて誤った主張を書いていた可能性が高い。

## 技術スタック

| 分類 | 技術 |
| --- | --- |
| 言語 | Python 3.14 |
| LLM | Google Gemini（gemini-2.5-flash-lite） |
| LLM連携 | langchain-core / langchain-google-genai（構造化出力に `with_structured_output`） |
| 論文ソース | arXiv API（Atom XML）, Semantic Scholar Graph API（JSON） |
| データ検証 | Pydantic v2 |
| 並列処理 | `concurrent.futures`（ThreadPoolExecutor） |
| HTTP | requests |
| テスト | pytest |

## テスト

79本のユニット・統合テストがある。すべて実APIを叩かない。

LLMと検索APIを関数として注入する設計にした。テストはモックを渡して全経路を検証する。外部依存なしで、パース・リトライ・並列/逐次・重複排除・自己批評ループ・根拠検証・整形を確認できる。

- 並列性は `threading.Barrier` で確定的に検証する。逐次実行ならタイムアウトして失敗扱いになるため、失敗ゼロが並列の証拠になる。
- リトライと間隔制御は待機関数を注入し、実時間ゼロで回数と秒数を検証する。
- LLMの失敗・不正出力は UNVERIFIED に倒れることを検証する。

内訳: Searcher 9 / Planner 12 / Collector 10 / Evaluator 11 / Synthesizer 9 / Verifier 12 / Orchestrator 6 / Reporter 10。

## セットアップと使い方

プロジェクト直下で実行する。Python 3.14を使う。

```
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env
```

`.env` の `GOOGLE_API_KEY` を設定する。arXivとSemantic Scholarはキー不要。S2のキーは任意（あればレート制限が緩む）。

テストを実行する。

```
.venv\Scripts\pytest
```

サーベイを実行する。CLIは無い。Pythonから呼ぶ。

```python
from src.paper_survey.orchestrator import build_gemini_dependencies, run_survey
from src.paper_survey.reporter import render_markdown
from src.paper_survey.schemas import SurveyConfig

deps = build_gemini_dependencies()
config = SurveyConfig.light()  # 消費を抑えた軽量設定
result = run_survey("RAGのハルシネーション低減手法", deps=deps, config=config)
print(render_markdown(result))
```

## 制約

設計上の制約と弱点を正直に挙げる。

- Semantic Scholarは無認証だとレート制限（429）が厳しい。逐次＋間隔制御で緩和するが、完全には防げない。失敗時はarXivの結果だけでレポートを作る。S2キーがあれば緩む。
- arXivの検索精度は高くない。`all:` クエリが観点と無関係な論文を返すことがある。クエリ生成の改善は今後の課題。
- Geminiの無料枠には上限がある。一時的な503や429が出る。`max_verifications` で照合回数を絞ると、多くの主張がUNVERIFIEDのまま残る。
- 意味的検証は要旨（abstract）に対して行う。本文は見ない。要旨に書かれていない裏付けは判定できない。
- 検証もLLMに依存する。判定自体が誤ることはある。だから判定を消さず残し、人間の確認余地を保つ。
- 本番配線（`build_gemini_dependencies`）の実行確認は1回のみ。langchainのバージョン差による不具合が残る可能性がある。
