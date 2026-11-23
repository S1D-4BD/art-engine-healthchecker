##logical engine that checks the health of artwork scrapers
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup


### 1 Settings





# Same User-Agent as the scraper, so Sotheby's serves us the same pages as production
HEADERS = {"User-Agent": "Python/3.11 aiohttp/3.9.5"}

# A real artwork image is much bigger than this
MIN_IMAGE_BYTES = 5000

# A real lot page is at least 40,000 characters: much less means a cut or empty answer
MIN_PAGE_CHARACTERS = 2000

# Text shown on "are you a robot?" pages (checked: none of these appear on normal Sotheby's pages)
BOT_CHALLENGE_TEXT = ["access denied", "just a moment", "attention required", "verify you are human",
                      "are you a robot", "press & hold", "captcha"]





# Failure types
STRUCTURE_CHANGE = "structure_change"    # HTML elements the scraper reads are gone
SCHEMA_CHANGE = "schema_change"          # JSON keys the scraper reads are gone
ANTI_BOT = "anti_bot"                    # 403 or a CAPTCHA / "access denied" page
RATE_LIMIT = "rate_limit"                # 429: too many requests
AUTH_EXPIRED = "auth_expired"            # 401 or redirected to a login page
TIMEOUT = "timeout"
NETWORK_ERROR = "network_error"
SERVER_ERROR = "server_error"            # 5xx on Sotheby's side
UNEXPECTED_STATUS = "unexpected_status"  # any other HTTP status
EMPTY_RESPONSE = "empty_response"        # empty, cut or "shell" page
NOT_FOUND = "not_found"                  # 404: the page doesn't exist anymore
DEGRADED_DATA = "degraded_data"          # the scraper worked but the data is worse than usual

# Problems that can disappear on their own: retried before judging
TRANSIENT_FAILURE_TYPES = [TIMEOUT, NETWORK_ERROR, SERVER_ERROR, EMPTY_RESPONSE, RATE_LIMIT]
# Problems that mean the scraper is (or will soon be) broken
CRITICAL_FAILURE_TYPES = [STRUCTURE_CHANGE, SCHEMA_CHANGE, ANTI_BOT, AUTH_EXPIRED]

# Severity of a single finding (one artwork) ...
ERROR = "error"
WARNING = "warning"
# ... and of a signal / alert (one problem for the whole source)
CRITICAL = "critical"

# When problems become signals
CANARY_MIN_FAILURES = 2         # a structure change must show up on at least 2 canary lots
SAMPLE_PROBLEM_SHARE = 0.20     # in a sample of pages or images, at least 20% must have the same problem
MIN_ARTWORKS_FOR_RATE = 10      # files with fewer artworks are too small to judge a rate
MAX_COUNT_DROP = 0.20           # alert if a file loses more than 20% of its artworks
BASELINE_MARGIN = 0.10          # alert if a file's broken-image rate is 10 points above its usual rate
OVERALL_BASELINE_MARGIN = 0.05  # same for all files together
MIN_BASELINE_RUNS = 1           # how many past values are needed before trusting the "usual rate"
BASELINE_LENGTH = 14            # how many past values are kept
DEGRADED_FILES_CRITICAL = 5     # from this many degraded files, the data alert is critical

# Checks that depend on the network: their problems must be confirmed on several runs
NETWORK_CHECKS = ["canary", "search", "pages", "images"]


### 2 Reading and writing files (local or S3)

S3_CLIENT = None


def get_s3_client():
    """Creates the S3 connection the first time it is needed (boto3 is only needed for s3:// paths)."""
    global S3_CLIENT
    if S3_CLIENT is None:
        import boto3
        S3_CLIENT = boto3.client("s3")
    return S3_CLIENT


def split_s3_path(location):
    """'s3://bucket/folder/file.json' -> ('bucket', 'folder/file.json')"""
    without_prefix = location.replace("s3://", "", 1)
    parts = without_prefix.split("/", 1)
    bucket = parts[0]
    key = ""
    if len(parts) > 1:
        key = parts[1]
    return bucket, key


def file_exists(location):
    if not location.startswith("s3://"):
        return os.path.exists(location)

    from botocore.exceptions import ClientError
    bucket, key = split_s3_path(location)
    try:
        get_s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def get_last_modified(location):
    """When the file was last written, as text like '2026-11-02T13:45:00+00:00'."""
    if location.startswith("s3://"):
        bucket, key = split_s3_path(location)
        info = get_s3_client().head_object(Bucket=bucket, Key=key)
        return info["LastModified"].isoformat()
    timestamp = os.path.getmtime(location)
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def read_json(location):
    if location.startswith("s3://"):
        bucket, key = split_s3_path(location)
        s3_object = get_s3_client().get_object(Bucket=bucket, Key=key)
        return json.loads(s3_object["Body"].read().decode("utf-8"))
    with open(location, encoding="utf-8") as file:
        return json.load(file)


def write_json(location, data):
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if location.startswith("s3://"):
        bucket, key = split_s3_path(location)
        get_s3_client().put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType="application/json")
    else:
        with open(location, "w", encoding="utf-8") as file:
            file.write(text)


