#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把年龄段 CSV 按 IncidentYear 拆成 2018-2024 各年。

只看 IncidentYear 一列，不碰 occupation / industry。

输出结构（年份目录里的文件名与输入完全一致，下一步可直接指过去）：

    OUTPUT_DIR/
      2018/  nvdrs_age_18_27.csv  nvdrs_age_28_37.csv  ...  nvdrs_age_58_67.csv
      2019/  ...
      ...
      2024/  ...
      _all_ages/   nvdrs_2018_all_ages.csv ... nvdrs_2024_all_ages.csv   合并版
      _excluded/   out_of_range.csv / blank_year.csv / unparseable_year.csv
      year_distribution.csv        每个年份值的行数
      summary_by_year_and_age.csv  年份 × 年龄段 计数表
      file_map.csv                 输入 -> 输出 完整路径对照

跑完之后，下一步（例如电工拆分）这样接：

    python run_electrician_split.py --input-dir "OUTPUT_DIR\\2024"

单文件，只依赖 pandas。分块读写，1.7 GB 输入也只占几百 MB 内存。

用法：把「配置区」的 INPUT_DIR 和 OUTPUT_DIR 填好，然后运行

    python split_by_year.py
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

# 【必填 1】输入。填法 A：目录（自动找 nvdrs_age_18_27.csv 等年龄段文件）
INPUT_DIR = r""          # 例：r"D:\School_project\Project\NVDRS\age_chunks"

# 填法 B：逐个列出（填了就忽略 INPUT_DIR）
INPUT_FILES = [
    r"",                 # 例：r"D:\...\nvdrs_age_18_27.csv"
    r"",                 # nvdrs_age_28_37.csv
    r"",                 # nvdrs_age_38_47.csv
    r"",                 # nvdrs_age_48_57.csv
    r"",                 # nvdrs_age_58_67.csv
]

# 【必填 2】输出目录（不存在会自动创建，里面按年份建子目录）
OUTPUT_DIR = r""         # 例：r"D:\School_project\Project\NVDRS\Label_year"

# -----------------------------------------------------------------------------
# 以下通常不用改
# -----------------------------------------------------------------------------

# 要拆出来的年份
YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]

# IncidentYear 列名。留空 = 自动识别
YEAR_COL = r""

# 是否把范围外 / 空白 / 读不出的年份单独写出来备查（放在 _excluded/ 里）
WRITE_EXCLUDED = True

ENCODING = "utf-8"
CHUNK_SIZE = 50_000

# =============================================================================
# 配置区结束
# =============================================================================

# IncidentYear 的候选列名（归一化后比较：去掉大小写、空格、下划线）
YEAR_COL_CANDIDATES = (
    "incidentyear",
    "incyear",
    "yearofincident",
    "incidentyearc",
)

# 这些列长得像年份但【不是】incident year。跨年案例中三者可以不同，
# 拿它们顶替会算错，所以只在报错信息里点名，绝不自动使用。
WRONG_YEAR_COLS = {
    "deathyear": "死亡年份",
    "yearofdeath": "死亡年份",
    "injuryyear": "受伤年份",
    "yearofinjury": "受伤年份",
    "filingyear": "归档年份",
    "reportyear": "报告年份",
    "abstractionyear": "摘录年份",
    "birthyear": "出生年份",
}

# 目录扫描时跳过的非样本文件
SKIP_NAME_PATTERNS = (
    re.compile(r"age_distribution", re.I),
    re.compile(r"_excluded", re.I),
    re.compile(r"all_ages", re.I),
    re.compile(r"^summary", re.I),
    re.compile(r"^file_map", re.I),
    re.compile(r"^year_distribution", re.I),
    re.compile(r"breakdown", re.I),
)

AGE_BAND_RE = re.compile(r"(\d{2})[_\-](\d{2})")

BLANK = "BLANK"
UNPARSEABLE = "UNPARSEABLE"
OUT_OF_RANGE = "OUT_OF_RANGE"
MISSING_TOKENS = {"", ".", "-", "--", "nan", "none", "null", "<na>"}


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


