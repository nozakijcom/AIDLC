"""受入基準1〜9 に対応する slugify の単体テスト。

外部依存がないためモックは使わない。標準ライブラリの unittest で記述する。
"""

import unittest

import slugkit
from slugkit.slugify import slugify


class TestAcceptance1BasicPhrase(unittest.TestCase):
    """受入基準1: slugify("Hello World") は "hello-world" を返す。"""

    def test_hello_world(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_three_words(self):
        self.assertEqual(slugify("My First Post"), "my-first-post")


class TestAcceptance2Lowercase(unittest.TestCase):
    """受入基準2: 大文字はすべて小文字にする。"""

    def test_all_uppercase(self):
        self.assertEqual(slugify("HELLO"), "hello")

    def test_mixed_case(self):
        self.assertEqual(slugify("HeLLo WoRLD"), "hello-world")

    def test_already_lowercase_is_unchanged(self):
        self.assertEqual(slugify("hello-world"), "hello-world")

    def test_digits_are_preserved(self):
        self.assertEqual(slugify("Python 3 Rocks"), "python-3-rocks")


class TestAcceptance3NonAlnumRunCollapses(unittest.TestCase):
    """受入基準3: 英数字以外の並び全体を1つの "-" にする。"""

    def test_spec_example(self):
        self.assertEqual(slugify("a  --  b!!c"), "a-b-c")

    def test_multiple_spaces(self):
        self.assertEqual(slugify("hello     world"), "hello-world")

    def test_assorted_punctuation(self):
        self.assertEqual(slugify("foo@#$%^&*()bar"), "foo-bar")

    def test_underscore_is_a_separator(self):
        self.assertEqual(slugify("foo_bar"), "foo-bar")

    def test_tabs_and_newlines(self):
        self.assertEqual(slugify("foo\t\n\r bar"), "foo-bar")

    def test_single_hyphen_is_kept_as_one(self):
        self.assertEqual(slugify("foo-bar"), "foo-bar")


class TestAcceptance4StripEdgeHyphens(unittest.TestCase):
    """受入基準4: 先頭と末尾の "-" を取り除く。"""

    def test_spec_example(self):
        self.assertEqual(slugify("  Hello!  "), "hello")

    def test_leading_hyphens(self):
        self.assertEqual(slugify("---hello"), "hello")

    def test_trailing_hyphens(self):
        self.assertEqual(slugify("hello---"), "hello")

    def test_both_ends(self):
        self.assertEqual(slugify("!!!hello world!!!"), "hello-world")

    def test_inner_hyphens_are_not_stripped(self):
        self.assertEqual(slugify("-a-b-"), "a-b")


class TestAcceptance5AccentFolding(unittest.TestCase):
    """受入基準5: アクセント付きラテン文字はアクセントを外す。"""

    def test_spec_example(self):
        self.assertEqual(slugify("Caf\u00e9 Cr\u00e8me"), "cafe-creme")

    def test_many_accents(self):
        self.assertEqual(slugify("\u00e0\u00e9\u00ee\u00f5\u00fc\u00e7"), "aeiouc")

    def test_precomposed_and_decomposed_agree(self):
        precomposed = "Caf\u00e9"
        decomposed = "Cafe\u0301"
        self.assertEqual(slugify(precomposed), slugify(decomposed))
        self.assertEqual(slugify(decomposed), "cafe")

    def test_uppercase_accent(self):
        self.assertEqual(slugify("\u00c9COLE"), "ecole")

    def test_dotted_capital_i_folds_to_i(self):
        # U+0130 -> NFKD "I" + U+0307(結合) -> "i"
        self.assertEqual(slugify("\u0130stanbul"), "istanbul")

    def test_compatibility_digits_and_fullwidth(self):
        # NFKD 互換分解で ASCII になる文字は英数字として扱われる
        self.assertEqual(slugify("\uff21\uff22\uff23"), "abc")
        self.assertEqual(slugify("No\u2461"), "no2")

    def test_eszett_is_not_latin_convertible(self):
        # "\u00df" は NFKD でも ASCII にならないので区切り扱い（受入基準6と同じ）
        self.assertEqual(slugify("stra\u00dfe"), "stra-e")


class TestAcceptance6NonLatinActsAsSeparator(unittest.TestCase):
    """受入基準6: ラテン文字に変換できない文字は区切りとして扱う。"""

    def test_spec_example(self):
        self.assertEqual(slugify("abc\u65e5本語def"), "abc-def")

    def test_not_merged_into_one_word(self):
        self.assertNotEqual(slugify("abc\u65e5本語def"), "abcdef")

    def test_single_non_latin_char_between_words(self):
        self.assertEqual(slugify("a\u65e5b"), "a-b")

    def test_cjk_at_both_ends_is_stripped(self):
        self.assertEqual(slugify("\u65e5本語abc\u65e5本語"), "abc")

    def test_cyrillic_and_greek_are_separators(self):
        self.assertEqual(slugify("a\u0434b"), "a-b")
        self.assertEqual(slugify("a\u03b1b"), "a-b")

    def test_emoji_is_a_separator(self):
        self.assertEqual(slugify("hi \U0001f600 there"), "hi-there")

    def test_non_latin_mixed_with_punctuation_collapses_once(self):
        self.assertEqual(slugify("abc \u65e5本語 !! def"), "abc-def")


class TestAcceptance7EmptyResult(unittest.TestCase):
    """受入基準7: 英数字を含まない入力は "" を返す（エラーにしない）。"""

    def test_empty_string(self):
        self.assertEqual(slugify(""), "")

    def test_only_whitespace(self):
        self.assertEqual(slugify("   "), "")

    def test_only_punctuation(self):
        self.assertEqual(slugify("!!!"), "")

    def test_only_hyphens(self):
        self.assertEqual(slugify("---"), "")

    def test_only_non_latin(self):
        self.assertEqual(slugify("\u65e5本語"), "")

    def test_only_combining_marks(self):
        self.assertEqual(slugify("\u0301\u0300"), "")


class TestAcceptance8NoneRaisesValueError(unittest.TestCase):
    """受入基準8: slugify(None) は ValueError。"""

    def test_none_raises_value_error(self):
        with self.assertRaises(ValueError):
            slugify(None)

    def test_none_is_value_error_not_type_error(self):
        # ValueError と TypeError に継承関係はないのでこの区別は有意
        with self.assertRaises(ValueError) as ctx:
            slugify(None)
        self.assertNotIsInstance(ctx.exception, TypeError)


class TestAcceptance9NonStrRaisesTypeError(unittest.TestCase):
    """受入基準9: str 以外の型は TypeError。"""

    def test_int_raises_type_error(self):
        with self.assertRaises(TypeError):
            slugify(123)

    def test_float_raises_type_error(self):
        with self.assertRaises(TypeError):
            slugify(1.5)

    def test_list_raises_type_error(self):
        with self.assertRaises(TypeError):
            slugify(["a", "b"])

    def test_dict_raises_type_error(self):
        with self.assertRaises(TypeError):
            slugify({"a": 1})

    def test_bytes_raises_type_error(self):
        with self.assertRaises(TypeError):
            slugify(b"Hello World")

    def test_bool_raises_type_error(self):
        with self.assertRaises(TypeError):
            slugify(True)

    def test_object_raises_type_error(self):
        with self.assertRaises(TypeError):
            slugify(object())

    def test_type_error_message_mentions_type_name(self):
        with self.assertRaises(TypeError) as ctx:
            slugify(123)
        self.assertIn("int", str(ctx.exception))


class TestStrSubclassAndPurity(unittest.TestCase):
    """境界的な入力と純粋関数としての性質。"""

    def test_str_subclass_is_accepted(self):
        class MyStr(str):
            pass

        self.assertEqual(slugify(MyStr("Hello World")), "hello-world")

    def test_return_type_is_str(self):
        self.assertIsInstance(slugify("Hello"), str)

    def test_input_is_not_mutated_and_result_is_stable(self):
        text = "Caf\u00e9 Cr\u00e8me"
        first = slugify(text)
        second = slugify(text)
        self.assertEqual(first, second)
        self.assertEqual(text, "Caf\u00e9 Cr\u00e8me")

    def test_idempotent_on_its_own_output(self):
        once = slugify("  Caf\u00e9 -- Cr\u00e8me!!  ")
        self.assertEqual(slugify(once), once)

    def test_output_charset_is_restricted(self):
        result = slugify("Caf\u00e9 \u65e5本語 -- 42 @ Stra\u00dfe")
        self.assertTrue(all(c.isdigit() or ("a" <= c <= "z") or c == "-" for c in result))
        self.assertNotIn("--", result)
        self.assertFalse(result.startswith("-"))
        self.assertFalse(result.endswith("-"))

    def test_long_pathological_input_is_linear(self):
        # 入れ子量指定子がないため大量の区切り文字でも破綽しない
        self.assertEqual(slugify("a" + ("-" * 20000) + "b"), "a-b")
        self.assertEqual(slugify("!" * 20000), "")


class TestPackageReExport(unittest.TestCase):
    """slugkit/__init__.py からの再エクスポート。"""

    def test_slugify_is_exported_from_package(self):
        self.assertIs(slugkit.slugify, slugify)

    def test_package_all(self):
        self.assertIn("slugify", slugkit.__all__)

    def test_exported_callable_works(self):
        self.assertEqual(slugkit.slugify("Hello World"), "hello-world")


if __name__ == "__main__":
    unittest.main()
