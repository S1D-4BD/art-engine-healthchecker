
import json
import random
import re
import time

import requests
from bs4 import BeautifulSoup

from healthcheck_core import *   # noqa: F401,F403  (download, failure types, add_finding...)


### Site settings used by the common engine

SITE_NAME = "Christie's"
SOURCE = 'christies'
CANARY_EMPTY_TEXT = 'empty pages: scraping now would lose lots'
THUMBNAIL_REDIRECT_TEXT = 'redirects to a thumbnail: '
TEST_MESSAGE = 'TEST: 2 of 6 canary lots lost the elements the scraper reads.'
SEARCH_HELP = 'Check the search page (needs Playwright)'
LIVE_PAGE_LINK_FORMATS = ('lot',)
LIVE_IMAGE_SKIPPED_FORMATS = ('empty', 'unknown')
extra_image_check = None              # an optional extra rule on image URLs (see phillips.py)


### What the scraper reads on this site

# Lot page links: https://www.christies.com/en/lot/lot-6594540 (sometimes without "/en", or with "?..." after)
LINK_FORMAT = re.compile(r"^https://www\.christies\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?lot/lot-\d+(?:[/?#].*)?$")


# Image URLs: https://www.christies.com/img/LotImages/2023/NYR/2023_NYR_22056_0115_000(...).jpg?maxwidth=3030&maxheight=1950
IMAGE_FORMAT = re.compile(r"^https://www\.christies\.com/img/LotImages/.+\.(?:jpe?g|png|gif|webp)(?:\?.*)?$", re.IGNORECASE)


IMAGE_MAX_WIDTH = re.compile(r"[?&]maxwidth=(\d+)", re.IGNORECASE)


IMAGE_MAX_HEIGHT = re.compile(r"[?&]maxheight=(\d+)", re.IGNORECASE)


# Christie's default picture, shown when the lot has no image (the scraper refuses it)
PLACEHOLDER_NAMES = ["christies-roundel"]


# The search page is loaded by the scraper with Playwright and a disguised browser (Christie's blocks plain
# headless browsers on search pages): same settings as christies_utils.py
STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


STEALTH_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]


# A page with less visible text than this is == empty "shell" page
MIN_VISIBLE_TEXT = 300


BOT_CHALLENGE_HTML = ["cf-chl-", "challenge-platform", "px-captcha", "captcha-delivery", "_incapsula_resource","errors.edgesuite.net"]


# Known past lots seent with nora (for testing)
CANARY_LOTS = [
    "https://www.christies.com/en/lot/lot-6594540",   # 2026, Michael Craig-Martin, Post-War to Present
    "https://www.christies.com/en/lot/lot-6549166",   # 2025, Alejandro Cardenas, 21st Century Day Sale
    "https://www.christies.com/en/lot/lot-6419372",   # 2023, Maxime Maufra, Paris
    "https://www.christies.com/en/lot/lot-6401455",   # 2022, Wu Guanzhong, Hong Kong
    "https://www.christies.com/en/lot/lot-6226265",   # 2019, Andy Warhol, Post-War to Present
    "https://www.christies.com/en/lot/lot-5946332",   # 2015, Mark Tobey
]


# what the scraper reads on a lot page (christies_utils.get_info_artwork_christies and the functions it calls)
MAIN_DIV_CLASS = "chr-lot-detail__main" # required: without it, the lot is dropped


TITLE_H1_CLASS = "chr-lot-header__title"


DESCRIPTION_DIV_CLASS = "container-fluid" # inside the main div: holds the sections below


ACCORDION_TAG = "chr-accordion-item"      # one per section: "Details", "Provenance", etc


IMAGE_CONTAINER_CLASS = "chr-lot-header__image-container"


IMAGE_CLASS = "chr-img lazyload"


IMAGE_ATTRIBUTE = "data-srcset"


LOT_HEADER_KEY = "lotHeader_1205114186"


LOT_HEADER_PATTERN = re.compile(rf"window\.chrComponents\.{re.escape(LOT_HEADER_KEY)}\s*=\s*({{\s*.*?}});", re.DOTALL)


LOT_DATA_PATTERN = re.compile(r'"lots":\s*\[\s*?({.*?)"lot_assets":', re.DOTALL)


