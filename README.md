# NVDRS 分组拆分工具

把已经按年龄段切好的 NVDRS CSV（`18-30`、`31-40`、`41-50`、`51-60`、`61-70`），
再按两个维度拆分：

1. **circumstance known** —— 布尔化（`Yes` → `True`，`No` → `False`）
2. **occupation** —— 四分类：`construction` / `non_construction` / `non_workforce` / `military`

## 完整流程

```bash
pip install pandas

# 第 0 步：按 incident year 2020-2023 筛选
python filter_years.py --input input_list.txt --years 2020-2023 --output-dir filtered/

# 第 1 步：按年龄分段切成 chunk（18-27 / 28-37 / 38-47 / 48-57 / 58-67）
python split_by_age.py --input filtered/ --output-dir age_chunks/

# 第 2 步：按 Census 2018 码筛出 construction（另出空白码文件 + 三个清单）
python filter_construction.py --input age_chunks/ --output-dir construction/

# 第 3 步（可选）：按 circumstance × occupation 做四分类拆分
# 数据里有 census_2018 列时，职业分组只用这一列（见下）
python nvdrs_split.py --input age_chunks/ --inspect
python nvdrs_split.py --input age_chunks/ --output-dir out/

# 第 4 步：核对派生的布尔列
python verify_circumstance.py --input out/labeled/ --mismatches-only
```

先筛年份再切年龄，后面每一步处理的数据量都小一截。

## 一步到位（推荐）：`run_electrician_pipeline.py`

**从原始大文件一遍扫完**：年龄分段 + 年份筛选 + 电工三分组。不写任何中间文件。

```python
INPUT_FILES = [ r"D:\...\Liu_1191_nvdrs_2024.csv" ]
OUTPUT_DIR  = r"D:\...\Electricians_18_70_2018_2024"
```

```bash
python run_electrician_pipeline.py
```

四个判断，全部直接读列：

```
年龄段  Age 落在 18-30 / 31-40 / 41-50 / 51-60 / 61-70
年份    IncidentYear 在 2018-2024
是电工  Census2018_Occupation 出现 "Electrician"
是建筑  Census2018_Industry   等于  "Construction"
```

### 2.1 GB 实测

在生成的 **2.0 GB、66 列、525 万行** 文件上实跑：

| | |
|---|---|
| 耗时 | **138 秒**（37,900 行/秒） |
| 峰值内存 | **252 MB** |
| 输出总大小 | 180 MB（只保留电工） |

内存不随文件大小增长，只随 chunk-size 和列数走。

### 输出

```
OUTPUT_DIR/
  All_year/                             ← 最终总表
    <组>_All_year.csv                   全年龄全年份
    <组>_All_year_age_18-30.csv ...     分年龄段
  by_year/2018/ ... by_year/2024/
    <组>_2018.csv                       该年全年龄
  summary_by_year_and_age.csv           年份 × 年龄段 × 组 计数
  funnel.csv                            逐级筛选的行数交代
  file_map.csv / 两个核对文件
```

每个输出行都带 `age_band` / `incident_year` / `electrician_group` 三列，所以从
`All_year` 总表随时能自己重新切分。`--per-year-per-band` 可额外输出
「每年 × 每段」的 140 个行级小文件（默认关闭，计数已在 summary 里）。

### 逐级筛选（funnel）

每次运行都交代清楚每一行去了哪：

```
                 stage     rows
                 读入总行数  5250000
            年龄无法解析（剔除）        0
        年龄不在 18-70（剔除） -2625503
            年份无法解析（剔除）        0
    年份不在 2018-2024（剔除）  -786337
职业不含 'electrician'（剔除） -1766862
            = 电工（进入分组）    71298
  （逐级相减 = 电工数，没有行被漏算）
```

加上 `All = Construction + Non_construction + Unknown` 的核对，以及
`All_year == 7 个年份之和 == 5 个年龄段之和`（测试有断言）。

### 不会拿错列

`DeathYear` 不会顶替 `IncidentYear`（跨年案例中不同），`AgeGroup` 不会顶替数值
`Age`，`Census2018_Industry` 不会被当成职业列。缺哪一列就报哪一列，并说明为什么
找到的那个不能替代。

## 分步版：按 IncidentYear 拆年 —— `split_by_year.py`

把年龄段 CSV 按 `IncidentYear` 拆成 2018–2024 各年。**只看这一列**，不碰
occupation / industry。

```bash
# 填好配置区的 INPUT_DIR 和 OUTPUT_DIR 后直接运行
python split_by_year.py

# 或命令行
python split_by_year.py --input-dir "D:\...\age_chunks" --output-dir "D:\...\Label_year"
```

### 输出结构

年份目录里的**文件名与输入完全一致**，所以下一步把 `--input-dir` 指过去就能跑：