def norm_colname(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def parse_year(value) -> "int | str":
    """取出 4 位年份，或 BLANK / UNPARSEABLE。

    接受纯年份（"2024"、"2024.0"、2024）和含年份的日期（"2024-05-13"、
    "5/13/2024"）。两位数年份（"24"）【不猜】，归为 UNPARSEABLE 并单独报出。
    """
    text = norm_text(value)
    if not text:
        return BLANK
    bare = re.fullmatch(r"(\d{4})(?:\.0+)?", text)
    if bare:
        return int(bare.group(1))
    years = re.findall(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)", text)
    if len(set(years)) == 1:
        return int(years[0])
    return UNPARSEABLE


def band_of(path: Path) -> str:
    """文件名里的年龄段，如 '18_27'；认不出就用文件名。"""
    m = AGE_BAND_RE.search(path.stem)
    return f"{m.group(1)}_{m.group(2)}" if m else path.stem


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def find_year_column(columns, override: str = "") -> str:
    if override and override.strip():
        name = override.strip()
        if name not in columns:
            fail(f"YEAR_COL 指定的列 {name!r} 不在文件里。\n"
                 f"该文件的列有：{', '.join(map(str, columns))}")
        return name

    normalised = {norm_colname(c): c for c in columns}
    for cand in YEAR_COL_CANDIDATES:
        if cand in normalised:
            return normalised[cand]
    for cand in YEAR_COL_CANDIDATES:
        for norm, original in normalised.items():
            if cand in norm and norm not in WRONG_YEAR_COLS:
                return original

    look_alikes = {
        original: WRONG_YEAR_COLS[norm]
        for norm, original in normalised.items()
        if norm in WRONG_YEAR_COLS
    }
    extra = ""
    if look_alikes:
        extra = (
            "\n找到了这些像年份的列，但它们【不是】incident year（跨年案例中会不同，"
            "不能顶替）：\n  "
            + "\n  ".join(f"{c}（{what}）" for c, what in look_alikes.items())
        )
    fail(
        f"找不到 IncidentYear 列（找过：{', '.join(YEAR_COL_CANDIDATES)}）。{extra}\n"
        f"该文件的列有：{', '.join(map(str, columns))}\n"
        "请在配置区填 YEAR_COL = r\"你的列名\"。"
    )


def resolve_inputs(input_dir: str, input_files: list[str]) -> list[Path]:
    listed = [f.strip() for f in input_files if f and f.strip()]
    if listed:
        paths = []
        for item in listed:
            path = Path(item)
            if not path.is_file():
                fail(f"INPUT_FILES 里这个路径找不到：\n  {path}")
            paths.append(path)
    elif input_dir and input_dir.strip():
        directory = Path(input_dir.strip())
        if not directory.is_dir():
            fail(f"INPUT_DIR 不是一个目录：\n  {directory}")
        every = sorted(directory.glob("*.csv"))
        if not every:
            fail(f"INPUT_DIR 里没有 .csv 文件：\n  {directory}")
        paths, skipped = [], []
        for path in every:
            if any(p.search(path.name) for p in SKIP_NAME_PATTERNS):
                skipped.append(path.name)
            else:
                paths.append(path)
        if skipped:
            print("已跳过以下非样本文件（汇总表 / 排除行 / 本脚本的输出）：")
            for name in skipped:
                print(f"  - {name}")
        if not paths:
            fail(f"目录里的 .csv 全部被当作非样本文件跳过了：\n  {directory}\n"
                 "请改用 INPUT_FILES 逐个列出。")
    else:
        fail(
            "输入路径还没填。请打开本脚本，在「配置区」填写：\n"
            "    INPUT_DIR   = r\"...\"      （填目录）\n"
            "  或\n"
            "    INPUT_FILES = [r\"...\", ]  （逐个列出）\n"
            "也可以用命令行：--input-dir \"路径\""
        )

    seen, unique = set(), []
    for path in paths:
        key = path.resolve()
        if key in seen:
            print(f"  （重复路径，只算一次）{path.name}")
            continue
        seen.add(key)
        unique.append(path)
    return unique


def resolve_output(output_dir: str) -> Path:
    if not output_dir or not output_dir.strip():
        fail(
            "输出路径还没填。请在「配置区」填写：\n"
            "    OUTPUT_DIR = r\"...\"\n"
            "也可以用命令行：--output-dir \"路径\""
        )
    path = Path(output_dir.strip())
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"创建输出目录失败：\n  {path}\n  {exc}")
    return path


class Writer:
    """按需创建的追加写入器：表头只写一次。"""

    def __init__(self, path: Path, encoding: str):
        self.path, self.encoding, self.handle, self.rows = path, encoding, None, 0

    def write(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        if self.handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, "w", newline="", encoding=self.encoding)
            frame.to_csv(self.handle, index=False, header=True)
        else:
            frame.to_csv(self.handle, index=False, header=False)
        self.rows += len(frame)

    def finish(self, columns) -> None:
        if self.handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=columns).to_csv(
                self.path, index=False, encoding=self.encoding
            )
        else:
            self.handle.close()
            self.handle = None


