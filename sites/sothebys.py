
import json
import random
import re
import time

import requests
from bs4 import BeautifulSoup

from healthcheck_core import *   # noqa: F401,F403  (download, failure types, add_finding...)


### Site settings used by the common engine

SITE_NAME = "Sotheby's"
SOURCE = 'sothebys'
CANARY_EMPTY_TEXT = 'shell pages: scraping now would lose images'
THUMBNAIL_REDIRECT_TEXT = 'redirects to a 150px thumbnail: '
TEST_MESSAGE = 'TEST: 2 of 6 canary lots lost the image elements the scraper reads.'
SEARCH_HELP = 'Check the search page (needs Playwright)'
LIVE_PAGE_LINK_FORMATS = ('new', 'old')
LIVE_IMAGE_SKIPPED_FORMATS = ('empty', 'unknown')
extra_image_check = None              # an optional extra rule on image URLs (see phillips.py)


### What the scraper reads on this site

# Lot page links come in two formats (checked against real S3 data)
LINK_NEW_FORMAT = re.compile(r"^https://www\.sothebys\.com/en/buy/auction/\d{4}/[a-z0-9-]+/[a-z0-9-]+$")


LINK_OLD_FORMAT = re.compile(r"^https://www\.sothebys\.com/en/auctions/ecatalogue/\d{4}/[a-z0-9-]+/lot\.\d+[A-Z]?\.html$")


# Image URLs come in three formats
IMAGE_DAM_FORMAT = re.compile(
    r"^https://dam\.sothebys\.com/dam/image/lot/[0-9a-f-]{36}/primary/(small|medium|large|extra_large)$"
)


IMAGE_BRIGHTSPOT_FORMAT = re.compile(
    r"^https://sothebys-(md|com)\.brightspotcdn\.com/dims4/default/[0-9a-f]+/2147483647/strip/(true|false)"
    r"/crop/\d+x\d+\+\d+\+\d+/resize/(\d+)x(\d+)!/quality/\d+/\?url=.+$"
)


# Pixel width served for each size name of a dam.sothebys.com URL
DAM_SIZE_IN_PIXELS = {"small": 385, "medium": 800, "large": 1024, "extra_large": 2048}


# Sotheby's default pictures, used when the lot has no real image
PLACEHOLDER_NAMES = ["2Flot.jpg", "2Fundercopyright.jpg"]


BOT_CHALLENGE_HTML = ["cf-chl-", "challenge-platform", "px-captcha", "captcha-delivery", "_incapsula_resource"]


# Known past lots that must always have an image: 3 recent pages and 3 old pages, each from a different sale
CANARY_LOTS = [
    "https://www.sothebys.com/en/buy/auction/2019/made-in-britain/david-hockney-r-a-the-haunted-castle-s-a-c-92-mca",
    "https://www.sothebys.com/en/buy/auction/2023/modern-british-art/fire-island",
    "https://www.sothebys.com/en/buy/auction/2024/made-in-britain-l24144/breakfast-with-stanley-in-malibu-aug-23-1989",
    "https://www.sothebys.com/en/auctions/ecatalogue/2015/contemporary-day-n09346/lot.259.html",
    "https://www.sothebys.com/en/auctions/ecatalogue/2019/contemporary-art-evening-n10149/lot.15.html",
    "https://www.sothebys.com/en/auctions/ecatalogue/2020/contemporary-curated-n10321/lot.16.html",
]


# HTML elements the scraper reads, for each page format: (tag, class)
SCRAPER_ELEMENTS = {
    "new": [("img", "css-1qh7smx"), ("img", "css-dllwl0")],
    "old": [("div", "LotPage-details"), ("li", "LotPage-pieceTitle"), ("div", "LotPage-productInfo"),
            ("img", "Image lazyload")],
}


# Keys the scraper reads in the page JSON of recent pages (the "LotV2:..." object)
REQUIRED_LOT_JSON_KEYS = ["title", "description", "estimateV2", "auction", "bidState", "session", "lotNumber"]


# Search page checked with Playwright, and the selectors the scraper uses on it
SEARCH_CANARY_URL = "https://www.sothebys.com/en/search?query=mark+bradford&pfilters.dateRange=past&sortBy=ending_latest"


SEARCH_CANARY_WORD = "bradford"


SEARCH_GRID = "div.SearchResultsGrid_lotCardsContainer__OaN_L"


SEARCH_CARD_TITLE = "a.LotCard-module_lotTitleLink__D5BhZ"


SEARCH_CARD_PRICE = "div.LotCard-module_bidInfoContainer__expQA"


