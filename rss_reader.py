#!/usr/bin/env python3
"""
Karpathy RSS Daily Digest
基于 Andrej Karpathy 推荐的 92 个顶级科技博客 RSS 源，
每天自动抓取文章全文，用 AI 生成高质量中文标题、摘要和详细解读，
生成可公开访问的网页（GitHub Pages），并推送精选到企业微信群。

用法:
    python rss_reader.py                          # 抓取并生成今日精选
    python rss_reader.py --days 3                 # 抓取最近3天的内容
    python rss_reader.py --webhook <URL>          # 抓取并推送到企业微信群
"""

import asyncio
import argparse
import hashlib
import html as html_mod
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from jinja2 import Template
from openai import OpenAI

# ── 日志配置 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────
MAX_CONCURRENT = 20
MAX_FETCH_PAGE = 10
REQUEST_TIMEOUT = 15.0
PAGE_TIMEOUT = 20.0
MAX_ARTICLES_NO_DATE = 3
MAX_CONTENT_LEN = 2000
LLM_BATCH_SIZE = 5
WECOM_MSG_MAX_LEN = 4096
BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
DOCS_DIR = BASE_DIR / "docs"          # GitHub Pages 目录
FEEDS_FILE = BASE_DIR / "feeds.opml"
SENT_DB_FILE = OUTPUT_DIR / ".sent_articles.json"

# 内容筛选配置（默认只保留科技/AI/商业相关内容）
ENABLE_CONTENT_FILTER = os.environ.get("ENABLE_CONTENT_FILTER", "true").lower() != "false"

# MiniMax API 配置
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY")
MINIMAX_BASE_URL = "https://api.minimax.chat/v1"
MINIMAX_MODEL = "MiniMax-M2.7"

# GitHub Pages 配置（自动从 GITHUB_REPOSITORY 推断，也可通过环境变量覆盖）
_default_pages_url = ""
_gh_repo = os.environ.get("GITHUB_REPOSITORY", "")  # GitHub Actions 内置，格式: owner/repo
if _gh_repo:
    _owner, _repo = _gh_repo.split("/", 1)
    _default_pages_url = f"https://{_owner}.github.io/{_repo}"
GITHUB_PAGES_URL = os.environ.get("GITHUB_PAGES_URL", _default_pages_url)


# ── 数据模型 ──────────────────────────────────────────────
@dataclass
class FeedSource:
    name: str
    xml_url: str
    html_url: str


@dataclass
class Article:
    title: str
    link: str
    source: str
    published: Optional[datetime] = None
    summary: str = ""
    author: str = ""
    tags: list = field(default_factory=list)
    full_content: str = ""
    ai_title: str = ""
    ai_summary: str = ""        # 一句话摘要（企微推送用）
    ai_detail: str = ""         # 详细中文解读（网页展示用）
    category: str = ""          # AI判断的类别：科技/AI/商业/其他
    is_relevant: bool = True    # 是否属于科技/AI/商业相关内容


# ── 已推送文章去重 ────────────────────────────────────────
def _article_id(article: Article) -> str:
    return hashlib.md5(article.link.encode()).hexdigest()


def load_sent_db() -> dict:
    if SENT_DB_FILE.exists():
        try:
            data = json.loads(SENT_DB_FILE.read_text(encoding="utf-8"))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            return {k: v for k, v in data.items() if v.get("sent_at", "") > cutoff}
        except Exception:
            return {}
    return {}


def save_sent_db(db: dict):
    SENT_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENT_DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_new_articles(articles: list[Article], sent_db: dict) -> list[Article]:
    return [a for a in articles if _article_id(a) not in sent_db]


