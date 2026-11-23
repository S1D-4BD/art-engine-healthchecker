
import json
import random
import re
import time

import requests
from bs4 import BeautifulSoup

from healthcheck_core import *   # noqa: F401,F403  (download, failure types, add_finding...)


### Site settings used by the common engine

SITE_NAME = 'Phillips'
SOURCE = 'phillips'
CANARY_EMPTY_TEXT = 'empty pages: scraping now would lose lots'
THUMBNAIL_REDIRECT_TEXT = 'redirects to a thumbnail: '
TEST_MESSAGE = 'TEST: 2 of 6 canary lots lost the elements the scraper reads.'
SEARCH_HELP = 'Check the search page (Playwright if installed)'
LIVE_PAGE_LINK_FORMATS = ('lot',)
LIVE_IMAGE_SKIPPED_FORMATS = ('empty', 'unknown', 'assets')


### What the scraper reads on this site

# Lot page links (checked on the live site):
#   https://www.phillips.com/detail/banksy/NY010813/161   (maker / sale code / lot number)
#   https://www.phillips.com/detail/banksy/238143         (maker / object number: the links of the search page)
# The search page gives relative links ("/detail/..."): both forms are accepted.
LINK_FORMAT = re.compile(
    r"^(?:https://www\.phillips\.com)?/(?:[a-z]{2}/)?detail/(?:[^/?#]+/){1,3}(?:[A-Z]{2}\d{6}/\d+[A-Z]?|\d+)/?(?:[?#].*)?$")


# Image URLs: two hosts (checked on the live site and in the database)
#   https://dist.phillips.com/auction-assets/NY010824/207316_001.jpg
#   https://assets.phillips.com/t_Website_CuratedAuction_TwoRowsTwoColumns/auctions/NY010813/161_001.jpg
IMAGE_FORMAT = re.compile(r"^https://(?:dist|assets)\.phillips\.com/.+\.(?:jpe?g|png|gif|webp)(?:\?.*)?$", re.IGNORECASE)


# The old image host no longer exists (checked: assets.phillips.com does not resolve anymore). The same files are on
# dist.phillips.com: .../t_<transformation>/auctions/NY010315/61_001.jpg -> .../auction-assets/NY010315/61_001.jpg
OLD_IMAGE_HOST = re.compile(r"^https://assets\.phillips\.com/[^/]+/auctions/([A-Z]{2}\d{6}/[^?#]+)")


NEW_IMAGE_PREFIX = "https://dist.phillips.com/auction-assets/"


# Names used for "no image" pictures
PLACEHOLDER_NAMES = ["placeholder", "no-image", "noimage", "no_image"]


# The search page is loaded by the scraper with Playwright and a browser User-Agent (same as phillips_utils.py)
BROWSER_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/118.0.0.0 Safari/537.36")


BROWSER_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-infobars", "--no-sandbox",
                       "--disable-dev-shm-usage"]


# A page with less visible text than this is an empty "shell" page
MIN_VISIBLE_TEXT = 300


BOT_CHALLENGE_HTML = ["cf-chl-", "challenge-platform", "px-captcha", "captcha-delivery", "_incapsula_resource",
                      "errors.edgesuite.net"]


# Known past lots, sold WITH a published price and an image, taken from the database (so in the link format the
# scraper really collects) and checked with the scraper's own code: different years, cities and kinds of work.
# A lot shown as "Sold" without an amount must not be used.
CANARY_LOTS = [
    "https://www.phillips.com/detail/nicolas-de-stael/229083",    # 2026 New York, de Stael, Fleurs sur fond rouge
    "https://www.phillips.com/detail/mel-bochner/203046",         # 2024 London, Bochner, Oh Well (K.W. 2020.02)
    "https://www.phillips.com/detail/adam-pendleton/178004",      # 2022 New York, Pendleton, Untitled
    "https://www.phillips.com/detail/lita-cabellut/149208",       # 2020 London, Cabellut, Malta
    "https://www.phillips.com/detail/hiroshi-sugimoto/119656",    # 2018 New York, Sugimoto, Lightning Fields 145
    "https://www.phillips.com/detail/tauba-auerbach/89562",       # 2015 London, Auerbach, Alive
]


# What the scraper reads on a lot page (phillips_utils.get_info_artwork_phillips)
PAYLOAD_PATTERN = re.compile(r"enqueue\((.*?)\);</script>", re.DOTALL)


