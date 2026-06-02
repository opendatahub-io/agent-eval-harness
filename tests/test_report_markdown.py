"""Tests for the report markdown renderer (skills/eval-run/scripts/report.py).

Focus: _md_to_html paragraph handling. Soft-wrapped source lines must be
joined into a single <p> so analysis.md paragraphs reflow to the container
width instead of each wrapped line becoming its own narrow paragraph.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "eval-run" / "scripts"))

from report import _md_to_html


def test_wrapped_paragraph_joins_into_single_p():
    md = "This is a paragraph\nthat was hard-wrapped\nacross three lines."
    html = _md_to_html(md)
    assert html.count("<p>") == 1
    assert "<p>This is a paragraph that was hard-wrapped across three lines.</p>" in html


def test_blank_line_separates_paragraphs():
    md = "First paragraph\nstill first.\n\nSecond paragraph\nstill second."
    html = _md_to_html(md)
    assert html.count("<p>") == 2
    assert "<p>First paragraph still first.</p>" in html
    assert "<p>Second paragraph still second.</p>" in html


def test_single_line_paragraph_unchanged():
    assert _md_to_html("Just one line.") == "<p>Just one line.</p>"


def test_paragraph_stops_at_block_constructs():
    # A paragraph immediately followed (no blank line) by each block type must
    # not absorb that block.
    md = (
        "Lead paragraph here\n"
        "wrapped a bit.\n"
        "## A heading\n"
        "Para before list\n"
        "- item one\n"
        "- item two\n"
        "Para before table\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
    )
    html = _md_to_html(md)
    assert "<p>Lead paragraph here wrapped a bit.</p>" in html
    assert "<h2>A heading</h2>" in html
    assert "<p>Para before list</p>" in html
    assert "<ul>" in html and "<li>item one</li>" in html
    assert "<p>Para before table</p>" in html
    assert "<table>" in html


def test_paragraph_stops_at_fenced_code():
    md = "Some prose\nthat wraps.\n```\ncode line\n```\n"
    html = _md_to_html(md)
    assert "<p>Some prose that wraps.</p>" in html
    assert "code line" in html
    assert html.count("<p>") == 1