```
OUTPUT_DIR/
  2018/  nvdrs_age_18_27.csv  nvdrs_age_28_37.csv  ...  nvdrs_age_58_67.csv
  2019/  ...
  ...
  2024/  ...
  _all_ages/   nvdrs_2018_all_ages.csv ... nvdrs_2024_all_ages.csv   各年合并版
  _excluded/   out_of_range.csv / blank_year.csv / unparseable_year.csv
  year_distribution.csv        IncidentYear 每个取值的行数
  summary_by_year_and_age.csv  年份 × 年龄段 计数表
  file_map.csv                 输入 -> 输出 完整路径对照
```

合并版**放在 `_all_ages/` 而不是年份目录里** —— 否则下一步扫描该目录时会把合并文件
和 5 个分段文件一起读进去，行数直接翻倍。

```
age_band  18_27  28_37  38_47  48_57  58_67  TOTAL
year
2018          8      8      8      8      8     40
2019          9      9      9      9      9     45
...
读入总行数：450
保留（2018-2024）：385
排除：65
  年份在范围外：30    年份空白：25    年份读不出：10
  （保留 + 排除 = 读入，没有行丢失或重复）
```

### 几个做严的地方

- **只认 IncidentYear，绝不拿 DeathYear 顶替**。跨年案例里（12 月受伤、1 月死亡）
  两者不同。文件里只有 `DeathYear` 时会报错并点名说明它不是 incident year，
  而不是默默拿它筛。测试里构造了两列给出相反答案的数据来验证选对了。
- **年份边界精确**：2017 和 2025 一定被排除（测试逐个边界验过）。
- **两位数年份（`24`）不猜**，归为 `UNPARSEABLE` 单独写出。日期格式
  （`2024-05-13`、`5/13/2020`）能正确取年。
- **每一行都有交代**：写进某年，或计入三类排除之一，相加必等于读入总数。
- 分块读写，1.7 GB 输入也只占几百 MB 内存；不同 chunk-size 输出一致。

### 接下一步

```bash
python run_electrician_split.py --input-dir "OUTPUT_DIR\2024" --output-dir "..."
```

测试里**实跑了这个串联**：确认 `2024/` 目录能被电工脚本读取，且只看到 2024 的行。

## 第二步（按年）：电工三分组 —— `run_electrician_by_year.py`

输入 `split_by_year.py` 产出的 `_all_ages/nvdrs_2018_all_ages.csv ... nvdrs_2024_all_ages.csv`，
每年拆成电工三组 + 跨年合并。

```bash
# 填好配置区的 INPUT_DIR（OUTPUT_DIR 已预填）后直接运行
python run_electrician_by_year.py
```

```
OUTPUT_DIR/
  2018/  Construction_electrician_2018.csv
         Non_construction_electrician_2018.csv
         Unknown_industry_electrician_2018.csv
         All_industry_electrician_2018.csv
  ...
  2024/  ...
  _all_years/  Construction_electrician_all_years.csv ...（带 incident_year 列）
  summary_by_year.csv / file_map.csv
  electrician_industry_values.csv / matched_occupation_values.csv
```

### 建筑业口径：exact 还是 contains

「行业是 Construction」和「行业出现 Construction」结果不同。默认 `exact`，但**每次运行
都会把两种口径的行数和差异值都算出来**，不用重跑就能对比：

```
建筑业判定口径对比
  等于 'construction'（exact）   ：95 名电工
  含有 'construction'（contains）：120 名电工
  当前用的是：exact

  这 25 行只有 contains 会算作建筑业：
          17  Construction and extraction
           8  Heavy construction contractors
  要改口径：把 CONSTRUCTION_MATCH 改成 "contains"
```

### 另外三点

- **年份取自文件名，但会和数据里的 `IncidentYear` 列核对**。不一致会告警并说明
  这些行仍按文件名归档 —— 输入文件放错时能立刻发现。没有 `IncidentYear` 列时
  自动跳过核对，不会误报。
- **两个文件解析出同一年份会直接报错**（否则后写的会覆盖先写的），并列出是哪两个。
- **每一年单独核对** `All = Construction + Non_construction + Unknown_industry`，
  不只是总数对得上。

## 第二步（按年龄段）：电工三分组 —— `run_electrician_split.py`

**判断只看两列的文本内容，不查任何码表：**

```
是电工    = Census2018_Occupation  含有  "Electrician"   （不区分大小写）
是建筑业  = Census2018_Industry     等于  "Construction"  （不区分大小写、忽略首尾空格）
```

| 组 | 定义 |
|---|---|
| `Construction_electrician` | 是电工 且 行业 = Construction |
| `Non_construction_electrician` | 是电工 且 行业 = 其他已填写的行业 |
| `Unknown_industry_electrician` | 是电工 且 行业为空 / Unknown |
| `All_industry_electrician` | 是电工，不分行业（= 上面三组之和） |

```bash
# 填好配置区的 INPUT_DIR（OUTPUT_DIR 已预填）后直接运行
python run_electrician_split.py

# 或命令行
python run_electrician_split.py --input-dir "D:\...\age_chunks"
```

