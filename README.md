# 白糖日报生成系统

每个工作日自动生成标准化的白糖期货日报草稿，并自动推送到网站。

## 快速开始

### 1. 安装 Python 依赖

```powershell
cd sugar-daily
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

## Brazil Sugar Metrics

Sugar News now maintains a Brazil dashboard area named `巴西糖价与库存`.
It is populated by the daily Sugar News workflow and contains three dynamic cards:
Brazil VHP raw sugar FOB premium/discount, Brazil sugar stock, and Brazil ethanol stock.

These values, data dates and YoY calculations must come from fetched sources and
historical records. They must not be hardcoded in production code. ANP is the
priority source for Brazil sugar and ethanol stock. If ANP does not expose a
verifiable standalone food-sugar stock field, the dashboard must show
`ANP暂未检索到可核实的食糖库存数据` and must not relabel ethanol, syrup, cane,
production, sales or derived balance data as sugar stock.

### 2. 配置 DeepSeek

```powershell
copy .env.example .env
# 编辑 .env，填入真实 API Key
```

### 3. 运行

```powershell
py scripts/run_daily.py
```

日报输出到 `outputs/YYYY-MM-DD/白糖日报_YYYYMMDD.md`，同时自动更新前端 JSON。

## 本地运行

```powershell
py scripts\run_daily.py
```

## 单独更新网页数据

```powershell
py scripts\update_web_reports.py
```

## 本地预览

不要直接双击 HTML 测试 JSON 加载，浏览器可能拦截本地 `fetch`。

使用：

```powershell
py -m http.server 8000
```

浏览器打开：http://localhost:8000

## 全自动流程

每天 05:40 定时任务自动执行以下全部步骤，无需人工干预：

```
05:40  定时任务触发
  → 抓取行情和基本面数据
  → 调用 DeepSeek 生成日报
  → 保存日报 Markdown
  → 生成前端 JSON
  → git add + commit + push
  → Vercel 自动部署
  → 网页更新完成
```

**你只需要：** 每天早上打开网站查看日报。

**前提条件：** 电脑必须在 05:40 处于开机状态（睡眠可唤醒，关机不执行）。

## 手动推送（备用）

如果自动推送失败，可手动执行：

```powershell
cd d:\Desktop\ai\sugar-daily
git add public/data
git commit -m "Update report"
git push
```

## Vercel 部署

1. 将 GitHub 仓库导入 Vercel
2. Framework Preset 选择 **Other**
3. 不配置 DeepSeek API Key 到前端
4. 前端只读取已经生成的 JSON
5. 每次 GitHub 有新提交后，Vercel 自动重新部署

## Sugar News 看板

Sugar News 使用现有 Vercel 项目发布，不新建项目或域名。看板路由：

```text
https://sugar-daily-cc.vercel.app/sugar-news
```

Excel 和网页 JSON 共用同一份已核验新闻清单：

```text
../Sugar News/data/verified_news/YYYY/MM/sugar_news_YYYY-MM-DD.json
```

生成后的网页数据保存到：

```text
public/sugar-news/data/reports/YYYY/MM/YYYY-MM-DD.json
public/sugar-news/data/index.json
public/sugar-news/data/status.json
```

手动运行一次完整 Sugar News 更新：

```powershell
PowerShell -NoProfile -ExecutionPolicy Bypass -File scripts\Run-Sugar-News.ps1 -Date 2026-07-19
```

只生成并本地校验，不推送时可直接运行：

```powershell
.venv\Scripts\python.exe scripts\sugar_news_pipeline.py --date 2026-07-19 --task-root "..\Sugar News" --offline-only
.venv\Scripts\python.exe scripts\verify_sugar_news_dashboard.py --date 2026-07-19
```

GitHub Actions 自动任务：

- 北京时间 06:00 执行；
- 北京时间 06:10 和 06:30 重试；
- GitHub Actions cron 使用 UTC，配置为 `0 22 * * *`、`10 22 * * *`、`30 22 * * *`；
- 日期计算固定使用 `Asia/Shanghai`，目标日期为北京时间上一自然日。

如果没有已核验新闻清单，云端任务会记录检索尝试日志并失败退出，不覆盖上一期正常页面，避免发布空白或未经核验的数据。

Sugar News 国家展示顺序固定为巴西、印度、泰国、中国、其他国家。中国作为独立重点国家检索和展示，不归入其他国家；没有中国重要新增新闻时不显示空白中国板块。

印度板块同时维护“印度糖价与库存”指标卡，数据来源于同一份已核验 Sugar News 结构化数据或最近一期有效看板数据。指标包括印度国内代表糖价、北方邦糖厂出厂价、印度食糖结转库存及其变化；价格由程序在 `₹/quintal` 与 `₹/kg` 间换算，库存由程序在 `lakh tonnes`、`million tonnes` 与 `万吨` 间换算。没有完成日期、口径和来源核验的价格或库存不得编造，指标卡显示“数据待更新”；没有新结转库存数据时仅保留最近一期有效数据及原始发布日期，不重复生成库存新闻。

## 安装定时任务（可选）

```powershell
# 安装（每天 05:40 自动执行）
PowerShell -ExecutionPolicy Bypass -File scripts\Install-Task.ps1

