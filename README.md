# 白糖日报生成系统

每个工作日自动生成标准化的白糖期货日报草稿，供研究员审阅后发布。

## 快速开始

### 1. 安装 Python 依赖

```powershell
cd sugar-daily
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 配置 DeepSeek

```powershell
copy .env.example .env
# 编辑 .env，填入真实 API Key
```

### 3. 运行

```powershell
python scripts/run_daily.py
```

日报输出到 `outputs/YYYY-MM-DD/白糖日报_YYYYMMDD.md`，同时自动更新前端 JSON。

## 本地运行

```powershell
.\.venv\Scripts\python.exe scripts\run_daily.py
```

## 单独更新网页数据

```powershell
.\.venv\Scripts\python.exe scripts\update_web_reports.py
```

## 本地预览

不要直接双击 HTML 测试 JSON 加载，浏览器可能拦截本地 `fetch`。

使用：

```powershell
.\.venv\Scripts\python.exe -m http.server 8000
```

浏览器打开：http://localhost:8000

## 推送 GitHub

```powershell
PowerShell -ExecutionPolicy Bypass -File scripts\Publish-Web.ps1
```

## Vercel 部署

1. 将 GitHub 仓库导入 Vercel
2. Framework Preset 选择 **Other**
3. 不配置 DeepSeek API Key 到前端
4. 前端只读取已经生成的 JSON
5. 每次 GitHub 有新提交后，Vercel 自动重新部署

## 安装定时任务（可选）

```powershell
# 安装（每天 05:40 自动执行，目标 06:00 前完成）
PowerShell -ExecutionPolicy Bypass -File scripts\Install-Task.ps1

# 卸载
PowerShell -ExecutionPolicy Bypass -File scripts\Install-Task.ps1 -Uninstall
```

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
| 基本面 | 自动抓取 + 研究员填写 | `inputs/fundamentals/` 为可选补充 |
| 交易策略 | 研究员确认 | `inputs/approved_view.md` |

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
