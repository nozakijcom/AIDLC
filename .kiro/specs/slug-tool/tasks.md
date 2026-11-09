# Implementation Plan: スラッグ変換ツール

## Overview

design.md の構成に沿って `slugkit/slugify.py` を実装し、受入基準1〜9のテストを書く。コーディング作業のみを対象とする。

## Task Dependency Graph

```
1. slugify の実装とテスト
   │
   ▼
2. カバレッジ確認と不足分岐の補完
```

```json
{
  "waves": [
    { "wave": 1, "tasks": [1] },
    { "wave": 2, "tasks": [2] }
  ]
}
```

## Tasks

- [ ] 1. `slugkit/slugify.py` に `slugify(text)` を実装する（要件1〜9）
  - `None` は `ValueError`、`str` 以外は `TypeError`
  - NFKD 分解でアクセントを外し、ASCII にできない文字は区切りとして扱う
  - 小文字化、英数字以外の並びを1つの `-` に置換、先頭と末尾の `-` を除去
  - `slugkit/__init__.py` から `slugify` を再エクスポートする
  - `tests/test_slugify.py` に受入基準1〜9のテストを書く

- [ ] 2. カバレッジを確認し、不足している分岐にテストを追加する
  - 行カバレッジ80%以上

## Notes

- 日本語のローマ字化・長さの上限・重複回避は requirements.md のスコープ外。
