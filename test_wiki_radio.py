"""wiki_radio.py の純粋関数ユニットテスト"""

import json
import pytest

from wiki_radio import _escape_json_control_chars, _parse_music_info, _parse_segments


# ---------------------------------------------------------------------------
# _parse_music_info ヘルパー
# ---------------------------------------------------------------------------


class _FakeAudio:
    """mutagen が返すオブジェクトのフェイク。"""

    def __init__(self, tags: dict | None):
        self.tags = tags


def _make_reader(tags: dict | None):
    """指定タグを持つ FakeAudio を返す callable を生成する。"""

    def reader(path, easy=True):
        return _FakeAudio(tags)

    return reader


_NULL_READER = _make_reader(None)  # audio.tags が None のケース用


# ---------------------------------------------------------------------------
# _parse_music_info — メタデータ優先パス
# ---------------------------------------------------------------------------


class TestParseMusicInfoMetadata:
    def test_title_and_artist_from_tags(self):
        reader = _make_reader({"title": ["My Song"], "artist": ["My Artist"]})
        assert _parse_music_info("any.mp3", _reader=reader) == ("My Artist", "My Song")

    def test_title_only_from_tags(self):
        reader = _make_reader({"title": ["My Song"]})
        assert _parse_music_info("any.mp3", _reader=reader) == ("", "My Song")

    def test_whitespace_in_tags_trimmed(self):
        reader = _make_reader({"title": ["  Song  "], "artist": ["  Artist  "]})
        assert _parse_music_info("any.mp3", _reader=reader) == ("Artist", "Song")

    def test_no_title_tag_falls_back_to_filename(self):
        # artist タグはあるが title がない → ファイル名フォールバック
        reader = _make_reader({"artist": ["My Artist"]})
        assert _parse_music_info("Artist - Title.mp3", _reader=reader) == ("Artist", "Title")

    def test_empty_tags_falls_back_to_filename(self):
        reader = _make_reader({})
        assert _parse_music_info("Artist - Title.mp3", _reader=reader) == ("Artist", "Title")

    def test_null_tags_falls_back_to_filename(self):
        assert _parse_music_info("Artist - Title.mp3", _reader=_NULL_READER) == (
            "Artist",
            "Title",
        )

    def test_reader_returns_none_falls_back_to_filename(self):
        assert _parse_music_info("Artist - Title.mp3", _reader=lambda p, easy=True: None) == (
            "Artist",
            "Title",
        )

    def test_reader_raises_falls_back_to_filename(self):
        def failing_reader(path, easy=True):
            raise RuntimeError("read error")

        assert _parse_music_info("Artist - Title.mp3", _reader=failing_reader) == (
            "Artist",
            "Title",
        )


# ---------------------------------------------------------------------------
# _parse_music_info — ファイル名フォールバックパス
# ---------------------------------------------------------------------------


class TestParseMusicInfo:
    def test_artist_and_title(self):
        assert _parse_music_info("Artist - Title.mp3", _reader=_NULL_READER) == ("Artist", "Title")

    def test_track_number_with_dot(self):
        assert _parse_music_info("01. Artist - Title.mp3", _reader=_NULL_READER) == ("Artist", "Title")

    def test_track_number_with_space(self):
        assert _parse_music_info("01 Artist - Title.flac", _reader=_NULL_READER) == ("Artist", "Title")

    def test_title_only(self):
        assert _parse_music_info("Title.mp3", _reader=_NULL_READER) == ("", "Title")

    def test_nested_directory_path(self):
        assert _parse_music_info("/music/jazz/Artist - Title.mp3", _reader=_NULL_READER) == ("Artist", "Title")

    def test_multiple_separators_splits_on_first(self):
        # "Artist - Part1 - Part2" → artist="Artist", title="Part1 - Part2"
        assert _parse_music_info("Artist - Part1 - Part2.mp3", _reader=_NULL_READER) == ("Artist", "Part1 - Part2")

    def test_artist_starting_with_digit_not_stripped(self):
        # "2Pac" は数字の後が文字なのでトラック番号と誤認しない
        assert _parse_music_info("2Pac - California Love.mp3", _reader=_NULL_READER) == ("2Pac", "California Love")

    def test_whitespace_around_separator_trimmed(self):
        # split後に各側がstrip()される
        assert _parse_music_info("Artist  - Title.mp3", _reader=_NULL_READER) == ("Artist", "Title")