def list_json_files(prefix):
    """All .json files in an S3 folder (or a local folder), with their last modification date."""
    files = []
    if prefix.startswith("s3://"):
        bucket, key_prefix = split_s3_path(prefix)
        paginator = get_s3_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
            for s3_object in page.get("Contents", []):
                key = s3_object["Key"]
                if key.endswith(".json"):
                    files.append({
                        "location": "s3://" + bucket + "/" + key,
                        "name": key.split("/")[-1],
                        "last_modified": s3_object["LastModified"].isoformat(),
                    })
    else:
        for name in sorted(os.listdir(prefix)):
            if name.endswith(".json"):
                location = os.path.join(prefix, name)
                files.append({"location": location, "name": name, "last_modified": get_last_modified(location)})
    return files


def load_artworks(location):
    """Reads a list of artworks from a local JSON file or from S3 (s3://bucket/key)."""
    data = read_json(location)

    # The files are usually a list of artworks so if it is a dict, use the first list found inside it
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                data = value
                break
        else:
            data = [data]

    artworks = []
    for item in data:
        if isinstance(item, dict):
            artworks.append(item)
    return artworks


### 3 Offline checks (no internet needed)











def add_finding(findings, rule, severity, artwork, detail="", failure_type=None):
    """Adds one problem about one artwork to the list of findings."""
    if artwork is None:
        artwork = {}
    findings.append({
        "rule": rule,
        "severity": severity,
        "failure_type": failure_type,
        "artwork_id": artwork.get("id"),
        "link": artwork.get("link"),
        "detail": detail,
    })


def check_artwork_offline(artwork, findings, counts, min_side):
    """
    Checks one artwork's link and image URL, without going on the internet.
    Returns True if the artwork's image is missing or broken.
    """
    link_format = get_link_format(artwork.get("link"))
    counts["links"][link_format] = counts["links"].get(link_format, 0) + 1
    if link_format in ("empty", "unknown"):
        add_finding(findings, "link_unrecognised", WARNING, artwork, "link format: " + link_format)

    image_url = artwork.get("image_url")
    image_format = get_image_format(image_url)
    counts["images"][image_format] = counts["images"].get(image_format, 0) + 1

    if image_format == "empty":
        add_finding(findings, "image_missing", ERROR, artwork, failure_type=DEGRADED_DATA)
        return True
    if is_placeholder(image_url):
        add_finding(findings, "image_placeholder", ERROR, artwork, image_url, DEGRADED_DATA)
        return True
    if image_format == "unknown":
        add_finding(findings, "image_format_unknown", ERROR, artwork, image_url, DEGRADED_DATA)
        return True
    if extra_image_check is not None:
        extra = extra_image_check(artwork, image_url, image_format, findings)
        if extra is not None:
            return extra
    if is_thumbnail(image_url):
        add_finding(findings, "image_thumbnail", ERROR, artwork, image_url, DEGRADED_DATA)
        return True

    size = get_image_size_in_pixels(image_url)
    if size is not None and size < min_side:
        add_finding(findings, "image_low_resolution", WARNING, artwork, f"{size}px < {min_side}px")
    return False


def compare_stages(before, after, findings):
    """
    Finds artworks that had an image in the first file but lost it (or changed it) in the second file.
    Returns how many artworks lost their image.
    """
    after_by_id = {}
    for artwork in after:
        if artwork.get("id"):
            after_by_id[artwork["id"]] = artwork

    images_lost = 0
    missing_in_after = 0
    for artwork in before:
        later = after_by_id.get(artwork.get("id"))
        if later is None:
            missing_in_after += 1
            continue

        had_image = get_image_format(artwork.get("image_url")) != "empty"
        has_image = get_image_format(later.get("image_url")) != "empty"

        if had_image and not has_image:
            images_lost += 1
            add_finding(findings, "image_lost_between_stages", ERROR, artwork, "present before, empty after",
                        DEGRADED_DATA)
        elif had_image and has_image and artwork.get("image_url") != later.get("image_url"):
            add_finding(findings, "image_changed_between_stages", WARNING, artwork, later.get("image_url"))

    if missing_in_after > 0:
        add_finding(findings, "artworks_absent_from_later_stage", WARNING, None, f"{missing_in_after} artwork ids")
    return images_lost


### 4 Downloading and classifying answers

def download(url, method="GET"):
    """
    Downloads a URL (following redirects). Tries twice when the network fails or the server answers 5xx.
    Always returns a dict:
      response  the requests response, or None
      status    the HTTP status, or None
      error     None, "timeout" or "connection"
      detail    a short description, for the report
    """
    result = {}
    for try_number in range(2):
        try:
            if method == "HEAD":
                response = requests.head(url, headers=HEADERS, timeout=20, allow_redirects=True)
            else:
                response = requests.get(url, headers=HEADERS, timeout=20)
            result = {"response": response, "status": response.status_code, "error": None,
                      "detail": f"HTTP {response.status_code}"}
            if response.status_code < 500:
                return result
        except requests.Timeout:
            result = {"response": None, "status": None, "error": "timeout", "detail": "no answer within 20 seconds"}
        except requests.RequestException as error:
            result = {"response": None, "status": None, "error": "connection", "detail": f"network error: {error}"}
        if try_number == 0:
            time.sleep(2)
    return result