单文件，只依赖 pandas，可以单独拷到任何机器上跑。每组产出年龄分层（5 个）+ 合并
（1 个），共 24 个 CSV。

### 关键：contains 与 equals 的区别是有意的

职业用**包含**，所以 `Electricians`、`Electrician`、`Electrician, apprentice` 都能
匹配；而 `Electrical power-line installers and repairers`、
`Electrical and electronics repairers` **不会**被当成电工（它们不含 "electrician"）。

行业用**等于**，所以 `Construction and extraction`、
`Heavy construction contractors` 这类含有 "construction" 但整格不等于的写法
**不算**建筑业。测试里专门放了这几个近似值来验证。

如果你的数据里写法不同，改配置区的 `ELECTRICIAN_KEYWORD` / `CONSTRUCTION_VALUE`
即可，不需要动代码。

### 两个核对文件

跑完先看这两个，确认匹配规则和你的数据写法一致：

```
matched_occupation_values.csv     被判为电工的职业原文 + 各自行数
electrician_industry_values.csv   这些电工所在行业的原文 + 行数 + 被归到哪一组
```

控制台也会直接打印，例如：

```
电工所在行业的原文取值（共 8 种）
                Census2018_Industry  n_electricians       counted_as
                       Construction              82     Construction
                             (空白)                34          Unknown
                       Retail trade              15 Non_construction
                            Unknown               8          Unknown
```

`counted_as` 的各组合计与实际输出文件行数逐项对账（测试有断言）。

匹配不上时会明确告警：一个电工都没匹配到会提示「这一列可能存的是数字码而不是
文字」；没有任何行业等于 Construction 会把实际出现的行业写法列出来让你对照。

### 另外两点

- **`All_industry` 是并集，行会重复出现**：同一名电工既在 All 文件里，也在它所属的
  行业组文件里。这是设计如此，每次运行都核对
  `All = Construction + Non_construction + Unknown_industry`。
- **行业未知的电工默认单独成组**，不并入对照组 —— 做 construction vs
  non-construction 对比时，把行业未知的人塞进对照组会污染对照组。要合并改
  `UNKNOWN_INDUSTRY_GOES_TO = "nonconstruction"`。

## 一键脚本（填空即用）：`run_construction_split.py`

已经有年龄段 chunk 了，只想按 **`census2018_industry`** 分出 construction /
非 construction，并且**同时要合并版和年龄分层版**，用这个。脚本顶部有「配置区」，
把路径填进空白引号里直接运行即可：

```python
INPUT_DIR  = r""      # 例：r"D:\NVDRS\age_chunks"
INPUT_FILES = [r"", ] # 或者逐个列出文件（填了这个就忽略 INPUT_DIR）
OUTPUT_DIR = r""      # 例：r"D:\NVDRS\construction_out"
```

```bash
python run_construction_split.py
# 或者不改文件，用命令行覆盖：
python run_construction_split.py --input-dir "D:\age_chunks" --output-dir "D:\out"
```

路径没填会**明确告诉你哪个空没填、怎么填**，不会抛 traceback。

### 输出

```
年龄分层（5 段 × 3 类）          合并（全年龄段）
nvdrs_age_18_27_construction.csv   all_ages_construction.csv
nvdrs_age_18_27_nonconstruction.csv  all_ages_nonconstruction.csv
nvdrs_age_18_27_unknown.csv        all_ages_unknown.csv
...
summary_by_age_band.csv            各年龄段计数
file_map.csv                       输入 -> 输出 路径对照表
census2018_industry_breakdown.csv  逐个行业码的行数 + 行业名称
```

合并文件带 `age_band` 和 `occupation_group` 两列，**分层信息不丢** —— 从合并文件
随时能还原出分层。测试断言了 `all_ages_X.csv` 与 5 个分层文件拼接后**完全相等**。

控制台和 `file_map.csv` 都会逐条列出每个输出文件对应的输入文件和完整路径。

### 行业码 vs 职业码：选出的不是同一批人

默认按 **industry**（`CLASSIFY_BY = "industry"`）：

| | industry（`census2018_industry`） | occupation（`census_2018`） |
|---|---|---|
| 含义 | 雇主属于哪个行业 | 本人做什么工种 |
| construction | **0770**（单个码，不是区间） | 6200–6765 |
| 采矿/采掘（选配） | 0370–0490 | 6800–6950 |
| 建筑公司的会计 | ✅ 算 | ❌ 不算 |
| 学校雇的木匠 | ❌ 不算 | ✅ 算 |

两套码表**不可互换**。industry 模式找不到行业列时不会退回职业列，而是报错并告诉你
找到的是另一套码表、以及怎么切换模式。测试里构造了三组人（只在 industry 里算、
只在 occupation 里算、两边都算）各 20 行，断言两种模式选出的集合确实不同。

要改回按职业划分：配置区 `CLASSIFY_BY = "occupation"`，或命令行
`--classify-by occupation`。

### 注意事项

- **自动跳过 `age_distribution.csv` 和 `nvdrs_age_excluded.csv`** —— 前者是汇总表，
  后者是年龄超范围被排除的行，都不是样本。用目录模式时会打印跳过了哪些文件。
