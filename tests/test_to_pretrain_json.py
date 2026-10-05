#!/usr/bin/env python3
"""Tests for to_pretrain_json.py.

What would quietly ruin a pretraining corpus:
  - blank cells turning into the literal text "nan"
  - integer columns coming out as "34.0", IDs losing leading zeros
  - non-ASCII text escaped to \\uXXXX or mangled by the wrong encoding
  - Excel sheets other than the first being silently skipped
  - empty rows written as empty samples

Run with:  python tests/test_to_pretrain_json.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import to_pretrain_json as tpj  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def run(argv: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        tpj.main(argv)
    return out.getvalue()


def expect_exit(argv: list[str]) -> str:
    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            tpj.main(argv)
    except SystemExit as exc:
        return str(exc.code)
    return ""


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_csv(path: Path, encoding: str = "utf-8") -> None:
    path.write_text(
        "CaseID,Age,Sex,Narrative\n"
        "007,34,Male,在工地触电。\n"
        "008,,Female,\"Fell from\n  a ladder\"\n"
        ",,,\n"
        "009,51,Male,在工地触电。\n",
        encoding=encoding,
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        csv_path = tmp / "cases.csv"
        make_csv(csv_path)

        print("default key: value format")
        out = tmp / "default.jsonl"
        run(["--input", str(csv_path), "--output", str(out)])
        recs = read_jsonl(out)
        check(len(recs) == 3, "blank row dropped, 3 samples written")
        check(recs[0]["text"] == "CaseID: 007\nAge: 34\nSex: Male\nNarrative: 在工地触电。",
              "leading zero kept, one column per line")
        check("Age" not in recs[1]["text"], "blank cell omitted, not 'nan'")
        check("Fell from\na ladder" in recs[1]["text"], "multi-line cell whitespace cleaned")
        check("\\u" not in out.read_text(encoding="utf-8"), "non-ASCII not escaped")

        print("template")
        out = tmp / "tpl.jsonl"
        run(["--input", str(csv_path), "--output", str(out),
             "--template", "{Age}岁{Sex}：{Narrative}", "--meta-cols", "CaseID",
             "--add-source"])
        recs = read_jsonl(out)
        check(recs[0] == {"text": "34岁Male：在工地触电。", "CaseID": "007",
                          "source": "cases.csv", "row": 1}, "template + meta + source")
        check(recs[2]["row"] == 4, "row number counts dropped rows too")

        print("text columns + dedupe + min chars")
        out = tmp / "cols.jsonl"
        log = run(["--input", str(csv_path), "--output", str(out),
                   "--text-cols", "Narrative", "--dedupe", "--min-chars", "3"])
        recs = read_jsonl(out)
        check([r["text"] for r in recs] == ["在工地触电。", "Fell from\na ladder"],
              "only Narrative, duplicate removed")
        check("重复 1" in log, "duplicate count reported")

        print("json array output")
        out = tmp / "arr.json"
        run(["--input", str(csv_path), "--output", str(out), "--text-cols", "Narrative"])
        arr = json.loads(out.read_text(encoding="utf-8"))
        check(isinstance(arr, list) and len(arr) == 3, ".json suffix -> JSON array")

        print("GBK-encoded CSV")
        gbk = tmp / "gbk" / "gbk.csv"
        gbk.parent.mkdir()
        make_csv(gbk, encoding="gbk")
        out = tmp / "gbk.jsonl"
        run(["--input", str(gbk), "--output", str(out), "--text-cols", "Narrative"])
        check(read_jsonl(out)[0]["text"] == "在工地触电。", "GBK auto-detected")

        print("Excel, multiple sheets, directory input")
        xdir = tmp / "xl"
        xdir.mkdir()
        with pd.ExcelWriter(xdir / "book.xlsx") as w:
            pd.DataFrame({"Age": [34, 51], "Narrative": ["甲", "乙"]}).to_excel(
                w, sheet_name="A", index=False)
            pd.DataFrame({"Age": [60.0], "Narrative": ["丙"]}).to_excel(
                w, sheet_name="B", index=False)
        (xdir / "~$book.xlsx").write_bytes(b"lock file")
        out = tmp / "xl.jsonl"
        run(["--input", str(xdir), "--output", str(out), "--add-source"])
        recs = read_jsonl(out)
        check([r["text"] for r in recs] == ["Age: 34\nNarrative: 甲", "Age: 51\nNarrative: 乙",
                                             "Age: 60\nNarrative: 丙"],
              "both sheets read, 60.0 -> 60, lock file skipped")
        check(recs[2]["source"] == "book.xlsx#B", "sheet name in source")

        out = tmp / "xl_b.jsonl"
        run(["--input", str(xdir), "--output", str(out), "--sheets", "B"])
        check(len(read_jsonl(out)) == 1, "--sheets limits to one sheet")

        print("output is a folder")
        odir = tmp / "Construction_case_&_Narrative" / "Json_all_and_sampling"
        odir.mkdir(parents=True)
        run(["--input", str(csv_path), "--output", str(odir)])
        check(len(read_jsonl(odir / "cases.jsonl")) == 3,
              "existing folder -> <input name>.jsonl inside it")
        newdir = tmp / "new_out"
        run(["--input", str(csv_path), str(xdir), "--output", str(newdir),
             "--format", "json"])
        check((newdir / "pretrain.json").is_file(),
              "missing suffix-less path created as folder, multi-input -> pretrain.json")

        print("errors")
        msg = expect_exit(["--input", str(csv_path), "--output", str(tmp / "x.jsonl"),
                           "--template", "{Missing}"])
        check("缺少列" in msg and "Missing" in msg, "missing template column named")
        msg = expect_exit(["--input", str(tmp / "nope.csv"), "--output", str(tmp / "x.jsonl")])
        check("找不到输入" in msg, "missing input reported")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