def classify_status(status):
    """Failure type for an HTTP status, or None for 200."""
    if status == 200:
        return None
    if status == 429:
        return RATE_LIMIT
    if status == 401:
        return AUTH_EXPIRED
    if status == 403:
        return ANTI_BOT
    if status in (404, 410):
        return NOT_FOUND
    if status >= 500:
        return SERVER_ERROR
    return UNEXPECTED_STATUS


def classify_download(result):
    """Looks at the answer of download() and returns the likely failure type, or None if it looks fine."""
    if result["error"] == "timeout":
        return TIMEOUT
    if result["error"] == "connection":
        return NETWORK_ERROR

    failure_type = classify_status(result["status"])
    if failure_type:
        return failure_type

    final_url = result["response"].url.lower()
    if "/login" in final_url or "signin" in final_url:
        return AUTH_EXPIRED
    return None


def looks_like_bot_challenge(html):
    """True if the page looks like a CAPTCHA or "access denied" page instead of the real page."""
    lower_html = html.lower()
    for marker in BOT_CHALLENGE_HTML:
        if marker in lower_html:
            return True

    soup = BeautifulSoup(html, "html.parser")
    visible_text = ""
    if soup.title:
        visible_text += soup.title.get_text(" ")
    if soup.body:
        visible_text += " " + soup.body.get_text(" ", strip=True)
    visible_text = visible_text.lower()
    for marker in BOT_CHALLENGE_TEXT:
        if marker in visible_text:
            return True
    return False


def severity_for(failure_type):
    if failure_type in CRITICAL_FAILURE_TYPES:
        return CRITICAL
    return WARNING


def make_signal(check, name, failure_type, severity, message, evidence):
    """
    A signal is one problem for the whole source (not one artwork), e.g. "2 of 6 canary lots lost their
    image elements". Signals become alerts in section 9.
    """
    return {
        "key": check + ":" + name,
        "check": check,
        "failure_type": failure_type,
        "severity": severity,
        "message": message,
        "evidence": evidence[:10],
    }


def signals_from_sample(check, results, sample_size, what):
    """
    results: list of (failure_type, detail, link) for the sampled items that had a problem.
    A failure type becomes a signal only if it hits at least 2 items and SAMPLE_PROBLEM_SHARE of the sample.
    """
    by_type = {}
    for failure_type, detail, link in results:
        if failure_type not in by_type:
            by_type[failure_type] = []
        by_type[failure_type].append(f"{link} - {detail}")

    signals = []
    for failure_type, evidence in by_type.items():
        share = len(evidence) / sample_size
        if len(evidence) >= 2 and share >= SAMPLE_PROBLEM_SHARE:
            message = f"{len(evidence)}/{sample_size} sampled {what}: {failure_type}"
            signals.append(make_signal(check, failure_type, failure_type, severity_for(failure_type), message, evidence))
    return signals


### 5 Lot pages and canaries













def run_canaries(delay):
    """Checks every canary lot. Returns (results for the report, signals)."""
    results = []
    for link in CANARY_LOTS:
        status, failure_type, detail = check_canary(link)
        results.append({"link": link, "status": status, "failure_type": failure_type, "detail": detail})
        time.sleep(delay)

    # Group the failures by type
    failed_by_type = {}
    inconclusive = []
    for result in results:
        if result["status"] == "FAILED":
            if result["failure_type"] not in failed_by_type:
                failed_by_type[result["failure_type"]] = []
            failed_by_type[result["failure_type"]].append(result)
        elif result["status"] == "INCONCLUSIVE":
            inconclusive.append(result)

    signals = []
    total = len(CANARY_LOTS)
    for failure_type, failed in failed_by_type.items():
        evidence = []
        for result in failed:
            evidence.append(f"{result['link']} - {result['detail']}")

        if failure_type == NOT_FOUND:
            # Not a scraper problem: the canary list needs a new lot
            message = f"{len(failed)} canary lot(s) no longer exist: replace them in CANARY_LOTS"
            signals.append(make_signal("canary", NOT_FOUND, NOT_FOUND, WARNING, message, evidence))
        elif len(failed) >= CANARY_MIN_FAILURES:
            message = f"{len(failed)}/{total} canary lots: {failure_type}"
            signals.append(make_signal("canary", failure_type, failure_type, severity_for(failure_type),
                                       message, evidence))

    if len(inconclusive) >= total / 2:
        evidence = []
        for result in inconclusive:
            evidence.append(f"{result['link']} - {result['detail']}")
        message = f"{len(inconclusive)}/{total} canary lots are {CANARY_EMPTY_TEXT}"
        signals.append(make_signal("canary", EMPTY_RESPONSE, EMPTY_RESPONSE, WARNING, message, evidence))

    return results, signals


def check_page_online(artwork):
    """Checks one lot page from the data. Returns (failure_type, detail): (None, ...) if the page is fine."""
    page = inspect_lot_page(artwork["link"])
    return page["failure_type"], page["detail"]


### 6 Search page (needs Playwright)



def check_search_page(attempts=2):
    for attempt_number in range(1, attempts + 1):
        failure_type, detail = try_search_page()
        if failure_type not in TRANSIENT_FAILURE_TYPES:
            break
        if attempt_number < attempts:
            time.sleep(15)
    return failure_type, detail