- **码为空、或码读不出来（非数字）的行默认单独成 `*_unknown.csv`**。这两种都是
  「行业未知」，和「已知不是建筑」不是一回事，混进去会让非建筑组的分母变大。要合并
  就把 `UNKNOWN_GOES_TO` 改成 `"nonconstruction"`（会打印提醒）。汇总表里
  `blank_code` 和 `unparseable_code` 两个计数是分开的。
- **读入行数 = 写出行数**，每次运行都会核对并打印，对不上会告警。
- 分块读写，1.7 GB 输入也只占几百 MB 内存；不同 chunk-size 输出一致。

## 职业分组只依据 census_2018

`census_2018.py` 是全仓库**唯一**的 Census 2018 码段定义，`filter_construction.py`
和 `nvdrs_split.py` 都从它导入，所以两个脚本对「construction」的定义不可能漂移
（测试里断言了两者引用同一个对象）。

**`nvdrs_split.py` 一旦检测到 `census_2018` 列，职业分组就只看这一列** —— 不跑关键词、
不看行业列、没有自由文本兜底。分组规则：

| census_2018 | 组 |
|---|---|
| 6200–6765 | `construction`（`--include-extraction` 再加 6800–6950，`--include-managers` 再加 0220） |
| 9800–9830 | `military` |
| 9920 | `non_workforce` |
| 其余 1–9799 | `non_construction` |
| 空白 / 非数字 / 码表外 | `unclassified` |

`occupation_group_rule` 列会写明是 `census_2018_code`、`census_2018_blank`、
`census_2018_unparseable` 还是 `census_2018_out_of_list`，一眼能看出这行凭什么被分到
那一组。

测试用**故意矛盾的数据**验证这一点：每行的 census_2018 码和 Occupation 文本、
OccupationCode、IndustryCode 都对着干（比如 `census_2018=3130` 但文本写
"Construction laborer"、行业码 0770），断言结果全部跟着 census_2018 走，且
`occupation_keyword` / `industry_code` / `fallback_text` 这些规则**一次都没触发**。

没有 `census_2018` 列时，仍然退回原来的多特征分类器（2010 码 + 关键词 + 行业），
`--inspect` 会明确告诉你当前是哪种模式：

```
occupation grouping mode: census_2018 only
```

要强制指定列名用 `--census-2018-col`。

## 按 Census 2018 筛 construction：`filter_construction.py`

对年龄段 chunk（或任何 CSV 列表）按 `census_2018` 列筛出建筑业，**空白码单独出一个
文件**，并生成可直接喂给下一步的清单。

```bash
# 1) 先看文件里实际出现了哪些建筑业码，不写文件
python filter_construction.py --input age_chunks/ --inspect

# 2) 筛选
python filter_construction.py --input age_chunks/ --output-dir construction/

# 把非建筑行也留下来核对
python filter_construction.py --input age_chunks/ --output-dir construction/ \
    --keep-nonconstruction
```

Python 里调用：

```python
from filter_construction import filter_construction
result = filter_construction(input_list, output_dir="construction/")
print(result["totals"])
```

### 码段定义（Census 2018）

2018 版 Census 职业码里 Construction and Extraction Occupations 占 **6200–6950**：

| 码段 | 内容 | 默认 |
|---|---|---|
| 6200–6765 | 建筑工种（含一线主管、Other construction and related workers） | **计入** |
| 6800–6950 | Extraction workers（采掘） | 不计入，`--include-extraction` 开启 |
| 0220 | Construction managers（归在 Management 大类下） | 不计入，`--include-managers` 开启 |

**注意 2018 与 2010 码表不通用** —— 2010 版 extraction 止于 6940，2018 版止于 6950。
本仓库 `nvdrs_split.py` 用的是 2010 码表，两者不要混用。如果文件里只有 `census_2010`
这类列，脚本会**报错并说明两套码表不可互换**，而不是拿它硬筛。

### 输出

每个输入文件产出：

```
<stem>_construction.csv       census_2018 落在建筑码段
<stem>_blank.csv              census_2018 为空白
<stem>_nonconstruction.csv    其余（需 --keep-nonconstruction）
```

加上三个清单和两个复核文件：

```
input_list.txt              实际读入的文件，按顺序
construction_list.txt       建筑业输出文件清单
blank_list.txt              空白码输出文件清单
construction_summary.csv    每个文件的各类计数
census_2018_breakdown.csv   逐个码的行数 + 职业名称 + 是否被计入
```

三个 `.txt` 清单可以直接作为下一步的 `--input`：

```bash
python filter_construction.py --input construction/construction_list.txt ...
```

运行时会打印实际出现的建筑业码及其职业名称，方便核对：

```
construction codes actually present:
census_2018                                    title  n
       6200 First-line supervisors of construc...    40
       6230                              Carpenters   40
       6260                   Construction laborers   40
       6330                            Electricians   40
```

