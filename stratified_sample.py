#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对筛好的 construction 数据做分层随机抽样，抽出一个子集。

默认输入是 run_construction_concat.py 的主输出 construction_all_ages_narrative.csv，
默认按 age_band（18-27 / 28-37 / 38-47 / 48-57 / 58-67）分层。

三种抽法（SAMPLING 选一种）：

  "proportional_n"    总共抽 SAMPLE_N 行，按各层在总体中的比例分配
                      （最大余数法取整，各层加起来正好等于 SAMPLE_N）
  "proportional_frac" 每层都抽 SAMPLE_FRAC 比例（例如 0.2 = 每层抽 20%）
  "equal_n"           每层都抽 SAMPLE_N_PER_STRATUM 行（层太小就整层全取）

随机种子固定（RANDOM_SEED），同样的输入和设置每次抽出同样的行，可复现。

用法：把下面「配置区」的路径填好，然后直接运行

    python stratified_sample.py

也可以用命令行覆盖：

    python stratified_sample.py --input "D:\\construction_all_ages_narrative.csv" \\
        --output-dir "D:\\sample_out" --sampling proportional_n --n 500

输出（OUTPUT_DIR 下）：

  <输入文件名>_sample.csv        ★ 抽中的子集（多一列 sample_weight = 层总数 / 层抽中数）
  <输入文件名>_not_sampled.csv   没抽中的行（和子集合起来 = 抽样总体）
  sample_allocation.csv          每层：总体行数、抽中行数、抽样比例、权重
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd

# =============================================================================
# 配置区 —— 把路径填进下面的空白引号里
# =============================================================================

# 【输入】筛好的数据（例：run_construction_concat.py 输出的主文件）
INPUT_FILE = r""           # 例：r"D:\NVDRS\construction_out\construction_all_ages_narrative.csv"

# 【输出】输出目录（不存在会自动创建）
OUTPUT_DIR = r""           # 例：r"D:\NVDRS\construction_sample"

# 分层变量。可以写多个，按组合分层，例如 ["age_band", "Sex"]
STRATA_COLS = ["age_band"]

# 抽法："proportional_n" / "proportional_frac" / "equal_n"
SAMPLING = "proportional_n"
SAMPLE_N = 500                 # proportional_n：总共抽多少行
SAMPLE_FRAC = 0.2              # proportional_frac：每层抽的比例（0-1）
SAMPLE_N_PER_STRATUM = 100     # equal_n：每层抽多少行

# 随机种子。改了种子就是另一组随机样本
RANDOM_SEED = 2024

# -----------------------------------------------------------------------------
# 以下为可选项，通常不用改
# -----------------------------------------------------------------------------

# 抽样前先去掉 Narrative 为空的行（做 narrative 编码时通常需要）。
# 留空 = 不去；填列名 = 去掉这些列全为空的行，例如 ["Narrative"]
REQUIRE_NONEMPTY_COLS: list[str] = []

# 分层变量为空的行怎么处理：
#   "own_stratum" -> 空值自成一层 "(空白)"（默认，不丢行）
#   "drop"        -> 不参与抽样
MISSING_STRATUM = "own_stratum"

ENCODING = "utf-8"

# =============================================================================
# 配置区结束
# =============================================================================

BLANK_LABEL = "(空白)"


def fail(message: str) -> "NoReturn":  # noqa: F821
    print(f"\n错误：{message}\n", file=sys.stderr)
    sys.exit(2)


def is_blank(series: pd.Series) -> pd.Series:
    return series.isna() | (series.astype(str).str.strip() == "")


def allocate_proportional(sizes: pd.Series, total: int) -> pd.Series:
    """最大余数法：按比例分配 total，各层之和正好等于 total，且不超过层大小。"""
    population = int(sizes.sum())
    if total >= population:
        return sizes.copy()
    exact = sizes * total / population
    alloc = exact.apply(math.floor).astype(int)
    remainder = total - int(alloc.sum())
    # 余数按小数部分从大到小分；同分时按层名排序，保证可复现
    order = (exact - alloc).sort_values(ascending=False, kind="stable").index
    for key in order:
        if remainder == 0:
            break
        if alloc[key] < sizes[key]:
            alloc[key] += 1
            remainder -= 1
    return alloc


