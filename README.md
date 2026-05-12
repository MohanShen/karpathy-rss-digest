# Karpathy RSS Daily Digest

基于 [Andrej Karpathy](https://twitter.com/karpathy) 推荐的 92 个顶级科技博客 RSS 源，AI 自动生成中文解读，每日更新。

## ✨ 功能特点

- **智能筛选**：自动过滤非科技/AI/商业类内容，只收录高质量文章
- **AI 解读**：使用 DeepSeek 生成中文标题、摘要和详细解读
- **自动更新**：可在本机定时运行后推送 `docs/`（见 `CONTEXT.md`）；仓库内 **GitHub Actions 定时任务已关闭**，仍可在 Actions 里 **手动运行** `RSS Digest`
- **企业微信推送**：每期自动挑选 3-5 篇最重要的 AI/科技文章推送，附完整解读链接
- **GitHub Pages**：生成精美网页，公开访问

## 📦 数据来源

来自 Andrej Karpathy 推荐的 92 个顶级科技博客，包括：
- Paul Graham、Steve Blank 等创业导师博客
- OpenAI、Google Research 等科技公司研究博客
- Rust、Go 等编程语言官方博客
- Krebs on Security、Troy Hunt 等安全博客
- 等等...

## 🚀 快速开始

### 本地运行

```bash
# 克隆仓库
git clone https://github.com/alexisyang718-beep/karpathy-rss-digest.git
cd karpathy-rss-digest

# 安装依赖
pip install feedparser httpx beautifulsoup4 python-dateutil jinja2 openai

# 设置环境变量
export DEEPSEEK_API_KEY="your-api-key"

# 运行（默认只保留科技/AI/商业类文章）
python rss_reader.py

# 禁用内容筛选，收录所有文章
python rss_reader.py --no-filter

# 推送到企业微信
python rss_reader.py --webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"

# 抓取最近3天的内容
python rss_reader.py --days 3
```

## ⚙️ GitHub Actions 自动化

项目已配置 GitHub Actions 工作流 `RSS Digest`（**无定时 cron**，仅 **手动触发**）。日常可在本机生成后推送 `docs/`，详见根目录 **`CONTEXT.md`**：

1. 克隆本仓库
2. 在仓库 **Settings → Secrets** 中配置 `MINIMAX_API_KEY`（若仍要手动在 GitHub 上跑 workflow）
3. 需要时在 **Actions → RSS Digest → Run workflow** 手动运行

## 📋 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--days` | 抓取最近 N 天的内容 | 1 |
| `--output` | 输出格式 (markdown/html) | html |
| `--webhook` | 企业微信 Webhook URL | - |
| `--no-filter` | 禁用内容筛选 | - |

## 🔧 环境变量

| 变量名 | 说明 | 必需 |
|--------|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | ✅ |
| `WECOM_WEBHOOK_URL` | 企业微信 Webhook URL | ❌ |
| `ENABLE_CONTENT_FILTER` | 是否启用内容筛选 | true |

## 📁 目录结构

```
karpathy-rss-digest/
├── rss_reader.py        # 主程序
├── feeds.opml           # RSS 源列表
├── .github/
│   └── workflows/
│       └── rss-digest.yml  # GitHub Actions 配置
└── docs/                # GitHub Pages 输出
    ├── index.html       # 目录页
    └── 2026-02-28.html  # 每日精选
```

## 🎯 内容筛选

AI 会自动判断每篇文章的类别：

- **AI**：人工智能、机器学习、深度学习、LLM、GPT 等
- **科技**：软件开发、编程语言、系统架构、云计算、开源项目等
- **商业**：科技公司动态、创业、投资、商业模式、产品发布等
- **其他**：被过滤（如美食、娱乐、体育等非科技内容）

## 📱 企业微信推送效果

每期从当前批次文章中自动挑选 3-5 篇最重要的 AI/科技类文章推送，完整解读链接附在底部：

```
📡 Karpathy RSS 精选
> 2026-02-27 14:30  |  本期 18 篇，精选 5 篇

**1. OpenAI 发布 GPT-5 技术预览**
> OpenAI 官方博客 · 02-27 10:00
> GPT-5 在推理能力和多模态理解上实现了重大突破...

**2. Rust 2026 版本路线图发布**
> Rust Blog · 02-27 09:00
> 新版本将引入异步 trait 和更强大的类型系统...

> [👉 查看全部 18 篇完整中文解读](https://xxx.github.io/karpathy-rss-digest/2026-02-27.html)
```

挑选逻辑：AI 类 > 科技类 > 商业类，同类内按发布时间降序。

## 🙏 致谢

- [Andrej Karpathy](https://twitter.com/karpathy) - 推荐的博客列表：https://gist.github.com/emschwartz/e6d2bf860ccc367fe37ff953ba6de66b
这份清单并非随机挑选，而是 2025 年 Hacker News 上最受欢迎的年度博客合集。Karpathy 的逻辑很直接：比起算法推荐的碎片，这些长文博客能提供更高密度的思考。
<img width="1080" height="1060" alt="image" src="https://github.com/user-attachments/assets/4e8cfae4-3edf-4bca-a996-5d285e71858f" />

- [DeepSeek](https://deepseek.com) - AI 摘要生成

## 📄 License

MIT
