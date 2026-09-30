import pytest

from veldbridge.text import (
    blen,
    chunk,
    clean,
    compact_line,
    cut_bytes,
    fit_list,
    media_label,
    truncate,
)


def test_clean_strips_emoji_and_collapses_whitespace():
    assert clean("hi 👋🏽  there\n\nfriend 👨‍👩‍👧 ❤️!") == "hi there friend !"
    assert clean("🇿🇦 flag") == "flag"


def test_clean_keeps_accents_and_non_latin():
    assert clean("café  Zoë") == "café Zoë"
    assert clean("Ngiyabonga") == "Ngiyabonga"


def test_cut_bytes_never_splits_a_character():
    s = "aé€😀"  # 1 + 2 + 3 + 4 bytes
    assert cut_bytes(s, 1) == "a"
    assert cut_bytes(s, 2) == "a"
    assert cut_bytes(s, 3) == "aé"
    assert cut_bytes(s, 9) == "aé€"
    assert cut_bytes(s, 10) == s


def test_truncate_counts_bytes_and_adds_ellipsis():
    s = "é" * 50  # 100 bytes
    t = truncate(s, 20)
    assert t.endswith("…")
    assert blen(t) <= 20
    assert truncate("short", 20) == "short"


@pytest.mark.parametrize(
    "kind,kw,want",
    [
        ("photo", {}, "[photo]"),
        ("video", {}, "[video]"),
        ("document", {"filename": "Invoice 42.pdf"}, "[pdf: Invoice 42.pdf]"),
        ("document", {"filename": "notes.docx"}, "[doc: notes.docx]"),
        ("location", {"lat": -30.1231234, "lon": 25.4561}, "[loc] -30.12312,25.45610"),
        ("contact", {}, "[contact]"),
        ("voice", {"duration": 42}, "[voice 0:42]"),
        ("voice", {"duration": 125}, "[voice 2:05]"),
        ("voice", {"transcript": "on my way"}, "[voice] on my way"),
        ("text", {}, None),
        (None, {}, None),
    ],
)
def test_media_labels(kind, kw, want):
    assert media_label(kind, **kw) == want


def test_compact_line_fits_full_name():
    assert compact_line(3, "Sam Smith", "see you at 5", 200) == "#3 Sam Smith: see you at 5"


def test_compact_line_falls_back_to_first_name_then_truncates():
    body = "x" * 300
    line = compact_line(12, "Samantha Katherine Jones", body, 200)
    assert line.startswith("#12 Samantha: ")
    assert line.endswith("…")
    assert blen(line) <= 200


def test_compact_line_budget_with_multibyte_text():
    body = "ñ" * 150  # 300 bytes
    line = compact_line(1, "Jo", body, 200)
    assert blen(line) <= 200
    assert line.startswith("#1 Jo: ñ")


def test_chunk_short_text_is_one_packet_without_prefix():
    assert chunk("hello", 200) == ["hello"]


def test_chunk_respects_budget_and_numbers_parts():
    text = " ".join(f"word{i}" for i in range(200))
    parts = chunk(text, 200)
    assert len(parts) > 1
    n = len(parts)
    for i, p in enumerate(parts, 1):
        assert p.startswith(f"[{i}/{n}] ")
        assert blen(p) <= 200
    # nothing lost, word boundaries kept
    rejoined = " ".join(p.split("] ", 1)[1] for p in parts)
    assert rejoined == text


def test_chunk_multibyte_without_spaces():
    text = "😀" * 120  # 480 bytes, no break points
    parts = chunk(text, 200)
    assert all(blen(p) <= 200 for p in parts)
    assert "".join(p.split("] ", 1)[1] for p in parts) == text


def test_chunk_handles_double_digit_part_counts():
    text = "a" * 3000
    parts = chunk(text, 200)
    assert len(parts) >= 10
    assert parts[-1].startswith(f"[{len(parts)}/{len(parts)}] ")
    assert all(blen(p) <= 200 for p in parts)


def test_fit_list_overflows_to_plus_n():
    items = [f"Name{i}" for i in range(50)]
    line = fit_list("50 waiting: ", items, " — r to read", 60)
    assert blen(line) <= 60
    assert "+" in line and line.endswith(" — r to read")
