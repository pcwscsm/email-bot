"""
Unit tests for WebSource.

Verifies:
  1. List parsing extracts items with correct fields
  2. First run records baseline, returns no items
  3. Second run only returns new items
  4. Third run with no changes returns no items
  5. Network failure -> empty result, no state mutation
  6. item_id stability (URL with numeric ID -> same hash regardless of title edits)
  7. seen_ids cap (FIFO drop)

Cross-platform: works on Windows, Linux, macOS. CI-friendly.
"""
import os
import sys
import tempfile
import importlib.util
from unittest.mock import patch, MagicMock

# Set USERPROFILE before importing email_bot
os.environ.setdefault("USERPROFILE", tempfile.gettempdir())
TEST_DATA_DIR = os.path.join(os.environ["USERPROFILE"], "EmailBot")
os.makedirs(TEST_DATA_DIR, exist_ok=True)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
bot = load_module("bot", os.path.join(REPO_ROOT, "email_bot.py"))


# ─── Sample HTML fixtures ──────────────────────────────────────────
LIST_HTML_V1 = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>计算机学院 - 通知公告</title></head>
<body>
<div class="content-wrap">
  <ul class="list">
    <li class="row">
      <a href="/info/1031/12345.htm">关于2026春季学期选课工作的通知</a>
      <span class="date">2026-04-28</span>
    </li>
    <li class="row">
      <a href="/info/1031/12344.htm">第十五届"挑战杯"竞赛报名通知</a>
      <span class="date">2026-04-25</span>
    </li>
    <li class="row">
      <a href="/info/1031/12343.htm">研究生导师见面会安排</a>
      <span class="date">2026-04-22</span>
    </li>
  </ul>
</div>
</body></html>"""

# v2 prepends one new notice
LIST_HTML_V2 = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>计算机学院 - 通知公告</title></head>
<body>
<div class="content-wrap">
  <ul class="list">
    <li class="row">
      <a href="/info/1031/12346.htm">关于2026年暑期实习信息汇总</a>
      <span class="date">2026-05-02</span>
    </li>
    <li class="row">
      <a href="/info/1031/12345.htm">关于2026春季学期选课工作的通知</a>
      <span class="date">2026-04-28</span>
    </li>
    <li class="row">
      <a href="/info/1031/12344.htm">第十五届"挑战杯"竞赛报名通知</a>
      <span class="date">2026-04-25</span>
    </li>
    <li class="row">
      <a href="/info/1031/12343.htm">研究生导师见面会安排</a>
      <span class="date">2026-04-22</span>
    </li>
  </ul>
</div>
</body></html>"""