def mark_as_sent(articles: list[Article], sent_db: dict) -> dict:
    for a in articles:
        sent_db[_article_id(a)] = {
            "title": a.title, "link": a.link,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
    return sent_db


# ── OPML 解析 ─────────────────────────────────────────────
def parse_opml(filepath: Path) -> list[FeedSource]:
    tree = ET.parse(filepath)
    root = tree.getroot()
    feeds = []
    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl")
        if xml_url:
            feeds.append(FeedSource(
                name=outline.get("text", outline.get("title", "Unknown")),
                xml_url=xml_url,
                html_url=outline.get("htmlUrl", ""),
            ))
    logger.info(f"从 OPML 中解析到 {len(feeds)} 个 RSS 源")
    return feeds


# ── RSS 抓取 ──────────────────────────────────────────────
def clean_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_date(entry: dict) -> Optional[datetime]:
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            try:
                dt = date_parser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                continue
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        val = entry.get(key)
        if val:
            try:
                from time import mktime
                dt = datetime.fromtimestamp(mktime(val), tz=timezone.utc)
                return dt
            except (ValueError, TypeError, OverflowError):
                continue
    return None


async def fetch_feed(client: httpx.AsyncClient, source: FeedSource, since: datetime) -> list[Article]:
    articles = []
    try:
        resp = await client.get(source.xml_url, follow_redirects=True)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        has_any_date = any(parse_date(e) is not None for e in feed.entries)
        collected = 0
        for entry in feed.entries:
            pub_date = parse_date(entry)
            if pub_date and pub_date < since:
                continue
            if pub_date is None:
                if has_any_date:
                    continue
                else:
                    collected += 1
                    if collected > MAX_ARTICLES_NO_DATE:
                        continue
            content_raw = ""
            if entry.get("content"):
                content_raw = entry["content"][0].get("value", "")
            if not content_raw:
                content_raw = entry.get("summary", "") or entry.get("description", "") or ""
            summary = clean_html(content_raw)
            tags = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
            articles.append(Article(
                title=entry.get("title", "无标题"),
                link=entry.get("link", source.html_url),
                source=source.name,
                published=pub_date,
                summary=summary[:500] if summary else "",
                author=entry.get("author", ""),
                tags=tags[:5],
                full_content=summary,
            ))
    except httpx.TimeoutException:
        logger.warning(f"⏰ 超时: {source.name} ({source.xml_url})")
    except httpx.HTTPStatusError as e:
        logger.warning(f"❌ HTTP {e.response.status_code}: {source.name}")
    except Exception as e:
        logger.warning(f"⚠️  失败: {source.name} - {type(e).__name__}: {e}")
    return articles


async def fetch_all_feeds(feeds: list[FeedSource], since: datetime) -> list[Article]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    async def bounded_fetch(client, source):
        async with semaphore:
            return await fetch_feed(client, source, since)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        headers={"User-Agent": "KarpathyRSS-DailyDigest/1.0"},
        limits=httpx.Limits(max_connections=MAX_CONCURRENT, max_keepalive_connections=10),
    ) as client:
        tasks = [bounded_fetch(client, feed) for feed in feeds]
        results = await asyncio.gather(*tasks)
    all_articles = []
    for result in results:
        all_articles.extend(result)
    all_articles.sort(
        key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    logger.info(f"共抓取到 {len(all_articles)} 篇文章")
    return all_articles


# ── 网页全文抓取 ──────────────────────────────────────────
def extract_text_from_html(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                      "form", "iframe", "noscript", "svg", "img"]):
        tag.decompose()
    article = soup.find("article")
    if article:
        text = article.get_text(separator="\n", strip=True)
    else:
        for selector in [".post-content", ".entry-content", ".article-body",
                         ".content", "main", "#content", ".post"]:
            container = soup.select_one(selector)
            if container and len(container.get_text(strip=True)) > 200:
                text = container.get_text(separator="\n", strip=True)
                break
        else:
            text = soup.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines)


async def fetch_page_content(client: httpx.AsyncClient, article: Article) -> str:
    try:
        resp = await client.get(article.link, follow_redirects=True)
        resp.raise_for_status()
        text = extract_text_from_html(resp.text)
        if len(text) > 200:
            return text
    except Exception as e:
        logger.debug(f"网页抓取失败 {article.link}: {e}")
    return ""


async def enrich_articles_with_full_content(articles: list[Article]):
    logger.info(f"📄 开始抓取 {len(articles)} 篇文章全文...")
    semaphore = asyncio.Semaphore(MAX_FETCH_PAGE)
    async def bounded_fetch(client, article):
        async with semaphore:
            content = await fetch_page_content(client, article)
            if content:
                article.full_content = content
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(PAGE_TIMEOUT),
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        limits=httpx.Limits(max_connections=MAX_FETCH_PAGE, max_keepalive_connections=5),
    ) as client:
        tasks = [bounded_fetch(client, a) for a in articles]
        await asyncio.gather(*tasks)
    has_content = sum(1 for a in articles if len(a.full_content) > 200)
    logger.info(f"✅ 成功获取 {has_content}/{len(articles)} 篇文章全文")


# ── AI 摘要生成 ──────────────────────────────────────────
def create_llm_client() -> OpenAI:
    return OpenAI(api_key=MINIMAX_API_KEY, base_url=MINIMAX_BASE_URL)


