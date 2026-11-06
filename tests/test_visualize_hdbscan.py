#!/usr/bin/env python3
"""Tests for visualize_hdbscan.py.

What would quietly mislead a reader of the picture:
  - a noise point (-1) drawn inside a cluster, or a cluster lost
  - the wrong word chosen as a cluster's center
  - HDBSCAN parameters from the file not reaching the page
  - one of the accepted input shapes being read as something else
  - a Chinese CSV saved as GBK failing to open

Run with:  python tests/test_visualize_hdbscan.py
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import visualize_hdbscan as vh  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def run(argv: list[str]) -> str:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        vh.main(argv)
    return out.getvalue()


def expect_exit(argv: list[str]) -> str:
    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            vh.main(argv)
    except SystemExit as e:
        return str(e)
    return ""


def embedded(html: str) -> dict:
    start = html.index("const DATA = ") + len("const DATA = ")
    end = html.index(";\n(function", start)
    return json.loads(html[start:end].replace("<\\/", "</"))


def build(path: Path, **kw) -> dict:
    records, params, fmt = vh.load_file(path)
    return vh.build_dataset(records, params, source=path, fmt=fmt, **kw)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="hdb-"))
    try:
        print("\n-- CSV with comment-line params --")
        csv_path = work / "words.csv"
        csv_path.write_text(
            "# min_cluster_size = 5\n# metric: cosine\n"
            "word,cluster,probability\n"
            "苹果,0,0.8\n香蕉,0,1.0\n橙子,0,0.6\n"
            "汽车,1,0.9\n火车,1.0,0.7\n"
            "今天,-1,0\n然后,,0\n",
            encoding="utf-8",
        )
        ds = build(csv_path)
        check(ds["params"] == {"min_cluster_size": 5, "metric": "cosine"}, "comment params parsed with types")
        check(ds["stats"]["n_clusters"] == 2, "two clusters")
        check(ds["stats"]["n_noise"] == 2, "-1 and blank both count as noise")
        check([c["id"] for c in ds["clusters"]] == [0, 1], "'1.0' folds into cluster 1")
        check(ds["clusters"][0]["center"]["word"] == "香蕉", "highest probability is the center")
        check(ds["clusters"][0]["size"] == 3 and len(ds["clusters"][0]["members"]) == 2,
              "center not repeated among members")
        check([m["word"] for m in ds["clusters"][0]["members"]] == ["苹果", "橙子"],
              "members ordered by probability")
        check(abs(ds["stats"]["noise_ratio"] - 2 / 7) < 1e-3, "noise ratio")

        print("\n-- explicit center flag wins over probability --")
        flag = work / "flag.tsv"
        flag.write_text("term\ttopic\tprob\tis_center\na\t0\t1.0\t0\nb\t0\t0.2\t1\nc\t0\t0.5\t0\n",
                        encoding="utf-8")
        ds = build(flag)
        check(ds["meta"]["mapping"]["词"] == "term" and ds["meta"]["mapping"]["簇标签"] == "topic",
              "alternate column names detected")
        check(ds["clusters"][0]["center"]["word"] == "b", "is_center column used")

        print("\n-- tied probabilities fall back to centroid distance --")
        tie = work / "tie.csv"
        tie.write_text("word,label,probability,x,y\nfar,0,1,10,10\nmid,0,1,1,1\nlow,0,0.5,0,0\nnear,0,1,0.5,0.5\n",
                       encoding="utf-8")
        ds = build(tie)
        # centroid = (2.875, 2.875); among the tied 1.0s, 'mid' is closest
        check(ds["clusters"][0]["center"]["word"] == "mid", "closest-to-centroid among ties")
        check(ds["meta"]["has_xy"], "coordinates detected")

        print("\n-- JSON: clusters form --")
        jc = work / "clusters.json"
        jc.write_text(json.dumps({
            "parameters": {"min_samples": 3},
            "clusters": [
                {"id": 0, "center": "水果", "members": ["苹果", {"word": "梨", "probability": 0.4}]},
                {"id": 1, "center": "交通", "members": ["交通", "汽车"]},
            ],
            "noise": ["东西"],
        }, ensure_ascii=False), encoding="utf-8")
        ds = build(jc)
        check(ds["params"] == {"min_samples": 3}, "parameters key read")
        check(ds["clusters"][0]["center"]["word"] == "水果" and ds["clusters"][0]["size"] == 3,
              "center not in members gets added")
        check(ds["clusters"][1]["size"] == 2, "center already in members is not duplicated")
        check(ds["stats"]["n_noise"] == 1, "noise list read")

        print("\n-- JSON: parallel arrays (clusterer.labels_ dump) --")
        jp = work / "arrays.json"
        jp.write_text(json.dumps({"words": ["a", "b", "c"], "labels_": [0, 0, -1],
                                  "probabilities_": [0.9, 0.3, 0.0]}), encoding="utf-8")
        ds = build(jp)
        check(ds["stats"]["n_clusters"] == 1 and ds["stats"]["n_noise"] == 1, "arrays zipped into rows")
        check(ds["meta"]["has_prob"], "probabilities_ detected")

        print("\n-- JSONL and record list --")
        jl = work / "rows.jsonl"
        jl.write_text('{"text":"x","cluster":2}\n{"text":"y","cluster":2}\n', encoding="utf-8")
        check(build(jl)["clusters"][0]["id"] == 2, "jsonl read")
        jr = work / "rows.json"
        jr.write_text('[{"cluster":0},{"cluster":0}]', encoding="utf-8")
        ds = build(jr)
        check(ds["clusters"][0]["center"]["word"] == "#0", "missing word column falls back to row number")

        print("\n-- GBK encoded CSV --")
        gbk = work / "gbk.csv"
        gbk.write_bytes("词,簇\n苹果,0\n香蕉,0\n".encode("gbk"))
        ds = build(gbk)
        check(ds["clusters"][0]["center"]["word"] == "苹果", "GBK decoded, Chinese column names matched")

        print("\n-- max_members --")
        ds = build(csv_path, max_members=1)
        check(len(ds["clusters"][0]["members"]) == 1 and ds["clusters"][0]["truncated"] == 1,
              "truncation recorded")
        check(ds["clusters"][0]["size"] == 3, "size still the real size")

        print("\n-- summary table: one row per cluster --")
        summ = work / "cluster_summary.csv"
        summ.write_text(
            "hdbscan_label,cluster_id,term_count,mean_membership_strength,frequent_term_preview\n"
            "-1,noise,50,0.0,a; b\n"
            "1,C1,30,0.84,\"pills, overdose, opioid\"\n"
            "0,C0,42,0.91,gun; rifle; pistol; bullet\n"
            "2,C2,18,0.77,rope (12); hanging (9)\n"
            "3,C3,25,0.88,\"['argument', 'divorce']\"\n",
            encoding="utf-8",
        )
        ds = build(summ)
        check(ds["meta"].get("mode") == "summary", "summary mode detected")
        check(ds["meta"]["mapping"]["簇标签"] == "hdbscan_label", "hdbscan_label preferred over cluster_id")
        check([c["id"] for c in ds["clusters"]] == [0, 1, 2, 3], "clusters sorted, -1 excluded")
        c0 = ds["clusters"][0]
        check(c0["center"]["word"] == "gun" and [m["word"] for m in c0["members"]] == ["rifle", "pistol", "bullet"],
              "first preview term is the center, rest are members")
        check(c0["size"] == 42 and c0["truncated"] == 38, "size from term_count, rest counted as omitted")
        check(c0["mean_prob"] == 0.91, "mean membership read")
        check(c0["info"].get("cluster_id") == "C0", "other columns kept for the tooltip")
        check([m["word"] for m in ds["clusters"][1]["members"]] == ["overdose", "opioid"], "comma separated")
        c2 = ds["clusters"][2]
        check(c2["center"]["word"] == "rope" and c2["center"]["extra"]["数值"] == 12, "'term (n)' split into term and number")
        check(ds["clusters"][3]["members"][0]["word"] == "divorce", "python list literal parsed")
        check(ds["stats"]["n_noise"] == 50 and ds["stats"]["n_points"] == 165, "noise and totals from term_count")
        check([p["word"] for p in ds["noise"]] == ["a", "b"], "noise preview terms kept")
        check(vh.split_terms("x | y ; z") == [("x", None), ("y", None), ("z", None)], "mixed separators")
        check(vh.split_terms("") == [] and vh.split_terms(None) == [], "empty preview")

        print("\n-- words shared across clusters --")
        sh = work / "shared.csv"
        sh.write_text(
            "hdbscan_label,term_count,frequent_term_preview\n"
            "-1,9,coach; wound\n"
            "0,10,gun; hunting; wound\n"
            "1,10,golf; hunting; Coach\n"
            "2,10,pills;  Wound \n"
            "3,10,coach; therapist\n",
            encoding="utf-8",
        )
        shared = {e["word"].lower(): e for e in build(sh)["shared"]}
        check(set(shared) == {"wound", "hunting", "coach"}, "shared words found, case and spaces ignored")
        check(shared["wound"]["clusters"] == [0, 2], "wound in clusters 0 and 2 (noise not counted)")
        check(shared["coach"]["as_center"] == [3], "center occurrence recorded")
        check(build(sh)["shared"][0]["word"].lower() in {"wound", "hunting", "coach"}, "sorted list")
        ds = build(csv_path)
        check(ds["shared"] == [], "no shared words when clusters are disjoint")
        cross = work / "cross.csv"
        cross.write_text("word,cluster\nx,0\ny,0\nx,1\nz,1\ny,1\n", encoding="utf-8")
        check([(e["word"], e["clusters"]) for e in build(cross)["shared"]] == [("x", [0, 1]), ("y", [0, 1])],
              "per-word tables too")

        print("\n-- CLI end to end --")
        out = work / "page.html"
        log = run([str(csv_path), "-o", str(out), "--param", "min_cluster_size=8",
                   "--param", "cluster_selection_method=leaf"])
        html = out.read_text(encoding="utf-8")
        data = embedded(html)
        check("已生成" in log, "reports output path")
        check(data["params"]["min_cluster_size"] == 8, "--param overrides file param")
        check(data["params"]["cluster_selection_method"] == "leaf", "--param adds new param")
        check(data["params"]["metric"] == "cosine", "file param kept")
        check("<title>HDBSCAN 聚类 · words.csv</title>" in html, "default title")
        default_out = csv_path.with_name("words_hdbscan.html")
        run([str(csv_path)])
        check(default_out.is_file(), "default output name")

        evil = work / "evil.json"
        evil.write_text(json.dumps([{"word": "</script><b>x", "cluster": 0}]), encoding="utf-8")
        run([str(evil), "-o", str(work / "evil.html")])
        html = (work / "evil.html").read_text(encoding="utf-8")
        check(html.count("</script>") == 1, "word text cannot close the script tag")
        check(embedded(html)["clusters"][0]["center"]["word"] == "</script><b>x", "word round-trips")

        print("\n-- errors --")
        bad = work / "bad.csv"
        bad.write_text("word,score\na,1\n", encoding="utf-8")
        check("簇标签列" in expect_exit([str(bad)]), "missing cluster column explained")
        check("找不到列" in expect_exit([str(csv_path), "--word-col", "nope"]), "bad override explained")
        check("找不到文件" in expect_exit([str(work / "none.csv")]), "missing file explained")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