### 几个做严的地方

- **空白码单独成文件**，不和非建筑混在一起 —— 空白是「不知道职业」，和「知道且不是
  建筑」是两回事，混在一起会影响分母。
- **三类互斥且完整**：construction + blank + other 恒等于读入行数（测试有断言）。
- **非数字码（如 `unknown`）计入 other 并单独报数**，绝不会被当成建筑。
- **职业名称只用于显示**，筛选完全依据数值码段，所以名称表不全也不会改变筛选结果
  （范围内但不在本地名称表的码会单独提示你去核对官方码表）。
- 空结果也写**带表头的文件**；完全没匹配到时会打印显著警告。
- 分块读写，大文件不占内存；不同 chunk-size 输出一致。

| 参数 | 说明 |
|---|---|
| `--input` | CSV 文件、目录，或每行一个路径的 `.txt` |
| `--output-dir` | 输出目录，默认 `construction` |
| `--census-col` | 手动指定 Census 2018 码列 |
| `--include-extraction` | 把 6800–6950 采掘业计入建筑 |
| `--include-managers` | 把 0220 建筑经理计入建筑 |
| `--keep-nonconstruction` | 额外写出非建筑行 |
| `--inspect` | 只报告出现了哪些码，不写文件 |
| `--chunk-size` | 每块行数，默认 50000 |

## 按年龄切 chunk：`split_by_age.py`

把一个大文件流式切成各年龄段的 CSV。默认 18–67，十岁一档：
`18-27`、`28-37`、`38-47`、`48-57`、`58-67`。

```bash
# 1) 先预览：只读年龄那一列，很快，不写文件
python split_by_age.py --input nvdrs_big.csv --inspect

# 2) 正式切
python split_by_age.py --input nvdrs_big.csv --output-dir age_chunks/

# 保留被排除的行以便核对，并压缩输出
python split_by_age.py --input nvdrs_big.csv --output-dir age_chunks/ \
    --keep-out-of-range --gzip
```

Python 里调用：

```python
from split_by_age import split_by_age
result = split_by_age("nvdrs_big.csv", output_dir="age_chunks/")
print(result["band_counts"])
```

### 大文件实测

在一个 **1.8 GB、63 列、515 万行**的 CSV 上实测过：

| chunk-size | 峰值内存 | 耗时 |
|---|---|---|
| 200,000 | 1033 MB | 164 s |
| **50,000（默认）** | **326 MB** | **171 s** |
| 25,000 | 217 MB | 183 s |

三种设置的输出文件 **md5 完全一致**。默认选 50000：比 20 万只慢 4%，内存少 3 倍。
内存不随文件大小增长，只随 chunk-size 和列数走，所以再大的文件也是这个量级。

`--inspect` 只读年龄一列，同一个文件 25 秒就能给出各段行数预览 —— 正式切之前先跑它。

### 输出

```
 band      n                file
18-27 258250 nvdrs_age_18_27.csv
28-37 256299 nvdrs_age_28_37.csv
...
total rows read : 5,150,000
written to bands: 1,287,321
excluded        : 3,862,679
  age outside 18-67: 3,862,679
  age blank        : 0
  age unreadable   : 0
```

外加 `age_distribution.csv`（逐个年龄值的行数）。**每一行都有交代**：写入某个段，或计入
三类排除之一，三者相加必等于读入总数（测试里有断言）。

### 几个做严的地方

- **区间闭区间且不重叠**。`18-27` 含 18 和 27；`parse_bands` 会拒绝重叠区间
  （否则一行会进两个文件）。测试里用 0–100 每个年龄各一行，逐个边界验过。
- **不拿 `AgeGroup` 之类的分类列当数值年龄**。只找数值年龄列；如果文件里只有
  `AgeGroup`/`AgeRange`，脚本会报错并指出这些是预先分好的类别列，而不是硬套。
  要用就显式 `--age-col`。
- **900 以上的年龄会单独告警**。NVDRS 这类数据常用 `999` 表示年龄未知，把它当成真实
  年龄会让统计出问题。这类行不会进任何区间，但会在汇总里单独点名。
- **多文件合并前校验表头**。表头不一致会报错并指出差异列，而不是错位拼接。
- 空的区间也会写出**带表头的空文件**，后续步骤不会因为缺文件而崩。

| 参数 | 说明 |
|---|---|
| `--input` | CSV 文件、目录，或每行一个路径的 `.txt` |
| `--output-dir` | 输出目录，默认 `age_chunks` |
| `--bands` | 自定义区间，如 `18-30 31-40`，默认 `18-27,28-37,38-47,48-57,58-67` |
| `--age-col` | 手动指定数值年龄列 |
| `--chunk-size` | 每块行数，默认 50000 |
| `--gzip` | 输出 `.csv.gz` |
| `--keep-out-of-range` | 额外写出被排除的行，带 `exclusion_reason` 列 |
| `--prefix` | 输出文件名前缀，默认 `nvdrs` |
| `--inspect` | 只预览年龄列和各段行数，不写文件 |