REQUIRED_LOT_JSON_KEYS = ["price_realised_txt", "estimate_txt", "lot_id_txt"]


LOCATION_SCRIPT_MARKERS = ["AnalyticsDataLayer.sale", "let setAnalyticsDLRequest"]


AUCTION_DATE_PATTERN = re.compile(r'end_date[^:]*:[^"]*"([^"]*)"')


AUCTION_NAME_PATTERN = re.compile(r'"title_txt"\s*:\s*"([^"]+)"')


AUCTION_URL_PATTERN = re.compile(r'"url"\s*:\s*"([^"]+)","is_auction_over"')


# Search page checked with Playwright, and the selectors the scraper uses on it (find_links_to_artworks)
# SEARCH_CANARY_URL: build it like build_url_query does, from CHRISTIES_DICT (data_collection_constants.py):
#   CHRISTIES_DICT["search_url1"] + "andy%20warhol" + CHRISTIES_DICT["search_url2"] + CHRISTIES_DICT["search_url_sold"]
# While it is None, --check-search is skipped.
SEARCH_CANARY_URL = None


SEARCH_CANARY_WORD = "warhol"


SEARCH_GRID_CLASS = "row"


SEARCH_GRID_ATTRS = {"data-qa": "search-result-tiles"}


SEARCH_TILE_CLASS = ("chr-search-lots-view__column-transition chr-search-lots-view__tile-section col-sm-6 col-md-4 " "chr-search-results tile-gutter col-lm-6 col-lg-4 col-xl-4")   # compared EXACTLY, like the scraper


SEARCH_TILE_ARTIST_CLASS = "chr-lot-tile__link"


SEARCH_PAGINATION_CLASS = "chr-page-pagination__list"


def get_link_format(link):
    if not link:
        return "empty"
    if LINK_FORMAT.match(link):
        return "lot"
    return "unknown"


def get_image_format(image_url):
    if not image_url or not image_url.strip():
        return "empty"
    if IMAGE_FORMAT.match(image_url):
        return "lotimages"
    return "unknown"


def get_image_size_in_pixels(image_url):
    sizes = []
    for pattern in (IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT):
        match = pattern.search(image_url)
        if match:
            sizes.append(int(match.group(1)))
    if sizes:
        return max(sizes)
    return None


def is_placeholder(image_url):
    for name in PLACEHOLDER_NAMES:
        if name in image_url:
            return True
    return False


def is_thumbnail(image_url):
    if re.search(r"[?&]w=1(?:&|$)", image_url):
        return True
    match = IMAGE_MAX_WIDTH.search(image_url)
    return bool(match) and int(match.group(1)) < 300


def find_lot_header_json(soup):
    script = soup.find("script", string=LOT_HEADER_PATTERN)
    if script is None or not script.string:
        return None, "the script window.chrComponents." + LOT_HEADER_KEY + " is not in the page"
    header = LOT_HEADER_PATTERN.search(script.string)
    if not header:
        return None, LOT_HEADER_KEY + " found, but not in the format the scraper reads"
    lot_data = LOT_DATA_PATTERN.search(header.group(1))
    if not lot_data:
        return None, '"lots": [...] followed by "lot_assets" not found in ' + LOT_HEADER_KEY
    try:
        return json.loads(lot_data.group(1).rstrip(",") + "}"), ""
    except ValueError:
        return None, "the lot JSON cannot be read anymore"


def find_image_url(soup):
    container = soup.find("div", {"class": IMAGE_CONTAINER_CLASS})
    if not container:
        return None, "div." + IMAGE_CONTAINER_CLASS
    image = container.find("img", {"class": IMAGE_CLASS})
    if not image:
        return None, "img." + IMAGE_CLASS.replace(" ", ".")
    srcset = image.get(IMAGE_ATTRIBUTE)
    if not srcset:
        return None, "img[" + IMAGE_ATTRIBUTE + "]"
    image_url = srcset.split(",")[0].strip().split(" ")[0]
    if is_placeholder(image_url):
        return None, "roundel"
    return image_url, ""