### 7 Images online

def check_image_online(artwork):
    """Checks that the image URL really returns an image. Returns (failure_type, detail)."""
    image_url = artwork["image_url"]

    # HEAD only asks for the headers (fast). Some servers refuse it or don't give the size: then we GET.
    result = download(image_url, method="HEAD")
    if result["response"] is None or result["status"] in (403, 405) or "Content-Length" not in result["response"].headers:
        result = download(image_url, method="GET")

    failure_type = classify_download(result)
    if failure_type:
        return failure_type, result["detail"]

    response = result["response"]
    content_type = response.headers.get("Content-Type", "")
    if "text/html" in content_type:
        if looks_like_bot_challenge(response.text):
            return ANTI_BOT, "a bot challenge page instead of the image"
        return DEGRADED_DATA, "a web page instead of an image"
    if not content_type.startswith("image/"):
        return DEGRADED_DATA, "not an image: " + (content_type or "no content type")

    if "Content-Length" in response.headers:
        size_in_bytes = int(response.headers["Content-Length"])
    else:
        size_in_bytes = len(response.content)
    if size_in_bytes < MIN_IMAGE_BYTES:
        return DEGRADED_DATA, f"suspiciously small image ({size_in_bytes} bytes)"

    # dam.sothebys.com URLs redirect to the real image: check where we ended up
    if is_thumbnail(response.url):
        return DEGRADED_DATA, THUMBNAIL_REDIRECT_TEXT + response.url

    return None, ""


### 8 Data files (after each collection)

def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def usual_rate(rate_history):
    """The usual broken-image rate of a file, or None if there isn't enough history yet."""
    if len(rate_history) < MIN_BASELINE_RUNS:
        return None
    return median(rate_history)


def judge_data_file(artworks_count, image_errors, history):
    """
    Compares a file with its own history. Returns (problem, detail):
      problem is None, "emptied", "dropped" or "degraded".
    The first time a file is seen, its rate is only learned: some artists always have many lots
    without images (copyright), that is their normal, not a scraper problem.
    """
    previous_count = 0
    rate_history = []
    if history:
        previous_count = history.get("artworks", 0)
        rate_history = history.get("rate_history", [])

    # Many artists simply have no Sotheby's lots: an empty file is only a problem if it had artworks before
    if artworks_count == 0:
        if previous_count > 0:
            return "emptied", f"{previous_count} -> 0 artworks"
        return None, ""

    if previous_count >= MIN_ARTWORKS_FOR_RATE and artworks_count < previous_count * (1 - MAX_COUNT_DROP):
        return "dropped", f"{previous_count} -> {artworks_count} artworks"

    if artworks_count < MIN_ARTWORKS_FOR_RATE:
        return None, ""

    usual = usual_rate(rate_history)
    if usual is None:
        return None, ""  # first time: learn this file's normal rate

    rate = image_errors / artworks_count
    if rate > usual + BASELINE_MARGIN:
        return "degraded", f"{rate:.0%} of images missing or broken (usually {usual:.0%})"
    return None, ""


def check_data_file(artworks, history, findings, counts, min_side):
    """Checks one artist file and returns its new entry for the state."""
    image_errors = 0
    for artwork in artworks:
        if check_artwork_offline(artwork, findings, counts, min_side):
            image_errors += 1

    rate_history = []
    if history:
        rate_history = list(history.get("rate_history", []))

    problem, detail = judge_data_file(len(artworks), image_errors, history)

    # Only normal values go into the history, so a broken collection doesn't become "the usual"
    if problem is None and len(artworks) >= MIN_ARTWORKS_FOR_RATE:
        rate_history.append(round(image_errors / len(artworks), 4))
        rate_history = rate_history[-BASELINE_LENGTH:]

    return {
        "artworks": len(artworks),
        "image_errors": image_errors,
        "rate_history": rate_history,
        "problem": problem,
        "detail": detail,
        "images_lost": 0,
    }


def pick_sample_candidates(artworks, how_many=2):
    """Keeps a few artworks of a file (only the fields needed) for the online checks."""
    candidates = []
    if len(artworks) <= how_many:
        chosen = artworks
    else:
        chosen = random.sample(artworks, how_many)
    for artwork in chosen:
        candidates.append({"id": artwork.get("id"), "link": artwork.get("link"), "image_url": artwork.get("image_url")})
    return candidates


def count_images_lost_in_next_stage(file, artworks, compare_prefix, findings):
    """Compares a raw file with the same file in the next stage, once that stage has processed it."""
    next_location = compare_prefix.rstrip("/") + "/" + file["name"]
    if not file_exists(next_location):
        return 0
    # The next stage hasn't processed this collection yet: comparing now would give false alarms
    if get_last_modified(next_location) < file["last_modified"]:
        return 0
    later_artworks = load_artworks(next_location)
    return compare_stages(artworks, later_artworks, findings)