def get_link_format(link):
    """Returns 'new', 'old', 'empty' or 'unknown'."""
    if not link:
        return "empty"
    if LINK_NEW_FORMAT.match(link):
        return "new"
    if LINK_OLD_FORMAT.match(link):
        return "old"
    return "unknown"


def get_image_format(image_url):
    """Returns 'dam', 'brightspot_md', 'brightspot_com', 'empty' or 'unknown'."""
    if not image_url or not image_url.strip():
        return "empty"
    if IMAGE_DAM_FORMAT.match(image_url):
        return "dam"
    match = IMAGE_BRIGHTSPOT_FORMAT.match(image_url)
    if match:
        return "brightspot_" + match.group(1)
    return "unknown"


def get_image_size_in_pixels(image_url):
    """Longest side of the image, read from the URL itself. None if the URL doesn't tell."""
    match = IMAGE_BRIGHTSPOT_FORMAT.match(image_url)
    if match:
        width = int(match.group(3))
        height = int(match.group(4))
        return max(width, height)

    match = IMAGE_DAM_FORMAT.match(image_url)
    if match:
        size_name = match.group(1)
        return DAM_SIZE_IN_PIXELS[size_name]

    return None


def is_placeholder(image_url):
    for name in PLACEHOLDER_NAMES:
        if name in image_url:
            return True
    return False


def is_thumbnail(image_url):
    """Sotheby's 150px thumbnails look like .../resize/150x112!/... or .../resize/112x150!/..."""
    return "/resize/150x" in image_url or "x150!" in image_url


def find_lot_json(soup):
    """The lot data the scraper reads in recent pages (the 'LotV2:...' object of __NEXT_DATA__), or None."""
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        return None

    try:
        page_data = json.loads(script.string)
        cache = page_data["props"]["pageProps"]["apolloCache"]
    except (ValueError, KeyError, TypeError):
        return None

    for key, value in cache.items():
        if key.startswith("LotV2:") and isinstance(value, dict):
            return value
    return None


def count_lot_json_images(lot_json):
    """Number of images listed in the lot JSON (stored under a key starting with 'media(')."""
    for key, value in lot_json.items():
        if key.startswith("media(") and isinstance(value, dict):
            images = value.get("images") or []
            return len(images)
    return None


def find_missing_json_keys(lot_json):
    missing = []
    for key in REQUIRED_LOT_JSON_KEYS:
        if key not in lot_json:
            missing.append(key)
    if count_lot_json_images(lot_json) is None:
        missing.append("media(...)")
    return missing


def find_missing_elements(soup, page_format):
    missing = []
    for tag, css_class in SCRAPER_ELEMENTS.get(page_format, []):
        if soup.find(tag, attrs={"class": css_class}) is None:
            missing.append(tag + "." + css_class)
    return missing


def inspect_lot_page(link):
    """
    Downloads a lot page like the scraper does (no JavaScript) and returns:
      failure_type  None if the page has everything the scraper needs, otherwise the likely cause
      detail        a short explanation
      html_images   number of <img> tags in the HTML
      json_images   number of images in the page JSON (None if unknown)
    The tests go from "we didn't get the page at all" to "the page changed".
    """
    page = {"failure_type": None, "detail": "", "html_images": 0, "json_images": None}

    # 1. Did we get an answer at all?
    result = download(link)
    failure_type = classify_download(result)
    if failure_type:
        page["failure_type"] = failure_type
        page["detail"] = result["detail"]
        return page

    html = result["response"].text

    # 2. Blocked, or cut?
    if looks_like_bot_challenge(html):
        page["failure_type"] = ANTI_BOT
        page["detail"] = "bot challenge page (CAPTCHA / access denied)"
        return page
    if len(html) < MIN_PAGE_CHARACTERS or "</html>" not in html.lower():
        page["failure_type"] = EMPTY_RESPONSE
        page["detail"] = f"cut or empty answer ({len(html)} characters)"
        return page

    soup = BeautifulSoup(html, "html.parser")
    page_format = get_link_format(link)

    title = ""
    if soup.title:
        title = soup.title.get_text(strip=True)
    visible_text = ""
    if soup.body:
        visible_text = soup.body.get_text(" ", strip=True)
    is_shell = title == "" or len(visible_text) == 0

    page["html_images"] = len(soup.find_all("img"))
    lot_json = find_lot_json(soup)
    if lot_json is not None:
        page["json_images"] = count_lot_json_images(lot_json)

    # 3. Recent pages keep their data in a JSON block
    if page_format == "new" and lot_json is None:
        if is_shell:
            page["failure_type"] = EMPTY_RESPONSE
            page["detail"] = "empty page, without the lot JSON"
        else:
            page["failure_type"] = SCHEMA_CHANGE
            page["detail"] = "the lot JSON (__NEXT_DATA__ / LotV2) is not in the page anymore"
        return page

    # 4. Shell page: Sotheby's sent the JSON but no rendered HTML (temporary on their side)
    if is_shell:
        page["failure_type"] = EMPTY_RESPONSE
        page["detail"] = f"shell page: no text, {page['json_images']} images in JSON, 0 in HTML"
        return page

    # 5. JSON keys the scraper reads
    if page_format == "new":
        missing_keys = find_missing_json_keys(lot_json)
        if missing_keys:
            page["failure_type"] = SCHEMA_CHANGE
            page["detail"] = f"JSON keys read by the scraper not found: {missing_keys}"
            return page

    # 6. Page rendered without its images
    if page["json_images"] and page["html_images"] == 0:
        page["failure_type"] = EMPTY_RESPONSE
        page["detail"] = f"page rendered without images ({page['json_images']} in JSON)"
        return page

    # 7. HTML elements the scraper reads
    missing_elements = find_missing_elements(soup, page_format)
    if missing_elements:
        page["failure_type"] = STRUCTURE_CHANGE
        page["detail"] = f"HTML elements read by the scraper not found: {missing_elements}"
        return page

    page["detail"] = f"{page['html_images']} <img> in HTML, {page['json_images']} in JSON"
    return page