REQUIRED_LOT_FIELDS = ["makerName", "description", "lotNumber"]      # in the page's own lot object


REQUIRED_PAYLOAD_KEYS = ["auctionDetailsSmall", "auctionName", "priceCurrency", "lotNumberFull"]


IMAGE_FIELD = "mainImagePath"


ESTIMATE_BLOCK = {"data-testid": "detail-list", "class_part": "pah-lot-estimate"}


SOLD_BLOCK = {"data-testid": "detail", "class_part": "seldon-bid-snapshot__sold"}


VALUE_DIV_CLASS = "seldon-detail__value"


# Search page (phillips_utils.find_links_to_artworks and build_url_query)
SEARCH_CANARY_URL = "https://www.phillips.com/search/1/?search=banksy"


SEARCH_CANARY_WORD = "banksy"


SEARCH_CONTAINER_CLASS = "gallery search-results-container"          # compared EXACTLY, like the scraper


SEARCH_ITEM_CLASSES = ["col-xs-6 col-sm-3 search-result-item", "col-xs-6 col-sm-3 pending search-result-item"]


SEARCH_MAKER_CLASS = "maker"


SEARCH_TEXT_CLASS = "search-result-text"


SEARCH_PAGINATION_SELECTOR = "span.num a"


def get_link_format(link):
    """Returns 'lot', 'empty' or 'unknown'."""
    if not link:
        return "empty"
    if LINK_FORMAT.match(link):
        return "lot"
    return "unknown"


def get_image_format(image_url):
    """Returns 'dist' (dist.phillips.com), 'assets' (assets.phillips.com), 'empty' or 'unknown'."""
    if not image_url or not image_url.strip():
        return "empty"
    if IMAGE_FORMAT.match(image_url):
        return "dist" if image_url.startswith("https://dist.") else "assets"
    return "unknown"


def new_image_url(image_url):
    """The dist.phillips.com address of an image stored with the old host, or None."""
    match = OLD_IMAGE_HOST.match(image_url or "")
    return NEW_IMAGE_PREFIX + match.group(1) if match else None


def get_image_size_in_pixels(image_url):
    """Phillips image URLs don't tell the size."""
    return None


def is_placeholder(image_url):
    lower_url = image_url.lower()
    for name in PLACEHOLDER_NAMES:
        if name in lower_url:
            return True
    return False


def is_thumbnail(image_url):
    """Phillips thumbnails have 'thumb' in their transformation name."""
    return "thumb" in image_url.lower()


def turbo_value(parsed, ref):
    """In the turbo-stream block, values are positions in one flat list (negative numbers mean null)."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        return parsed[ref] if 0 <= ref < len(parsed) else None
    return ref


def turbo_object(parsed, obj):
    """Decodes one turbo-stream object ({"_<key position>": value position}) into {key: value}."""
    if not isinstance(obj, dict):
        return {}
    decoded = {}
    for key, value in obj.items():
        if isinstance(key, str) and key.startswith("_") and key[1:].isdigit():
            name = turbo_value(parsed, int(key[1:]))
            if isinstance(name, str):
                decoded[name] = turbo_value(parsed, value)
    return decoded


def read_payload(html):
    """
    The lot data block, read exactly like get_info_artwork_phillips(). Returns (parsed, problem):
    parsed is the flat list of the turbo-stream, or None if it cannot be read.
    """
    match = PAYLOAD_PATTERN.search(html)
    if not match:
        return None, "the enqueue(...) data block is not in the page: the scraper drops the lot"
    try:
        parsed = json.loads(match.group(1).strip('"').encode("utf-8").decode("unicode_escape"))
    except (ValueError, UnicodeDecodeError):
        return None, "the enqueue(...) data block cannot be read anymore: the scraper drops the lot"
    if not isinstance(parsed, list) or not parsed:
        return None, "the enqueue(...) data block is not a list anymore"
    return parsed, ""


def find_main_lot(parsed):
    """The page's own lot object (loaderData -> the "detail" route -> lot), like _decode_main_lot()."""
    try:
        root = turbo_object(parsed, parsed[0])
        routes = turbo_object(parsed, root.get("loaderData"))
        for route_name, route_data in routes.items():
            if "detail" in route_name:
                lot = turbo_object(parsed, turbo_object(parsed, route_data).get("lot"))
                if lot:
                    return lot
    except (IndexError, TypeError, AttributeError):
        pass
    return {}