SUMMARIZE_PROMPT = """\
你是科技编辑。判断文章类别并生成中文标题和摘要。

类别（category 字段必须且只能是下面四个字之一，不要用英文、不要组合、不要解释）：
- AI：人工智能、机器学习、LLM、深度学习等
- 科技：软件开发、编程、安全、密码学、硬件、云计算、数学/算法在工程中的应用等
- 商业：科技公司、创业、投资、产品发布等
- 其他：纯生活、健康医疗、美食、体育、文艺、与科技行业无关的社会评论等

RSS 列表里的博客多为科技/开发者向：涉及编程、安全、工程、数学、产品技术、行业分析的，应归入 AI 或 科技，不要轻易标为其他。

非「其他」类时 is_relevant 必须为 true，并填写 title 与 summary；为「其他」时 is_relevant 为 false，title/summary 可为空字符串。

只输出一行合法 JSON（不要 markdown、不要前后说明）：
{"category": "AI", "is_relevant": true, "title": "中文标题(≤30字)", "summary": "一句话摘要(≤80字)"}
"""

DETAIL_PROMPT = """\
你是资深科技编辑。用5-8句话写完整中文解读：第一段讲文章内容，第二段提炼核心观点/数据，第三段说对从业者的启发。专有名词保留英文（GPT、Transformer、Rust等）。只输出解读文本，不加其他内容。
"""