# ---------------------------------------------------------------------------
# _escape_json_control_chars
# ---------------------------------------------------------------------------


class TestEscapeJsonControlChars:
    def test_valid_json_unchanged(self):
        s = '[{"speaker": "A", "text": "hello world"}]'
        assert _escape_json_control_chars(s) == s

    def test_literal_newline_in_string_escaped(self):
        s = '[{"text": "hello\nworld"}]'
        result = _escape_json_control_chars(s)
        assert "\\n" in result
        assert json.loads(result) == [{"text": "hello\nworld"}]

    def test_literal_tab_in_string_escaped(self):
        s = '[{"text": "hello\tworld"}]'
        result = _escape_json_control_chars(s)
        assert json.loads(result) == [{"text": "hello\tworld"}]

    def test_literal_carriage_return_in_string_escaped(self):
        s = '[{"text": "hello\rworld"}]'
        result = _escape_json_control_chars(s)
        assert json.loads(result) == [{"text": "hello\rworld"}]

    def test_control_char_outside_string_not_touched(self):
        # JSON構造上の改行（文字列の外）は変更しない
        s = '\n[{"text": "hello"}]'
        assert _escape_json_control_chars(s)[0] == "\n"

    def test_already_escaped_sequence_not_double_escaped(self):
        # すでに \\n とエスケープ済みの場合は二重エスケープしない
        s = '[{"text": "hello\\nworld"}]'
        assert _escape_json_control_chars(s) == s

    def test_nul_char_escaped_as_unicode(self):
        s = '[{"text": "hello\x00world"}]'
        result = _escape_json_control_chars(s)
        assert "\\u0000" in result
        assert json.loads(result) == [{"text": "hello\x00world"}]


# ---------------------------------------------------------------------------
# _parse_segments
# ---------------------------------------------------------------------------


class TestParseSegments:
    def test_plain_json_duo(self):
        raw = '[{"speaker": "A", "text": "こんにちは"}, {"speaker": "B", "text": "やあ"}]'
        assert _parse_segments(raw, "duo") == [
            {"speaker": "A", "text": "こんにちは"},
            {"speaker": "B", "text": "やあ"},
        ]

    def test_code_fence_with_json_label(self):
        raw = '```json\n[{"speaker": "A", "text": "hello"}]\n```'
        assert _parse_segments(raw, "duo") == [{"speaker": "A", "text": "hello"}]

    def test_code_fence_without_label(self):
        raw = '```\n[{"speaker": "A", "text": "hello"}]\n```'
        assert _parse_segments(raw, "duo") == [{"speaker": "A", "text": "hello"}]

    def test_json_embedded_in_surrounding_text(self):
        # LLMが説明文と一緒にJSONを返した場合、最外の [ ] を抽出する
        raw = '以下が台本です。\n[{"speaker": "A", "text": "hello"}]\n以上です。'
        assert _parse_segments(raw, "duo") == [{"speaker": "A", "text": "hello"}]

    def test_control_char_fallback(self):
        # LLMがJSON文字列内にリテラル改行を埋め込んだケース → フォールバック合成で復元
        raw = '[{"speaker": "A", "text": "hello\nworld"}]'
        assert _parse_segments(raw, "duo") == [{"speaker": "A", "text": "hello\nworld"}]

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="台本のJSONパースに失敗"):
            _parse_segments("これはJSONではありません", "duo")

    def test_solo_style_forces_all_speaker_a(self):
        raw = '[{"speaker": "B", "text": "hello"}, {"speaker": "A", "text": "world"}]'
        result = _parse_segments(raw, "solo")
        assert all(seg["speaker"] == "A" for seg in result)

    def test_speaker_normalization_lowercase(self):
        raw = '[{"speaker": "b", "text": "hello"}, {"speaker": "a", "text": "world"}]'
        result = _parse_segments(raw, "duo")
        assert result[0]["speaker"] == "B"
        assert result[1]["speaker"] == "A"

    def test_empty_text_segments_filtered(self):
        raw = '[{"speaker": "A", "text": "hello"}, {"speaker": "B", "text": "   "}]'
        result = _parse_segments(raw, "duo")
        assert result == [{"speaker": "A", "text": "hello"}]

    def test_unknown_speaker_falls_back_to_a(self):
        raw = '[{"speaker": "C", "text": "hello"}]'
        assert _parse_segments(raw, "duo") == [{"speaker": "A", "text": "hello"}]