def readable(text):
    """Accents come out of the data block garbled (like in the scraper, before its ftfy cleaning): fix them."""
    if not isinstance(text, str):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def find_block(soup, block):
    """A <div> found like the scraper does: its data-testid, and a class containing a given word."""
    return soup.find("div", attrs={"data-testid": block["data-testid"],
                                   "class": lambda classes, part=block["class_part"]: classes and part in classes})


def block_value(block_div):
    """The text of div.seldon-detail__value > span, or None if this path doesn't exist."""
    value_div = block_div.find("div", class_=VALUE_DIV_CLASS) if block_div else None
    span = value_div.find("span") if value_div else None
    return span.get_text(strip=True) if span else None


def inspect_lot_page(link):
    """
    Downloads a lot page like the scraper does (aiohttp, no JavaScript) and returns:
      failure_type  None if the page has everything the scraper needs, otherwise the likely cause
      detail        a short explanation
      image_url     the lot's image (None if none)
      no_image      True if the lot has no image
      sold_price    the sold price text ("" = "Sold" without an amount, None = no sold block)
    The tests go from "we didn't get the page at all" to "the page changed".
    """
    page = {"failure_type": None, "detail": "", "image_url": None, "no_image": False, "sold_price": None}

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

    # 3. The lot data block: without it, the scraper drops the lot
    parsed, problem = read_payload(html)
    if parsed is None:
        visible_text = soup.body.get_text(" ", strip=True) if soup.body else ""
        if len(visible_text) < MIN_VISIBLE_TEXT:
            page["failure_type"] = EMPTY_RESPONSE
            page["detail"] = f"empty page ({len(visible_text)} characters of text)"
        else:
            page["failure_type"] = SCHEMA_CHANGE
            page["detail"] = problem
        return page

    # 4. The page's own lot object: without it, the scraper guesses and saves wrong data
    lot = find_main_lot(parsed)
    empty_fields = [field for field in REQUIRED_LOT_FIELDS if lot.get(field) in (None, "", "None")]
    if empty_fields:
        page["failure_type"] = SCHEMA_CHANGE
        page["detail"] = (f"lot object not readable (empty: {empty_fields}): the scraper would guess the values "
                          "from the whole page and save wrong data (or the lot no longer exists)")
        return page

    missing_keys = [key for key in REQUIRED_PAYLOAD_KEYS if key not in parsed]
    if missing_keys:
        page["failure_type"] = SCHEMA_CHANGE
        page["detail"] = f"keys read by the scraper not found in the data block: {missing_keys}"
        return page

    # 5. The image
    image_url = lot.get(IMAGE_FIELD)
    if isinstance(image_url, str) and image_url.strip():
        page["image_url"] = image_url
    else:
        page["no_image"] = True

    # 6. The estimate and the sold price, read in the HTML
    if block_value(find_block(soup, ESTIMATE_BLOCK)) is None:
        page["failure_type"] = STRUCTURE_CHANGE
        page["detail"] = ("estimate block not found: div[data-testid=detail-list].pah-lot-estimate "
                          "> div.seldon-detail__value > span")
        return page
    sold_block = find_block(soup, SOLD_BLOCK)
    if sold_block is not None:
        page["sold_price"] = block_value(sold_block) or ""

    sold_text = {None: "no sold block", "": "sold without an amount"}.get(page["sold_price"], page["sold_price"])
    image_text = "no image" if page["no_image"] else "image found"
    page["detail"] = (f"{readable(lot.get('makerName'))}, {readable(lot.get('description'))}, lot {lot.get('lotNumber')}: "
                      f"{sold_text}, {image_text}")
    return page


def check_canary(link, attempts=3, wait_seconds=10):
    """
    Checks one known lot. Returns (status, failure_type, detail) where status is:
      OK            the page has everything the scraper needs
      INCONCLUSIVE  Phillips served an empty page every time (temporary on their side)
      FAILED        another problem that didn't go away after retrying
    """
    for attempt_number in range(1, attempts + 1):
        page = inspect_lot_page(link)
        if page["failure_type"] not in TRANSIENT_FAILURE_TYPES:
            break
        if attempt_number < attempts:
            time.sleep(wait_seconds)

    if page["failure_type"] == EMPTY_RESPONSE:
        return "INCONCLUSIVE", EMPTY_RESPONSE, page["detail"] + f" (on all {attempts} attempts)"
    if page["failure_type"]:
        return "FAILED", page["failure_type"], page["detail"]
    # Canary lots were all sold with a price, and all have an image
    if not page["sold_price"] or not re.search(r"\d", page["sold_price"]):
        return "FAILED", STRUCTURE_CHANGE, ("sold price not found (div[data-testid=detail].seldon-bid-snapshot__sold): "
                                            "the scraper would record sold lots as bought in")
    if page["no_image"]:
        return "FAILED", DEGRADED_DATA, "the lot's image (mainImagePath) is missing"
    return "OK", None, page["detail"]