def run(
    input_file: str,
    output_dir: str,
    *,
    strata_cols=("age_band",),
    sampling: str = "proportional_n",
    sample_n: int = 500,
    sample_frac: float = 0.2,
    sample_n_per_stratum: int = 100,
    random_seed: int = 2024,
    require_nonempty_cols=(),
    missing_stratum: str = "own_stratum",
    encoding: str = "utf-8",
) -> dict:
    if not input_file or not str(input_file).strip():
        fail("输入路径还没填。请在「配置区」填写 INPUT_FILE = r\"...\"，"
             "或用命令行 --input \"路径\"")
    in_path = Path(str(input_file).strip())
    if not in_path.is_file():
        fail(f"INPUT_FILE 找不到：\n  {in_path}")
    if not output_dir or not str(output_dir).strip():
        fail("输出路径还没填。请在「配置区」填写 OUTPUT_DIR = r\"...\"，"
             "或用命令行 --output-dir \"路径\"")
    out_dir = Path(str(output_dir).strip())
    out_dir.mkdir(parents=True, exist_ok=True)

    if sampling not in {"proportional_n", "proportional_frac", "equal_n"}:
        fail(f"SAMPLING 只能是 proportional_n / proportional_frac / equal_n，现在是 {sampling!r}")
    if sampling == "proportional_frac" and not 0 < sample_frac <= 1:
        fail(f"SAMPLE_FRAC 要在 0 到 1 之间，现在是 {sample_frac}")
    if sampling == "proportional_n" and sample_n <= 0:
        fail(f"SAMPLE_N 要大于 0，现在是 {sample_n}")
    if sampling == "equal_n" and sample_n_per_stratum <= 0:
        fail(f"SAMPLE_N_PER_STRATUM 要大于 0，现在是 {sample_n_per_stratum}")
    if missing_stratum not in {"own_stratum", "drop"}:
        fail(f"MISSING_STRATUM 只能是 own_stratum / drop，现在是 {missing_stratum!r}")

    data = pd.read_csv(in_path, dtype=str, keep_default_na=False, na_values=[""],
                       encoding=encoding)
    strata_cols = list(strata_cols)
    if not strata_cols:
        fail("STRATA_COLS 不能为空")
    missing_cols = [c for c in strata_cols + list(require_nonempty_cols) if c not in data.columns]
    if missing_cols:
        fail(f"输入文件里没有这些列：{missing_cols}\n该文件的列有：{', '.join(data.columns)}")

    n_input = len(data)
    frame = data
    if require_nonempty_cols:
        keep = ~pd.concat([is_blank(frame[c]) for c in require_nonempty_cols], axis=1).all(axis=1)
        frame = frame.loc[keep]
    n_after_nonempty = len(frame)

    strata_blank = pd.concat([is_blank(frame[c]) for c in strata_cols], axis=1).any(axis=1)
    if missing_stratum == "drop":
        frame = frame.loc[~strata_blank]
    frame = frame.copy()
    for col in strata_cols:
        frame[col] = frame[col].where(~is_blank(frame[col]), BLANK_LABEL)
    if frame.empty:
        fail("抽样总体为空（检查 REQUIRE_NONEMPTY_COLS / MISSING_STRATUM 的设置）")

    sizes = frame.groupby(strata_cols, sort=True).size()
    if sampling == "proportional_n":
        alloc = allocate_proportional(sizes, int(sample_n))
    elif sampling == "proportional_frac":
        alloc = sizes.apply(lambda s: min(s, int(round(s * sample_frac))))
    else:
        alloc = sizes.clip(upper=int(sample_n_per_stratum))

    picked = []
    # groupby 和 sizes 都按层名排序，第 i 层一一对应
    for i, (_, group) in enumerate(frame.groupby(strata_cols, sort=True)):
        k = int(alloc.iloc[i])
        if k:
            # 每层用 种子+层序号，改一层的设置不会连带改变其他层抽中的人
            picked.append(group.sample(n=k, random_state=random_seed + i))
    sample = pd.concat(picked) if picked else frame.iloc[0:0]
    sample = sample.sort_index()

    weight = (sizes / alloc.where(alloc > 0)).rename("sample_weight")
    sample = sample.join(weight, on=strata_cols)
    not_sampled = frame.drop(index=sample.index)

    stem = in_path.stem
    sample_path = out_dir / f"{stem}_sample.csv"
    rest_path = out_dir / f"{stem}_not_sampled.csv"
    sample.to_csv(sample_path, index=False, encoding=encoding)
    not_sampled.to_csv(rest_path, index=False, encoding=encoding)

    table = pd.DataFrame({"population": sizes, "sampled": alloc}).reset_index()
    table["sampling_rate"] = (table["sampled"] / table["population"]).round(4)
    table["sample_weight"] = (table["population"] / table["sampled"].where(table["sampled"] > 0)).round(4)
    table["short"] = table["sampled"] < {
        "proportional_n": 0, "proportional_frac": 0, "equal_n": sample_n_per_stratum,
    }[sampling]
    table.to_csv(out_dir / "sample_allocation.csv", index=False, encoding=encoding)

    # ---- 报告 --------------------------------------------------------------
    print("=" * 78)
    print(f"输入：{in_path}  （{n_input:,} 行）")
    if require_nonempty_cols:
        print(f"  去掉 {', '.join(require_nonempty_cols)} 为空的行后：{n_after_nonempty:,} 行")
    if int(strata_blank.sum()):
        action = "已排除" if missing_stratum == "drop" else f"归入 {BLANK_LABEL} 层"
        print(f"  分层变量为空：{int(strata_blank.sum()):,} 行，{action}")
    print(f"抽样总体：{len(frame):,} 行   分层：{' × '.join(strata_cols)}   "
          f"抽法：{sampling}   种子：{random_seed}")
    print("=" * 78)
    print(table.drop(columns="short").to_string(index=False))
    if table["short"].any():
        print(f"\n  提醒：{int(table['short'].sum())} 层不够 {sample_n_per_stratum} 行，已整层全取")
    if sampling == "proportional_n" and sample_n > len(frame):
        print(f"\n  提醒：SAMPLE_N={sample_n:,} 大于总体 {len(frame):,}，已全部取出")
    print(f"\n抽中 {len(sample):,} 行 -> {sample_path}")
    print(f"未抽中 {len(not_sampled):,} 行 -> {rest_path}")
    print(f"分配表 -> {out_dir / 'sample_allocation.csv'}")

    return {"sample": sample, "not_sampled": not_sampled, "allocation": table,
            "population": len(frame)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="分层随机抽样")
    parser.add_argument("--input", default=None, help="覆盖配置区的 INPUT_FILE")
    parser.add_argument("--output-dir", default=None, help="覆盖配置区的 OUTPUT_DIR")
    parser.add_argument("--strata", nargs="+", default=None, help="覆盖 STRATA_COLS")
    parser.add_argument("--sampling", choices=["proportional_n", "proportional_frac", "equal_n"],
                        default=None)
    parser.add_argument("--n", type=int, default=None, help="覆盖 SAMPLE_N")
    parser.add_argument("--frac", type=float, default=None, help="覆盖 SAMPLE_FRAC")
    parser.add_argument("--n-per-stratum", type=int, default=None,
                        help="覆盖 SAMPLE_N_PER_STRATUM")
    parser.add_argument("--seed", type=int, default=None, help="覆盖 RANDOM_SEED")
    parser.add_argument("--require-nonempty", nargs="+", default=None,
                        help="覆盖 REQUIRE_NONEMPTY_COLS，例：--require-nonempty Narrative")
    parser.add_argument("--missing-stratum", choices=["own_stratum", "drop"], default=None)
    parser.add_argument("--encoding", default=None)
    args = parser.parse_args(argv)

    pick = lambda cli, cfg: cli if cli is not None else cfg  # noqa: E731
    result = run(
        pick(args.input, INPUT_FILE),
        pick(args.output_dir, OUTPUT_DIR),
        strata_cols=pick(args.strata, STRATA_COLS),
        sampling=pick(args.sampling, SAMPLING),
        sample_n=pick(args.n, SAMPLE_N),
        sample_frac=pick(args.frac, SAMPLE_FRAC),
        sample_n_per_stratum=pick(args.n_per_stratum, SAMPLE_N_PER_STRATUM),
        random_seed=pick(args.seed, RANDOM_SEED),
        require_nonempty_cols=pick(args.require_nonempty, REQUIRE_NONEMPTY_COLS),
        missing_stratum=pick(args.missing_stratum, MISSING_STRATUM),
        encoding=pick(args.encoding, ENCODING),
    )
    return 0 if len(result["sample"]) else 1


if __name__ == "__main__":
    sys.exit(main())