def run_data_check(prefix, compare_prefix, state, findings, counts, min_side, max_files):
    """
    Checks every file of the folder that changed since the last run.
    Returns (number of files checked, sample candidates for the online checks).
    """
    files = list_json_files(prefix)
    changed_files = []
    for file in files:
        previous = state["files"].get(file["name"])
        if previous is None or previous.get("last_modified") != file["last_modified"]:
            changed_files.append(file)
    if max_files:
        changed_files = changed_files[:max_files]
    print(f"Data: {len(changed_files)} of {len(files)} files changed since the last run")

    candidates = []
    for number, file in enumerate(changed_files, start=1):
        try:
            artworks = load_artworks(file["location"])
        except Exception as error:
            # Not saved as checked, so it is retried on the next run
            print(f"  cannot read {file['name']}: {error}")
            continue

        history = state["files"].get(file["name"])
        entry = check_data_file(artworks, history, findings, counts, min_side)
        entry["last_modified"] = file["last_modified"]
        if compare_prefix:
            entry["images_lost"] = count_images_lost_in_next_stage(file, artworks, compare_prefix, findings)
        state["files"][file["name"]] = entry

        candidates.extend(pick_sample_candidates(artworks))
        if number % 200 == 0:
            print(f"  {number}/{len(changed_files)} files checked")

    return len(changed_files), candidates


def data_signals(state, files_were_checked):
    """Builds the data signals from every file known in the state (not only the ones checked today)."""
    emptied, dropped, degraded, lost = [], [], [], []
    total_artworks = 0
    total_image_errors = 0
    for name, entry in state["files"].items():
        total_artworks += entry.get("artworks", 0)
        total_image_errors += entry.get("image_errors", 0)
        problem = entry.get("problem")
        if problem == "emptied":
            emptied.append(f"{name}: {entry['detail']}")
        elif problem == "dropped":
            dropped.append(f"{name}: {entry['detail']}")
        elif problem == "degraded":
            degraded.append(f"{name}: {entry['detail']}")
        if entry.get("images_lost", 0) > 0:
            lost.append(f"{name}: {entry['images_lost']} images lost in the next stage")

    signals = []
    groups = [
        ("emptied_files", EMPTY_RESPONSE, emptied, "artist files became empty"),
        ("artworks_dropped", DEGRADED_DATA, dropped, "artist files lost more than 20% of their artworks"),
        ("degraded_files", DEGRADED_DATA, degraded, "artist files have far more missing/broken images than usual"),
    ]
    for name, failure_type, evidence, text in groups:
        if evidence:
            severity = WARNING
            if len(evidence) >= DEGRADED_FILES_CRITICAL:
                severity = CRITICAL
            signals.append(make_signal("data", name, failure_type, severity, f"{len(evidence)} {text}", evidence))

    if lost:
        signals.append(make_signal("stages", "image_lost", DEGRADED_DATA, WARNING,
                                   f"{len(lost)} files lost images between stages", lost))

    # All files together, compared with the usual overall rate
    if total_artworks > 0:
        overall_rate = total_image_errors / total_artworks
        usual = usual_rate(state["overall_rate_history"])
        if usual is not None and overall_rate > usual + OVERALL_BASELINE_MARGIN:
            message = f"{overall_rate:.1%} of all images missing or broken (usually {usual:.1%})"
            signals.append(make_signal("data", "overall_image_rate", DEGRADED_DATA, CRITICAL, message, []))
        elif files_were_checked:
            state["overall_rate_history"].append(round(overall_rate, 4))
            state["overall_rate_history"] = state["overall_rate_history"][-BASELINE_LENGTH:]

    return signals


### 9 Alerts (history, confirmation, cooldown)

def new_state():
    return {"signals": {}, "files": {}, "overall_rate_history": [], "last_run": None}


def load_state(location):
    if not file_exists(location):
        print("No state file yet: starting a new history at", location)
        return new_state()
    state = read_json(location)
    # Fill in anything missing (e.g. a state written by an older version of this file)
    for key, value in new_state().items():
        if key not in state:
            state[key] = value
    return state


def hours_between(earlier_text, later_text):
    earlier = datetime.fromisoformat(earlier_text)
    later = datetime.fromisoformat(later_text)
    return (later - earlier).total_seconds() / 3600


def make_notification(kind, key, entry):
    return {
        "kind": kind,
        "source": SOURCE,
        "key": key,
        "failure_type": entry["failure_type"],
        "severity": entry["severity"],
        "message": entry["message"],
        "evidence": entry.get("evidence", []),
        "first_seen": entry["first_seen"],
        "runs_in_a_row": entry["runs_in_a_row"],
    }