def _coerce_bool(v, default: bool = True) -> bool:
    """模型或 JSON 可能返回字符串形式的布尔值。"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1", "是"):
            return True
        if s in ("false", "no", "0", "否"):
            return False
    return default


def normalize_category_and_relevance(data: dict) -> tuple[str, bool]:
    """
    将 MiniMax 返回的 category / is_relevant 规范为四字类别 + 布尔。
    修复：缺省 category 被当成「其他」导致整批被误杀；英文类别名未映射等。
    """
    raw_cat = data.get("category")
    if raw_cat is not None and not isinstance(raw_cat, str):
        raw_cat = str(raw_cat)
    raw_cat = (raw_cat or "").strip()
    # 模型照抄示例里的占位符
    if raw_cat in ("AI/科技/商业/其他", "AI、科技、商业、其他", "AI/科技/商业"):
        raw_cat = ""

    is_rel = _coerce_bool(data.get("is_relevant"), default=True)
    c_lower = raw_cat.lower()

    # 已是规范四字之一
    if raw_cat in ("AI", "科技", "商业", "其他"):
        canonical = raw_cat
    elif any(
        x in c_lower
        for x in (
            "artificial intelligence",
            "machine learning",
            "deep learning",
            " llm",
            "neural",
            "gpt",
        )
    ) or "人工智能" in raw_cat or "机器学习" in raw_cat:
        canonical = "AI"
    elif any(
        x in c_lower
        for x in (
            "technology",
            "tech",
            "software",
            "developer",
            "programming",
            "engineering",
            "cyber",
            "security",
            "cryptograph",
            "hardware",
            "cloud",
            "devops",
            "algorithm",
            "math",
        )
    ) or any(
        x in raw_cat
        for x in ("编程", "开发", "安全", "密码", "算法", "软件", "硬件", "网络", "系统", "工程", "数学", "技术")
    ):
        canonical = "科技"
    elif any(x in c_lower for x in ("business", "startup", "venture", "invest", "finance")) or any(
        x in raw_cat for x in ("商业", "创业", "投资", "融资", "上市")
    ):
        canonical = "商业"
    elif "AI" in raw_cat or "科技" in raw_cat or "商业" in raw_cat:
        if "AI" in raw_cat or "人工智能" in raw_cat:
            canonical = "AI"
        elif "商业" in raw_cat:
            canonical = "商业"
        else:
            canonical = "科技"
    elif not raw_cat:
        # 未返回 category：不要默认「其他」误杀；结合 is_relevant
        if is_rel:
            canonical = "科技"
        else:
            canonical = "其他"
    else:
        # 无法识别的非空串：若模型认为相关则归为科技，否则其他
        canonical = "科技" if is_rel else "其他"

    if canonical == "其他":
        is_rel = False
    else:
        # 非其他类：以模型 is_relevant 为准，缺省为 true
        is_rel = _coerce_bool(data.get("is_relevant"), default=True)

    return canonical, is_rel


def summarize_with_llm(client: OpenAI, articles: list[Article]) -> list[dict]:
    results = []
    for article in articles:
        content = article.full_content or article.summary or ""
        if not content:
            results.append({"title": article.title, "summary": "", "category": "其他", "is_relevant": False})
            continue
        content_trimmed = content[:MAX_CONTENT_LEN]
        user_msg = f"原标题: {article.title}\n来源: {article.source}\n\n{content_trimmed}"
        try:
            response = client.chat.completions.create(
                model=MINIMAX_MODEL,
                messages=[
                    {"role": "system", "content": SUMMARIZE_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=320,
            )
            resp_text = response.choices[0].message.content.strip()
            json_match = re.search(r'\{.*\}', resp_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                category, is_relevant = normalize_category_and_relevance(data)
                results.append({
                    "title": data.get("title", article.title) if is_relevant else article.title,
                    "summary": data.get("summary", "") if is_relevant else "",
                    "category": category,
                    "is_relevant": is_relevant,
                })
            else:
                if os.environ.get("RSS_DEBUG_LLM", "").lower() in ("1", "true", "yes"):
                    logger.warning(f"LLM 未返回可解析 JSON，原文片段: {resp_text[:400]!r}")
                results.append({"title": article.title, "summary": "", "category": "其他", "is_relevant": False})
        except Exception as e:
            logger.warning(f"LLM 摘要失败 [{article.title[:30]}]: {e}")
            results.append({"title": article.title, "summary": article.summary, "category": "其他", "is_relevant": False})
    return results


def enrich_detail_with_llm(client: OpenAI, articles: list[Article]) -> None:
    """对已过滤的相关文章补充详细中文解读（网页展示用）"""
    for article in articles:
        content = article.full_content or article.summary or ""
        if not content:
            article.ai_detail = article.ai_summary
            continue
        content_trimmed = content[:MAX_CONTENT_LEN]
        user_msg = f"标题: {article.ai_title or article.title}\n来源: {article.source}\n\n{content_trimmed}"
        try:
            response = client.chat.completions.create(
                model=MINIMAX_MODEL,
                messages=[
                    {"role": "system", "content": DETAIL_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.5,
                max_tokens=400,
            )
            article.ai_detail = response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"LLM 详细解读失败 [{article.title[:30]}]: {e}")
            article.ai_detail = article.ai_summary


def ai_summarize_articles(articles: list[Article], enable_filter: bool = True) -> list[Article]:
    if not articles:
        return articles
    logger.info(f"🧠 开始用 AI 生成 {len(articles)} 篇文章的中文解读...")
    if enable_filter:
        logger.info("   📌 内容筛选已启用：只保留科技/AI/商业相关内容")
    client = create_llm_client()
    total = len(articles)
    for i in range(0, total, LLM_BATCH_SIZE):
        batch = articles[i:i + LLM_BATCH_SIZE]
        batch_num = i // LLM_BATCH_SIZE + 1
        total_batches = (total + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE
        logger.info(f"  处理批次 {batch_num}/{total_batches} ({len(batch)} 篇)")
        results = summarize_with_llm(client, batch)
        for j, result in enumerate(results):
            idx = i + j
            articles[idx].ai_title = result["title"]
            articles[idx].ai_summary = result["summary"]
            articles[idx].category = result["category"]
            articles[idx].is_relevant = result["is_relevant"]

    if enable_filter:
        relevant_articles = [a for a in articles if a.is_relevant]
        filtered_out = len(articles) - len(relevant_articles)
        if filtered_out > 0:
            logger.info(f"✅ 分类完成: 保留 {len(relevant_articles)} 篇相关文章, 过滤掉 {filtered_out} 篇非科技/AI/商业内容")
        else:
            logger.info(f"✅ 分类完成: 全部 {len(relevant_articles)} 篇文章均为相关内容")
    else:
        relevant_articles = articles
        logger.info(f"✅ 分类完成: {len(articles)} 篇文章")

    # 仅对相关文章生成详细解读（网页展示用）
    if relevant_articles:
        logger.info(f"🧠 生成 {len(relevant_articles)} 篇相关文章的详细中文解读...")
        enrich_detail_with_llm(client, relevant_articles)
        logger.info(f"✅ 详细解读生成完成")

    return relevant_articles


# ── 企业微信推送 ──────────────────────────────────────────
def _utf8_len(text: str) -> int:
    """计算字符串的 UTF-8 字节长度（企业微信按字节限制）"""
    return len(text.encode("utf-8"))


def _select_top_articles(articles: list[Article], n: int = 5) -> list[Article]:
    """从所有文章中挑选最重要的 n 篇：AI 类优先，其次科技，其次商业，同类按发布时间降序"""
    priority = {"AI": 0, "科技": 1, "商业": 2}
    sorted_articles = sorted(
        articles,
        key=lambda a: (
            priority.get(a.category, 3),
            -(a.published.timestamp() if a.published else 0),
        ),
    )
    return sorted_articles[:n]


def _build_wecom_markdown(articles: list[Article], page_url: str = "", total_count: int = 0) -> str:
    """构建企业微信 Markdown 消息：精简摘要 + 底部完整解读链接"""
    header = f"📡 **Karpathy RSS 精选**\n> {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if total_count > len(articles):
        header += f"  |  本期 {total_count} 篇，精选 {len(articles)} 篇"
    header += "\n\n"

    body = ""
    for i, a in enumerate(articles):
        title = a.ai_title or a.title
        summary = a.ai_summary or ""
        time_str = a.published.strftime('%m-%d %H:%M') if a.published else "近期"

        block = f"**{i + 1}. {title}**\n"
        block += f"> {a.source} · {time_str}\n"
        if summary:
            if len(summary) > 80:
                summary = summary[:80] + "..."
            block += f"> {summary}\n"
        block += "\n"
        body += block

    footer = ""
    if page_url:
        footer = f"> [👉 查看全部 {total_count} 篇完整中文解读]({page_url})" if total_count > len(articles) else f"> [👉 查看完整中文解读]({page_url})"

    return header + body + footer


async def send_to_wecom(webhook_url: str, articles: list[Article], page_url: str = ""):
    if not articles:
        logger.info("没有新文章需要推送")
        return
    total_count = len(articles)
    top_articles = _select_top_articles(articles, n=5)
    msg = _build_wecom_markdown(top_articles, page_url, total_count=total_count)
    logger.info(f"📤 向企业微信推送精选 {len(top_articles)}/{total_count} 篇文章")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        payload = {"msgtype": "markdown", "markdown": {"content": msg}}
        try:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode") == 0:
                logger.info(f"  ✅ 消息发送成功")
            else:
                logger.warning(f"  ❌ 消息发送失败: {result}")
        except Exception as e:
            logger.error(f"  ❌ 消息发送异常: {e}")


# ── 分类 ──────────────────────────────────────────────────
def categorize_articles(articles: list[Article]) -> dict[str, list[Article]]:
    """使用AI判断的category进行分类"""
    # 定义类别到展示名称的映射
    category_mapping = {
        "AI": "🤖 AI / 机器学习",
        "科技": "💻 科技 / 技术",
        "商业": "📈 商业 / 行业",
    }
    
    categories = {
        "🤖 AI / 机器学习": [],
        "💻 科技 / 技术": [],
        "📈 商业 / 行业": [],
    }
    
    for a in articles:
        cat = a.category or "其他"
        display_name = category_mapping.get(cat)
        if display_name and display_name in categories:
            categories[display_name].append(a)
    
    # 移除空分类
    return {k: v for k, v in categories.items() if v}


# ── 页面生成 ──────────────────────────────────────────────
HTML_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Karpathy RSS 实时精选 - {{ date }}</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans SC', sans-serif; background: #0a0a0a; color: #e0e0e0; line-height: 1.8; }
  .container { max-width: 860px; margin: 0 auto; padding: 30px 20px; }
  h1 { font-size: 1.6em; margin-bottom: 6px; color: #fff; }
  .site-desc { color: #666; font-size: 0.85em; margin-bottom: 20px; }
  .meta { color: #888; font-size: 0.88em; margin-bottom: 30px; border-bottom: 1px solid #222; padding-bottom: 15px; }
  .category { margin-bottom: 35px; }
  .category h2 { font-size: 1.2em; color: #4fc3f7; margin-bottom: 15px; padding-bottom: 8px; border-bottom: 1px solid #1a1a1a; }
  .article { background: #111; border-radius: 12px; padding: 22px 24px; margin-bottom: 16px; border: 1px solid #1e1e1e; transition: border-color 0.2s, transform 0.1s; }
  .article:hover { border-color: #333; transform: translateY(-1px); }
  .article h3 { font-size: 1.08em; margin-bottom: 8px; color: #fff; line-height: 1.5; }
  .article-meta { font-size: 0.8em; color: #666; margin-bottom: 12px; display: flex; gap: 16px; flex-wrap: wrap; align-items: center; }
  .article-meta .category-tag { background: #1a3a2a; color: #4ade80; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }
  .detail { font-size: 0.93em; color: #bbb; line-height: 1.9; margin-bottom: 14px; white-space: pre-line; }
  .read-original { display: inline-block; font-size: 0.85em; color: #4fc3f7; text-decoration: none; padding: 6px 16px; border: 1px solid #2a3a4a; border-radius: 6px; transition: all 0.2s; }
  .read-original:hover { background: #1a2a3a; border-color: #4fc3f7; }
  .tags { margin-bottom: 10px; }
  .tag { display: inline-block; background: #1a1a1a; color: #4fc3f7; font-size: 0.72em; padding: 2px 8px; border-radius: 4px; margin-right: 5px; margin-bottom: 4px; }
  .footer { text-align: center; color: #444; font-size: 0.78em; margin-top: 50px; padding-top: 20px; border-top: 1px solid #1a1a1a; }
  .footer a { color: #4fc3f7; text-decoration: none; }
  .toc { background: #111; border-radius: 12px; padding: 20px 24px; margin-bottom: 30px; border: 1px solid #1e1e1e; }
  .toc h3 { font-size: 0.95em; color: #888; margin-bottom: 10px; }
  .toc ul { list-style: none; }
  .toc li { font-size: 0.88em; padding: 4px 0; border-bottom: 1px solid #1a1a1a; }
  .toc li:last-child { border-bottom: none; }
  .toc a { color: #ccc; text-decoration: none; }
  .toc a:hover { color: #4fc3f7; }
  .toc .cat-label { color: #4fc3f7; font-size: 0.8em; margin-left: 8px; }
</style>
</head>
<body>
<div class="container">
<h1>Karpathy RSS 实时精选</h1>
<div class="site-desc">基于 Andrej Karpathy 推荐的 92 个顶级科技博客，AI 生成中文解读</div>
<div class="meta">📅 {{ date }}  |  共 {{ total }} 篇来自 {{ source_count }} 个博客  |  由 AI 自动生成中文解读</div>

<div class="toc">
<h3>📑 目录</h3>
<ul>
{% set ns = namespace(idx=0) %}
{% for category, articles in categories.items() %}
{% for a in articles %}
{% set ns.idx = ns.idx + 1 %}
<li><a href="#article-{{ ns.idx }}">{{ ns.idx }}. {{ a.ai_title or a.title }}</a><span class="cat-label">{{ category }}</span></li>
{% endfor %}
{% endfor %}
</ul>
</div>

{% set ns2 = namespace(idx=0) %}
{% for category, articles in categories.items() %}
<div class="category">
<h2>{{ category }}</h2>
{% for a in articles %}
{% set ns2.idx = ns2.idx + 1 %}
<div class="article" id="article-{{ ns2.idx }}">
  <h3>{{ a.ai_title or a.title }}</h3>
  <div class="article-meta">
    <span>📝 {{ a.source }}{% if a.author %} · {{ a.author }}{% endif %}</span>
    <span>🕐 {{ a.published.strftime('%Y-%m-%d %H:%M') if a.published else '近期' }}</span>
    {% if a.category and a.category != '其他' %}<span class="category-tag">{{ a.category }}</span>{% endif %}
  </div>
  {% if a.tags %}<div class="tags">{% for t in a.tags %}<span class="tag">{{ t }}</span>{% endfor %}</div>{% endif %}
  {% if a.ai_detail %}<div class="detail">{{ a.ai_detail }}</div>
  {% elif a.ai_summary %}<div class="detail">{{ a.ai_summary }}</div>{% endif %}
  <a class="read-original" href="{{ a.link }}" target="_blank">📖 阅读英文原文 →</a>
</div>
{% endfor %}
</div>
{% endfor %}

<div class="footer">
  由 <a href="https://github.com/" target="_blank">Karpathy RSS Daily Digest</a> 自动生成<br>
  数据来源: Andrej Karpathy 推荐的 92 个顶级科技博客
</div>
</div>
</body>
</html>
""")