## 按 incident year 筛选：`filter_years.py`

只保留 incident year 在指定范围内的行，默认 2020–2023。**放在整个流程最前面**，
这样后面的年龄段拆分和职业分类只会看到你要的年份。

在 Python 里直接把已有的 `input_list` 传进去：

```python
from filter_years import filter_year_range

result = filter_year_range(input_list, years=(2020, 2023), output_dir="filtered/")
print(result["rows_kept"], "rows kept of", result["rows_read"])
```

命令行：

```bash
# 先看各文件的年份分布，不写文件
python filter_years.py --input raw/ --inspect

# 筛选。--input 接受 CSV 路径、目录，或一个每行一个路径的 .txt
python filter_years.py --input input_list.txt --years 2020-2023 --output-dir filtered/

# 不连续的年份也行
python filter_years.py --input raw/ --years 2020 2022 2023 --output-dir filtered/
```

输出每个文件的年份分布表（标明哪些年份被保留）、保留/丢弃计数，以及汇总的
`year_distribution.csv`。没有任何行匹配时退出码为 1。

```
nvdrs_2018_2024.csv  (year column: incident_year)
   year  n  kept
   2018  3 False
   2020  3  True
   ...
MISSING  1 False
  22 rows -> kept 12, dropped 10
```

**关于「incident year」的重要说明**：NVDRS 里同时有 incident year、death year、
injury year，跨年案例中三者可以不同。脚本**只**自动识别 incident 年份列
（`IncidentYear`、`incident_year`、`IncidentDate` 等）；如果文件里只有
`death_year` / `injury_year` 这类列，它会**停下来报错并指出这些列不是 incident
year**，而不是拿它们凑合。要用其他列就显式写 `--year-col`。

年份取值支持纯年份（`2020`、`2020.0`）和含年份的日期（`2020-05-13`、`5/13/2020`、
`13JUL2022`）。两位数年份（`20`）**不会**被猜成 20xx —— 归为 `UNPARSEABLE` 单独报出。
空值归为 `MISSING`。这两类都会被丢弃，但会在汇总里明确列出行数，不会悄悄消失。

文件是分块读、分块追加写的，大文件不占内存（测试里断言了分块与单次读结果一致）。
重复传同一个路径会自动去重，不会把行数翻倍。

| 参数 | 说明 |
|---|---|
| `--input` | CSV 文件、目录，或每行一个路径的 `.txt` |
| `--years` | `2020-2023`、`2020:2023`，或 `2020 2021 2022 2023` |
| `--output-dir` | 输出目录，默认 `filtered` |
| `--year-col` | 手动指定年份列（自动识别失败或选错时） |
| `--suffix` | 输出文件名后缀，默认 `_2020_2023` |
| `--inspect` | 只报告年份列和分布，不写文件 |
| `--batch-size` | 每块行数，默认 100000 |

`--input` 可以是一个目录（读取其中所有 `*.csv`），也可以是若干个文件：

```bash
python nvdrs_split.py --input data/nvdrs_18-30.csv data/nvdrs_31-40.csv --output-dir out/
```

年龄段标签直接从文件名里解析（匹配 `18-30`、`18_30`、`18 to 30` 这类模式）；
认不出来就用文件名本身作为标签。

## 输出结构

```
out/
├── labeled/
│   └── 18_30_labeled.csv                 原始列 + 4 个派生列
├── splits/
│   └── 18_30/
│       ├── construction__circumstance_known_yes.csv
│       ├── construction__circumstance_known_no.csv
│       ├── non_construction__circumstance_known_yes.csv
│       ├── ...                            （4 类 × 2 布尔 = 8 个文件/年龄段）
│       └── military__circumstance_known_undetermined.csv
├── summary_counts.csv                     长表：年龄段 × 职业类 × 各布尔计数
├── summary_crosstab_circumstance_known_yes.csv   宽表：年龄段 × 职业类（仅 known=Yes）
├── occupation_group_audit.csv             每个职业文本 → 归到哪类、命中哪条规则、几行
└── unmapped_occupations.csv               只命中兜底规则的职业值，需人工复核
```

`labeled/` 里加的 4 列：

| 列 | 含义 |
|---|---|
| `circumstance_known_bool` | `True` / `False` / `<NA>` |
| `occupation_group` | 四分类之一，或 `unclassified` |
| `occupation_group_rule` | 这行是靠哪条规则定的（见下） |
| `age_band` | 从文件名解析出的年龄段 |

`splits/` 各文件是原数据的一个**无重叠、无遗漏的划分**（测试里有断言校验）。

## 分类规则

### circumstance known

脚本自动寻找列名（忽略大小写/空格/下划线）：`CircumstancesKnown`、`CircumstanceKnown`
等；找不到就用 `--circumstance-col` 显式指定。

- `Yes` / `Y` / `True` / `1` / `是` → `True`
- `No` / `N` / `False` / `0` / `否` → `False`
- **其他一切**（空值、`Unknown`、`Not available`…）→ `<NA>`，**不会**被当成 `False`

