#!/usr/bin/env python3
"""Tests for narratives_to_json.py.

What would quietly ruin the narrative set:
  - a decedent's LE paired with another decedent's CME
  - narrative text altered (whitespace collapsed, quotes mangled by encoding)
  - two victims of one incident collapsed into one case_id
  - rows vanishing without being counted

Run with:  python tests/test_narratives_to_json.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import narratives_to_json as ntj  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def run(argv: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        ntj.main(argv)
    return out.getvalue()


def expect_exit(argv: list[str]) -> str:
    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            ntj.main(argv)
    except SystemExit as exc:
        return str(exc.code)
    return ""


def load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


TAB_FILE = (
    "IncidentID\tPersonID\tNarrativeLE\tNarrativeCME\n"
    "1001\t1\tV was found  at the site.\tCME: electrocution.\n"
    "1001\t2\tV2 fell from a scaffold.\t\n"
    "1002\t1\t\t\n"
    "\t1\tno id\tno id\n"
    "1003\t1\t\"Line one\r\nLine two\"\tDecedent's \"note\".\n"
    "1001\t1\tDUPLICATE LE\tDUPLICATE CME\n"
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "narratives.txt"
        src.write_bytes(TAB_FILE.encode("utf-8"))

        print("tab-delimited, IncidentID + PersonID")
        out_dir = tmp / "Construction_case_&_Narrative" / "Json"
        out_dir.mkdir(parents=True)
        log = run(["--input", str(src), "--output", str(out_dir)])
        recs = load(out_dir / "narratives.json")
        check([r["case_id"] for r in recs] == ["1001_1", "1001_2", "1003_1"],
              "two victims of one incident stay separate; blank rows dropped")
        check(list(recs[0]) == ["case_id", "LE", "CME"], "exact key order")
        check(recs[0] == {"case_id": "1001_1", "LE": "V was found  at the site.",
                          "CME": "CME: electrocution."}, "text kept verbatim, first copy kept")
        check(recs[1]["CME"] == "", "missing CME is empty string, row kept")
        check(recs[2]["LE"] == "Line one\nLine two", "multi-line narrative, CRLF normalised")
        check(recs[2]["CME"] == "Decedent's \"note\".", "quotes intact")
        check("case_id = IncidentID + PersonID" in log, "chosen columns printed")
        check("编号为空     1" in log and "都为空  1" in log and "重复 case_id 1" in log,
              "every dropped row counted")
        dup = (out_dir / "duplicate_case_ids.csv").read_text(encoding="utf-8")
        check("1001_1,narratives.txt,6" in dup, "duplicate written with file and row")

        print("require both")
        out = tmp / "both.json"
        run(["--input", str(src), "--output", str(out), "--require-both"])
        check([r["case_id"] for r in load(out)] == ["1001_1", "1003_1"],
              "row without CME dropped")

        print("pipe-delimited cp1252, single CaseID column, custom names")
        pipe = tmp / "pipe" / "n.txt"
        pipe.parent.mkdir()
        pipe.write_bytes("CaseID|LE_Narrative|CME_Narrative\n"
                         "007|V’s truck|Café owner\n".encode("cp1252"))
        out = tmp / "pipe.json"
        run(["--input", str(pipe.parent), "--output", str(out)])
        check(load(out) == [{"case_id": "007", "LE": "V’s truck", "CME": "Café owner"}],
              "cp1252 decoded, leading zero kept, column aliases found")

        print("keep-ids filter")
        keep = tmp / "construction_cases.csv"
        keep.write_text("IncidentID,PersonID,Age\n1001,2,40\n1003,1.0,50\n9999,1,60\n",
                        encoding="utf-8")
        out = tmp / "keep.json"
        log = run(["--input", str(src), "--output", str(out), "--keep-ids", str(keep)])
        check([r["case_id"] for r in load(out)] == ["1001_2", "1003_1"],
              "only ids in the keep file; 1.0 matches 1")
        check("1 个 case_id 在叙述文件里没找到" in log, "missing keep ids reported")

        print("explicit columns and errors")
        out = tmp / "inc.json"
        run(["--input", str(src), "--output", str(out), "--id-cols", "IncidentID"])
        check([r["case_id"] for r in load(out)] == ["1001", "1003"],
              "--id-cols IncidentID: one record per incident")
        msg = expect_exit(["--input", str(src), "--output", str(out), "--le-col", "Nope"])
        check("没有 LE 列" in msg, "wrong LE column named")
        bad = tmp / "bad.txt"
        bad.write_text("IncidentID\tSomething\n1\tx\n", encoding="utf-8")
        msg = expect_exit(["--input", str(bad), "--output", str(out)])
        check("找不到 LE 叙述列" in msg and "Something" in msg, "missing LE column lists columns")

        print("empty result is still valid JSON")
        out = tmp / "empty.json"
        empty_keep = tmp / "empty_keep.csv"
        empty_keep.write_text("IncidentID,PersonID\n5,5\n", encoding="utf-8")
        run(["--input", str(src), "--output", str(out), "--keep-ids", str(empty_keep)])
        check(load(out) == [], "[] written")
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
