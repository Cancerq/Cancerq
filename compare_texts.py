#!/usr/bin/env python3
"""Compare the texts in ./data with an LLM and write a markdown summary.

For every text the script asks Claude to extract:

  1. overall themes
  2. character building techniques
  3. language style

and then makes one more call that contrasts all texts in chronological order,
so the report shows how each of the three dimensions changes over time.

Chronological order comes from the first 4-digit year (1500-2099) found in the
file name, e.g. ``1925_draft.txt`` or ``story-2003-rev.md``. Files without a
year go after the dated ones, sorted by name. Rename the files if the order
comes out wrong; the order used is printed and written into the report.

Supported inputs: .txt, .md (and .docx if python-docx is installed).

Per-text results are cached in OUTPUT_DIR/_cache keyed by the file content, so
re-running after revising one text only re-analyses that text.

Setup:

    pip install anthropic pydantic        # python-docx optional, for .docx
    export ANTHROPIC_API_KEY=...          # or `ant auth login`

Usage:

    python compare_texts.py
    python compare_texts.py --data-dir ./data --output out/text_comparison.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TOKENS = 16000
SUFFIXES = {".txt", ".md", ".docx"}
YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")
# Bump when the prompts or schemas change so old cache entries are not reused.
CACHE_VERSION = "1"


# =============================================================================
# Output schemas
# =============================================================================


class TextAnalysis(BaseModel):
    """Analysis of a single text."""

    themes: list[str] = Field(description="Main themes, most central first")
    theme_summary: str = Field(description="2-3 sentences on what the text is about at its core")
    character_techniques: list[str] = Field(
        description="Techniques used to build characters (e.g. indirect characterization "
        "through dialogue, interior monologue, foils), each with a brief example"
    )
    key_characters: list[str] = Field(description="Main characters, 'Name - role' each")
    language_style: list[str] = Field(
        description="Features of the prose: diction, sentence length and rhythm, "
        "point of view, tense, imagery, figurative language, tone, dialogue handling"
    )
    representative_quote: str = Field(description="One short quote (under 40 words) typical of the style")


class DimensionRow(BaseModel):
    label: str = Field(description="Text label exactly as given")
    summary: str = Field(description="One or two sentences for this text on this dimension")
    change_from_previous: str = Field(
        description="What changed compared with the previous text; 'Baseline' for the first"
    )


class DimensionComparison(BaseModel):
    rows: list[DimensionRow] = Field(description="One row per text, in the given order")
    trend: str = Field(description="2-4 sentences on the overall development across all texts")


class Comparison(BaseModel):
    themes: DimensionComparison
    character_building: DimensionComparison
    language_style: DimensionComparison
    constants: list[str] = Field(description="Features that stay the same across all texts")
    overall_assessment: str = Field(description="One paragraph on how the writing evolved over time")


# =============================================================================
# Prompts
# =============================================================================

SYSTEM = (
    "You are a literary analyst. Base every claim on the text you are given, "
    "and be specific: name the device and point to where it appears. "
    "Write in the same language as the user's request."
)

ANALYZE_PROMPT = """Analyse the text below on three dimensions:
1. overall themes
2. character building techniques
3. language style

Text label: {label}

<text>
{text}
</text>"""

COMPARE_PROMPT = """Below are analyses of {n} texts, listed in chronological order (oldest first).
Contrast them on three dimensions: overall themes, character building techniques,
and language style. For each dimension write one row per text, in this order, and
describe what changed relative to the text before it. Point out constants as well
as shifts.