`<NA>` 的行默认单独写成 `*_known_undetermined.csv`，方便你先看清有多少这种行；
确认要丢掉就加 `--drop-unknown-circumstance`。

### occupation 四分类

判定按阶段进行，先命中先定；每个阶段内部的优先级是
**military > construction > non_workforce > non_construction**。

**阶段 1：Census 2010 职业编码**（`OccupationCode` 等列，NVDRS 用的就是这套）

| 组 | 编码范围 |
|---|---|
| `military` | 9800–9830（Military Specific Occupations） |
| `construction` | 6200–6765（建筑工种及其一线主管） |
| `non_workforce` | 9840（从未工作 / 近 5 年无工作经历）、9920（失业无工作经历） |
| `non_construction` | 其余任何 < 9800 的有效民用编码 |

6800–6940（extraction，采掘业）**默认不算** construction。如果你的定义要把采掘
并进来，加 `--include-extraction`。

**阶段 2：职业自由文本**关键词匹配（编码缺失或落在无效区间时）。
先判"未知"类取值（`Unknown`、`N/A`…），再按 military → construction →
non_workforce 顺序匹配关键词。

**阶段 3：行业列**（可选）。前两阶段没定下来时，若行业编码为 `0770`（Census 2010
的 Construction）或行业文本含 `construction`，归入 `construction`。

**阶段 4：兜底**。职业文本非空但没命中任何关键词 → `non_construction`
（即"有职业、但不是建筑也不是军人"），并记入 `unmapped_occupations.csv`。
职业信息完全缺失 → `unclassified`。

`occupation_group_rule` 列会写明是哪一步定的：`occupation_code`、
`occupation_keyword`、`industry_code`、`industry_keyword`、`fallback_text`、
`occupation_unknown_value`、`missing_occupation`。

### 几个需要你确认的口径判断

这些是我按常见做法定的默认口径，不同意就改（改法见下一节）：

- **`military` = 现役**。`veteran`、`retired military` 这类文本归到 `non_workforce`
  （已退出劳动力），不算 military。NVDRS 另有专门的退伍军人变量，不在本脚本处理范围。
- **`non_workforce` = 不在劳动力人口中**：失业、退休、学生、家务、残障无法工作、
  被收容/服刑、无偿劳动等。
- **`non_construction` = 在职但非建筑、非军人**的民用职业。
- 四类互斥，加上 `unclassified` 后覆盖全部行。
- `painter` 默认算 construction（NVDRS 文本里多指建筑油漆工）。如果你的数据里
  艺术类画家不少，从关键词表里删掉它。

## 调整规则

导出默认规则、改完再喂回去：

```bash
python nvdrs_split.py --dump-rules > my_rules.json
# 编辑 my_rules.json
python nvdrs_split.py --input data/ --output-dir out/ --rules my_rules.json
```

`--rules` 是**增量覆盖**：只写你要改的键，其余沿用默认。`code_ranges` 和 `keywords`
这两个字典按**子键**合并，所以可以只覆盖其中一项；但被覆盖的那一项是**整体替换**
（列表不做合并），要写全你想要的完整列表。仓库里
`config/occupation_rules.default.json` 是默认值的副本，可直接当模板拷贝需要的列表。

比如把采掘业并入建筑（只需覆盖两个子键）：

```json
{
  "code_ranges": {
    "construction": [[6200, 6940]],
    "construction_extraction": []
  }
}
```

而要把 `painter` 从建筑关键词里去掉，就得把 `keywords.construction` 整个列表
（从默认 JSON 里拷出来、删掉 `painter` 和 `painting contractor`）写进覆盖文件。

**建议流程**：跑完先看 `unmapped_occupations.csv` 和 `occupation_group_audit.csv`，
把确实属于建筑/军人/非劳动力却掉进兜底的取值补进关键词表，再重跑一次。

## 全部参数

| 参数 | 说明 |
|---|---|
| `--input` | CSV 文件或目录，可多个 |
| `--output-dir` | 输出目录，默认 `out` |
| `--inspect` | 只报告检测到的列和取值分布，不写文件 |
| `--encoding` | 输入编码，默认 `utf-8`（失败自动回退 `latin-1`） |
| `--circumstance-col` | 手动指定 circumstance known 列 |
| `--occupation-col` | 手动指定职业文本列 |
| `--occupation-code-col` | 手动指定职业编码列 |
| `--industry-col` / `--industry-code-col` | 手动指定行业列（可选） |
| `--include-extraction` | 把 6800–6940 采掘业算作 construction |
| `--drop-unknown-circumstance` | 剔除 circumstance 既非 Yes 也非 No 的行 |
| `--no-keep-unclassified` | 不输出 `unclassified` 的拆分文件 |
| `--write-empty` | 空子集也写文件（默认跳过） |
| `--rules` | 规则覆盖 JSON |
| `--dump-rules` | 打印默认规则 JSON 后退出 |