def run(
    input_dir: str,
    input_files: list[str],
    output_dir: str,
    *,
    years=tuple(YEARS),
    year_col: str = "",
    write_excluded: bool = True,
    encoding: str = "utf-8",
    chunk_size: int = 50_000,
) -> dict:
    wanted = sorted({int(y) for y in years})
    if not wanted:
        fail("YEARS 是空的，至少要有一个年份")

    files = resolve_inputs(input_dir, input_files)
    out_dir = resolve_output(output_dir)

    print("=" * 78)
    print("输入文件：")
    for path in files:
        print(f"  [{band_of(path):>5}] {path}  ({path.stat().st_size / 1048576:,.0f} MB)")
    print(f"\n输出目录：{out_dir}")
    print(f"\n只看 IncidentYear 一列，不涉及 occupation / industry")
    print(f"要拆出的年份：{wanted[0]}-{wanted[-1]}  {wanted}")
    print("=" * 78)

    # 合并版（每年一个，跨全部年龄段）在整个过程中保持打开
    merged = {
        y: Writer(out_dir / "_all_ages" / f"nvdrs_{y}_all_ages.csv", encoding)
        for y in wanted
    }
    excluded = {}
    if write_excluded:
        excluded = {
            OUT_OF_RANGE: Writer(out_dir / "_excluded" / "out_of_range.csv", encoding),
            BLANK: Writer(out_dir / "_excluded" / "blank_year.csv", encoding),
            UNPARSEABLE: Writer(out_dir / "_excluded" / "unparseable_year.csv", encoding),
        }

    merged_columns = None
    year_counts: dict[object, int] = {}
    summary_rows: list[dict] = []
    rows_map: list[dict] = []
    total_read = 0

    try:
        for path in files:
            band = band_of(path)
            header = pd.read_csv(path, dtype=str, nrows=0, encoding=encoding)
            column = find_year_column(header.columns, year_col)

            out_columns = list(header.columns)
            if "incident_year_band" not in out_columns:
                out_columns.append("incident_year_band")
            if merged_columns is None:
                merged_columns = out_columns
            elif out_columns != merged_columns:
                fail(
                    f"{path.name} 的列和前面的文件不一致，合并会错位。\n"
                    f"  前面的列：{merged_columns}\n  这个文件：{out_columns}"
                )

            # 这一个年龄段在各年的输出，文件名与输入保持一致
            per_year = {
                y: Writer(out_dir / str(y) / path.name, encoding) for y in wanted
            }
            n_read = 0
            counts = {y: 0 for y in wanted}
            n_out = n_blank = n_bad = 0

            for chunk in pd.read_csv(
                path, dtype=str, keep_default_na=True, encoding=encoding,
                chunksize=chunk_size,
            ):
                n_read += len(chunk)
                parsed = chunk[column].map(parse_year)
                for value, n in parsed.value_counts().items():
                    year_counts[value] = year_counts.get(value, 0) + int(n)

                for year in wanted:
                    subset = chunk.loc[parsed == year]
                    if subset.empty:
                        continue
                    subset = subset.copy()
                    subset["incident_year_band"] = f"{year}|{band}"
                    counts[year] += len(subset)
                    per_year[year].write(subset[out_columns])
                    merged[year].write(subset[out_columns])

                is_blank = parsed == BLANK
                is_bad = parsed == UNPARSEABLE
                is_out = parsed.map(
                    lambda v: isinstance(v, int) and v not in wanted
                )
                n_blank += int(is_blank.sum())
                n_bad += int(is_bad.sum())
                n_out += int(is_out.sum())

                if write_excluded:
                    for key, mask in (
                        (OUT_OF_RANGE, is_out), (BLANK, is_blank), (UNPARSEABLE, is_bad)
                    ):
                        part = chunk.loc[mask]
                        if part.empty:
                            continue
                        part = part.copy()
                        part["incident_year_band"] = f"EXCLUDED|{band}"
                        part["exclusion_reason"] = key
                        excluded[key].write(part[out_columns + ["exclusion_reason"]])

            for year in wanted:
                per_year[year].finish(out_columns)
                rows_map.append({
                    "year": year, "age_band": band,
                    "input_file": str(path),
                    "output_file": str(per_year[year].path),
                    "rows": counts[year],
                })
                summary_rows.append({
                    "year": year, "age_band": band,
                    "rows": counts[year], "input_file": str(path),
                })

            total_read += n_read
            kept = sum(counts.values())
            print(
                f"  [{band:>5}] {n_read:,} 行 -> 保留 {kept:,}，"
                f"排除 {n_read - kept:,}"
                f"（范围外 {n_out:,} / 空白 {n_blank:,} / 读不出 {n_bad:,}）"
            )
            print("           " + "  ".join(
                f"{y}:{counts[y]:,}" for y in wanted
            ))
    finally:
        for writer in merged.values():
            writer.finish(merged_columns or [])
        for writer in excluded.values():
            writer.finish((merged_columns or []) + ["exclusion_reason"])

    for year in wanted:
        rows_map.append({
            "year": year, "age_band": "ALL",
            "input_file": "; ".join(str(p) for p in files),
            "output_file": str(merged[year].path),
            "rows": merged[year].rows,
        })

    summary = pd.DataFrame(summary_rows)
    pivot = summary.pivot_table(
        index="year", columns="age_band", values="rows", aggfunc="sum", fill_value=0
    )
    pivot["TOTAL"] = pivot.sum(axis=1)
    pivot.to_csv(out_dir / "summary_by_year_and_age.csv")
    pd.DataFrame(rows_map).to_csv(out_dir / "file_map.csv", index=False)

    dist = pd.DataFrame(
        [
            {
                "IncidentYear": str(v),
                "n": n,
                "kept": isinstance(v, int) and v in wanted,
            }
            for v, n in sorted(year_counts.items(), key=lambda kv: str(kv[0]))
        ],
        columns=["IncidentYear", "n", "kept"],
    )
    dist.to_csv(out_dir / "year_distribution.csv", index=False)

    # ---- 报告 --------------------------------------------------------------
    print("\n" + "=" * 78)
    print("年份 × 年龄段 行数")
    print("=" * 78)
    print(pivot.to_string())

    print("\n" + "=" * 78)
    print("各年合并文件（跨全部年龄段）")
    print("=" * 78)
    for year in wanted:
        print(f"  {year}  {merged[year].rows:>12,} 行   {merged[year].path}")

    kept_total = sum(merged[y].rows for y in wanted)
    n_excluded = total_read - kept_total
    print(f"\n读入总行数：{total_read:,}")
    print(f"保留（{wanted[0]}-{wanted[-1]}）：{kept_total:,}")
    print(f"排除：{n_excluded:,}")
    if write_excluded:
        for key, label in ((OUT_OF_RANGE, "年份在范围外"), (BLANK, "年份空白"),
                           (UNPARSEABLE, "年份读不出")):
            if excluded[key].rows:
                print(f"  {label}：{excluded[key].rows:,}  -> {excluded[key].path}")
        recovered = kept_total + sum(w.rows for w in excluded.values())
        if recovered == total_read:
            print("  （保留 + 排除 = 读入，没有行丢失或重复）")
        else:
            print(f"  警告：对不上，差 {total_read - recovered:,} 行")

    if kept_total == 0:
        print(
            "\n  警告：没有任何行落在指定年份里。请看 year_distribution.csv —— "
            "如果 IncidentYear 实际是别的年份范围，改配置区的 YEARS。"
        )
    else:
        print("\n" + "=" * 78)
        print("IncidentYear 实际取值分布")
        print("=" * 78)
        print(dist.to_string(index=False))

    print("\n" + "=" * 78)
    print("输入文件 -> 输出文件 对照")
    print("=" * 78)
    for row in rows_map:
        tag = "合并" if row["age_band"] == "ALL" else row["age_band"]
        print(f"  [{row['year']}] [{tag:>5}] {row['rows']:>10,} 行")
        if row["age_band"] != "ALL":
            print(f"            输入: {row['input_file']}")
        print(f"            输出: {row['output_file']}")

    print("\n另外写出：")
    print(f"  {out_dir / 'summary_by_year_and_age.csv'}")
    print(f"  {out_dir / 'file_map.csv'}")
    print(f"  {out_dir / 'year_distribution.csv'}")
    print(f"\n下一步（例如电工拆分）把 --input-dir 指到某一年的目录即可，例如：")
    print(f"  {out_dir / str(wanted[-1])}")

    return {
        "input_files": files,
        "output_dir": out_dir,
        "years": wanted,
        "year_rows": {y: merged[y].rows for y in wanted},
        "summary": pivot,
        "year_distribution": dist,
        "rows_read": total_read,
        "rows_kept": kept_total,
        "rows_excluded": n_excluded,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按 IncidentYear 把年龄段 CSV 拆成各年（只看这一列）"
    )
    parser.add_argument("--input-dir", default=None)
    parser.add_argument("--input", nargs="+", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--years", nargs="+", type=int, default=None)
    parser.add_argument("--year-col", default=None)
    parser.add_argument("--no-excluded", action="store_true",
                        help="不写 _excluded/ 里的排除行文件")
    parser.add_argument("--encoding", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    args = parser.parse_args(argv)

    pick = lambda a, b: a if a is not None else b  # noqa: E731
    result = run(
        pick(args.input_dir, INPUT_DIR),
        pick(args.input, INPUT_FILES),
        pick(args.output_dir, OUTPUT_DIR),
        years=pick(args.years, YEARS),
        year_col=pick(args.year_col, YEAR_COL),
        write_excluded=(not args.no_excluded) if args.no_excluded else WRITE_EXCLUDED,
        encoding=pick(args.encoding, ENCODING),
        chunk_size=pick(args.chunk_size, CHUNK_SIZE),
    )
    return 0 if result["rows_kept"] else 1


if __name__ == "__main__":
    sys.exit(main())
