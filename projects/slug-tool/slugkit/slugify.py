"""任意の文字列を URL 用スラッグに変換する純粋関数。

標準ライブラリ（``unicodedata``, ``re``）のみを使用する。
"""

import re
import unicodedata

__all__ = ["slugify"]

# 英数字以外の1文字以上の並びを1つの "-" に畳み込む。
# 単純な文字クラス + 単一量指定子のみであり、入れ子量指定子を持たないため
# カタストロフィックバックトラッキングは起こらない（線形時間）。
_NON_ALNUM_RUN = re.compile(r"[^a-z0-9]+")

# ASCII に変換できない文字の代わりに差し込む区切り文字。
# 後段の _NON_ALNUM_RUN で他の記号と一緒に1つの "-" へ畳み込まれる。
_SEPARATOR = "-"


def slugify(text: str) -> str:
    """``text`` を URL で使えるスラッグに変換して返す。

    変換手順:

    1. ``None`` なら :class:`ValueError`、``str`` 以外なら :class:`TypeError`
    2. NFKD 分解してアクセント（結合文字）を取り除く。ASCII に変換できない
       文字は削除せず区切りとして扱う（受入基準5, 6）
    3. 小文字にする
    4. ``[a-z0-9]`` 以外の文字の並びを1つの ``"-"`` に置き換える
    5. 先頭と末尾の ``"-"`` を取り除く

    英数字を1文字も含まない場合は空文字列を返す（例外にしない）。

    :param text: 変換対象の文字列
    :returns: スラッグ文字列（英数字とハイフンのみ）
    :raises ValueError: ``text`` が ``None`` の場合
    :raises TypeError: ``text`` が ``str`` 以外の場合
    """
    if text is None:
        raise ValueError("text must not be None")
    if not isinstance(text, str):
        raise TypeError("text must be str, got " + type(text).__name__)

    decomposed = unicodedata.normalize("NFKD", text)

    folded = []
    for char in decomposed:
        if unicodedata.combining(char):
            # アクセントなどの結合文字は落とす（"e" + U+0301 -> "e"）
            continue
        # ASCII に落とせない文字（日本語など）は区切りとして扱う。
        # 削除すると "abc日本語def" が "abcdef" になってしまう。
        folded.append(char if char.isascii() else _SEPARATOR)

    lowered = "".join(folded).lower()
    return _NON_ALNUM_RUN.sub(_SEPARATOR, lowered).strip(_SEPARATOR)