## 核对布尔列：`verify_circumstance.py`

分批核对原始列 `circumstance_known_c`（Yes/No）与派生列 `circumstance_known_bool`
（TRUE/FALSE）是否一致。文件是**分块（chunked）读**的，几十万行也不占内存。

```bash
# 每 1000 行一批，走完整个文件
python verify_circumstance.py --input out/labeled/18_30_labeled.csv

# 只打印有问题的批次（文件大时最实用）
python verify_circumstance.py --input out/labeled/ --mismatches-only

# 翻页看：每批 500 行，一次只看 2 批，按提示用 --start-row 继续
python verify_circumstance.py --input file.csv --batch-size 500 --max-batches 2

# 把不一致的行和取值配对表写出来
python verify_circumstance.py --input out/labeled/ --output-dir report/
```

每批输出「原始值 × 布尔值」的计数，标 `[OK]` 或 `[N MISMATCH]`；不一致的行直接列出
行号、ID、两列取值、以及应该是什么。最后给全量交叉表和准确率。有不一致时退出码为 1，
可以直接串进脚本里。

判定标准就是映射规则本身：`Yes`→`TRUE`，`No`→`FALSE`，**其他一切**（空值、`Unknown`）
→`UNDETERMINED`（即 pandas 的 `<NA>`）。注意报告里用 `UNDETERMINED` 而不是 `<NA>` 或
`NA` —— 后两者是 pandas 的默认缺失值标记，写进 CSV 再读回来会变成 NaN。

如果你拆分时用了 `--rules` 改过 Yes/No 的取值集合，核对时传同一个文件：
`--rules my_rules.json`。

| 参数 | 说明 |
|---|---|
| `--input` | CSV 文件或目录，可多个 |
| `--raw-col` / `--bool-col` | 手动指定两列（默认自动识别） |
| `--id-col` | 不一致行旁边显示的标识列（默认找 IncidentID） |
| `--batch-size` | 每批行数，默认 1000 |
| `--start-row` / `--max-batches` | 翻页 |
| `--show-rows` | 每批最多打印几行不一致，默认 10 |
| `--mismatches-only` | 只打印有问题的批次 |
| `--output-dir` | 输出 `circumstance_mismatches.csv` 和 `circumstance_value_pairs.csv` |

## 测试

用合成数据（不含任何真实 NVDRS 记录）做端到端校验：

```bash
python tests/make_sample_data.py tests/sample_data   # 可选，单独生成样例
PYTHONPATH=tests python tests/test_nvdrs_split.py
python tests/test_verify_circumstance.py
python tests/test_filter_years.py
python tests/test_split_by_age.py
python tests/test_filter_construction.py
python tests/test_census_2018_only.py
python tests/test_run_construction_split.py
python tests/test_run_electrician_split.py
python tests/test_split_by_year.py
python tests/test_run_electrician_by_year.py
python tests/test_run_electrician_pipeline.py
```

`test_nvdrs_split.py` 覆盖：编码路径与关键词路径的分类正确性、`Yes/No/Unknown/空`
的布尔映射、8 个拆分文件齐全、拆分对原数据无重叠无遗漏、汇总计数自洽、兜底复核文件
内容、`--inspect` / `--drop-unknown-circumstance` / `--include-extraction` 各模式可运行。

`test_verify_circumstance.py` 会**故意注入 3 处错误**（Yes 配成 FALSE、No 配成 TRUE、
Unknown 被当成 FALSE），断言脚本恰好抓到这 3 行、行号正确、分块读不丢行，以及干净文件
报 100%。

`test_filter_years.py` 覆盖：年份边界精确（2019 和 2024 一定被排除）、只有 death year
时拒绝猜测、incident year 与 death year 同时存在时选对列、`input_list` / 目录 / `.txt`
三种输入等价、重复路径去重、分块读与单次读结果一致、日期格式取年、空值与不可解析年份
被单独报出。

`test_census_2018_only.py` 用矛盾数据证明 `nvdrs_split.py` 的职业分组只受 census_2018
影响，并断言两个脚本共用同一份码段定义。

`test_filter_construction.py` 覆盖：Census 2018 码段边界精确（6199/6766/6800/6951 都在
界外）、`--include-extraction` 与 `--include-managers` 的效果、三类互斥且完整、空白码
进独立文件、非数字码不被当成建筑、三个清单可回喂作输入、拒绝 2010 码列、不同 chunk-size
输出一致。

`test_split_by_age.py` 用 0–100 每个年龄各一行来逐个验证边界：各段恰好 10 行、合起来
正好覆盖 18–67、17 和 68 被排除、没有行进两个文件、写入+排除等于读入、不同 chunk-size
输出完全一致、表头只写一次、拒绝分类年龄列、拒绝表头不一致的合并、gzip 与普通输出内容
相同。

## 注意

本仓库不包含任何 NVDRS 数据。NVDRS 是限制性使用数据，请不要把数据文件提交进来
（`.gitignore` 已排除 `*.csv` 与常见数据目录）。
