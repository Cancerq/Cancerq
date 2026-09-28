#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NVDRS · Construction 预处理：按 Census2018_Industry 筛 construction，
再用这些 case 的 PersonID 去 NVDRS concat 里找 Narrative，合并回来。

前提：已经按年龄切好了 18-67 岁的文件（18-27 / 28-37 / 38-47 / 48-57 / 58-67）。

三步：

  第 1 步  逐个年龄段文件读 Census2018_Industry，
           等于 "Construction"（不区分大小写、忽略首尾空格）的行 = construction case，
           收集这些 case 的 PersonID。

  第 2 步  流式扫一遍 NVDRS concat（只读 PersonID 和 Narrative 两列，省内存），
           取出这些 PersonID 对应的 Narrative。

  第 3 步  把 Narrative 按 PersonID 合并回 construction 行，
           Narrative 列放在 IncidentID 后面；5 个年龄段合成一张表（带 age_band 列）。

用法：把下面「配置区」的路径空白填好，然后直接运行

    python run_construction_concat.py

也可以不改文件，用命令行覆盖：

    python run_construction_concat.py --input-dir "D:\\age_chunks" \\
        --concat-file "D:\\NVDRS_concat.csv" --output-dir "D:\\construction_out"

输出（OUTPUT_DIR 下）：

  construction_all_ages_narrative.csv     ★ 主输出：全部年龄段 construction + Narrative
  construction_age_<段>_narrative.csv     每个年龄段各一份（同样带 Narrative）
  construction_PersonID.csv               construction case 的 PersonID + age_band + 来源文件
  PersonID_not_found_in_concat.csv        concat 里找不到的 PersonID（空 = 全部找到）
  PersonID_multiple_narratives.csv        concat 里同一 PersonID 有多条不同 Narrative（空 = 没有）
  industry_values.csv                     Census2018_Industry 每个取值的行数（核对用）
  summary.csv                             各年龄段计数
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【输入 1】年龄段文件（18-67 岁）。两种填法，二选一。
#
# 填法 A：只填目录，脚本自动找里面文件名带 18_27 / 28-37 这类年龄段的 .csv
AGE_CHUNK_DIR = r""        # 例：r"D:\NVDRS\age_chunks"

# 填法 B：逐个列出文件（填了这个就忽略 AGE_CHUNK_DIR）
AGE_CHUNK_FILES = [
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_18_27.csv"
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_28_37.csv"
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_38_47.csv"
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_48_57.csv"
    r"",                   # 例：r"D:\NVDRS\age_chunks\nvdrs_age_58_67.csv"
]

# 【输入 2】NVDRS concat（里面有 PersonID 和 Narrative）
NVDRS_CONCAT_FILE = r""    # 例：r"D:\NVDRS\NVDRS_concat.csv"

# 【输出】输出目录（不存在会自动创建）
OUTPUT_DIR = r""           # 例：r"D:\NVDRS\construction_out"

# -----------------------------------------------------------------------------
# 以下为可选项，通常不用改
# -----------------------------------------------------------------------------

# 列名。留空 = 自动识别（不区分大小写、忽略下划线和空格）
INDUSTRY_COL = r""         # 自动找 Census2018_Industry
PERSON_ID_COL = r""        # 自动找 PersonID（年龄段文件和 concat 都用这个名字找）
INCIDENT_ID_COL = r""      # 自动找 IncidentID（Narrative 插在它后面）

# concat 里的 Narrative 列。留空 = 自动：
#   有叫 Narrative 的列就只用它；
#   没有就用所有名字里含 narrative 的列（例如 NarrativeCME、NarrativeLE），
#   按 concat 里的顺序一起插在 IncidentID 后面。
NARRATIVE_COLS: list[str] = []

# Census2018_Industry 等于以下任一值即算 construction（不区分大小写、忽略首尾空格）
CONSTRUCTION_VALUES = ["Construction"]

# 输入文件编码。NVDRS 的 Windows 导出常见 "utf-8" 或 "cp1252"
ENCODING = "utf-8"

# 每次读入内存的行数。越小越省内存
CHUNK_SIZE = 100_000