def update_alerts(state, signals, checks_run, now, confirm_runs, cooldown_hours):
    """
    Turns this run's signals into notifications, using what happened on previous runs:
      NEW       the problem was confirmed and not notified yet
      REMINDER  still there, and the last notification is older than cooldown_hours
      RESOLVED  it was notified before and is gone now
    """
    previous_signals = state["signals"]
    current_signals = {}
    notifications = []

    for signal in signals:
        key = signal["key"]
        entry = previous_signals.get(key)
        if entry is None:
            entry = {"first_seen": now, "runs_in_a_row": 0, "last_notified": None}
        entry["runs_in_a_row"] += 1
        entry["last_seen"] = now
        entry["check"] = signal["check"]
        entry["failure_type"] = signal["failure_type"]
        entry["severity"] = signal["severity"]
        entry["message"] = signal["message"]
        entry["evidence"] = signal["evidence"]

        # Network problems can be blips: wait until they show up on several runs in a row
        needed_runs = 1
        if signal["check"] in NETWORK_CHECKS:
            needed_runs = confirm_runs

        if entry["runs_in_a_row"] >= needed_runs:
            kind = None
            if entry["last_notified"] is None:
                kind = "NEW"
            elif hours_between(entry["last_notified"], now) >= cooldown_hours:
                kind = "REMINDER"
            if kind:
                notifications.append(make_notification(kind, key, entry))
                entry["last_notified"] = now

        current_signals[key] = entry

    for key, entry in previous_signals.items():
        if key in current_signals:
            continue
        if entry.get("check") not in checks_run:
            # This check didn't run this time: we don't know, keep the problem as it was
            current_signals[key] = entry
        elif entry.get("last_notified") is not None:
            notifications.append(make_notification("RESOLVED", key, entry))

    state["signals"] = current_signals
    return notifications


### 10 Report

def print_summary(report, findings, counts):
    print()
    print("=" * 80)
    print(SITE_NAME + " healthcheck -", report["generated_at"])
    print("Checks run:", ", ".join(report["checks_run"]) or "none")

    if report["canary"]:
        print("\nCanary lots:")
        for result in report["canary"]:
            print(f"  {result['status']:12} {result['link']}")
            print(f"               {result['detail']}")

    if report["search"]:
        print("\nSearch page:", report["search"]["failure_type"] or "OK", "-", report["search"]["detail"])

    if counts["links"] or counts["images"]:
        print("\nData:", report["data"]["files_checked"], "files checked")
        print("  Link formats: ", counts["links"])
        print("  Image formats:", counts["images"])
        rule_counts = {}
        for finding in findings:
            key = (finding["severity"], finding["rule"])
            rule_counts[key] = rule_counts.get(key, 0) + 1
        for (severity, rule), count in sorted(rule_counts.items()):
            print(f"  [{severity:7}] {rule:34} {count}")

    print("\nSignals this run:", len(report["signals"]))
    for signal in report["signals"]:
        print(f"  [{signal['severity']:8}] {signal['failure_type']:18} {signal['message']}")

    print("\nNotifications to send:", len(report["notifications"]))
    for notification in report["notifications"]:
        print(f"  {notification['kind']:9} [{notification['severity']}] {notification['failure_type']}: "
              f"{notification['message']}")
        for line in notification["evidence"][:5]:
            print("            -", line)
    print("=" * 80)