def has_details_section(main_div):
    """True if the "Details" section is there (description, artist, size and date come from it)."""
    description_div = main_div.find("div", {"class": DESCRIPTION_DIV_CLASS})
    if not description_div:
        return False
    for section in description_div.find_all(ACCORDION_TAG):
        first_line = section.get_text("\n", strip=True).split("\n")[0]
        if "Details" in first_line and "Further Details" not in first_line:
            return True
    return False


def find_missing_elements(soup, main_div):
    """HTML elements and scripts the scraper reads that are not in the page (besides the main div and the image)."""
    missing = []
    if not soup.find("h1", class_=TITLE_H1_CLASS):
        missing.append("h1." + TITLE_H1_CLASS)
    if not has_details_section(main_div):
        missing.append("'Details' section (" + ACCORDION_TAG + " in div." + DESCRIPTION_DIV_CLASS + ")")

    location_script = None
    for marker in LOCATION_SCRIPT_MARKERS:
        location_script = soup.find("script", string=lambda text, marker=marker: text and marker in text)
        if location_script:
            break
    if not location_script:
        missing.append("auction location script (" + " / ".join(LOCATION_SCRIPT_MARKERS) + ")")

    components = soup.find("script", string=re.compile(r"window\.chrComponents\." + re.escape(LOT_HEADER_KEY)))
    if not components:
        components = soup.find("script", string=re.compile(r"window\.chrComponents"))
    components_text = components.string if components and components.string else ""
    if not AUCTION_DATE_PATTERN.search(components_text):
        missing.append("auction date (end_date in window.chrComponents)")
    if not AUCTION_NAME_PATTERN.search(components_text) and not AUCTION_URL_PATTERN.search(components_text):
        missing.append("auction name (title_txt or url in window.chrComponents)")
    return missing


def inspect_lot_page(link):
    page = {"failure_type": None, "detail": "", "image_url": None, "no_image": False}

    # 1 Did we get an answer at all?
    result = download(link)
    failure_type = classify_download(result)
    if failure_type:
        page["failure_type"] = failure_type
        page["detail"] = result["detail"]
        return page

    html = result["response"].text

    # 2 Blocked, or cut?
    if looks_like_bot_challenge(html):
        page["failure_type"] = ANTI_BOT
        page["detail"] = "bot challenge page (CAPTCHA / access denied)"
        return page
    if len(html) < MIN_PAGE_CHARACTERS or "</html>" not in html.lower():
        page["failure_type"] = EMPTY_RESPONSE
        page["detail"] = f"cut or empty answer ({len(html)} characters)"
        return page

    soup = BeautifulSoup(html, "html.parser")
    visible_text = ""
    if soup.body:
        visible_text = soup.body.get_text(" ", strip=True)

    # 3 The main div: without it, the scraper drops the lot
    main_div = soup.find("div", {"class": MAIN_DIV_CLASS})
    if not main_div:
        if len(visible_text) < MIN_VISIBLE_TEXT:
            page["failure_type"] = EMPTY_RESPONSE
            page["detail"] = f"empty page ({len(visible_text)} characters of text)"
        else:
            page["failure_type"] = STRUCTURE_CHANGE
            page["detail"] = f"div.{MAIN_DIV_CLASS} not found: the scraper drops every lot"
        return page

    # 4 The lot data with the prices
    lot_json, problem = find_lot_header_json(soup)
    if lot_json is None:
        page["failure_type"] = SCHEMA_CHANGE
        page["detail"] = problem + ": the scraper loses prices, estimates and lot numbers"
        return page
    missing_keys = [key for key in REQUIRED_LOT_JSON_KEYS if key not in lot_json]
    if missing_keys:
        page["failure_type"] = SCHEMA_CHANGE
        page["detail"] = f"keys read by the scraper not found in {LOT_HEADER_KEY}: {missing_keys}"
        return page

    # 5 The image
    image_url, problem = find_image_url(soup)
    if problem == "roundel":
        page["no_image"] = True
    elif image_url is None:
        page["failure_type"] = STRUCTURE_CHANGE
        page["detail"] = f"image element read by the scraper not found: {problem}"
        return page
    page["image_url"] = image_url

    # 6 The other elements the scraper reads
    missing_elements = find_missing_elements(soup, main_div)
    if missing_elements:
        page["failure_type"] = STRUCTURE_CHANGE
        page["detail"] = f"elements read by the scraper not found: {missing_elements}"
        return page

    image_text = "no image (roundel)" if page["no_image"] else "image found"
    page["detail"] = f"title, Details, prices ({lot_json.get('price_realised_txt') or 'no price'}), {image_text}"
    return page