# =============================================================================
# 配置区结束
# =============================================================================

AGE_BAND_RE = re.compile(r"(?<!\d)(\d{2})[_\-](\d{2})(?!\d)")
MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>"}
BLANK_LABEL = "(空白)"

INDUSTRY_COL_CANDIDATES = (
    "census2018industry", "censusindustry2018", "industrycensus2018",
    "census2018ind", "industry2018",
)
PERSON_ID_CANDIDATES = ("personid", "personnumber", "victimid")
INCIDENT_ID_CANDIDATES = ("incidentid", "incidentnumber")


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def norm_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = re.sub(r"\s+", " ", str(value)).strip().lower()
    return "" if text in MISSING_TOKENS else text


def norm_colname(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def norm_id(series: pd.Series) -> pd.Series:
    """ID 统一成字符串：去空格，"12345.0" 当成 "12345"（Excel/浮点导出常见）。"""
    text = series.fillna("").astype(str).str.strip()
    return text.str.replace(r"^(\d+)\.0+$", r"\1", regex=True)


def band_from_name(path: Path) -> str | None:
    match = AGE_BAND_RE.search(path.stem)
    return f"{match.group(1)}-{match.group(2)}" if match else None


def find_column(columns, override: str, candidates, label: str, where: str,
                config_name: str) -> str:
    if override and override.strip():
        if override.strip() not in columns:
            fail(f"{where} 里没有 {config_name} 指定的列 {override.strip()!r}")
        return override.strip()
    normalised = {norm_colname(c): c for c in columns}
    for cand in candidates:
        if cand in normalised:
            return normalised[cand]
    fail(
        f"{where} 里找不到 {label} 列。\n"
        f"该文件的列有：{', '.join(map(str, columns))}\n"
        f"请在配置区填 {config_name} = r\"你的列名\"。"
    )


def find_narrative_columns(columns, override: list[str], where: str) -> list[str]:
    if override:
        missing = [c for c in override if c not in columns]
        if missing:
            fail(f"{where} 里没有 NARRATIVE_COLS 指定的列：{missing}")
        return list(override)
    exact = [c for c in columns if norm_colname(c) == "narrative"]
    if exact:
        return exact[:1]
    found = [c for c in columns if "narrative" in norm_colname(c)]
    if not found:
        fail(
            f"{where} 里找不到 Narrative 列。\n"
            f"该文件的列有：{', '.join(map(str, columns))}\n"
            "请在配置区填 NARRATIVE_COLS = [\"你的列名\"]。"
        )
    return found


# ---- 输入输出路径 ------------------------------------------------------------

def resolve_age_files(input_dir: str, input_files: list[str]) -> list[Path]:
    listed = [f.strip() for f in input_files if f and f.strip()]
    if listed:
        paths = []
        for item in listed:
            path = Path(item)
            if not path.is_file():
                fail(f"AGE_CHUNK_FILES 里这个路径找不到：\n  {path}")
            paths.append(path)
    elif input_dir and input_dir.strip():
        directory = Path(input_dir.strip())
        if not directory.is_dir():
            fail(f"AGE_CHUNK_DIR 不是一个目录：\n  {directory}")
        # 只收文件名里带年龄段的：age_distribution.csv、*_excluded.csv 这类汇总不算样本
        paths = [
            p for p in sorted(directory.glob("*.csv"))
            if band_from_name(p)
            and not re.search(r"excluded|distribution|narrative", p.name, re.I)
        ]
        if not paths:
            fail(
                f"AGE_CHUNK_DIR 里没有文件名带年龄段（如 18_27）的 .csv：\n  {directory}\n"
                "请改用 AGE_CHUNK_FILES 逐个列出。"
            )
    else:
        fail(
            "年龄段输入路径还没填。请打开本脚本，在「配置区」填写：\n"
            "    AGE_CHUNK_DIR   = r\"...\"      （填目录）\n"
            "  或\n"
            "    AGE_CHUNK_FILES = [r\"...\", ]  （逐个列出文件）\n"
            "也可以用命令行：--input-dir \"路径\"  或  --input 文件1 文件2 ..."
        )

    seen, unique = set(), []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def resolve_concat(concat_file: str) -> Path:
    if not concat_file or not concat_file.strip():
        fail(
            "NVDRS concat 路径还没填。请在「配置区」填写：\n"
            "    NVDRS_CONCAT_FILE = r\"...\"\n"
            "也可以用命令行：--concat-file \"路径\""
        )
    path = Path(concat_file.strip())
    if not path.is_file():
        fail(f"NVDRS_CONCAT_FILE 找不到：\n  {path}")
    return path


def resolve_output(output_dir: str) -> Path:
    if not output_dir or not output_dir.strip():
        fail(
            "输出路径还没填。请在「配置区」填写：\n"
            "    OUTPUT_DIR = r\"...\"\n"
            "也可以用命令行：--output-dir \"路径\""
        )
    path = Path(output_dir.strip())
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- 主流程 ------------------------------------------------------------------

def insert_after(frame: pd.DataFrame, anchor: str, new_cols: list[str]) -> pd.DataFrame:
    """把 new_cols 挪到 anchor 列的正后面。"""
    rest = [c for c in frame.columns if c not in new_cols]
    pos = rest.index(anchor) + 1
    return frame[rest[:pos] + new_cols + rest[pos:]]


def run(
    input_dir: str,
    input_files: list[str],
    concat_file: str,
    output_dir: str,
    *,
    industry_col: str = "",
    person_id_col: str = "",
    incident_id_col: str = "",
    narrative_cols: list[str] = (),
    construction_values=("Construction",),
    encoding: str = "utf-8",
    chunk_size: int = 100_000,
) -> dict:
    age_files = resolve_age_files(input_dir, input_files)
    concat_path = resolve_concat(concat_file)
    out_dir = resolve_output(output_dir)
    wanted = {norm_text(v) for v in construction_values} - {""}
    if not wanted:
        fail("CONSTRUCTION_VALUES 不能为空")

    concat_header = list(pd.read_csv(concat_path, dtype=str, nrows=0, encoding=encoding).columns)
    c_pid = find_column(concat_header, person_id_col, PERSON_ID_CANDIDATES,
                        "PersonID", "NVDRS concat", "PERSON_ID_COL")
    c_narr = find_narrative_columns(concat_header, list(narrative_cols), "NVDRS concat")

    print("=" * 78)
    print("年龄段输入：")
    for path in age_files:
        print(f"  [{band_from_name(path) or path.stem:>5}] {path}")
    print(f"NVDRS concat：{concat_path}")
    print(f"输出目录：    {out_dir}")
    print(f"construction 判定：Census2018_Industry ∈ {sorted(construction_values)}")
    print(f"匹配键：PersonID   取回列：{', '.join(c_narr)}")
    print("=" * 78)

    # ---- 第 1 步：年龄段文件 -> construction 行 + PersonID -------------------
    bands: list[tuple[str, Path, pd.DataFrame]] = []
    industry_counts: dict[str, int] = {}
    summary_rows: list[dict] = []
    columns_ref = None
    a_pid = a_iid = None

    for path in age_files:
        band = band_from_name(path) or path.stem
        header = list(pd.read_csv(path, dtype=str, nrows=0, encoding=encoding).columns)
        ind = find_column(header, industry_col, INDUSTRY_COL_CANDIDATES,
                          "Census2018_Industry", path.name, "INDUSTRY_COL")
        a_pid = find_column(header, person_id_col, PERSON_ID_CANDIDATES,
                            "PersonID", path.name, "PERSON_ID_COL")
        a_iid = find_column(header, incident_id_col, INCIDENT_ID_CANDIDATES,
                            "IncidentID", path.name, "INCIDENT_ID_COL")
        if columns_ref is None:
            columns_ref = header
        elif header != columns_ref:
            fail(
                f"{path.name} 的列和前面的文件不一致，合并会错位。\n"
                f"  前面：{columns_ref}\n  这个：{header}"
            )

        parts, n_read, n_blank = [], 0, 0
        for chunk in pd.read_csv(path, dtype=str, encoding=encoding, chunksize=chunk_size):
            for value, n in chunk[ind].fillna(BLANK_LABEL).value_counts().items():
                industry_counts[value] = industry_counts.get(value, 0) + int(n)
            industry = chunk[ind].map(norm_text)
            n_read += len(chunk)
            n_blank += int((industry == "").sum())
            parts.append(chunk.loc[industry.isin(wanted)])

        constr = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=header)
        constr["age_band"] = band
        bands.append((band, path, constr))
        summary_rows.append({
            "age_band": band, "input_file": str(path), "rows_read": n_read,
            "construction": len(constr), "industry_blank": n_blank,
        })
        pct = len(constr) / n_read * 100 if n_read else 0.0
        print(f"  [{band:>5}] {n_read:,} 行 -> construction {len(constr):,} ({pct:.2f}%)")

    all_constr = pd.concat([b[2] for b in bands], ignore_index=True)
    all_constr["_pid"] = norm_id(all_constr[a_pid])
    wanted_ids = set(all_constr["_pid"]) - {""}

    pid_table = all_constr[[a_pid, a_iid, "age_band"]].copy()
    pid_table["source_file"] = [str(p) for _, p, df in bands for _ in range(len(df))]
    pid_table.to_csv(out_dir / "construction_PersonID.csv", index=False, encoding=encoding)

    n_blank_pid = int((all_constr["_pid"] == "").sum())
    dup_pid = all_constr.loc[all_constr["_pid"].duplicated(keep=False) & (all_constr["_pid"] != "")]
    print(f"\nconstruction case 共 {len(all_constr):,} 行，PersonID {len(wanted_ids):,} 个")
    if n_blank_pid:
        print(f"  警告：{n_blank_pid:,} 行 PersonID 为空，无法匹配 Narrative")
    if not dup_pid.empty:
        print(f"  警告：{dup_pid['_pid'].nunique():,} 个 PersonID 在 construction 行里重复出现")

    # ---- 第 2 步：concat -> 这些 PersonID 的 Narrative ------------------------
    narr_parts, n_concat = [], 0
    for chunk in pd.read_csv(concat_path, dtype=str, encoding=encoding,
                             usecols=[c_pid] + c_narr, chunksize=chunk_size):
        n_concat += len(chunk)
        ids = norm_id(chunk[c_pid])
        mask = ids.isin(wanted_ids)
        if mask.any():
            hit = chunk.loc[mask, c_narr].copy()
            hit.insert(0, "_pid", ids[mask].values)
            narr_parts.append(hit)
    narr = (pd.concat(narr_parts, ignore_index=True) if narr_parts
            else pd.DataFrame(columns=["_pid"] + c_narr))

    # 同一 PersonID 在 concat 里多行：内容相同就只算一条；内容不同就记下来，取第一条非空
    narr = narr.drop_duplicates()
    conflicts = narr[narr["_pid"].duplicated(keep=False)]
    if not conflicts.empty:
        conflicts.rename(columns={"_pid": "PersonID"}).to_csv(
            out_dir / "PersonID_multiple_narratives.csv", index=False, encoding=encoding)
        narr["_has"] = narr[c_narr].notna().any(axis=1)
        narr = narr.sort_values("_has", ascending=False, kind="stable").drop(columns="_has")
    else:
        pd.DataFrame(columns=["PersonID"] + c_narr).to_csv(
            out_dir / "PersonID_multiple_narratives.csv", index=False, encoding=encoding)
    narr = narr.drop_duplicates("_pid", keep="first")

    # ---- 第 3 步：按 PersonID 合并，Narrative 放在 IncidentID 后 ----------------
    base = all_constr.drop(columns=[c for c in c_narr if c in all_constr.columns])
    merged = base.merge(narr, on="_pid", how="left", validate="many_to_one")
    found = set(narr["_pid"])
    missing = all_constr.loc[~all_constr["_pid"].isin(found), [a_pid, a_iid, "age_band"]]
    missing.to_csv(out_dir / "PersonID_not_found_in_concat.csv", index=False, encoding=encoding)
    merged = insert_after(merged.drop(columns="_pid"), a_iid, c_narr)

    main_path = out_dir / "construction_all_ages_narrative.csv"
    merged.to_csv(main_path, index=False, encoding=encoding)
    for band, _, _ in bands:
        merged.loc[merged["age_band"] == band].to_csv(
            out_dir / f"construction_age_{band.replace('-', '_')}_narrative.csv",
            index=False, encoding=encoding)

    summary = pd.DataFrame(summary_rows)
    summary["narrative_found"] = [
        int(merged.loc[merged["age_band"] == b, c_narr].notna().any(axis=1).sum())
        for b, _, _ in bands
    ]
    summary.to_csv(out_dir / "summary.csv", index=False, encoding=encoding)
    pd.DataFrame(
        [{"Census2018_Industry": v, "n": n, "is_construction": norm_text(v) in wanted}
         for v, n in sorted(industry_counts.items(), key=lambda kv: -kv[1])]
    ).to_csv(out_dir / "industry_values.csv", index=False, encoding=encoding)

    # ---- 报告 --------------------------------------------------------------
    print("\n" + "=" * 78)
    print(f"NVDRS concat 扫描 {n_concat:,} 行")
    print(f"  PersonID 找到 Narrative：{len(found):,} / {len(wanted_ids):,}")
    if len(missing):
        print(f"  警告：{len(missing):,} 行在 concat 里找不到 PersonID，"
              "见 PersonID_not_found_in_concat.csv（Narrative 留空）")
    if not conflicts.empty:
        print(f"  警告：{conflicts['_pid'].nunique():,} 个 PersonID 在 concat 里有多条不同的 "
              "Narrative，已取第一条非空，全部见 PersonID_multiple_narratives.csv")
    if len(merged) != len(all_constr):
        print(f"  警告：合并前 {len(all_constr):,} 行，合并后 {len(merged):,} 行")
    print("=" * 78)
    print(summary[["age_band", "rows_read", "construction", "narrative_found"]]
          .to_string(index=False))
    print(f"\n列顺序：... {a_iid}, {', '.join(c_narr)}, ...")
    print(f"★ 主输出：{main_path}")

    return {
        "summary": summary,
        "merged": merged,
        "n_construction": len(all_constr),
        "n_found": len(found),
        "n_missing": len(missing),
        "narrative_cols": c_narr,
        "output_dir": out_dir,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按 Census2018_Industry 筛 construction，用 PersonID 从 NVDRS concat 合并 Narrative",
    )
    parser.add_argument("--input-dir", default=None, help="覆盖配置区的 AGE_CHUNK_DIR")
    parser.add_argument("--input", nargs="+", default=None, help="覆盖配置区的 AGE_CHUNK_FILES")
    parser.add_argument("--concat-file", default=None, help="覆盖配置区的 NVDRS_CONCAT_FILE")
    parser.add_argument("--output-dir", default=None, help="覆盖配置区的 OUTPUT_DIR")
    parser.add_argument("--industry-col", default=None)
    parser.add_argument("--person-id-col", default=None)
    parser.add_argument("--incident-id-col", default=None)
    parser.add_argument("--narrative-cols", nargs="+", default=None)
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    args = parser.parse_args(argv)

    pick = lambda cli, cfg: cli if cli is not None else cfg  # noqa: E731
    result = run(
        pick(args.input_dir, AGE_CHUNK_DIR),
        pick(args.input, AGE_CHUNK_FILES),
        pick(args.concat_file, NVDRS_CONCAT_FILE),
        pick(args.output_dir, OUTPUT_DIR),
        industry_col=pick(args.industry_col, INDUSTRY_COL),
        person_id_col=pick(args.person_id_col, PERSON_ID_COL),
        incident_id_col=pick(args.incident_id_col, INCIDENT_ID_COL),
        narrative_cols=pick(args.narrative_cols, NARRATIVE_COLS),
        construction_values=CONSTRUCTION_VALUES,
        encoding=pick(args.encoding, ENCODING),
        chunk_size=pick(args.chunk_size, CHUNK_SIZE),
    )
    return 0 if result["n_construction"] else 1


if __name__ == "__main__":
    sys.exit(main())