### Main program

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", help="One artworks JSON: local path or s3://bucket/key")
    parser.add_argument("--compare", help="With source: the same file in a later stage, to check that images survive")
    parser.add_argument("--data-prefix", help="Folder of artist files to check (only those changed since the last run)")
    parser.add_argument("--compare-prefix", help="With --data-prefix: folder of the next stage, to check that images survive")
    parser.add_argument("--max-files", type=int, default=None, help="With --data-prefix: check at most this many files")
    parser.add_argument("--canary", action="store_true", help="Check the known canary lots")
    parser.add_argument("--check-search", action="store_true", help=SEARCH_HELP)
    parser.add_argument("--check-pages", action="store_true", help="Download a sample of lot pages")
    parser.add_argument("--live", action="store_true", help="Download a sample of image URLs")
    parser.add_argument("--sample", type=int, default=None, help="How many artworks to check online (default: all)")
    parser.add_argument("--limit", type=int, default=None, help="With source: only check the first N artworks")
    parser.add_argument("--min-side", type=int, default=1024, help="Below this many pixels: low-resolution warning")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds to wait between two requests to " + SITE_NAME)
    parser.add_argument("--state", help="History file (local or s3://) used to confirm, repeat and resolve alerts")
    parser.add_argument("--confirm-runs", type=int, default=2, help="With --state: runs in a row before a network alert")
    parser.add_argument("--cooldown-hours", type=float, default=12, help="With --state: hours between two reminders")
    parser.add_argument("--report", help="Write the full report as JSON to this path (local or s3://)")
    parser.add_argument("--teams-webhook", default=os.environ.get("TEAMS_WEBHOOK_URL"),
                        help="Teams webhook URL to post the alerts to (default: the TEAMS_WEBHOOK_URL variable)")
    parser.add_argument("--teams-test", action="store_true", help="Only send a test card to --teams-webhook, then stop")
    args = parser.parse_args()

    if args.teams_test:
        if not args.teams_webhook:
            parser.error("--teams-test needs --teams-webhook or the TEAMS_WEBHOOK_URL variable")
        return send_teams_test(args.teams_webhook)

    if not args.source and not args.data_prefix and not args.canary and not args.check_search:
        parser.error("give a source JSON, --data-prefix, --canary or --check-search")

    now = datetime.now(timezone.utc).isoformat()
    findings = []
    counts = {"links": {}, "images": {}}
    signals = []
    checks_run = []
    sample_candidates = []
    files_checked = 0
    canary_results = []
    search_result = None

    # Read the history. Without one, there is nothing to confirm with: report problems immediately.
    try:
        if args.state:
            state = load_state(args.state)
            confirm_runs = args.confirm_runs
        else:
            state = new_state()
            confirm_runs = 1
    except Exception as error:
        print("Cannot read the state file:", error, file=sys.stderr)
        return 2

    # One artist file
    if args.source:
        try:
            artworks = load_artworks(args.source)
            later_artworks = None
            if args.compare:
                later_artworks = load_artworks(args.compare)
            last_modified = get_last_modified(args.source)
        except Exception as error:
            print("Cannot read input:", error, file=sys.stderr)
            return 2

        if args.limit:
            artworks = artworks[:args.limit]
        name = args.source.split("/")[-1]
        entry = check_data_file(artworks, state["files"].get(name), findings, counts, args.min_side)
        entry["last_modified"] = last_modified
        if later_artworks is not None:
            entry["images_lost"] = compare_stages(artworks, later_artworks, findings)
            checks_run.append("stages")
        state["files"][name] = entry
        files_checked += 1
        for artwork in artworks:
            sample_candidates.append({"id": artwork.get("id"), "link": artwork.get("link"),
                                      "image_url": artwork.get("image_url")})
        checks_run.append("data")

    # A whole folder of artist files
    if args.data_prefix:
        try:
            checked, candidates = run_data_check(args.data_prefix, args.compare_prefix, state, findings, counts,
                                                 args.min_side, args.max_files)
        except Exception as error:
            print("Cannot read the data folder:", error, file=sys.stderr)
            return 2
        files_checked += checked
        sample_candidates.extend(candidates)
        checks_run.append("data")
        if args.compare_prefix:
            checks_run.append("stages")

    if "data" in checks_run:
        signals.extend(data_signals(state, files_checked > 0))

    # Online checks on a sample of the artworks just read
    if args.check_pages:
        pool = []
        for candidate in sample_candidates:
            if get_link_format(candidate["link"]) in LIVE_PAGE_LINK_FORMATS:
                pool.append(candidate)
        if args.sample and len(pool) > args.sample:
            pool = random.sample(pool, args.sample)
        if pool:
            print(f"Downloading {len(pool)} lot pages...")
            problems = []
            for artwork in pool:
                failure_type, detail = check_page_online(artwork)
                if failure_type:
                    problems.append((failure_type, detail, artwork["link"]))
                    add_finding(findings, "page_problem", ERROR, artwork, detail, failure_type)
                time.sleep(args.delay)
            signals.extend(signals_from_sample("pages", problems, len(pool), "lot pages"))
            checks_run.append("pages")

    if args.live:
        pool = []
        for candidate in sample_candidates:
            if get_image_format(candidate["image_url"]) not in LIVE_IMAGE_SKIPPED_FORMATS:
                pool.append(candidate)
        if args.sample and len(pool) > args.sample:
            pool = random.sample(pool, args.sample)
        if pool:
            print(f"Downloading {len(pool)} images...")
            problems = []
            for artwork in pool:
                failure_type, detail = check_image_online(artwork)
                if failure_type:
                    problems.append((failure_type, detail, artwork["link"]))
                    add_finding(findings, "image_problem", ERROR, artwork, detail, failure_type)
                time.sleep(args.delay)
            signals.extend(signals_from_sample("images", problems, len(pool), "images"))
            checks_run.append("images")

    if args.canary:
        print("Checking canary lots...")
        canary_results, canary_signals = run_canaries(args.delay)
        signals.extend(canary_signals)
        checks_run.append("canary")

    if args.check_search:
        print("Checking the search page...")
        failure_type, detail = check_search_page()
        search_result = {"failure_type": failure_type, "detail": detail}
        if failure_type == "skipped":
            print("  skipped:", detail)
        else:
            checks_run.append("search")
            if failure_type:
                signals.append(make_signal("search", failure_type, failure_type, severity_for(failure_type),
                                           "search page: " + detail, [SEARCH_CANARY_URL]))

    notifications = update_alerts(state, signals, checks_run, now, confirm_runs, args.cooldown_hours)
    state["last_run"] = now

    errors_for_report = []
    for finding in findings:
        if finding["severity"] == ERROR and len(errors_for_report) < 5000:
            errors_for_report.append(finding)

    report = {
        "generated_at": now,
        "checks_run": checks_run,
        "canary": canary_results,
        "search": search_result,
        "data": {"files_checked": files_checked, "link_formats": counts["links"], "image_formats": counts["images"]},
        "signals": signals,
        "notifications": notifications,
        "errors": errors_for_report,
    }
    print_summary(report, findings, counts)

    # Teams first: if sending fails, the state is corrected (alerts kept as "not notified") before it is saved
    if args.teams_webhook:
        report["teams_sent"] = send_notifications_to_teams(args.teams_webhook, notifications, state)

    if args.state:
        write_json(args.state, state)
        print("State saved to", args.state)
    if args.report:
        write_json(args.report, report)
        print("Report written to", args.report)

    for notification in notifications:
        if notification["kind"] in ("NEW", "REMINDER"):
            return 1
    return 0


### 11 Microsoft Teams

TEAMS_TIMEOUT_SECONDS = 15
TEAMS_TRIES = 3
TEAMS_MAX_EVIDENCE = 5          # links shown per alert (the full list is in the --report file)