def check_canary(link, attempts=3, wait_seconds=10):
    for attempt_number in range(1, attempts + 1):
        page = inspect_lot_page(link)
        if page["failure_type"] not in TRANSIENT_FAILURE_TYPES:
            break
        if attempt_number < attempts:
            time.sleep(wait_seconds)

    if page["failure_type"] is None and page["no_image"]:
        # Canary lots all have an image: losing it means the scraper would lose images too
        return "FAILED", DEGRADED_DATA, "Christie's default picture instead of the lot's image"
    if page["failure_type"] is None:
        return "OK", None, page["detail"]
    if page["failure_type"] == EMPTY_RESPONSE:
        return "INCONCLUSIVE", EMPTY_RESPONSE, page["detail"] + f" (on all {attempts} attempts)"
    return "FAILED", page["failure_type"], page["detail"]


def apply_stealth(page):
    try:
        from playwright_stealth import stealth_sync           # playwright-stealth 1.x
        stealth_sync(page)
        return
    except ImportError:
        pass
    try:
        from playwright_stealth import Stealth                # playwright-stealth 2.x
        Stealth().apply_stealth_sync(page)
    except (ImportError, AttributeError):
        pass                                                 # not installed: the User-Agent is still disguised


def try_search_page():
    if not SEARCH_CANARY_URL:
        return "skipped", "SEARCH_CANARY_URL is not set (section 1): build it from CHRISTIES_DICT"
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "skipped", "Playwright is not installed (pip install playwright && python -m playwright install chromium)"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=STEALTH_LAUNCH_ARGS)
        try:
            context = browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=STEALTH_USER_AGENT,
                                          locale="en-US", extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
            page = context.new_page()
            apply_stealth(page)

            response = page.goto(SEARCH_CANARY_URL, wait_until="domcontentloaded", timeout=60000)
            if response is not None:
                failure_type = classify_status(response.status)
                if failure_type:
                    return failure_type, f"HTTP {response.status} on the search page"
            time.sleep(2)                                    # like the scraper

            html = page.content()
            if looks_like_bot_challenge(html):
                return ANTI_BOT, "bot challenge on the search page"

            # Exactly the scraper's own reading (BeautifulSoup, same classes)
            soup = BeautifulSoup(html, "html.parser")
            grid = soup.find("div", class_=SEARCH_GRID_CLASS, attrs=SEARCH_GRID_ATTRS)
            if not grid:
                visible_text = soup.body.get_text(" ", strip=True) if soup.body else ""
                if len(visible_text) < MIN_VISIBLE_TEXT:
                    return EMPTY_RESPONSE, "the search page did not render"
                return STRUCTURE_CHANGE, 'results grid not found: div.row[data-qa="search-result-tiles"]'

            tiles = grid.find_all("div", class_=SEARCH_TILE_CLASS)
            if not tiles:
                return STRUCTURE_CHANGE, "no result tile with the exact class the scraper looks for"

            links = [tile.find("a", href=True) for tile in tiles]
            links = [link for link in links if link]
            if not links:
                return STRUCTURE_CHANGE, "no link in the result tiles"

            names = []
            for tile in tiles:
                name_link = tile.find("a", class_=SEARCH_TILE_ARTIST_CLASS)
                if name_link:
                    names.append(name_link.text.strip())
            if not names:
                return STRUCTURE_CHANGE, f"no artist name (a.{SEARCH_TILE_ARTIST_CLASS}): every link would be skipped"
            if not any(SEARCH_CANARY_WORD in name.lower() for name in names):
                return STRUCTURE_CHANGE, f"artist names read are {names[:3]}: the name selector reads something else"

            has_pages = bool(soup.find_all("ul", class_=SEARCH_PAGINATION_CLASS))
            return None, f"{len(tiles)} tiles, {len(links)} links, first artist: {names[0]}, pagination: {has_pages}"
        except PlaywrightTimeout:
            return TIMEOUT, "the search page took more than 60 seconds to load"
        finally:
            browser.close()