INDEX_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Karpathy RSS 实时精选</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans SC', sans-serif; background: #0a0a0a; color: #e0e0e0; line-height: 1.8; }
  .container { max-width: 700px; margin: 0 auto; padding: 60px 20px; text-align: center; }
  h1 { font-size: 1.8em; color: #fff; margin-bottom: 10px; }
  .desc { color: #888; margin-bottom: 40px; font-size: 0.95em; }
  .digest-list { text-align: left; }
  .digest-item { background: #111; border-radius: 10px; padding: 18px 22px; margin-bottom: 10px; border: 1px solid #1e1e1e; transition: border-color 0.2s; }
  .digest-item:hover { border-color: #4fc3f7; }
  .digest-item a { color: #e0e0e0; text-decoration: none; font-size: 1.05em; }
  .digest-item a:hover { color: #4fc3f7; }
  .digest-date { color: #666; font-size: 0.82em; margin-top: 4px; }
</style>
</head>
<body>
<div class="container">
<h1>Karpathy RSS 实时精选</h1>
<p class="desc">基于 Andrej Karpathy 推荐的 92 个顶级科技博客<br>AI 自动生成中文解读，每日更新</p>
<div class="digest-list">
{% for item in digests %}
<div class="digest-item">
  <a href="{{ item.filename }}">📰 {{ item.title }}</a>
  <div class="digest-date">{{ item.date }}</div>
</div>
{% endfor %}
{% if not digests %}
<div class="digest-item" style="text-align:center;color:#666;">暂无内容，请先运行 rss_reader.py 生成精选</div>
{% endif %}
</div>
</div>
</body>
</html>
""")


def generate_html_page(articles: list[Article]) -> str:
    if not articles:
        return ""
    sources = set(a.source for a in articles)
    categories = categorize_articles(articles)
    today = datetime.now().strftime("%Y年%m月%d日")
    context = {
        "date": today,
        "total": len(articles),
        "source_count": len(sources),
        "categories": categories,
    }
    return HTML_TEMPLATE.render(**context)


def save_html_page(content: str) -> Path:
    """保存 HTML 到 docs/ 目录（GitHub Pages）"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    filepath = DOCS_DIR / f"{today}.html"
    filepath.write_text(content, encoding="utf-8")
    logger.info(f"📄 网页已保存: {filepath}")
    # 更新 index.html
    _update_index()
    return filepath


def _update_index():
    """更新 docs/index.html 目录页"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    html_files = sorted(DOCS_DIR.glob("20*.html"), reverse=True)
    digests = []
    for f in html_files[:30]:  # 只展示最近30天
        date_str = f.stem  # e.g. "2026-02-25"
        digests.append({
            "filename": f.name,
            "title": f"Karpathy RSS 实时精选 - {date_str}",
            "date": date_str,
        })
    index_html = INDEX_TEMPLATE.render(digests=digests)
    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")


def _get_page_url(date_str: str = None) -> str:
    """获取当天网页的公开 URL"""
    if not GITHUB_PAGES_URL:
        return ""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    base = GITHUB_PAGES_URL.rstrip("/")
    return f"{base}/{date_str}.html"


# ── Markdown 输出 ─────────────────────────────────────────
MARKDOWN_TEMPLATE = Template("""\
# Karpathy RSS 实时精选

> 📅 {{ date }}  |  共 {{ total }} 篇来自 {{ source_count }} 个博客

---
{% for category, articles in categories.items() %}

## {{ category }}
{% for a in articles %}

### {{ loop.index }}. {{ a.ai_title or a.title }}
- **来源**: {{ a.source }}{% if a.author %} · {{ a.author }}{% endif %}  |  **时间**: {{ a.published.strftime('%Y-%m-%d %H:%M') if a.published else '近期' }}
- **原文**: [{{ a.title }}]({{ a.link }})
{%- if a.tags %}
- **标签**: {{ a.tags | join(', ') }}
{%- endif %}
{%- if a.ai_detail %}

{{ a.ai_detail }}
{%- elif a.ai_summary %}

> {{ a.ai_summary }}
{%- endif %}
{% endfor %}

---
{% endfor %}

_由 Karpathy RSS Daily Digest 自动生成_
""")


def generate_markdown(articles: list[Article]) -> str:
    if not articles:
        return "今天暂无新文章。"
    sources = set(a.source for a in articles)
    categories = categorize_articles(articles)
    today = datetime.now().strftime("%Y年%m月%d日")
    return MARKDOWN_TEMPLATE.render(
        date=today, total=len(articles),
        source_count=len(sources), categories=categories,
    )


def save_markdown(content: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    filepath = OUTPUT_DIR / f"digest-{today}.md"
    filepath.write_text(content, encoding="utf-8")
    logger.info(f"📄 Markdown 已保存: {filepath}")
    return filepath


# ── 核心流程 ──────────────────────────────────────────────
async def fetch_and_process(days: int, since: datetime = None,
                            webhook_url: str = None,
                            sent_db: dict = None,
                            enable_filter: bool = True) -> list[Article]:
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=days)

    feeds = parse_opml(FEEDS_FILE)
    articles = await fetch_all_feeds(feeds, since)
    if not articles:
        logger.info("暂无新文章")
        return []

    if sent_db is not None:
        before = len(articles)
        articles = filter_new_articles(articles, sent_db)
        skipped = before - len(articles)
        if skipped > 0:
            logger.info(f"🔄 跳过 {skipped} 篇已推送文章，剩余 {len(articles)} 篇新文章")
        if not articles:
            logger.info("没有新文章需要处理")
            return []

    await enrich_articles_with_full_content(articles)
    articles = ai_summarize_articles(articles, enable_filter)
    
    if not articles:
        logger.info("筛选后无相关文章")
        return []

    # 生成网页（始终生成，供 GitHub Pages 使用）
    html_content = generate_html_page(articles)
    if html_content:
        save_html_page(html_content)

    # 推送到企业微信
    if webhook_url:
        page_url = _get_page_url()
        await send_to_wecom(webhook_url, articles, page_url)

    if sent_db is not None:
        mark_as_sent(articles, sent_db)
        save_sent_db(sent_db)

    return articles


def parse_since_cli(value: str) -> datetime:
    """解析 --since（ISO8601）；无时区则视为 UTC。"""
    dt = date_parser.parse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── 主逻辑 ────────────────────────────────────────────────
async def run_digest(days: int = 1, fmt: str = "markdown",
                     print_output: bool = True, webhook_url: str = None,
                     enable_filter: bool = True,
                     since_override: Optional[datetime] = None):
    if since_override is not None:
        since = since_override.astimezone(timezone.utc)
        logger.info(
            f"🚀 开始抓取，时间窗口起点: {since.strftime('%Y-%m-%d %H:%M UTC')} "
            f"（与某次 CI 一致时，把日志里的「自 … UTC」贴到 --since）"
        )
    else:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        logger.info(f"🚀 开始抓取，时间范围: 最近 {days} 天 (自 {since.strftime('%Y-%m-%d %H:%M UTC')})")

    sent_db = load_sent_db() if webhook_url else None
    articles = await fetch_and_process(days, since, webhook_url, sent_db, enable_filter)

    if not articles:
        return

    # 同时保存 Markdown
    md_content = generate_markdown(articles)
    save_markdown(md_content)

    if print_output:
        print("\n" + "=" * 60)
        if fmt == "html":
            print(f"网页已生成: docs/{datetime.now().strftime('%Y-%m-%d')}.html")
            print(f"Markdown: output/digest-{datetime.now().strftime('%Y-%m-%d')}.md")
        else:
            print(md_content)
        print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Karpathy RSS 实时精选 - 92个顶级科技博客 AI 中文解读 + 企业微信推送",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python rss_reader.py                          # 抓取今天的内容（默认只保留科技/AI/商业类）
  python rss_reader.py --days 3                 # 抓取最近3天
  python rss_reader.py --no-filter              # 禁用内容筛选，收录所有文章
  python rss_reader.py --webhook <URL>          # 抓取并推送到企业微信群
  python rss_reader.py --since "2026-05-10T16:33:00Z"  # 复现 CI 日志里的时间窗口（需 MINIMAX_API_KEY）
        """,
    )
    parser.add_argument("--days", type=int, default=1, help="抓取最近N天的内容 (默认: 1)")
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="本地复现：文章发布时间下限（ISO8601），与 Actions 日志「自 … UTC」一致",
    )
    parser.add_argument("--output", choices=["markdown", "html"], default="html", help="输出格式 (默认: html)")
    parser.add_argument("--webhook", type=str, default=None, help="企业微信群 Webhook URL (或设置 WECOM_WEBHOOK_URL 环境变量)")
    parser.add_argument("--no-filter", action="store_true", help="禁用内容筛选（收录所有类别文章）")
    args = parser.parse_args()

    enable_filter = ENABLE_CONTENT_FILTER and not args.no_filter
    webhook_url = args.webhook or os.environ.get("WECOM_WEBHOOK_URL")
    since_override = parse_since_cli(args.since) if args.since else None

    asyncio.run(
        run_digest(
            args.days,
            args.output,
            webhook_url=webhook_url,
            enable_filter=enable_filter,
            since_override=since_override,
        )
    )


if __name__ == "__main__":
    main()
