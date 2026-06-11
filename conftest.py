"""
pytest 設定。

このファイルが存在することで pytest がプロジェクトルートを sys.path に追加し、
`import src.paper_survey...` が名前空間パッケージとして解決できるようになる
（src/__init__.py を置かない運用のため）。
"""
