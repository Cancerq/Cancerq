#!/usr/bin/env python3
"""Tests for nvdrs_state_map.py.

The question is "which states did the 2018-2024 suicide cases come from", so
what would quietly ruin the answer:

  - counting DeathState or ResidenceState instead of InjuryState (a case moves
    states between injury and death), or letting SiteID silently override a
    state that InjuryState already gave
  - a state value the script cannot read being dropped without a word
  - homicides or undetermined deaths counted as suicides
  - 2017 / 2025 rows slipping into a 2018-2024 count
  - the "35 states + DC" panel figure being drawn from a hardcoded list that
    drifts from the panel CSV
  - a state with zero cases disappearing from the table instead of being drawn
    grey on the map

Run with:  python tests/test_nvdrs_state_map.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nvdrs_state_map as nsm  # noqa: E402

failures: list[str] = []
YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]
PANEL_CSV = str(Path(nsm.HERE) / "config" / "panel36_filter_key_2018_2024.csv")
GEOJSON = str(Path(nsm.HERE) / "assets" / "us_states_lowres.geojson")


def check(condition: bool, message: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        result = fn(*a, **kw)
    return result, out.getvalue() + err.getvalue()


def expect_exit(fn, *a, **kw) -> str:
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            fn(*a, **kw)
    except SystemExit:
        return out.getvalue() + err.getvalue()
    return ""


def write_csv(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def case(state="Alabama", year=2020, site="", manner="Suicide", ident="x") -> dict:
    return {
        "IncidentID": ident, "IncidentYear": year, "InjuryState": state,
        "SiteID": site, "AbstractorAssignedDeathManner": manner,
    }


def run_on(rows: list[dict], tmp: Path, **kwargs) -> dict:
    write_csv(tmp / "in" / "cases.csv", rows)
    result, _ = quiet(
        nsm.run, str(tmp / "in"), [], str(tmp / "out"),
        panel_csv=PANEL_CSV, geojson=GEOJSON, years=YEARS,
        map_style=kwargs.pop("map_style", "none"), **kwargs,
    )
    return result


# =============================================================================

print("\n面板文件：州的口径全部从这里来")
panel, _ = quiet(nsm.load_panel, PANEL_CSV)
check(len(panel) == 51, f"51 个辖区（50 州 + DC），读到 {len(panel)}")
check(len(panel.panel_usps) == 36,
      f"2018 面板 in_panel=1 的有 36 个，读到 {len(panel.panel_usps)}")
check("DC" in panel.panel_usps and len(panel.panel_usps - {"DC"}) == 35,
      "面板 = 35 州 + DC（DC 在里面）")
check({"CA", "TX", "FL", "NY", "PA", "IL"}.isdisjoint(panel.panel_usps),
      "2018 没覆盖的大州（CA/TX/FL/NY/PA/IL）不在面板里")
check(panel.fips_of["AL"] == 1 and panel.fips_of["WY"] == 56,
      "FIPS 对照读对了（AL=1, WY=56）")

print("\n地图边界文件")
shapes = nsm.load_geometry(GEOJSON)
check(set(shapes) == set(panel.usps_order),
      f"州界文件正好覆盖面板里的 {len(panel)} 个辖区，不多不少")
check("PR" not in shapes, "波多黎各没混进来（面板里没有它）")
geo = json.loads(Path(GEOJSON).read_text(encoding="utf-8"))
check("source" in geo.get("metadata", {}), "边界文件里写明了出处")

print("\n方块图的格子")
check(set(nsm.GRID_LAYOUT) == set(panel.usps_order),
      "方块图的格子正好一州一格，覆盖全部 51 个辖区")
check(len(set(nsm.GRID_LAYOUT.values())) == len(nsm.GRID_LAYOUT),
      "没有两个州抢同一格")

print("\n州取值的识别")
resolver = nsm.StateResolver(panel)
for value, expect in [
    ("Alabama", "AL"), ("alabama", "AL"), ("  New   Mexico ", "NM"),
    ("District of Columbia", "DC"), ("AL", "AL"), ("wy", "WY"),
    ("6", "CA"), ("06", "CA"), ("6.0", "CA"), (11, "DC"),
    ("Alabama (AL)", "AL"),
    ("", nsm.BLANK), ("Unknown", nsm.BLANK), (None, nsm.BLANK),
    ("Freedonia", nsm.UNRESOLVED), ("777", nsm.UNRESOLVED),
]:
    got = resolver.resolve(value)
    check(got == expect, f"{value!r} -> {expect}（得到 {got}）")

# =============================================================================

with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)

    print("\n只数自杀，只数 2018-2024")
    rows = (
        [case(year=y, ident=f"y{y}") for y in YEARS]           # 7 例，全留
        + [case(year=2017, ident="old"), case(year=2025, ident="new")]
        + [case(year="", ident="blankyear"), case(year="24", ident="twodigit")]
        + [case(manner="Homicide", ident="h"), case(manner="Undetermined", ident="u")]
        + [case(manner="suicide", ident="lower")]              # 大小写不挑
    )
    result = run_on(rows, tmp)
    stats = result["stats"]
    check(stats["kept"] == 8, f"留下 8 例（7 年各 1 例 + 小写 suicide），得到 {stats['kept']}")
    check(stats["dropped_year"] == 4, f"2017/2025/空白/两位数年份都剔掉，得到 {stats['dropped_year']}")
    check(stats["dropped_bad_year"] == 1, "两位数年份 '24' 不猜，单独记成读不出")
    check(stats["dropped_not_suicide"] == 2, "他杀和未定性不算自杀")
    check(
        stats["rows_read"] == stats["kept"] + stats["dropped_year"]
        + stats["dropped_not_suicide"] + stats["dropped_blank_state"]
        + stats["dropped_unresolved_state"],
        "逐级相减 = 计入数，没有行被漏算",
    )
    table = result["table"]
    check(int(table["TOTAL"].sum()) == stats["kept"], "表里的总数 = funnel 里的计入数")
    check(int(table.loc[table["usps"] == "AL", "TOTAL"].iloc[0]) == 8,
          "全部算到 Alabama 头上")

    print("\nInjuryState 为主，空白才退回 SiteID")
    rows = [
        case(state="Alabama", site=6, ident="both"),      # 两列打架：听 InjuryState
        case(state="", site=6, ident="siteonly"),         # 空白：退回 SiteID -> CA
        case(state="Unknown", site=32, ident="unk"),      # Unknown 也退回 -> NV
        case(state="Freedonia", site=8, ident="bad"),     # 认不出也退回 -> CO
        case(state="", site="", ident="neither"),         # 两列都没有：剔除
        case(state="", site="777", ident="junk"),         # 认不出：剔除，但要报出来
    ]
    result = run_on(rows, tmp)
    counts = dict(zip(result["table"]["usps"], result["table"]["TOTAL"]))
    check(counts["AL"] == 1 and counts["CA"] == 1,
          "InjuryState 有值时不会被 SiteID 顶掉；空白时 SiteID 顶上")
    check(counts["NV"] == 1 and counts["CO"] == 1,
          "InjuryState 是 Unknown / 认不出时才退回 SiteID")
    check(result["stats"]["dropped_blank_state"] == 1, "两列都空的那行算「州空白」剔除")
    check(result["stats"]["dropped_unresolved_state"] == 1, "SiteID=777 认不出，剔除")
    check(any("777" in v for v in result["unresolved_values"]),
          "认不出的取值原样写进 unresolved_state_values.csv，不静默丢弃")
    check(result["source_counts"].get("InjuryState", 0) == 1
          and result["source_counts"].get("SiteID", 0) == 3,
          "每行的州是从哪一列认出来的，分别记了账")

    print("\nsite_only：只认 SiteID")
    result = run_on(
        [case(state="Alabama", site=6, ident="both")], tmp, state_source="site_only",
    )
    counts = dict(zip(result["table"]["usps"], result["table"]["TOTAL"]))
    check(counts["CA"] == 1 and counts["AL"] == 0, "state_source=site_only 时只看 SiteID")

    print("\n死亡方式是编码时：必须自己给取值，不瞎猜")
    coded = [
        {"IncidentID": i, "IncidentYear": 2020, "InjuryState": "Ohio",
         "SiteID": "", "MannerOfDeath": m}
        for i, m in enumerate(["1", "2", "2", "3"])
    ]
    result = run_on(coded, tmp, suicide_values=["2"])
    check(int(result["table"]["TOTAL"].sum()) == 2, "MannerOfDeath=2 的两行算自杀，其余不算")
    result = run_on(coded, tmp, suicide_values=[])
    check(int(result["table"]["TOTAL"].sum()) == 0,
          "没给取值时，编码列里没有 'suicide' 字样 -> 一个都不算（不会把 1/3 当自杀）")

    print("\n表格：51 行都在，没有 case 的州也留着")
    rows = [case(state="Ohio", year=2019, ident="a"), case(state="Ohio", year=2019, ident="b"),
            case(state="Maine", year=2024, ident="c"),
            case(state="Texas", year=2021, ident="d")]
    result = run_on(rows, tmp, map_style="none")
    table, out_dir = result["table"], result["output_dir"]
    check(len(table) == 51, f"州 × 年计数表 51 行（含 0），得到 {len(table)}")
    check(len(result["with_cases"]) == 3 and len(result["without_cases"]) == 48,
          "有 case 的 3 个 + 没有 case 的 48 个 = 51")
    check(set(result["with_cases"]["usps"]) == {"OH", "ME", "TX"},
          "问题 1 的答案：case 来自 OH / ME / TX")
    per_year = table[[str(y) for y in YEARS]].sum(axis=1)
    check(bool((per_year == table["TOTAL"]).all()), "每行的各年相加 = TOTAL")
    check(int(table.loc[table["usps"] == "OH", "2019"].iloc[0]) == 2,
          "年份列对上了（Ohio 2019 = 2）")
    check(len(result["panel_table"]) == 36
          and set(result["panel_table"]["usps"]) == panel.panel_usps,
          "面板表 = 面板 CSV 里 in_panel=1 的 36 个辖区，不是脚本里另写的名单")
    check("TX" not in set(result["panel_table"]["usps"]),
          "有 case 但 in_panel=0 的州（TX）不进面板表")
    for name in ("state_year_counts.csv", "states_with_cases.csv",
                 "states_without_cases.csv", "panel36_state_year_counts.csv",
                 "state_source_counts.csv", "unresolved_state_values.csv", "funnel.csv"):
        check((out_dir / name).is_file(), f"写出了 {name}")
    written = pd.read_csv(out_dir / "state_year_counts.csv")
    check(len(written) == 51 and int(written["TOTAL"].sum()) == 4,
          "落盘的计数表和内存里的一致")

    print("\n画图")
    result = run_on(rows, tmp, map_style="both")
    figures = {p.name: p for p in result["figures"]}
    if nsm._mpl() is None:
        check(not figures, "没装 matplotlib 时只出表，不报错")
    else:
        for name in ("map_all_states_2018_2024.png", "map_panel36_2018_2024.png",
                     "map_all_states_2018_2024_grid.png", "map_panel36_2018_2024_grid.png"):
            check(name in figures and figures[name].stat().st_size > 10_000,
                  f"{name} 画出来了")
        result = run_on(rows, tmp, map_style="geo", theme="both")
        names = {p.name for p in result["figures"]}
        check("map_all_states_2018_2024_dark.png" in names, "深色版也能出")

    print("\n分档")
    check(nsm.make_bins([0, 0, 5], 5) == [(5, 5)], "只有一个取值时就一档")
    check(nsm.make_bins([], 5) == [], "全是 0 时没有档（整张图都是灰的）")
    bins = nsm.make_bins(list(range(1, 101)), 5)
    check(len(bins) == 5 and bins[0][0] == 1 and bins[-1][1] == 100,
          f"100 个取值切 5 档，覆盖 1-100：{bins}")
    check(all(a[1] + 1 == b[0] for a, b in zip(bins, bins[1:])),
          "档与档首尾相接，不重叠也不留缝")
    check(all(nsm.bin_index(v, bins) == i for i, (lo, hi) in enumerate(bins)
              for v in (lo, hi)), "每档的上下界都落回自己那一档")

    print("\n拿错列会报错，不会默默算错")
    wrong = [{"IncidentID": 1, "IncidentYear": 2020, "DeathState": "Ohio",
              "ResidenceState": "Kentucky", "AbstractorAssignedDeathManner": "Suicide"}]
    write_csv(tmp / "wrong" / "cases.csv", wrong)
    output = expect_exit(
        nsm.run, str(tmp / "wrong"), [], str(tmp / "out_wrong"),
        panel_csv=PANEL_CSV, geojson=GEOJSON, years=YEARS, map_style="none",
    )
    check("DeathState" in output and "死亡地州" in output,
          "只有 DeathState / ResidenceState 时报错，并点名它们不是受伤地州")
    check("ResidenceState" in output, "居住地州也被点名")

    output = expect_exit(
        nsm.run, str(tmp / "in"), [], str(tmp / "out_bad"),
        panel_csv=str(tmp / "nope.csv"), geojson=GEOJSON, years=YEARS, map_style="none",
    )
    check("面板文件" in output, "面板文件找不到时说清楚是它，而不是继续用内置名单")

    print("\n分块大小不影响结果")
    many = [case(state=s, year=y, ident=f"{s}{y}{i}")
            for s in ("Ohio", "Maine", "Utah") for y in YEARS for i in range(5)]
    a = run_on(many, tmp, chunk_size=7)["table"]
    b = run_on(many, tmp, chunk_size=10_000)["table"]
    check(a.equals(b), "chunk-size 7 和 10000 得到同一张表")

print()
if failures:
    print(f"{len(failures)} 项没过：")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("全部通过")