{analyses}"""


# =============================================================================
# Loading
# =============================================================================


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        try:
            import docx  # python-docx
        except ImportError:
            sys.exit(f"{path.name}: install python-docx to read .docx files (pip install python-docx)")
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError("latin-1 decodes any byte string")


def year_of(path: Path) -> int | None:
    m = YEAR_RE.search(path.stem)
    return int(m.group(1)) if m else None


def find_texts(data_dir: Path) -> list[Path]:
    """Text files in data_dir, oldest first (see module docstring for the rule)."""
    files = [
        p
        for p in data_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith((".", "~$"))
    ]

    def key(p: Path):
        y = year_of(p)
        return (y is None, y or 0, str(p.relative_to(data_dir)).lower())

    return sorted(files, key=key)


def label_of(path: Path, data_dir: Path) -> str:
    rel = path.relative_to(data_dir).with_suffix("").as_posix()
    y = year_of(path)
    return rel if y is None or str(y) in rel else f"{rel} ({y})"


# =============================================================================
# LLM calls
# =============================================================================


def ask(client, prompt: str, schema: type[BaseModel]) -> BaseModel:
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        output_config={"effort": EFFORT},
        # Let the API retry on its recommended model if a safety classifier declines.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    if response.stop_reason == "refusal":
        detail = response.stop_details.explanation if response.stop_details else ""
        raise RuntimeError(f"The model declined the request. {detail}".strip())
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"Response hit max_tokens={MAX_TOKENS}; raise MAX_TOKENS and retry.")
    if response.parsed_output is None:
        raise RuntimeError(f"No structured output returned (stop_reason={response.stop_reason}).")
    return response.parsed_output


def analyze(client, label: str, text: str, cache_dir: Path) -> TextAnalysis:
    digest = hashlib.sha256(f"{CACHE_VERSION}|{MODEL}|{label}|{text}".encode()).hexdigest()[:16]
    cache_file = cache_dir / f"{digest}.json"
    if cache_file.exists():
        print(f"  {label}: cached")
        return TextAnalysis.model_validate_json(cache_file.read_text(encoding="utf-8"))
    print(f"  {label}: analysing ({len(text):,} chars)...")
    result = ask(client, ANALYZE_PROMPT.format(label=label, text=text), TextAnalysis)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def compare(client, analyses: list[tuple[str, TextAnalysis]]) -> Comparison:
    blocks = "\n\n".join(
        f'<analysis label="{label}">\n{a.model_dump_json(indent=2)}\n</analysis>' for label, a in analyses
    )
    return ask(client, COMPARE_PROMPT.format(n=len(analyses), analyses=blocks), Comparison)


# =============================================================================
# Markdown
# =============================================================================


def cell(value) -> str:
    if isinstance(value, list):
        value = "<br>".join(f"• {v}" for v in value)
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", "<br>").strip()


def table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def render(analyses: list[tuple[str, TextAnalysis]], comparison: Comparison | None) -> str:
    out = ["# Text Comparison", ""]
    out += [f"Model: `{MODEL}`. Texts in chronological order:", ""]
    out += [f"{i}. {label}" for i, (label, _) in enumerate(analyses, 1)]

    if comparison is not None:
        out += ["", "## Summary", ""]
        out.append(
            table(
                ["Dimension", "Development over time"],
                [
                    ["Overall themes", comparison.themes.trend],
                    ["Character building", comparison.character_building.trend],
                    ["Language style", comparison.language_style.trend],
                    ["Constants", comparison.constants],
                ],
            )
        )
        out += ["", comparison.overall_assessment]

        sections = [
            ("1. Overall Themes", comparison.themes),
            ("2. Character Building Techniques", comparison.character_building),
            ("3. Language Style Variations Over Time", comparison.language_style),
        ]
        for title, dim in sections:
            out += ["", f"## {title}", ""]
            out.append(
                table(
                    ["Text", "Summary", "Change from previous"],
                    [[r.label, r.summary, r.change_from_previous] for r in dim.rows],
                )
            )
            out += ["", f"**Trend:** {dim.trend}"]

    out += ["", "## Per-Text Details", ""]
    out.append(
        table(
            ["Text", "Themes", "Character techniques", "Key characters", "Language style", "Sample"],
            [
                [label, a.themes, a.character_techniques, a.key_characters, a.language_style,
                 f"“{a.representative_quote}”"]
                for label, a in analyses
            ],
        )
    )
    return "\n".join(out) + "\n"


# =============================================================================
# Main
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("out/text_comparison.md"))
    args = parser.parse_args(argv)

    if not args.data_dir.is_dir():
        sys.exit(f"Data folder not found: {args.data_dir.resolve()}")
    paths = find_texts(args.data_dir)
    if not paths:
        sys.exit(f"No {', '.join(sorted(SUFFIXES))} files in {args.data_dir.resolve()}")

    texts = []
    for p in paths:
        text = read_text(p).strip()
        if text:
            texts.append((label_of(p, args.data_dir), text))
        else:
            print(f"Skipping empty file: {p}")
    print("Order used (oldest first):")
    for i, (label, _) in enumerate(texts, 1):
        print(f"  {i}. {label}")

    import anthropic

    client = anthropic.Anthropic()
    cache_dir = args.output.parent / "_cache"
    try:
        print("Analysing each text:")
        analyses = [(label, analyze(client, label, text, cache_dir)) for label, text in texts]
        comparison = None
        if len(analyses) > 1:
            print("Comparing across texts...")
            comparison = compare(client, analyses)
    except anthropic.AuthenticationError:
        sys.exit("Authentication failed: set ANTHROPIC_API_KEY or run `ant auth login`.")
    except anthropic.BadRequestError as e:
        sys.exit(f"Request rejected: {e.message}")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Could not reach the API; check the network connection.")
    except RuntimeError as e:
        sys.exit(str(e))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(analyses, comparison), encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
