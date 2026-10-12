#!/usr/bin/env python3
"""Tests for narratives_to_json.py.

What would quietly ruin the narrative set:
  - a decedent's LE paired with another decedent's CME
  - narrative text altered (whitespace collapsed, quotes mangled by encoding)
  - the IncidentID filter matching on anything but IncidentID
  - a sample that is not reproducible, repeats cases, or leaks unfiltered ones
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

        print("tab-delimited, case_id = IncidentID")
        out_dir = tmp / "Construction_case_&_Narrative" / "Json"
        out_dir.mkdir(parents=True)
        log = run(["--input", str(src), "--output", str(out_dir), "--sample", "0"])
        recs = load(out_dir / "narratives.json")
        check([r["case_id"] for r in recs] == ["1001", "1003"],
              "one record per IncidentID; blank rows dropped")
        check(list(recs[0]) == ["case_id", "LE", "CME"], "exact key order")
        check(recs[0] == {"case_id": "1001", "LE": "V was found  at the site.",
                          "CME": "CME: electrocution."}, "text kept verbatim, first copy kept")
        check(recs[1]["LE"] == "Line one\nLine two", "multi-line narrative, CRLF normalised")
        check(recs[1]["CME"] == "Decedent's \"note\".", "quotes intact")
        check("case_id = IncidentID " in log, "chosen columns printed")
        check("编号为空     1" in log and "都为空  1" in log and "重复 case_id 2" in log,
              "every dropped row counted")
        dup = (out_dir / "duplicate_case_ids.csv").read_text(encoding="utf-8")
        check("1001,narratives.txt,2" in dup and "1001,narratives.txt,6" in dup,
              "duplicates written with file and row")
        check(not list(out_dir.glob("*sample*")), "--sample 0 writes no sample file")

        print("--id-cols still allows IncidentID + PersonID")
        out = tmp / "pair.json"
        run(["--input", str(src), "--output", str(out), "--id-cols", "IncidentID,PersonID",
             "--sample", "0"])
        check([r["case_id"] for r in load(out)] == ["1001_1", "1001_2", "1003_1"],
              "override joins columns")

        print("require both")
        out = tmp / "both.json"
        run(["--input", str(src), "--output", str(out), "--require-both", "--sample", "0"])
        check([r["case_id"] for r in load(out)] == ["1001", "1003"], "row without CME dropped")

        print("pipe-delimited cp1252, column aliases")
        pipe = tmp / "pipe" / "n.txt"
        pipe.parent.mkdir()
        pipe.write_bytes("Incident_ID|LE_Narrative|CME_Narrative\n"
                         "007|V\u2019s truck|Caf\u00e9 owner\n".encode("cp1252"))
        out = tmp / "pipe.json"
        run(["--input", str(pipe.parent), "--output", str(out), "--sample", "0"])
        check(load(out) == [{"case_id": "007", "LE": "V\u2019s truck", "CME": "Caf\u00e9 owner"}],
              "cp1252 decoded, leading zero kept, column aliases found")

        print("keep-ids filter matches on IncidentID only")
        keep = tmp / "construction_cases.csv"
        keep.write_text("IncidentID,PersonID,Age\n1003.0,9,50\n9999,1,60\n", encoding="utf-8")
        out = tmp / "keep.json"
        log = run(["--input", str(src), "--output", str(out), "--keep-ids", str(keep),
                   "--sample", "0"])
        check([r["case_id"] for r in load(out)] == ["1003"],
              "PersonID in keep file ignored; 1003.0 matches 1003")
        check("1 个 IncidentID 在叙述文件里没找到" in log, "missing keep ids reported")
        out = tmp / "keep_pair.json"
        run(["--input", str(src), "--output", str(out), "--keep-ids", str(keep),
             "--id-cols", "IncidentID,PersonID", "--sample", "0"])
        check([r["case_id"] for r in load(out)] == ["1003_1"],
              "filter stays on IncidentID even when case_id has PersonID")
        no_inc = tmp / "no_inc.csv"
        no_inc.write_text("CaseID\n1\n", encoding="utf-8")
        msg = expect_exit(["--input", str(src), "--output", str(out), "--keep-ids", str(no_inc)])
        check("找不到 IncidentID 列" in msg, "keep file without IncidentID rejected")

        print("random sample of 500")
        big = tmp / "big" / "narr.txt"
        big.parent.mkdir()
        lines = ["IncidentID\tNarrativeLE\tNarrativeCME"]
        lines += [f"{i}\tLE {i}\tCME {i}" for i in range(1, 2001)]
        big.write_text("\n".join(lines) + "\n", encoding="utf-8")
        keep_big = tmp / "keep_big.csv"
        keep_big.write_text("IncidentID\n" + "\n".join(str(i) for i in range(1, 1501, 1)) + "\n",
                            encoding="utf-8")
        out_a = tmp / "sa"
        log = run(["--input", str(big), "--output", str(out_a), "--keep-ids", str(keep_big)])
        full = load(out_a / "narratives.json")
        samp = load(out_a / "narratives_sample_500.json")
        ids = [int(r["case_id"]) for r in samp]
        check(len(full) == 1500 and len(samp) == 500, "1500 filtered, 500 sampled")
        check(len(set(ids)) == 500 and all(1 <= i <= 1500 for i in ids),
              "sample has no repeats and only filtered cases")
        check(ids == sorted(ids), "sample kept in file order")
        check(all(r in full for r in samp[:20]), "sampled records identical to full ones")
        check("随机抽样 500 个案例（seed=42）" in log, "sample reported")
        out_b = tmp / "sb"
        run(["--input", str(big), "--output", str(out_b), "--keep-ids", str(keep_big)])
        check(load(out_b / "narratives_sample_500.json") == samp, "same seed -> same sample")
        out_c = tmp / "sc"
        run(["--input", str(big), "--output", str(out_c), "--keep-ids", str(keep_big),
             "--seed", "7"])
        check(load(out_c / "narratives_sample_500.json") != samp, "different seed -> different sample")
        lo = sum(1 for i in ids if i <= 750)
        check(150 < lo < 350, f"sample spread over the file ({lo} of 500 in first half)")

        out = tmp / "small.json"
        log = run(["--input", str(src), "--output", str(out)])
        check(len(load(tmp / "small_sample_500.json")) == 2, "fewer than 500 -> all kept")
        check("不足 500" in log, "shortfall reported")

        print("errors")
        msg = expect_exit(["--input", str(src), "--output", str(out), "--le-col", "Nope"])
        check("没有 LE 列" in msg, "wrong LE column named")
        bad = tmp / "bad.txt"
        bad.write_text("IncidentID\tSomething\n1\tx\n", encoding="utf-8")
        msg = expect_exit(["--input", str(bad), "--output", str(out)])
        check("找不到 LE 叙述列" in msg and "Something" in msg, "missing LE column lists columns")

        print("empty result is still valid JSON")
        out = tmp / "empty.json"
        empty_keep = tmp / "empty_keep.csv"
        empty_keep.write_text("IncidentID\n5\n", encoding="utf-8")
        run(["--input", str(src), "--output", str(out), "--keep-ids", str(empty_keep)])
        check(load(out) == [] and load(tmp / "empty_sample_500.json") == [], "[] written")
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
