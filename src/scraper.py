"""
scraper.py - Free search tool for AI agents using DuckDuckGo (DDGS) + BeautifulSoup.

Usage:
    import scraper
    result = scraper.get_result("What is the weather in New York?")
    print(result)
"""

import re
from datetime import datetime, timezone, timedelta
import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_FETCH        = 3     # Max pages to fully scrape
PAGE_CHAR_LIMIT  = 3000  # Max chars pulled from each scraped page
TOTAL_CHAR_LIMIT = 8000  # Max chars in final output

# For time-sensitive queries, snippets/news older than this are dropped
FRESHNESS_DAYS = {
    "weather":  1,   # weather data expires in 1 day
    "news":     7,   # news articles kept for 7 days
    "default": 30,   # general time-sensitive info kept for 30 days
}

# Domains known to be JS-heavy / return useless scraped content
SKIP_SCRAPE_DOMAINS = {
    "weather.com", "accuweather.com", "forecast.weather.gov",
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "reddit.com", "linkedin.com",
}

# Noise lines to strip from scraped text (case-insensitive substrings)
NOISE_PATTERNS = re.compile(
    r"(advertisement|cookie|sign in|log in|subscribe|newsletter"
    r"|privacy policy|terms of use|all rights reserved"
    r"|javascript|enable js|loading\.\.\.|chevron|video player"
    r"|mapbox|openstreetmap|trending now|watch.*video"
    r"|skip to content|back to top)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1).lower() if m else ""


def _ddgs_text(query: str, max_results: int = 6) -> list[dict]:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


def _ddgs_answers(query: str) -> list[dict]:
    """Instant-answer results (Wikipedia summaries, calculations, etc.)"""
    try:
        with DDGS() as ddgs:
            return list(ddgs.answers(query))
    except Exception:
        return []


def _ddgs_news(query: str, max_results: int = 4) -> list[dict]:
    try:
        with DDGS() as ddgs:
            return list(ddgs.news(query, max_results=max_results))
    except Exception:
        return []


def _is_news_query(query: str) -> bool:
    keywords = ("news", "latest", "breaking", "recent",
                 "update", "just in", "announcement", "headlines")
    q = query.lower()
    return any(k in q for k in keywords)


def _is_weather_query(query: str) -> bool:
    keywords = ("weather", "forecast", "temperature", "rain", "snow",
                 "humidity", "wind", "storm", "sunny", "cloudy")
    q = query.lower()
    return any(k in q for k in keywords)


def _is_time_sensitive(query: str) -> bool:
    """Returns True for queries where stale data would be misleading."""
    keywords = ("weather", "forecast", "price", "stock", "score", "standings",
                 "traffic", "outage", "status", "today", "right now", "currently",
                 "live", "breaking", "latest", "update", "hours", "open")
    q = query.lower()
    return any(k in q for k in keywords)


def _parse_date(date_str: str) -> datetime | None:
    """Parse an ISO-8601 or common date string into an aware UTC datetime."""
    if not date_str:
        return None
    # Normalise trailing timezone offset variations
    date_str = date_str.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(date_str[:26], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _is_fresh(date_str: str, max_days: int) -> bool:
    """Return True if date_str is within max_days of now, or if unparseable."""
    dt = _parse_date(date_str)
    if dt is None:
        return True  # can't determine age → keep it
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    return dt >= cutoff


def _fetch_page_text(url: str, char_limit: int = PAGE_CHAR_LIMIT) -> str:
    """Fetch URL, extract clean visible text, strip noise lines."""
    if _domain(url) in SKIP_SCRAPE_DOMAINS:
        return ""
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=8, follow_redirects=True)
        resp.raise_for_status()
        if "html" not in resp.headers.get("content-type", ""):
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise tags
        for tag in soup(["script", "style", "noscript", "header", "footer",
                          "nav", "aside", "form", "iframe", "figure",
                          "button", "svg", "img"]):
            tag.decompose()

        # Prefer semantic content containers
        body = (soup.find("article")
                or soup.find("main")
                or soup.find(id=re.compile(r"content|main|article", re.I))
                or soup.find(class_=re.compile(r"content|main|article|body", re.I))
                or soup.body
                or soup)

        raw = body.get_text(separator="\n")

        # Clean up lines
        lines = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if len(ln) < 25:          # skip very short fragments / nav labels
                continue
            if NOISE_PATTERNS.search(ln):
                continue
            lines.append(ln)

        text = "\n".join(lines)
        # Collapse excess blank lines
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:char_limit]

    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_result(query: str) -> str:
    """
    Search the web for *query* and return a rich text summary suitable for
    an AI agent to reason over.

    Strategy:
    - Always runs a standard web search (snippets)
    - For weather queries: also fetches weather-specific snippets + scrapes
      non-JS-heavy forecast pages
    - For news queries: also pulls DDGS news results
    - Pulls instant answers (Wikipedia / DuckDuckGo knowledge panel) when
      available
    - Full-page scraping for the top non-blocked results

    Parameters
    ----------
    query : str
        A natural-language question or search phrase.

    Returns
    -------
    str
        Formatted string with all gathered context, capped at TOTAL_CHAR_LIMIT.
    """
    if not query or not query.strip():
        return "Error: query must be a non-empty string."

    sections: list[str] = []

    # 1. Instant answers (Wikipedia panel, calculations, etc.)
    answers = _ddgs_answers(query)
    if answers:
        ans_lines = []
        for a in answers[:3]:
            text = a.get("text", "")
            url  = a.get("url", "")
            if text:
                ans_lines.append(f"{text}\nSource: {url}" if url else text)
        if ans_lines:
            sections.append("=== INSTANT ANSWER ===\n" + "\n\n".join(ans_lines))

    # 2. News results — only for explicit news queries, filtered to recent articles
    if _is_news_query(query):
        news = _ddgs_news(query, max_results=8)
        if news:
            max_days = FRESHNESS_DAYS["news"]
            fresh_news = [n for n in news if _is_fresh(n.get("date", ""), max_days)]
            news_lines = []
            for n in fresh_news:
                title = n.get("title", "")
                body  = n.get("body", "")
                url   = n.get("url", "")
                date  = n.get("date", "")
                news_lines.append(f"[{date}] {title}\n{body}\nURL: {url}")
            if news_lines:
                sections.append("=== NEWS RESULTS ===\n" + "\n\n".join(news_lines))
            elif news:
                sections.append(
                    f"=== NEWS RESULTS ===\n"
                    f"(All {len(news)} results were older than {max_days} days and filtered out.)"
                )

    # 3. Standard web-search snippets — drop stale results for time-sensitive queries
    results = _ddgs_text(query, max_results=6)
    if not results and not sections:
        return f"No search results found for: {query!r}"

    if results:
        if _is_weather_query(query):
            max_days = FRESHNESS_DAYS["weather"]
        elif _is_time_sensitive(query):
            max_days = FRESHNESS_DAYS["default"]
        else:
            max_days = None  # no date filter for general queries

        snippets = []
        for i, r in enumerate(results, 1):
            title   = r.get("title", "No title")
            href    = r.get("href", "")
            snippet = r.get("body", "")
            # DDGS text results don't always carry a date; skip filtering when absent
            date    = r.get("date", "")
            if max_days and date and not _is_fresh(date, max_days):
                continue
            snippets.append(f"[{i}] {title}\nURL: {href}\n{snippet}")
        if snippets:
            sections.append("=== WEB SEARCH SNIPPETS ===\n" + "\n\n".join(snippets))

    # 4. Full-page scraping for top results (skip JS-heavy domains)
    scraped: list[str] = []
    for r in results[:MAX_FETCH]:
        url   = r.get("href", "")
        title = r.get("title", url)
        if not url or _domain(url) in SKIP_SCRAPE_DOMAINS:
            continue
        page_text = _fetch_page_text(url)
        if page_text:
            scraped.append(f"--- {title} ---\nSource: {url}\n\n{page_text}")

    if scraped:
        sections.append("=== FULL PAGE CONTENT ===\n" + "\n\n".join(scraped))

    combined = re.sub(r"\n{3,}", "\n\n", "\n\n".join(sections)).strip()
    return combined[:TOTAL_CHAR_LIMIT]