def check_canary(link, attempts=3, wait_seconds=10):
    """
    Checks one known lot. Returns (status, failure_type, detail) where status is:
      OK            the page has everything the scraper needs
      INCONCLUSIVE  Sotheby's served an empty/shell page every time (temporary on their side)
      FAILED        another problem that didn't go away after retrying
    """
    for attempt_number in range(1, attempts + 1):
        page = inspect_lot_page(link)
        if page["failure_type"] not in TRANSIENT_FAILURE_TYPES:
            break
        if attempt_number < attempts:
            time.sleep(wait_seconds)

    if page["failure_type"] is None:
        return "OK", None, page["detail"]
    if page["failure_type"] == EMPTY_RESPONSE:
        return "INCONCLUSIVE", EMPTY_RESPONSE, page["detail"] + f" (on all {attempts} attempts)"
    return "FAILED", page["failure_type"], page["detail"]


def try_search_page():
    """One look at the search page, like the scraper's first step. Returns (failure_type, detail)."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "skipped", "Playwright is not installed (pip install playwright && python -m playwright install chromium)"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            # Like the scraper: the first visit is often redirected to "upcoming", the second one sticks
            page.goto(SEARCH_CANARY_URL, wait_until="domcontentloaded", timeout=30000)
            response = page.goto(SEARCH_CANARY_URL, timeout=30000)

            if response is not None:
                failure_type = classify_status(response.status)
                if failure_type:
                    return failure_type, f"HTTP {response.status} on the search page"

            if looks_like_bot_challenge(page.content()):
                return ANTI_BOT, "bot challenge on the search page"

            if "dateRange=past" not in page.url:
                return STRUCTURE_CHANGE, f"the 'past' filter was redirected to {page.url}: the scraper would collect the wrong lots"

            try:
                page.wait_for_selector(SEARCH_GRID, timeout=15000)
            except PlaywrightTimeout:
                visible_text = page.inner_text("body").strip()
                if len(visible_text) < 200:
                    return EMPTY_RESPONSE, "the search page did not render"
                return STRUCTURE_CHANGE, "results grid not found: " + SEARCH_GRID
            time.sleep(2)

            titles = page.locator(SEARCH_CARD_TITLE)
            prices = page.locator(SEARCH_CARD_PRICE)
            if titles.count() == 0:
                return STRUCTURE_CHANGE, "no card titles found: " + SEARCH_CARD_TITLE
            if prices.count() == 0:
                return STRUCTURE_CHANGE, "no card prices found: " + SEARCH_CARD_PRICE

            first_title = titles.first.inner_text()
            first_price = prices.first.inner_text()
            if SEARCH_CANARY_WORD not in first_title.lower():
                return STRUCTURE_CHANGE, f"first card title is {first_title!r}: the title selector reads something else"
            if "Lot sold" not in first_price and "Estimate" not in first_price:
                return STRUCTURE_CHANGE, f"unexpected price text {first_price!r}: the scraper reads prices from it"

            return None, f"{titles.count()} cards, first one: {first_title} / {first_price}"
        except PlaywrightTimeout:
            return TIMEOUT, "the search page took more than 30 seconds to load"
        finally:
            browser.close()