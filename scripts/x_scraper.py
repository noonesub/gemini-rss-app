import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, Page


DATA_DIR = Path(__file__).parent / "x_data"
STATE_FILE = DATA_DIR / "state.json"
COOKIES_FILE = DATA_DIR / "cookies.json"
RESULTS_FILE = DATA_DIR / "tweets.json"


@dataclass
class Tweet:
    id: str
    author: str
    author_display: str
    author_avatar: str
    content: str
    created_at: str
    url: str
    likes: int
    retweets: int
    replies: int
    views: Optional[int]
    images: list[str]


def load_config() -> dict:
    config_path = DATA_DIR / "config.json"
    default = {
        "users": [],
        "max_tweets_per_user": 20,
        "headless": True,
        "scroll_times": 3,
    }
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return {**default, **json.load(f)}
    return default


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": [], "last_run": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_results() -> list[dict]:
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_results(tweets: list[dict]):
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(tweets, f, ensure_ascii=False, indent=2)


def cookies_exist() -> bool:
    return COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 10


def parse_number(text: str) -> int:
    text = text.strip()
    if not text or text == "":
        return 0
    multipliers = {"K": 1000, "M": 1000000, "B": 1000000000}
    suffix = text[-1].upper()
    if suffix in multipliers:
        try:
            return int(float(text[:-1].strip()) * multipliers[suffix])
        except (ValueError, IndexError):
            return 0
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return 0


def extract_tweet_id(url: str) -> str:
    match = re.search(r"/status/(\d+)", url)
    return match.group(1) if match else ""


async def login_flow(page: Page):
    print("正在打开 X 登录页面...")
    await page.goto("https://x.com/login", timeout=60000, wait_until="domcontentloaded")
    input("请在弹出的浏览器中完成登录（输入验证码等），然后按 Enter 继续...")
    cookies = await page.context.cookies()
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print("登录凭据已保存到", COOKIES_FILE)


async def inject_auth_token(context, auth_token: str):
    import datetime as dt
    expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=365)
    cookies = [
        {
            "name": "auth_token",
            "value": auth_token,
            "domain": ".x.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "None",
            "expires": int(expiry.timestamp()),
        },
        {
            "name": "ct0",
            "value": "dummy",
            "domain": ".x.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
            "expires": int(expiry.timestamp()),
        },
    ]
    await context.add_cookies(cookies)
    print("已通过 auth_token 注入登录凭据")


async def scrape_user_tweets(
    page: Page, username: str, max_tweets: int, scroll_times: int
) -> list[Tweet]:
    print(f"正在抓取 @{username} 的推文...")
    url = f"https://x.com/{username}"
    await page.goto(url, timeout=90000, wait_until="domcontentloaded")
    for i in range(30):
        await asyncio.sleep(1)
        rendered = await page.locator('[data-testid="tweet"]').count()
        if rendered > 0:
            break

    login_link = await page.locator('a[href="/login"]').count()
    if login_link > 0:
        tweets_check = await page.locator('article[data-testid="tweet"]').count()
        if tweets_check == 0:
            print("  ⚠️ 未登录状态，auth_token 可能无效")
            return []

    tweets: list[Tweet] = []
    seen_ids: set[str] = set()
    no_new_count = 0

    for scroll in range(scroll_times):
        article_cards = page.locator('article[data-testid="tweet"]')
        count = await article_cards.count()
        print(f"  滚动 {scroll + 1}/{scroll_times}, 发现 {count} 条推文")

        for i in range(count):
            card = article_cards.nth(i)
            try:
                tweet_data = await extract_tweet_data(page, card, username)
                if tweet_data and tweet_data.id not in seen_ids:
                    seen_ids.add(tweet_data.id)
                    tweets.append(tweet_data)
            except Exception as e:
                print(f"    解析推文 {i} 失败: {e}")

        if len(tweets) >= max_tweets:
            tweets = tweets[:max_tweets]
            break

        if count == 0:
            no_new_count += 1
            if no_new_count >= 2:
                break
        else:
            no_new_count = 0

        await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

    print(f"  @{username}: 共获取 {len(tweets)} 条推文")
    return tweets


