"""
Email Aggregator Bot v1.4 (Stage C revised)
- IMAPSource (mail) + WebSource (HTML) + JSONAPISource (JSON endpoints)
- Adds BJTU Job site sources: internships and recruitment fairs
- Per-source prompt registry for differentiated LLM evaluation

Run modes:
    python email_bot.py                  # normal run
    python email_bot.py --setup          # create config template if missing
    python email_bot.py --install-task   # register Task Scheduler entry (every 10 min)
    python email_bot.py --uninstall-task # remove the scheduled task
    python email_bot.py --dry-run        # fetch + summarize but don't push
    python email_bot.py --list-folders   # list IMAP folders for each account (debug)
    python email_bot.py --reset-state    # wipe state.json (forces re-baseline)
"""
import os
import sys
import re
import json
import time
import hashlib
import imaplib
import email
import socket
import random
import requests
import argparse
import subprocess
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

DATA_DIR    = Path(os.environ["USERPROFILE"]) / "EmailBot"
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE  = DATA_DIR / "state.json"
LOG_FILE    = DATA_DIR / "bot.log"
TASK_NAME   = "EmailBot-Poll"

DATA_DIR.mkdir(exist_ok=True)

STATE_VERSION = 2

CONFIG_TEMPLATE = {
    "accounts": [
        {
            "name":         "QQ",
            "host":         "imap.qq.com",
            "port":         993,
            "user":         "your_qq@qq.com",
            "password":     "16_digit_authcode",
            "webmail_url":  "https://mail.qq.com/",
            "folders":      ["INBOX"],
            "initial_mode": "recent24h"
        },
        {
            "name":         "Gmail",
            "host":         "imap.gmail.com",
            "port":         993,
            "user":         "your@gmail.com",
            "password":     "16_digit_app_password",
            "webmail_url":  "https://mail.google.com/",
            "folders":      ["INBOX"],
            "initial_mode": "recent24h"
        },
        {
            "name":         "BJTU",
            "host":         "mail.bjtu.edu.cn",
            "port":         993,
            "user":         "youraccount@bjtu.edu.cn",
            "password":     "your_bjtu_password",
            "webmail_url":  "https://mail.bjtu.edu.cn/",
            "folders":      ["INBOX"],
            "initial_mode": "unseen"
        }
    ],
    "websites": [
        {
            "name":            "计算机学院通知",
            "list_url":        "http://example.bjtu.edu.cn/notices/",
            "list_selector":   "ul.list > li",
            "title_selector":  "a",
            "url_attr":        "href",
            "date_selector":   ".date",
            "date_regex":      "(20\\d{2}[-./]\\d{1,2}[-./]\\d{1,2})",
            "detail_selector": ".content",
            "fetch_detail":    True,
            "max_per_run":     5,
            "route":           "mail"
        }
    ],
    "jsonapi_sources": [
        {
            "name":         "BJTU实习",
            "type":         "bjtu_internship",
            "max_per_run":  8,
            "first_run_preview": 3,
            "skip_cities":  [],
            "skip_natures": [],
            "route":        "mail"
        },
        {
            "name":         "BJTU宣讲会",
            "type":         "bjtu_fair",
            "max_per_run":  8,
            "first_run_preview": 3,
            "max_forward_days": 30,
            "route":        "mail"
        }
    ],
    "llm": {
        "dashscope_api_key": "sk-xxxxxxxx",
        "model": "qwen-turbo"
    },
    "feishu": {
        "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx"
    },
    "filters": {
        "skip_low_importance": True,
        "skip_senders": ["noreply@example.com"],
        "max_per_run": 20
    }
}

