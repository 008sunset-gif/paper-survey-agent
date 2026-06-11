"""
paper_survey - 論文サーベイエージェント

研究テーマから arXiv / Semantic Scholar の実在論文を集め、出典を機械的に
裏取りした日本語サーベイを生成するための新モジュール群。
既存の deepdive (src/agents, src/graph 等) とは独立して追加される。

ステップ1ではこのパッケージに Searcher (searcher.py) と
データ型 (schemas.py) のみを実装する。
"""