# 卸载
PowerShell -ExecutionPolicy Bypass -File scripts\Install-Task.ps1 -Uninstall
```

定时任务会执行 `scripts/Run-Daily.ps1`：

1. 生成当日 `outputs/YYYY-MM-DD/白糖日报_YYYYMMDD.md`。
2. 检查当日 Markdown、`public/data/reports/YYYY-MM-DD.json` 和 `reports.json` 是否存在。
3. 如果当日文件缺失，自动补跑一次生成流程。
4. 执行 `scripts/Publish-Web.ps1`，提交并推送 `public/data` 与 dashboard 文件，触发 Vercel 自动部署。
5. 轮询访问 `https://sugar-daily-cc.vercel.app/public/data/reports.json` 和 `public/data/reports/YYYY-MM-DD.json`，确认线上最新日报日期等于当天。
6. 校验 `https://sugar-daily-cc.vercel.app/public/dashboard/sugar_basis_dashboard_data.json` 和 HTML 页面可访问。
7. 只有 Vercel 日报和 dashboard 都校验通过，工作流才算完成；否则任务失败并保留本地提交等待下次 push。
8. 运行日志写入 `outputs/task_YYYYMMDD.log`。

## 数据源

| 数据 | 来源 | 说明 |
|------|------|------|
| 郑糖行情 | 新浪财经 SR0 | 公开接口，展示为"郑糖主力合约" |
| 美糖行情 | 新浪财经 RS | 公开接口，展示为"ICE原糖主力合约" |
| 南宁现货 | `market_fallback.csv` | 无稳定公开免费数据源 |
| 巴西进口利润 | 泛糖科技 | "食糖进口成本及利润估算"，CSV仅作备用 |
| 巴西基本面 | UNICA | Bi-weekly bulletin |
| 印度基本面 | NFCSF/coopsugar | ALL INDIA Sugar Production |
| 泰国基本面 | SugarZone + OCSB官方多入口 | 优先级: SugarZone → Open Data → PRD → 主站 → Facebook → 缓存 → 预测 |
| 中国基本面 | 沐甜产销预估栏目 + 各省产销栏目 | 优先级: 产销预估栏目 → 各省产销 → 糖协 → 泛糖 → 缓存 |
| 基本面 | 自动抓取 + 研究员填写 | `inputs/fundamentals/` 为可选补充 |
| 交易策略 | 研究员确认 | `inputs/approved_view.md` |

## 日报写作规则

以下规则适用于提示词、模板、兜底文案、生成后清洗和最终校验。

1. 正文不得出现数据缺失提示类表述。
   - 禁止写“暂无最新对比数据”“暂无可比数据”“暂无最新数据”“暂未更新”“数据尚未公布”“暂无数据”“对比数据不足”“数据缺失”“尚未公布”等类似表述。
   - 某项最新数据或对比数据缺失时，只使用当前能够确认的有效数据进行客观描述。
   - 无法形成有效结论时，直接省略该项对比内容。
   - 不得为了填充内容编造数据，也不得在正文中加入影响报告完整性的缺失提示。

2. 产量单位必须规范转换。
   - 不得照抄网页、接口、数据库或原始文件中的英文单位和字段代码。
   - `lmt` / `Lmts` 统一按 `1 lmt = 10万吨` 换算为中文单位。
   - 例如 `273.90lmt` 或 `273.90 Lmts` 必须写为 `2739万吨`，正文不得保留 `lmt`。

3. 国际糖价走势表述以震荡为基础判断。
   - 常规句式统一为“国际糖价预计维持震荡格局。”
   - 不得默认写“国际糖价维持震荡偏强格局。”
   - 使用“偏强”“震荡偏强”“价格表现偏强”前，必须确认 ICE 原糖价格能够在 15 美分/磅以上维持。
   - ICE 未达到 15 美分/磅、只是短暂突破、或无法确认是否持续维持时，统一写“国际糖价预计维持震荡格局。”

## 项目结构

```
sugar-daily/
├── index.html                    # 前端页面（Vercel 部署入口）
├── public/
│   └── data/
│       ├── reports.json          # 日报索引（前端读取）
│       └── reports/
│           ├── 2026-06-14.json   # 每日日报 JSON
│           └── 2026-06-13.json
├── data/
│   └── sugar_daily_data.csv      # 基本面数据台账
├── outputs/
│   └── YYYY-MM-DD/
│       └── 白糖日报_YYYYMMDD.md  # 日报 Markdown
├── inputs/
│   ├── fundamentals/
│   │   └── YYYY-MM-DD.md
│   ├── approved_view.md
│   └── market_fallback.csv
├── scripts/
│   ├── run_daily.py              # 日报生成主程序
│   ├── update_web_reports.py     # 解析日报 → 前端 JSON
│   ├── fetch_market.py           # 行情抓取
│   ├── fetch_fundamentals.py     # 基本面抓取
│   ├── update_data_csv.py        # CSV 管理
│   ├── Run-Daily.ps1             # 定时任务脚本
│   ├── Publish-Web.ps1           # 推送到 GitHub
│   └── Install-Task.ps1          # 安装定时任务
├── config.yaml
├── .env.example
├── .gitignore
└── README.md
```

## 容错策略

| 情况 | 行为 |
|------|------|
| 新浪接口超时 | 自动重试 2 次，仍失败则回退到 CSV |
| 网络 + CSV 均不可用 | 生成 FAILED 日报 |
| 基本面文件缺失 | 跳过模型调用，标注"待人工补充" |
| DeepSeek 调用失败 | 基本面标注"生成失败，待人工补充" |
| 前端 JSON 更新失败 | 不影响日报生成，保留历史 JSON |
