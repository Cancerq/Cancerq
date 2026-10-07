#!/usr/bin/env python3
"""Tests for compare_texts.py, with the Claude API replaced by a stub.

What would quietly ruin the comparison:
  - texts sent in the wrong chronological order
  - a revised text answered from a stale cache entry
  - '|' or newlines in the model's answer breaking the markdown tables

Run with:  python tests/test_compare_texts.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import compare_texts as ct  # noqa: E402


class StubMessages:
    def __init__(self):
        self.prompts = []

    def parse(self, *, messages, output_format, **kwargs):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if output_format is ct.TextAnalysis:
            out = ct.TextAnalysis(
                themes=["loss | memory", "home"],
                theme_summary="About home.",
                character_techniques=["dialogue\nreveals motive"],
                key_characters=["Ann - narrator"],
                language_style=["short sentences"],
                representative_quote="We left.",
            )
        else:
            labels = [line.split('"')[1] for line in prompt.splitlines() if line.startswith("<analysis label=")]
            dim = ct.DimensionComparison(
                rows=[ct.DimensionRow(label=l, summary="s", change_from_previous="c") for l in labels],
                trend="t",
            )
            out = ct.Comparison(
                themes=dim, character_building=dim, language_style=dim,
                constants=["first person"], overall_assessment="Grew sparer.",
            )
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=out)


def run(data: Path, out: Path) -> StubMessages:
    stub = StubMessages()
    import anthropic

    original = anthropic.Anthropic
    anthropic.Anthropic = lambda: SimpleNamespace(beta=SimpleNamespace(messages=stub))
    try:
        assert ct.main(["--data-dir", str(data), "--output", str(out)]) == 0
    finally:
        anthropic.Anthropic = original
    return stub


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        data = tmp / "data"
        data.mkdir()
        (data / "story_2010.txt").write_text("Later text.", encoding="utf-8")
        (data / "1998-first.md").write_text("Early text.", encoding="utf-8")
        (data / "notes.txt").write_text("Undated.", encoding="utf-8")
        (data / "empty.txt").write_text("  \n", encoding="utf-8")
        (data / "ignore.csv").write_text("a,b", encoding="utf-8")
        out = tmp / "out" / "report.md"

        stub = run(data, out)
        assert len(stub.prompts) == 4, stub.prompts  # 3 texts + 1 comparison
        assert "Early text." in stub.prompts[0] and "Later text." in stub.prompts[1]
        assert "Undated." in stub.prompts[2]
        cmp_prompt = stub.prompts[3]
        assert cmp_prompt.index("1998-first") < cmp_prompt.index("story_2010") < cmp_prompt.index("notes")

        report = out.read_text(encoding="utf-8")
        assert "## 1. Overall Themes" in report and "## 3. Language Style Variations Over Time" in report
        assert "loss \\| memory" in report and "dialogue<br>reveals motive" in report
        cols = None
        for line in report.splitlines():
            if line.startswith("| Text | Themes"):
                cols = line.count(" | ")
            elif cols is not None and line.startswith("| 1998-first"):
                assert line.replace("\\|", "").count(" | ") == cols, line

        # Unchanged texts come from cache; a revised one is re-analysed.
        (data / "story_2010.txt").write_text("Later text, revised.", encoding="utf-8")
        stub = run(data, out)
        assert len(stub.prompts) == 2 and "revised" in stub.prompts[0], stub.prompts
    finally:
        shutil.rmtree(tmp)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