def log(msg, level="INFO"):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}][{level}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── Config ──────────────────────────────────────────────────────
def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(CONFIG_TEMPLATE, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        log(f"Config template created at {CONFIG_FILE}", "INFO")
        log("Please fill in real credentials and re-run", "WARN")
        sys.exit(0)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for acc in cfg.get("accounts", []):
        acc.setdefault("folders", ["INBOX"])
        acc.setdefault("initial_mode", "recent24h")
    return cfg

# ── State ───────────────────────────────────────────────────────
def _state_key(source_type, source_name, folder=None):
    if folder:
        return f"{source_type}:{source_name}:{folder}"
    return f"{source_type}:{source_name}"

def load_state():
    if not STATE_FILE.exists():
        return {"version": STATE_VERSION, "sources": {}}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"state.json unreadable, starting fresh: {e}", "WARN")
        return {"version": STATE_VERSION, "sources": {}}

    if "version" not in raw and "uids" in raw:
        log("Migrating state.json from v1 to v2", "INFO")
        migrated = {"version": STATE_VERSION, "sources": {}}
        for acc_name, last_uid in raw.get("uids", {}).items():
            key = _state_key("imap", acc_name, "INBOX")
            migrated["sources"][key] = {"last_uid": int(last_uid)}
        save_state(migrated)
        return migrated

    raw.setdefault("version", STATE_VERSION)
    raw.setdefault("sources", {})
    return raw

def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

def reset_state():
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        log("state.json removed", "INFO")
    else:
        log("state.json does not exist, nothing to do", "INFO")

# ── MIME decoder helpers ────────────────────────────────────────
def decode_mime_header(raw):
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                decoded.append(text.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                decoded.append(text.decode("utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded).strip()

def extract_text_body(msg):
    """Extract plain text body, preferring text/plain over text/html."""
    text_plain = ""
    text_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp  = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")
                if ctype == "text/plain" and not text_plain:
                    text_plain = content
                elif ctype == "text/html" and not text_html:
                    text_html = content
            except Exception:
                continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                text_plain = payload.decode(charset, errors="replace")
        except Exception:
            pass

    if text_plain:
        return text_plain
    if text_html:
        import re
        clean = re.sub(r"<[^>]+>", " ", text_html)
        clean = re.sub(r"\s+", " ", clean)
        return clean.strip()
    return ""

# ════════════════════════════════════════════════════════════════
# Source abstraction
# ════════════════════════════════════════════════════════════════
class Source:
    """
    Base class for all information sources.

    Contract:
      - __init__ does only validation, no I/O.
      - fetch(state_slice) is a pure function: it must not write to disk,
        push to Feishu, or mutate any global state.
      - fetch returns (items, state_updates) where:
          items         : list[dict] in the unified Item shape
          state_updates : dict[state_key -> state_dict] to merge into state["sources"]

      Failure handling:
        - On non-recoverable error, return ([], {}) so the main loop
          leaves existing state untouched.
        - On partial success, return whatever was successfully fetched
          plus state_updates only for the successful slices.
    """
    source_type = "base"  # subclasses override

    def __init__(self, config):
        self.config = config
        self.name = config["name"]

    def fetch(self, state_slice):
        """
        state_slice : dict — only the entries relevant to this source.
                            keys are state_key strings, values are state dicts.
        Returns (items, state_updates).
        """
        raise NotImplementedError

    def state_keys(self):
        """
        Return the list of state_keys this source will read/write.
        Used by main() to slice the global state before calling fetch().
        """
        raise NotImplementedError


def _build_search_criteria(initial_mode, last_uid):
    """
    Returns (criteria_str, is_uid_search)
    - criteria_str=None means "uid_only baseline mode": don't fetch bodies, just record max UID.
    """
    if last_uid > 0:
        return f"UID {last_uid + 1}:*", True
    if initial_mode == "uid_only":
        return None, True
    if initial_mode == "unseen":
        return "UNSEEN", True
    since = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
    return f'(SINCE "{since}")', True


class IMAPSource(Source):
    """
    One IMAP account, possibly spanning multiple folders.
    Each folder maintains its own last_uid in state.
    """
    source_type = "imap"

    def __init__(self, account_config):
        super().__init__(account_config)
        self.host         = account_config["host"]
        self.port         = account_config.get("port", 993)
        self.user         = account_config["user"]
        self.password     = account_config["password"]
        self.folders      = account_config.get("folders", ["INBOX"])
        self.initial_mode = account_config.get("initial_mode", "recent24h")
        self.webmail_url  = account_config.get("webmail_url", "")
        self.route        = account_config.get("route", "mail")
        self.max_per_run  = account_config.get("max_per_run", 20)

    def state_keys(self):
        return [_state_key(self.source_type, self.name, f) for f in self.folders]

    def fetch(self, state_slice):
        time.sleep(random.uniform(0, 3))
        items = []
        state_updates = {}

        m = self._connect()
        if m is None:
            return [], {}

        try:
            for folder in self.folders:
                key = _state_key(self.source_type, self.name, folder)
                last_uid = state_slice.get(key, {}).get("last_uid", 0)
                folder_items, new_uid = self._fetch_folder(m, folder, last_uid)
                items.extend(folder_items)
                state_updates[key] = {"last_uid": new_uid}
        finally:
            try: m.logout()
            except: pass

        return items, state_updates

    def list_folders(self):
        """Debug helper. Connects, calls LIST, prints raw response."""
        log(f"  [{self.name}] listing folders on {self.host}...")
        m = self._connect()
        if m is None:
            return
        try:
            status, data = m.list()
            if status != "OK":
                log(f"  [{self.name}] LIST failed", "ERR")
                return
            for raw in data:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                print(f"    {raw}")
        finally:
            try: m.logout()
            except: pass

    # ── Internals ────────────────────────────────────────────────
    def _connect(self):
        log(f"  [{self.name}] connecting to {self.host}...")
        socket.setdefaulttimeout(30)
        try:
            m = imaplib.IMAP4_SSL(self.host, self.port)
            m.login(self.user, self.password)
            return m
        except Exception as e:
            log(f"  [{self.name}] connection/login failed: {e}", "ERR")
            return None

    def _fetch_folder(self, m, folder, last_uid):
        try:
            status, _ = m.select(folder, readonly=True)
            if status != "OK":
                log(f"  [{self.name}/{folder}] folder not found", "WARN")
                return [], last_uid
        except Exception as e:
            log(f"  [{self.name}/{folder}] select failed: {e}", "ERR")
            return [], last_uid

        try:
            criteria, _ = _build_search_criteria(self.initial_mode, last_uid)

            # uid_only baseline: just record current max UID
            if criteria is None:
                status, data = m.uid("SEARCH", None, "ALL")
                if status == "OK" and data and data[0]:
                    all_uids = [int(u) for u in data[0].split()]
                    new_max = max(all_uids) if all_uids else last_uid
                    log(f"  [{self.name}/{folder}] uid_only baseline: max_uid={new_max}")
                    return [], new_max
                return [], last_uid

            status, data = m.uid("SEARCH", None, criteria)
            if status != "OK" or not data or not data[0]:
                log(f"  [{self.name}/{folder}] no new mail")
                return [], last_uid

            uids = [int(u) for u in data[0].split()]
            if last_uid > 0:
                uids = [u for u in uids if u > last_uid]
            if not uids:
                log(f"  [{self.name}/{folder}] no new mail")
                return [], last_uid

            uids = sorted(uids)[-self.max_per_run:]
            log(f"  [{self.name}/{folder}] {len(uids)} new mail(s)")

            results = []
            new_max_uid = last_uid
            for uid in uids:
                item = self._fetch_one(m, folder, uid)
                if item is not None:
                    results.append(item)
                    new_max_uid = max(new_max_uid, uid)
            return results, new_max_uid

        except Exception as e:
            log(f"  [{self.name}/{folder}] fetch error: {e}", "ERR")
            return [], last_uid

    def _fetch_one(self, m, folder, uid):
        try:
            status, msg_data = m.uid("FETCH", str(uid).encode(), "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return None
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = decode_mime_header(msg.get("Subject", ""))
            from_raw = decode_mime_header(msg.get("From", ""))
            from_name, from_addr = parseaddr(from_raw)
            if not from_name:
                from_name = from_addr.split("@")[0] if from_addr else "unknown"
            date_raw = msg.get("Date", "")
            try:
                dt = parsedate_to_datetime(date_raw)
                date_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_str = ""
            body = extract_text_body(msg)[:1500]

            return {
                "source_type": self.source_type,
                "source_name": self.name,
                "folder":      folder,
                "item_id":     f"uid:{uid}",
                "title":       subject or "(no subject)",
                "from_name":   from_name,
                "from_addr":   from_addr,
                "url":         self.webmail_url,
                "date":        date_str,
                "body":        body,
                "route":       self.route,
            }
        except Exception as e:
            log(f"  [{self.name}/{folder}] failed to parse uid {uid}: {e}", "WARN")
            return None


def build_sources(cfg):
    """
    Factory: instantiate all Source objects from config.
    """
    sources = []
    for acc in cfg.get("accounts", []):
        sources.append(IMAPSource(acc))
    for site in cfg.get("websites", []):
        if not HAS_BS4:
            log("WebSource configured but beautifulsoup4 not installed; skipping web sources", "WARN")
            log("Install with: pip install beautifulsoup4", "INFO")
            break
        sources.append(WebSource(site))
    for api_cfg in cfg.get("jsonapi_sources", []):
        api_type = api_cfg.get("type", "")
        cls = JSONAPI_SOURCE_REGISTRY.get(api_type)
        if cls is None:
            log(f"Unknown jsonapi source type '{api_type}' — skipping", "WARN")
            continue
        sources.append(cls(api_cfg))
    return sources


# ════════════════════════════════════════════════════════════════
# WebSource: HTML scraping for school notice pages
# ════════════════════════════════════════════════════════════════
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SEEN_IDS_CAP = 200   # max number of historical IDs kept per source


def make_item_id(url, title, date_str):
    """
    Compute a stable ID for a notice.

    Strategy (most stable first):
      1. Numeric ID in URL path (e.g. /info/12345.htm -> "url:12345")
      2. Hash of (date + title) as fallback
    """
    if url:
        m = re.search(r"/(\d{3,})(?:\.html?|/)?$", url)
        if m:
            return f"url:{m.group(1)}"
    h = hashlib.md5(f"{date_str}|{title}".encode("utf-8")).hexdigest()[:10]
    return f"hash:{h}"


class WebSource(Source):
    """
    Scrapes a webpage with a list of notices.

    Required config fields:
      name           : display name (e.g. "计算机学院通知")
      list_url       : URL of the listing page
      list_selector  : CSS selector matching each list item (e.g. "ul.list > li")
      title_selector : CSS selector inside list item, finds the <a> for title+url
                       (or just the title element if separate from link)

    Optional config fields:
      url_attr       : attribute on title element holding the URL (default "href")
      date_selector  : CSS selector inside list item finding the date text
      date_regex     : regex extracting date from selector text (default catches YYYY-M-D / YYYY/M/D / YYYY.M.D)
      detail_selector: CSS selector on detail page locating main content (e.g. ".article-content")
      fetch_detail   : whether to download each detail page (default True)
      max_per_run    : max new items per run (default 10)
      route          : "mail" or "notice" (default "notice")
      max_body_chars : truncate body to this many chars (default 1500)
    """
    source_type = "web"

    def __init__(self, site_config):
        super().__init__(site_config)
        self.list_url        = site_config["list_url"]
        self.list_selector   = site_config["list_selector"]
        self.title_selector  = site_config.get("title_selector", "a")
        self.url_attr        = site_config.get("url_attr", "href")
        self.date_selector   = site_config.get("date_selector")
        self.date_regex      = site_config.get("date_regex", r"(20\d{2}[-./]\d{1,2}[-./]\d{1,2})")
        self.detail_selector = site_config.get("detail_selector")
        self.fetch_detail    = site_config.get("fetch_detail", True)
        self.max_per_run     = site_config.get("max_per_run", 10)
        self.route           = site_config.get("route", "notice")
        self.max_body_chars  = site_config.get("max_body_chars", 1500)
        self.timeout         = site_config.get("timeout", 15)

    def state_keys(self):
        return [_state_key(self.source_type, self.name)]

    def fetch(self, state_slice):
        key = _state_key(self.source_type, self.name)
        existing_state = state_slice.get(key, {})
        seen_ids   = list(existing_state.get("seen_ids", []))
        first_run  = (len(seen_ids) == 0 and not existing_state)

        # 1. Fetch the list page
        try:
            entries = self._fetch_list()
        except Exception as e:
            log(f"  [web:{self.name}] list page fetch failed: {e}", "ERR")
            return [], {}

        if not entries:
            log(f"  [web:{self.name}] list page returned no items "
                f"(check selectors)", "WARN")
            return [], {}

        # 2. Determine which entries are new
        seen_set = set(seen_ids)
        new_entries = [e for e in entries if e["item_id"] not in seen_set]

        if first_run:
            # Baseline mode: don't push anything, just record current IDs
            baseline_ids = [e["item_id"] for e in entries][-SEEN_IDS_CAP:]
            log(f"  [web:{self.name}] first run baseline: "
                f"{len(baseline_ids)} ids recorded, no items pushed")
            return [], {key: {"seen_ids": baseline_ids}}

        if not new_entries:
            log(f"  [web:{self.name}] no new notices")
            # Still update seen_ids in case the list changed (FIFO refresh)
            return [], {}

        # 3. Cap and fetch detail pages
        new_entries = new_entries[:self.max_per_run]
        log(f"  [web:{self.name}] {len(new_entries)} new notice(s)")

        items = []
        for entry in new_entries:
            try:
                if self.fetch_detail and entry.get("url"):
                    body = self._fetch_detail(entry["url"])
                else:
                    body = entry.get("preview", "")
                items.append({
                    "source_type": self.source_type,
                    "source_name": self.name,
                    "folder":      "",
                    "item_id":     entry["item_id"],
                    "title":       entry["title"],
                    "from_name":   "",   # web sources don't have senders
                    "from_addr":   "",
                    "url":         entry.get("url", self.list_url),
                    "date":        entry.get("date", ""),
                    "body":        body[:self.max_body_chars],
                    "route":       self.route,
                })
                # be polite between detail fetches
                if self.fetch_detail:
                    time.sleep(random.uniform(1.0, 2.5))
            except Exception as e:
                log(f"  [web:{self.name}] failed on '{entry['title'][:30]}': {e}", "WARN")
                continue

        # 4. Update seen_ids: append new ones, FIFO-cap
        new_ids = [e["item_id"] for e in new_entries]
        merged = seen_ids + new_ids
        # Deduplicate while preserving order
        deduped = []
        seen_dedup = set()
        for x in merged:
            if x not in seen_dedup:
                seen_dedup.add(x)
                deduped.append(x)
        capped = deduped[-SEEN_IDS_CAP:]

        return items, {key: {"seen_ids": capped}}

    # ── Internals ────────────────────────────────────────────────
    def _fetch_list(self):
        """Fetch and parse the list page. Returns list of {item_id, title, url, date}."""
        log(f"  [web:{self.name}] fetching list: {self.list_url}")
        resp = requests.get(self.list_url, headers=WEB_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        # Auto-detect encoding (handles GBK)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        rows = soup.select(self.list_selector)
        entries = []
        for row in rows:
            title_el = row.select_one(self.title_selector)
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue
            href = title_el.get(self.url_attr, "")
            url = urljoin(self.list_url, href) if href else ""

            date_str = ""
            if self.date_selector:
                date_el = row.select_one(self.date_selector)
                if date_el:
                    text = date_el.get_text(" ", strip=True)
                    m = re.search(self.date_regex, text)
                    if m:
                        date_str = m.group(1)
            if not date_str:
                # Fallback: search the whole row text for a date
                row_text = row.get_text(" ", strip=True)
                m = re.search(self.date_regex, row_text)
                if m:
                    date_str = m.group(1)

            item_id = make_item_id(url, title, date_str)
            entries.append({
                "item_id": item_id,
                "title":   title,
                "url":     url,
                "date":    date_str,
                "preview": row.get_text(" ", strip=True)[:500],
            })
        return entries

    def _fetch_detail(self, url):
        """Fetch and extract body text from a detail page."""
        resp = requests.get(url, headers=WEB_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        if self.detail_selector:
            el = soup.select_one(self.detail_selector)
            if el:
                return el.get_text("\n", strip=True)

        # Fallback heuristic: pick the largest text block
        candidates = []
        for tag in soup.find_all(["article", "div", "section", "td"]):
            text = tag.get_text(" ", strip=True)
            if 100 <= len(text) <= 8000:
                inner_blocks = tag.find_all(["article", "div", "section"])
                if len(inner_blocks) <= 5:
                    candidates.append((tag, len(text), text))
        if candidates:
            candidates.sort(key=lambda x: -x[1])
            return candidates[0][2]
        return soup.get_text("\n", strip=True)[:2000]


# ════════════════════════════════════════════════════════════════
# JSONAPISource: structured JSON endpoints (vs scraping HTML)
# ════════════════════════════════════════════════════════════════
class JSONAPISource(Source):
    """
    Base class for sources that hit JSON APIs directly.

    Subclasses implement:
      - _fetch_records()          -> list[dict] of raw API records
      - _record_to_item(record)   -> Item dict, or None to skip
      - _record_id(record)        -> stable ID for dedup

    The base class handles:
      - state management (seen_ids with FIFO cap)
      - first-run baseline (first_run_preview controls how many to push)
      - max_per_run cap
    """
    source_type = "jsonapi"   # subclasses can override

    def __init__(self, config):
        super().__init__(config)
        self.max_per_run        = config.get("max_per_run", 8)
        self.first_run_preview  = config.get("first_run_preview", 0)
        self.route              = config.get("route", "mail")
        self.timeout            = config.get("timeout", 15)

    def state_keys(self):
        return [_state_key(self.source_type, self.name)]

    def fetch(self, state_slice):
        key = _state_key(self.source_type, self.name)
        existing_state = state_slice.get(key, {})
        seen_ids       = list(existing_state.get("seen_ids", []))
        first_run      = (not existing_state)

        # 1. Fetch raw records
        try:
            records = self._fetch_records()
        except Exception as e:
            log(f"  [api:{self.name}] fetch failed: {e}", "ERR")
            return [], {}

        if not records:
            log(f"  [api:{self.name}] no records returned")
            return [], {}

        # 2. Determine which records are new (or take preview slice on first run)
        seen_set = set(seen_ids)
        new_records = [r for r in records if self._record_id(r) not in seen_set]

        if first_run:
            # First run: optionally show a few previews so user can verify the format
            preview = new_records[:self.first_run_preview]
            # All records (preview + skipped) go into baseline
            baseline_ids = [self._record_id(r) for r in records][:SEEN_IDS_CAP]
            log(f"  [api:{self.name}] first run: "
                f"{len(records)} records seen, pushing {len(preview)} preview")
            items = self._records_to_items(preview)
            return items, {key: {"seen_ids": baseline_ids}}

        if not new_records:
            log(f"  [api:{self.name}] no new records")
            return [], {}

        # 3. Cap and convert
        new_records = new_records[:self.max_per_run]
        log(f"  [api:{self.name}] {len(new_records)} new record(s)")
        items = self._records_to_items(new_records)

        # 4. Update seen_ids (FIFO cap)
        new_ids = [self._record_id(r) for r in new_records]
        merged = seen_ids + new_ids
        deduped = []
        seen_dedup = set()
        for x in merged:
            if x not in seen_dedup:
                seen_dedup.add(x)
                deduped.append(x)
        capped = deduped[-SEEN_IDS_CAP:]

        return items, {key: {"seen_ids": capped}}

    def _records_to_items(self, records):
        items = []
        for r in records:
            try:
                item = self._record_to_item(r)
                if item is not None:
                    items.append(item)
            except Exception as e:
                log(f"  [api:{self.name}] failed to convert record: {e}", "WARN")
        return items

    # Subclasses must implement these
    def _fetch_records(self):
        raise NotImplementedError

    def _record_to_item(self, record):
        raise NotImplementedError

    def _record_id(self, record):
        return record.get("id") or ""


# ── BJTU Job site sources ───────────────────────────────────────
BJTU_JOB_BASE = "https://job.bjtu.edu.cn"
BJTU_JOB_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": BJTU_JOB_BASE,
    "Referer": f"{BJTU_JOB_BASE}/frontpage/bjtu/html/index.html",
    "X-Requested-With": "XMLHttpRequest",
}

CITY_BLOCKLIST_DEFAULT = []   # user can extend in config


class BJTUInternshipSource(JSONAPISource):
    """
    Source for BJTU job site internship listings.
    Endpoint: ajax_findRecruitmentinfoLimitList with positionType=2
    """
    source_type = "bjtu_internship"

    def __init__(self, config):
        super().__init__(config)
        self.skip_cities  = set(config.get("skip_cities", []))
        self.skip_natures = set(config.get("skip_natures", []))
        self.fetch_count  = config.get("fetch_count", 30)

    def _fetch_records(self):
        ts = int(time.time() * 1000)
        url = f"{BJTU_JOB_BASE}/f/ajaxHome/ajax_findRecruitmentinfoLimitList?ts={ts}"
        resp = requests.post(
            url,
            headers={**BJTU_JOB_HEADERS,
                     "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            data={"num": str(self.fetch_count), "positionType": "2"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("state") != 1:
            log(f"  [api:{self.name}] API returned state={data.get('state')}: "
                f"{data.get('msg')}", "WARN")
            return []
        return data.get("object", []) or []

    def _record_to_item(self, record):
        corp = record.get("corporationinfo", {}) or {}
        company  = corp.get("name", "(未知公司)")
        nature   = corp.get("corporationNatureValue", "")
        scale    = corp.get("corporationScaleValue", "")
        city     = record.get("cityName", "")
        edu      = record.get("education", "")
        end_time = record.get("endTime", "")
        start    = record.get("startTime", "")
        position_num = record.get("positionNum", 0)
        rel_url  = record.get("url", "")
        full_url = BJTU_JOB_BASE + rel_url if rel_url.startswith("/") else rel_url

        # ── Hard filters ──
        if any(skip in city for skip in self.skip_cities):
            return None
        if nature in self.skip_natures:
            return None

        # ── Build display title ──
        title_bits = [company]
        if city:
            title_bits.append(city)
        if edu:
            title_bits.append(edu)
        title = " · ".join(title_bits)

        # ── Build body for LLM analysis ──
        body_lines = [
            f"公司: {company}",
            f"性质: {nature} / 规模: {scale}",
            f"城市: {city}",
            f"学历要求: {edu}",
            f"岗位数: {position_num}",
            f"投递期: {start[:10]} ~ {end_time[:10]}",
        ]
        major = record.get("majorName", "")
        if major:
            body_lines.append(f"专业: {major[:200]}")
        body = "\n".join(body_lines)

        # Date for display (use endTime — the deadline matters most)
        display_date = f"截止 {end_time[:10]}" if end_time else start[:10]

        return {
            "source_type":  self.source_type,
            "source_name":  self.name,
            "folder":       "",
            "item_id":      f"intern:{record.get('id', '')}",
            "title":        title,
            "from_name":    "",
            "from_addr":    "",
            "url":          full_url,
            "date":         display_date,
            "body":         body,
            "route":        self.route,
            # Extra metadata for prompt template
            "_meta": {
                "kind":     "internship",
                "company":  company,
                "nature":   nature,
                "scale":    scale,
                "city":     city,
                "education": edu,
            },
        }


class BJTUFairSource(JSONAPISource):
    """
    Source for BJTU job site recruitment fairs (宣讲会/双选会).
    Endpoint: ajax_findRecruitmentFairLimitList (GET)
    """
    source_type = "bjtu_fair"

    def __init__(self, config):
        super().__init__(config)
        self.max_forward_days = config.get("max_forward_days", 30)
        self.fetch_count      = config.get("fetch_count", 50)

    def _fetch_records(self):
        ts = int(time.time() * 1000)
        url = (f"{BJTU_JOB_BASE}/f/ajaxHome/ajax_findRecruitmentFairLimitList"
               f"?ts={ts}&num={self.fetch_count}&positionType=1")
        resp = requests.get(url, headers=BJTU_JOB_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("state") != 1:
            log(f"  [api:{self.name}] API returned state={data.get('state')}: "
                f"{data.get('msg')}", "WARN")
            return []
        return data.get("object", []) or []

    def _record_to_item(self, record):
        # ── Hard filters: drop expired and far-future ──
        forward_day = record.get("forwardDay", 0)
        if isinstance(forward_day, str):
            try:
                forward_day = int(forward_day)
            except ValueError:
                forward_day = 0
        if forward_day < 0:
            return None  # already passed
        if forward_day > self.max_forward_days:
            return None  # too far in the future, will surface again later
        if record.get("isExpired") == "1" and forward_day < 0:
            return None

        corp     = record.get("corporationinfo", {}) or {}
        company  = corp.get("name", "(未知公司)")
        title    = record.get("title", company)
        start    = record.get("startTimeExport") or record.get("startTime", "")
        # raw_place is the actual link/address; fieldExport is the display label
        raw_place    = record.get("place", "") or ""
        display_place = record.get("fieldExport") or raw_place
        rel_url  = record.get("url", "")
        full_url = BJTU_JOB_BASE + rel_url if rel_url.startswith("/") else rel_url

        # Detect online vs on-campus: either raw place is an http link
        # or the display label explicitly says "线上" / "online"
        is_online = (
            raw_place.startswith("http")
            or "线上" in display_place
            or "online" in display_place.lower()
        )

        # Display title
        when_str = ""
        if forward_day == 0:
            when_str = "今天"
        elif forward_day == 1:
            when_str = "明天"
        elif forward_day <= 7:
            when_str = f"{forward_day}天后"
        else:
            when_str = f"{forward_day}天后"

        location_label = "线上" if is_online else (display_place[:20] if display_place else "")
        bits = [title]
        if when_str:
            bits.append(when_str)
        if location_label:
            bits.append(location_label)
        display_title = " · ".join(bits)

        # Body for LLM
        body_lines = [
            f"标题: {title}",
            f"公司: {company}",
            f"开始时间: {start}",
            f"距今: {forward_day} 天",
            f"地点: {display_place}",
            f"形式: {'线上' if is_online else '校内现场'}",
        ]
        body = "\n".join(body_lines)

        return {
            "source_type":  self.source_type,
            "source_name":  self.name,
            "folder":       "",
            "item_id":      f"fair:{record.get('id', '')}",
            "title":        display_title,
            "from_name":    "",
            "from_addr":    "",
            "url":          full_url,
            "date":         start[:16] if start else "",
            "body":         body,
            "route":        self.route,
            "_meta": {
                "kind":         "fair",
                "company":      company,
                "forward_day":  forward_day,
                "is_online":    is_online,
            },
        }


# Registry for dispatching jsonapi_sources by config "type"
JSONAPI_SOURCE_REGISTRY = {
    "bjtu_internship": BJTUInternshipSource,
    "bjtu_fair":       BJTUFairSource,
}


# ── LLM summary + importance ────────────────────────────────────
SUMMARY_PROMPT_MAIL = """你是邮件助手。下面是一封邮件，请生成简短摘要并评估重要性。

发件人: {from_name} <{from_addr}>
主题: {title}
正文 (节选):
{body}

请严格按以下 3 行格式输出，不要其他内容：
摘要: 一句话不超过 30 字
重要性: 高 或 中 或 低
理由: 不超过 15 字说明为什么是这个等级

判断依据：
- 高: 来自学校/导师/老师/官方机构, 涉及成绩/通知/截止日期/账户安全
- 中: 个人邮件, 工作相关, 订阅的有用内容
- 低: 营销广告, 群发通知, 自动确认邮件"""

SUMMARY_PROMPT_NOTICE = """你是学校通知助手。下面是一条来自 {source_name} 的网页通知，请生成摘要并评估对一名计算机大类大一学生的相关性。

标题: {title}
日期: {date}
正文 (节选):
{body}

请严格按以下 3 行格式输出，不要其他内容：
摘要: 一句话不超过 30 字
重要性: 高 或 中 或 低
理由: 不超过 15 字说明为什么是这个等级

判断依据（核心是"对计算机大类大一学生是否相关"）：
- 高: 涉及选课/专业分流/学籍/奖助学金/全体本科生通知/计算机大类/重要截止日期
- 中: 学院级讲座/竞赛/可参加的活动/实习信息
- 低: 教师专属/研究生专属/其他学院专属/纯仪式性通知/会议预告"""

SUMMARY_PROMPT_INTERNSHIP = """你是就业信息助手。下面是一条实习招聘信息，请评估对一名计算机大类大一学生的相关性。

公司: {company}
公司性质: {nature}
规模: {scale}
城市: {city}
学历要求: {education}
详细信息:
{body}

请严格按以下 3 行格式输出，不要其他内容：
摘要: 一句话不超过 25 字，突出岗位特点和地点
重要性: 高 或 中 或 低
理由: 不超过 15 字

判断依据（核心是"对一名想做技术方向的本科低年级学生是否值得关注"）：
- 高: 互联网/科技公司、外企、北京可达、对本科开放、不限专业或包含计算机/软件/电子/信息
- 中: 国企/央企技术岗、北京周边城市、本硕都要但本科可投
- 低: 偏远地区、销售/客服/行政岗、明确仅招硕博、明显非技术方向"""

SUMMARY_PROMPT_FAIR = """你是就业信息助手。下面是一条招聘宣讲会/双选会信息，请评估对一名计算机大类大一学生的相关性。

标题: {title}
公司: {company}
日期: {date}
距今: {forward_day} 天
形式: {form}
详细信息:
{body}

请严格按以下 3 行格式输出，不要其他内容：
摘要: 一句话不超过 25 字，突出公司和时间地点
重要性: 高 或 中 或 低
理由: 不超过 15 字

判断依据（核心是"是否值得参加"）：
- 高: 时间在一周内、知名科技公司/外企、校内现场举办、技术方向公司
- 中: 时间在两周到一个月内、有相关性的公司、线上形式但内容相关
- 低: 距今超过一个月、明显非技术行业（如保险/快消）、或已过期"""

def summarize_item(item, llm_cfg):
    if not llm_cfg.get("dashscope_api_key") or "xxx" in llm_cfg["dashscope_api_key"]:
        return {
            "summary": (item["title"] or "")[:30],
            "level": "中",
            "reason": "未配置 LLM"
        }

    source_type = item.get("source_type", "imap")
    meta = item.get("_meta", {}) or {}

    if source_type == "bjtu_internship":
        prompt = SUMMARY_PROMPT_INTERNSHIP.format(
            company=meta.get("company", ""),
            nature=meta.get("nature", ""),
            scale=meta.get("scale", ""),
            city=meta.get("city", ""),
            education=meta.get("education", ""),
            body=item["body"][:1000] or "(无正文)",
        )
    elif source_type == "bjtu_fair":
        prompt = SUMMARY_PROMPT_FAIR.format(
            title=item["title"],
            company=meta.get("company", ""),
            date=item.get("date", ""),
            forward_day=meta.get("forward_day", "?"),
            form=("线上" if meta.get("is_online") else "校内现场"),
            body=item["body"][:1000] or "(无正文)",
        )
    elif source_type == "web":
        prompt = SUMMARY_PROMPT_NOTICE.format(
            source_name=item.get("source_name", ""),
            title=item["title"],
            date=item.get("date", ""),
            body=item["body"][:1000] or "(无正文)"
        )
    else:
        prompt = SUMMARY_PROMPT_MAIL.format(
            from_name=item.get("from_name", ""),
            from_addr=item.get("from_addr", ""),
            title=item["title"],
            body=item["body"][:1000] or "(无正文)"
        )

    try:
        resp = requests.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {llm_cfg['dashscope_api_key']}",
                "Content-Type": "application/json"
            },
            json={
                "model": llm_cfg.get("model", "qwen-turbo"),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.2
            },
            timeout=30
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"  LLM call failed: {e}", "WARN")
        return {
            "summary": (item["title"] or "")[:30],
            "level": "中",
            "reason": "LLM 调用失败"
        }

    summary, level, reason = "", "中", ""
    for line in text.split("\n"):
        line = line.strip()
        low = line.replace("：", ":")
        if low.startswith("摘要"):
            summary = low.split(":", 1)[-1].strip()
        elif low.startswith("重要性"):
            raw = low.split(":", 1)[-1].strip()
            for keyword in ["高", "中", "低"]:
                if keyword in raw:
                    level = keyword
                    break
        elif low.startswith("理由"):
            reason = low.split(":", 1)[-1].strip()

    if not summary:
        summary = item["title"][:30]
    return {"summary": summary, "level": level, "reason": reason}

# ── Feishu push ─────────────────────────────────────────────────
LEVEL_EMOJI = {"高": "🔴", "中": "🟡", "低": "⚪"}

def push_to_feishu(items, webhook, accounts_cfg):
    if not items:
        return

    by_level = {"高": [], "中": [], "低": []}
    for it in items:
        by_level.setdefault(it["level"], []).append(it)

    has_mail   = any(it.get("source_type") == "imap" for it in items)
    has_notice = any(it.get("source_type") == "web"  for it in items)
    has_career = any(it.get("source_type", "").startswith("bjtu_") for it in items)

    title_parts = []
    if has_mail:   title_parts.append("邮件")
    if has_notice: title_parts.append("通知")
    if has_career: title_parts.append("就业")

    if title_parts:
        header_title = " & ".join(title_parts) + "速递"
    else:
        header_title = "邮件管家"

    elements = []
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"**{len(items)} 条新消息** · {datetime.now().strftime('%H:%M')}"
        }
    })

    webmail_map = {a["name"]: a.get("webmail_url", "") for a in accounts_cfg}

    for level in ["高", "中", "低"]:
        bucket = by_level.get(level, [])
        if not bucket:
            continue
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"### {LEVEL_EMOJI[level]} {level}重要 ({len(bucket)} 条)"
            }
        })
        for it in bucket:
            url = it.get("url") or webmail_map.get(it["source_name"], "")
            link = f"[{it['source_name']}]({url})" if url else it["source_name"]

            stype = it.get("source_type", "")
            if stype.startswith("bjtu_"):
                from_line = f"来源: {link}\n"
            elif stype == "web":
                from_line = f"来源: {link}\n"
            elif it.get("from_name"):
                from_line = f"发件: {it['from_name']} · {link}\n"
            else:
                from_line = f"来源: {link}\n"

            content = (
                f"**{it['title'][:80]}**\n"
                f"{from_line}"
                f"摘要: {it['summary']}\n"
                f"<font color='grey'>{it['reason']} · {it['date']}</font>"
            )
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": content}
            })

    color = "red" if by_level.get("高") else "blue"
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": header_title},
                "template": color
            },
            "elements": elements
        }
    }
    try:
        resp = requests.post(webhook, json=card, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            log("Feishu push OK", "OK")
        else:
            log(f"Feishu API: {result}", "WARN")
    except Exception as e:
        log(f"Feishu push failed: {e}", "ERR")

# ── Task Scheduler ──────────────────────────────────────────────
def install_task():
    script_path = Path(__file__).resolve()
    python_exe = sys.executable.replace("python.exe", "pythonw.exe")
    cmd = [
        "schtasks", "/Create", "/TN", TASK_NAME,
        "/TR", f'"{python_exe}" "{script_path}"',
        "/SC", "MINUTE", "/MO", "10",
        "/RL", "LIMITED",
        "/F"
    ]
    try:
        subprocess.run(cmd, check=True)
        print(f"Task '{TASK_NAME}' installed: runs every 10 minutes")
        print("View it: Win+R -> taskschd.msc")
    except subprocess.CalledProcessError as e:
        print(f"Failed: {e}")

def uninstall_task():
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    try:
        subprocess.run(cmd, check=True)
        print(f"Task '{TASK_NAME}' removed")
    except subprocess.CalledProcessError:
        print(f"Task not found")

# ── Main ────────────────────────────────────────────────────────
def slice_state_for(source, full_state):
    """Extract only the state entries relevant to this source."""
    keys = source.state_keys()
    return {k: full_state["sources"].get(k, {}) for k in keys}

def main(dry_run=False):
    log("===== Email Bot v1.4 start =====")
    cfg = load_config()
    state = load_state()

    skip_low     = cfg["filters"].get("skip_low_importance", True)
    skip_senders = [s.lower() for s in cfg["filters"].get("skip_senders", [])]
    max_per_run  = cfg["filters"].get("max_per_run", 20)

    # Propagate global max_per_run into per-account if not set explicitly
    for acc in cfg["accounts"]:
        acc.setdefault("max_per_run", max_per_run)

    sources = build_sources(cfg)

    all_items = []
    pending_state_updates = {}

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {}
        for src in sources:
            state_slice = slice_state_for(src, state)
            futures[pool.submit(src.fetch, state_slice)] = src.name
        for fut in as_completed(futures):
            src_name = futures[fut]
            try:
                items, updates = fut.result()
                all_items.extend(items)
                pending_state_updates.update(updates)
            except Exception as e:
                log(f"  [{src_name}] fetch crashed: {e}", "ERR")

    if not all_items:
        log("No new items across all sources")
        state["sources"].update(pending_state_updates)
        save_state(state)
        log("===== Done =====")
        return

    log(f"Total new items: {len(all_items)}")

    pushable = []
    for item in all_items:
        if item.get("from_addr", "").lower() in skip_senders:
            log(f"  filtered (skip_senders): {item['title'][:30]}")
            continue
        analysis = summarize_item(item, cfg["llm"])
        item.update(analysis)
        if skip_low and item["level"] == "低":
            log(f"  filtered (low importance): {item['title'][:30]}")
            continue
        pushable.append(item)
        log(f"  [{item['level']}] {item['title'][:40]} - {item['summary']}")
        time.sleep(0.5)

    if pushable and not dry_run:
        push_to_feishu(pushable, cfg["feishu"]["webhook"], cfg["accounts"])
    elif dry_run:
        log(f"Dry run: would push {len(pushable)} item(s)")

    state["sources"].update(pending_state_updates)
    save_state(state)
    log(f"===== Done. Pushed: {len(pushable)} =====")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup",          action="store_true")
    parser.add_argument("--install-task",   action="store_true")
    parser.add_argument("--uninstall-task", action="store_true")
    parser.add_argument("--dry-run",        action="store_true")
    parser.add_argument("--list-folders",   action="store_true")
    parser.add_argument("--reset-state",    action="store_true")
    args = parser.parse_args()

    if args.install_task:
        install_task()
    elif args.uninstall_task:
        uninstall_task()
    elif args.setup:
        load_config()
    elif args.reset_state:
        reset_state()
    elif args.list_folders:
        cfg = load_config()
        for src in build_sources(cfg):
            if isinstance(src, IMAPSource):
                src.list_folders()
    else:
        main(dry_run=args.dry_run)