# Title and color of each kind of notification (colors are Adaptive Card names)
TEAMS_STYLES = {
    ("NEW", CRITICAL): ("New critical alert", "Attention"),
    ("NEW", WARNING): ("New warning", "Warning"),
    ("REMINDER", CRITICAL): ("Still broken (critical)", "Attention"),
    ("REMINDER", WARNING): ("Still there (warning)", "Warning"),
    ("RESOLVED", CRITICAL): ("Resolved", "Good"),
    ("RESOLVED", WARNING): ("Resolved", "Good"),
}


def teams_alert_block(notification):
    """One alert, as a block of the Teams card: title, message, facts and a few example links."""
    title, color = TEAMS_STYLES.get((notification["kind"], notification["severity"]),
                                    (notification["kind"], "Default"))
    facts = [
        {"title": "Check", "value": notification["key"]},
        {"title": "Failure type", "value": notification["failure_type"]},
        {"title": "First seen", "value": notification["first_seen"][:16].replace("T", " ") + " UTC"},
        {"title": "Runs in a row", "value": str(notification["runs_in_a_row"])},
    ]
    block = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "color": color, "wrap": True},
        {"type": "TextBlock", "text": notification["message"], "wrap": True, "spacing": "Small"},
        {"type": "FactSet", "facts": facts},
    ]
    evidence = [link for link in notification.get("evidence", []) if isinstance(link, str)][:TEAMS_MAX_EVIDENCE]
    if evidence:
        lines = "\n".join("- [" + link.split("/")[-1][:60] + "](" + link + ")" if link.startswith("http") else "- " + link
                          for link in evidence)
        block.append({"type": "TextBlock", "text": lines, "wrap": True, "size": "Small", "isSubtle": True})
    return {"type": "Container", "separator": True, "spacing": "Medium", "items": block}


def teams_message(notifications, title):
    """All the notifications of the run in ONE message (one card), so a bad run doesn't flood the channel."""
    counts = {}
    for notification in notifications:
        counts[notification["kind"]] = counts.get(notification["kind"], 0) + 1
    summary = ", ".join(f"{n} {kind.lower()}" for kind, n in sorted(counts.items()))
    body = [
        {"type": "TextBlock", "text": title, "size": "Large", "weight": "Bolder", "wrap": True},
        {"type": "TextBlock", "text": summary + " - " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
         "isSubtle": True, "spacing": "None", "wrap": True},
    ]
    body += [teams_alert_block(notification) for notification in notifications]
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "msteams": {"width": "Full"}, "body": body}
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def post_to_teams(webhook_url, payload):
    """Sends the card. Retries on a network error, a 429 or a 5xx. Returns (True, '') or (False, why)."""
    why = ""
    for attempt in range(1, TEAMS_TRIES + 1):
        try:
            response = requests.post(webhook_url, json=payload, timeout=TEAMS_TIMEOUT_SECONDS)
        except requests.RequestException as error:
            why = f"network error: {error}"
        else:
            # Workflows answers 202 Accepted, the old Incoming Webhook answers 200
            if 200 <= response.status_code < 300:
                return True, ""
            why = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code != 429 and response.status_code < 500:
                return False, why                  # 400, 401, 403, 404: retrying won't help
        if attempt < TEAMS_TRIES:
            time.sleep(2 ** attempt)
    return False, why


def send_notifications_to_teams(webhook_url, notifications, state):
    """
    Posts this run's notifications. If it fails, the NEW and REMINDER alerts are marked as not notified
    in the state, so that the next run sends them again instead of losing them.
    """
    if not notifications:
        return True
    ok, why = post_to_teams(webhook_url, teams_message(notifications, SITE_NAME + " scraper healthcheck"))
    if ok:
        print(f"Teams: {len(notifications)} notification(s) sent")
        return True
    print("Teams: could not send the notifications:", why, file=sys.stderr)
    for notification in notifications:
        entry = state["signals"].get(notification["key"])
        if entry is not None and notification["kind"] in ("NEW", "REMINDER"):
            entry["last_notified"] = None          # sent again on the next run
    return False


def send_teams_test(webhook_url):
    """A sample card, to check that the webhook works and see what an alert looks like."""
    now = datetime.now(timezone.utc).isoformat()
    sample = {"kind": "NEW", "source": SOURCE, "key": "canary:structure_change",
              "failure_type": STRUCTURE_CHANGE, "severity": CRITICAL,
              "message": TEST_MESSAGE,
              "evidence": CANARY_LOTS[:2], "first_seen": now, "runs_in_a_row": 1}
    ok, why = post_to_teams(webhook_url, teams_message([sample], SITE_NAME + " scraper healthcheck (test)"))
    print("Teams: test card sent" if ok else f"Teams: test failed: {why}")
    return 0 if ok else 2


# Default for the optional extra rule on image URLs: a site can define its own (see sites/phillips.py)
extra_image_check = None


def use_site(site_module):
    """Installs a site (a module of sites/): its settings and functions replace the engine's defaults."""
    for name, value in vars(site_module).items():
        if name.startswith("_") or type(value).__name__ == "module":
            continue
        if globals().get(name) is not value:
            globals()[name] = value


if __name__ == "__main__":
    sys.exit("Run healthcheck.py instead: python healthcheck.py <site> [options]")