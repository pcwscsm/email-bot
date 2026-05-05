"""
Recon script: analyze a target webpage to figure out its structure.

Usage:
    python recon.py <list_url>

Example:
    python recon.py http://scit.bjtu.edu.cn/cms/tzgg/

What it does:
    1. Fetches the page (handles GBK/UTF-8 auto-detection)
    2. Reports HTTP status, encoding, content size
    3. Identifies likely "notice list" containers using common heuristics
    4. Prints a few candidate items with their HTML structure
    5. Suggests CSS selectors for config.json
"""
import sys
import requests
from bs4 import BeautifulSoup
from collections import Counter
from urllib.parse import urlparse, urljoin

if len(sys.argv) < 2:
    print("Usage: python recon.py <url>")
    sys.exit(1)

url = sys.argv[1]
print(f"=== Reconnaissance: {url} ===\n")

# ── Step 1: Fetch ──
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

try:
    resp = requests.get(url, headers=HEADERS, timeout=15)
except Exception as e:
    print(f"FETCH FAILED: {e}")
    sys.exit(1)

print(f"HTTP {resp.status_code}")
print(f"Declared encoding: {resp.encoding}")
print(f"Detected encoding: {resp.apparent_encoding}")
print(f"Content-Type: {resp.headers.get('Content-Type')}")
print(f"Content size: {len(resp.content)} bytes\n")

# Use detected encoding
resp.encoding = resp.apparent_encoding
html = resp.text

# ── Step 2: Parse ──
soup = BeautifulSoup(html, "html.parser")
title = soup.find("title")
print(f"<title>: {title.get_text(strip=True) if title else '(none)'}\n")

# ── Step 3: Find link-rich containers (heuristic for notice lists) ──
# A notice list usually has many <a> tags with similar structure inside the same parent.
print("=== Top link-rich containers (likely candidates for notice lists) ===\n")

candidates = []
for tag in soup.find_all(["ul", "div", "table", "tbody"]):
    links = tag.find_all("a", recursive=True, href=True)
    # We want the *immediate* container, not the whole body
    if 5 <= len(links) <= 50:
        # Check that links look like notices (have meaningful text)
        meaningful = [a for a in links if len(a.get_text(strip=True)) >= 5]
        if len(meaningful) >= 5:
            candidates.append((tag, meaningful))

# Deduplicate: prefer the smallest container that holds a given set of links
candidates.sort(key=lambda x: len(x[1]))
seen_link_sets = []
unique_candidates = []
for tag, links in candidates:
    link_hrefs = frozenset(a.get("href") for a in links)
    if any(link_hrefs.issubset(s) for s in seen_link_sets):
        continue
    seen_link_sets.append(link_hrefs)
    unique_candidates.append((tag, links))

if not unique_candidates:
    print("(no obvious notice-list container found)\n")
    print("This page may use JavaScript to render the list, or the structure")
    print("is unusual. Check with browser DevTools.\n")
else:
    for i, (tag, links) in enumerate(unique_candidates[:3]):
        # Build a CSS-ish path
        path_parts = []
        cur = tag
        depth = 0
        while cur and cur.name and depth < 5:
            part = cur.name
            if cur.get("id"):
                part += f"#{cur['id']}"
            elif cur.get("class"):
                part += "." + ".".join(cur["class"][:2])
            path_parts.append(part)
            cur = cur.parent
            depth += 1
        path = " > ".join(reversed(path_parts))

        print(f"--- Candidate #{i+1}: {len(links)} links ---")
        print(f"Container path: {path}")
        print(f"Tag: <{tag.name}", end="")
        if tag.get("id"):
            print(f' id="{tag["id"]}"', end="")
        if tag.get("class"):
            print(f' class="{" ".join(tag["class"])}"', end="")
        print(">")

        # Show first 3 link items in detail
        print("\nFirst 3 items:")
        for j, a in enumerate(links[:3]):
            text = a.get_text(strip=True)[:60]
            href = a.get("href", "")
            full_url = urljoin(url, href)
            # What's the parent of the <a>? (li, td, div, etc)
            parent_tag = a.parent.name if a.parent else "?"
            # Look for a date near the link
            date_hint = ""
            sibling_text = ""
            if a.parent:
                sibling_text = a.parent.get_text(" ", strip=True)
            import re
            m = re.search(r"(20\d{2}[-./]\d{1,2}[-./]\d{1,2})", sibling_text)
            if m:
                date_hint = f"  [date hint: {m.group(1)}]"
            print(f"  [{j+1}] <{parent_tag}> {text}{date_hint}")
            print(f"      -> {full_url}")
        print()

# ── Step 4: Look at one detail page (if we found candidates) ──
if unique_candidates:
    print("=== Sample detail page (first item of best candidate) ===\n")
    first_link = unique_candidates[0][1][0]
    detail_url = urljoin(url, first_link.get("href"))
    print(f"Fetching: {detail_url}\n")
    try:
        dresp = requests.get(detail_url, headers=HEADERS, timeout=15)
        dresp.encoding = dresp.apparent_encoding
        dsoup = BeautifulSoup(dresp.text, "html.parser")
        dtitle = dsoup.find("title")
        print(f"Detail <title>: {dtitle.get_text(strip=True) if dtitle else '(none)'}")

        # Find the main content block: usually the largest block of text
        text_blocks = []
        for tag in dsoup.find_all(["div", "article", "section", "td"]):
            text = tag.get_text(" ", strip=True)
            if 100 <= len(text) <= 5000:
                # Penalize containers that have lots of nested content blocks
                inner = tag.find_all(["div", "article"])
                if len(inner) <= 5:
                    text_blocks.append((tag, len(text), text))
        text_blocks.sort(key=lambda x: -x[1])
        if text_blocks:
            tag, length, text = text_blocks[0]
            cls = ".".join(tag.get("class", []))
            tid = tag.get("id", "")
            print(f"Likely content block: <{tag.name}", end="")
            if tid:    print(f' id="{tid}"', end="")
            if cls:    print(f' class="{cls}"', end="")
            print(f"> ({length} chars)")
            print(f"Preview: {text[:200]}...")
        else:
            print("(no obvious content block found)")
    except Exception as e:
        print(f"detail page fetch failed: {e}")

print("\n=== END ===")
print("\nNext step: paste this output back to me, I'll generate the config.")