def read_search_page(html):
    """Reads the search page exactly like find_links_to_artworks() and build_url_query(). Returns (failure_type, detail)."""
    if looks_like_bot_challenge(html):
        return ANTI_BOT, "bot challenge on the search page"
    soup = BeautifulSoup(html, "html.parser")

    container = soup.find("div", class_=SEARCH_CONTAINER_CLASS)
    if not container:
        visible_text = soup.body.get_text(" ", strip=True) if soup.body else ""
        if len(visible_text) < MIN_VISIBLE_TEXT:
            return EMPTY_RESPONSE, "the search page did not render"
        return STRUCTURE_CHANGE, f"results container not found: div.{SEARCH_CONTAINER_CLASS.replace(' ', '.')}"

    items = container.find_all("li", class_=SEARCH_ITEM_CLASSES)
    if not items:
        return STRUCTURE_CHANGE, "no result item with the exact classes the scraper looks for"

    links, names, dated = 0, [], 0
    for item in items:
        if item.find("a", href=True):
            links += 1
        maker = item.find("strong", class_=SEARCH_MAKER_CLASS)
        if maker:
            names.append(maker.get_text().strip())
        text_div = item.find("div", class_=SEARCH_TEXT_CLASS)
        if text_div and text_div.find("p"):
            dated += 1
    if not links:
        return STRUCTURE_CHANGE, "no link in the result items"
    if not names:
        return STRUCTURE_CHANGE, f"no artist name (strong.{SEARCH_MAKER_CLASS}): every result would be skipped"
    if not any(SEARCH_CANARY_WORD in name.lower() for name in names):
        return STRUCTURE_CHANGE, f"artist names read are {names[:3]}: the name selector reads something else"
    if dated == 0:
        return STRUCTURE_CHANGE, (f"no date (div.{SEARCH_TEXT_CLASS} > p): every lot would be filed as past, "
                                  "upcoming lots included")

    # Pagination, read like build_url_query: int(href.split('/')[2]) on the last "span.num a" link
    page_links = soup.select(SEARCH_PAGINATION_SELECTOR)
    pages_text = "1 page"
    if page_links:
        href = page_links[-1].get("href") or ""
        try:
            pages_text = f"{int(href.split('/')[2])} pages"
        except (IndexError, ValueError):
            return STRUCTURE_CHANGE, (f"pagination link {href!r} is not in the format the scraper reads "
                                      "('/search/<n>/?search=...'): build_url_query fails for every artist "
                                      "with more than one page of results")
    return None, f"{len(items)} results, {links} links, first artist: {names[0]}, {pages_text}"


def try_search_page():
    """One look at the search page, with Playwright like the scraper (or without a browser if it is not installed)."""
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        # The Phillips search page is rendered on the server: it can be read without a browser
        result = download(SEARCH_CANARY_URL)
        failure_type = classify_download(result)
        if failure_type:
            return failure_type, result["detail"] + " on the search page"
        failure_type, detail = read_search_page(result["response"].text)
        return failure_type, detail + " (read without Playwright)"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=BROWSER_LAUNCH_ARGS)
        try:
            context = browser.new_context(user_agent=BROWSER_USER_AGENT)
            page = context.new_page()
            response = page.goto(SEARCH_CANARY_URL, timeout=60000)
            if response is not None:
                failure_type = classify_status(response.status)
                if failure_type:
                    return failure_type, f"HTTP {response.status} on the search page"
            time.sleep(2)                                    # like the scraper
            return read_search_page(page.content())
        except PlaywrightTimeout:
            return TIMEOUT, "the search page took more than 60 seconds to load"
        finally:
            browser.close()


def extra_image_check(artwork, image_url, image_format, findings):
    """The old image host no longer exists: the image cannot be shown anymore."""
    if image_format == "assets":
        add_finding(findings, "image_old_host", ERROR, artwork,
                    "assets.phillips.com no longer exists; same file: " + str(new_image_url(image_url)), DEGRADED_DATA)
        return True
    return None