DETAIL_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>通知详情</title></head>
<body>
<div class="article-content">
<h2>关于2026年暑期实习信息汇总</h2>
<p>各位同学：</p>
<p>现将2026年暑期实习相关信息汇总如下：</p>
<p>1. 实习时间：7月1日至8月31日</p>
<p>2. 报名方式：通过学院就业系统提交申请</p>
<p>3. 截止日期：2026年5月20日</p>
</div>
</body></html>"""

CONFIG = {
    "name":            "测试学院通知",
    "list_url":        "http://test.example.com/notices/",
    "list_selector":   "ul.list > li",
    "title_selector":  "a",
    "url_attr":        "href",
    "date_selector":   ".date",
    "fetch_detail":    True,
    "detail_selector": ".article-content",
    "max_per_run":     5,
}


def make_mock_response(text, status=200):
    m = MagicMock()
    m.status_code = status
    m.text = text
    m.encoding = "utf-8"
    m.apparent_encoding = "utf-8"
    m.raise_for_status = MagicMock()
    return m


# ════════════════════════════════════════════════════════════════
# Test 1: List parsing
# ════════════════════════════════════════════════════════════════
print("=== Test 1: list parsing ===")
src = bot.WebSource(CONFIG)
with patch.object(bot.requests, "get", return_value=make_mock_response(LIST_HTML_V1)):
    entries = src._fetch_list()
assert len(entries) == 3, f"expected 3 entries, got {len(entries)}"
assert entries[0]["title"] == "关于2026春季学期选课工作的通知"
assert entries[0]["url"] == "http://test.example.com/info/1031/12345.htm"
assert entries[0]["date"] == "2026-04-28"
assert entries[0]["item_id"] == "url:12345"
print(f"  Parsed {len(entries)} entries with correct fields")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 2: First run = baseline only
# ════════════════════════════════════════════════════════════════
print("=== Test 2: first run = baseline only (no items pushed) ===")
src = bot.WebSource(CONFIG)
with patch.object(bot.requests, "get", return_value=make_mock_response(LIST_HTML_V1)):
    items, updates = src.fetch({})
assert len(items) == 0, "first run should not push items"
key = "web:测试学院通知"
assert key in updates
assert len(updates[key]["seen_ids"]) == 3
print(f"  Baseline established: {len(updates[key]['seen_ids'])} ids recorded")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 3: Second run with one new notice
# ════════════════════════════════════════════════════════════════
print("=== Test 3: second run with one new notice ===")
src = bot.WebSource(CONFIG)
state_slice = updates  # carry forward state

# Mock: first GET returns list HTML, second GET returns detail HTML
mock_responses = [make_mock_response(LIST_HTML_V2), make_mock_response(DETAIL_HTML)]
with patch.object(bot.requests, "get", side_effect=mock_responses), \
     patch.object(bot.time, "sleep"):
    items, updates2 = src.fetch(state_slice)

assert len(items) == 1, f"expected 1 new item, got {len(items)}"
item = items[0]
assert item["item_id"] == "url:12346"
assert item["title"] == "关于2026年暑期实习信息汇总"
assert item["source_type"] == "web"
assert item["from_name"] == ""  # web sources have no senders
assert "实习时间" in item["body"]
assert "url:12346" in updates2[key]["seen_ids"]
assert len(updates2[key]["seen_ids"]) == 4
print(f"  New item: {item['title']}")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 4: Third run with no changes
# ════════════════════════════════════════════════════════════════
print("=== Test 4: third run, no changes ===")
src = bot.WebSource(CONFIG)
with patch.object(bot.requests, "get", return_value=make_mock_response(LIST_HTML_V2)):
    items, _ = src.fetch(updates2)
assert len(items) == 0
print("  No items returned (expected)")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 5: Network failure
# ════════════════════════════════════════════════════════════════
print("=== Test 5: network failure handled gracefully ===")
src = bot.WebSource(CONFIG)
with patch.object(bot.requests, "get", side_effect=Exception("connection refused")):
    items, updates_fail = src.fetch(updates2)
assert items == []
assert updates_fail == {}, "failed fetch must not produce state updates"
print("  Empty result, no state mutation")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 6: item_id stability across title edits
# ════════════════════════════════════════════════════════════════
print("=== Test 6: make_item_id stability ===")
id1 = bot.make_item_id("http://x.com/info/1031/9999.htm", "标题A", "2026-01-01")
id2 = bot.make_item_id("http://x.com/info/1031/9999.htm", "标题B (改了错别字)", "2026-01-01")
assert id1 == id2 == "url:9999"

# No URL -> hash fallback, same input -> same hash
id3 = bot.make_item_id("", "通知 X", "2026-01-01")
id4 = bot.make_item_id("", "通知 X", "2026-01-01")
assert id3 == id4 and id3.startswith("hash:")
print(f"  url-based ID stable across title edits: {id1}")
print(f"  hash-based ID deterministic: {id3}")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 7: seen_ids cap (FIFO drop)
# ════════════════════════════════════════════════════════════════
print("=== Test 7: seen_ids capped at SEEN_IDS_CAP ===")
big_state = {key: {"seen_ids": [f"url:{i}" for i in range(199)]}}

new_html = """
<ul class="list">
""" + "\n".join(
    f'<li><a href="/info/1031/{1000+i}.htm">新通知{i}</a><span class="date">2026-05-{i+1:02d}</span></li>'
    for i in range(5)
) + "\n</ul>"

detail_mock = make_mock_response("<div class='article-content'>test body</div>")


def get_side(*args, **kwargs):
    url = args[0] if args else kwargs.get("url")
    if "info/1031/" in url:
        return detail_mock
    return make_mock_response(new_html)


src = bot.WebSource(CONFIG)
with patch.object(bot.requests, "get", side_effect=get_side), \
     patch.object(bot.time, "sleep"):
    items, updates_capped = src.fetch(big_state)

cap = bot.SEEN_IDS_CAP
seen_after = updates_capped[key]["seen_ids"]
assert len(seen_after) == cap, f"expected exactly {cap}, got {len(seen_after)}"
# Oldest 4 should have been dropped (199 + 5 = 204, cap to 200 -> drop 4 oldest)
assert "url:0" not in seen_after, "oldest should be dropped"
assert "url:1004" in seen_after, "newest should be kept"
print(f"  Before: 199 seen + 5 new = 204 candidates")
print(f"  After cap: {len(seen_after)} (cap = {cap}); oldest dropped, newest kept")
print("PASS\n")

print("=" * 50)
print("ALL WEBSOURCE TESTS PASSED ✓")
print("=" * 50)