async def extract_tweet_data(
    page: Page, card, expected_username: str
) -> Optional[Tweet]:
    try:
        link_el = card.locator("a[href*='/status/']").first
        href = await link_el.get_attribute("href", timeout=5000)
        if not href:
            return None
        tweet_id = extract_tweet_id(href)
        if not tweet_id:
            return None
        tweet_url = f"https://x.com{href}" if href.startswith("/") else href

        content_el = card.locator('[data-testid="tweetText"]').first
        content = await content_el.inner_text(timeout=5000) or ""

        time_el = card.locator("time").first
        datetime_attr = await time_el.get_attribute("datetime", timeout=3000)
        created_at = datetime_attr or ""

        author_name_el = card.locator(
            '[data-testid="User-Name"] a:not([href*="/status/"])'
        ).first
        author_display = (await author_name_el.inner_text(timeout=3000)) or expected_username

        avatar = ""
        for sel in ['img[alt*=" avatar"]', 'img[alt*="photo"]', 'img[src*="twimg.com"]']:
            avatar_el = card.locator(sel).first
            if await avatar_el.count() > 0:
                try:
                    avatar = await avatar_el.get_attribute("src", timeout=2000) or ""
                    if avatar:
                        break
                except Exception:
                    continue

        likes = 0
        retweets = 0
        replies = 0
        views = None
        for testid, name in [("reply", "replies"), ("retweet", "retweets"), ("like", "likes")]:
            el = card.locator(f'[data-testid="{testid}"]').first
            if await el.count() > 0:
                try:
                    text = await el.inner_text(timeout=2000)
                    if text.strip():
                        val = parse_number(text.strip())
                        if name == "replies": replies = val
                        elif name == "retweets": retweets = val
                        elif name == "likes": likes = val
                except Exception:
                    pass
        view_el = card.locator('[data-testid="app-text-transition-container"]').first
        if await view_el.count() > 0:
            try:
                vt = await view_el.inner_text(timeout=2000)
                if vt.strip():
                    views = parse_number(vt.strip())
            except Exception:
                pass

        images = []
        img_els = card.locator('img[alt="Image"]')
        img_count = await img_els.count()
        for j in range(img_count):
            src = await img_els.nth(j).get_attribute("src")
            if src:
                images.append(src)

        return Tweet(
            id=tweet_id,
            author=expected_username,
            author_display=author_display.strip(),
            author_avatar=avatar,
            content=content,
            created_at=created_at,
            url=tweet_url,
            likes=likes,
            retweets=retweets,
            replies=replies,
            views=views,
            images=images,
        )
    except Exception as e:
        print(f"    提取推文数据失败: {e}")
        return None


async def main():
    config = load_config()
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))

    if not config["users"]:
        print(
            "请先在 scripts/x_data/config.json 中配置要抓取的用户列表"
        )
        print('示例: {"users": ["elonmusk", "Twitter"]}')
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        config_path = DATA_DIR / "config.json"
        if not config_path.exists():
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"users": ["Twitter"], "max_tweets_per_user": 20, "scroll_times": 3},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            print(f"已创建默认配置文件: {config_path}")
        return

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=config.get("headless", True),
            channel="chrome",
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        auth_token = config.get("auth_token")
        if auth_token:
            await inject_auth_token(context, auth_token)
        elif cookies_exist():
            with open(COOKIES_FILE, encoding="utf-8") as f:
                cookies = json.load(f)
            await context.add_cookies(cookies)
            print("已加载登录凭据")
        else:
            page = await context.new_page()
            await login_flow(page)
            await page.close()

        page = await context.new_page()

        all_new_tweets: list[Tweet] = []
        for user in config["users"]:
            try:
                tweets = await scrape_user_tweets(
                    page,
                    user,
                    config["max_tweets_per_user"],
                    config["scroll_times"],
                )
                for t in tweets:
                    if t.id not in seen_ids:
                        seen_ids.add(t.id)
                        all_new_tweets.append(t)
            except Exception as e:
                print(f"抓取 @{user} 失败: {e}")

        await browser.close()

    if all_new_tweets:
        tweets_dict = [asdict(t) for t in all_new_tweets]
        existing = load_results()
        existing_ids = {t["id"] for t in existing}
        new_entries = [t for t in tweets_dict if t["id"] not in existing_ids]
        save_results(existing + new_entries)
        print(f"新增 {len(new_entries)} 条推文，累计共 {len(existing) + len(new_entries)} 条")
    else:
        print("没有新的推文")

    state["seen_ids"] = list(seen_ids)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
