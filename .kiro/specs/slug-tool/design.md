# スラッグ変換ツール 設計

## 構成

```
slugkit/
  ├── __init__.py   slugify を再エクスポート
  └── slugify.py    slugify(text) の実装
tests/
  └── test_slugify.py
```

標準ライブラリ（`unicodedata`, `re`）のみで実装する。外部サービス・外部パッケージには依存しない。

## 関数仕様

```python
def slugify(text: str) -> str:
    """
    1. text が None なら ValueError、str 以外なら TypeError
    2. unicodedata.normalize("NFKD") で分解し、ASCII に変換できない文字を落とす前に
       区切りとして扱えるようにする（受入基準5, 6）
    3. 小文字にする
    4. [a-z0-9] 以外の文字の並びを1つの "-" に置き換える
    5. 先頭と末尾の "-" を取り除く
    """
```

受入基準6（`"abc日本語def"` → `"abc-def"`）を満たすため、ASCII にできない文字は削除ではなく区切りとして扱う。単に削除すると `"abcdef"` になってしまう。

## テスト方針

外部依存がないため、モックは不要。受入基準1〜9をそれぞれ1つ以上のテストで確認する。テストは標準ライブラリの `unittest` で書く（pytest でも実行できる）。
