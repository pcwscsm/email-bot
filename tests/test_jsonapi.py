"""
Unit tests for JSONAPISource and its BJTU subclasses.

Verifies:
  1. BJTUInternshipSource: API call, record-to-item mapping, hard filters
  2. BJTUFairSource: forward_day filtering, online vs on-campus detection
  3. JSONAPISource base: first-run preview, seen_ids dedup
  4. Failure isolation
  5. State==0 from API handled
  6. build_sources factory dispatches by type
  7. Prompt template dispatches by source_type
  8. Regression: IMAPSource still works in current bundle

Cross-platform: works on Windows, Linux, macOS. CI-friendly.
"""
import os
import sys
import tempfile
import importlib.util
from unittest.mock import patch, MagicMock
from email.message import EmailMessage

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


# ════════════════════════════════════════════════════════════════
# Sample API responses (taken from real-world capture, abridged)
# ════════════════════════════════════════════════════════════════
INTERN_RESPONSE = {
    "state": 1, "msg": "操作成功",
    "object": [
        {
            "id": "intern-001",
            "corporationinfo": {
                "name": "北京顺丰同城科技有限公司",
                "corporationScaleValue": "200人-500人",
                "corporationNatureValue": "其他企业",
            },
            "title": "北京顺丰同城科技有限公司",
            "positionType": "2",
            "startTime": "2026-04-28 17:47:53",
            "endTime":   "2026-07-30 00:00:00",
            "positionNum": 6,
            "education": "本科,硕士",
            "cityName": "北京市海淀区",
            "majorName": "计算机类,软件工程,不限专业",
            "url": "/f/recruitmentinfo/show?recruitmentId=intern-001",
        },
        {
            "id": "intern-002",
            "corporationinfo": {
                "name": "英特尔（中国）有限公司",
                "corporationScaleValue": "500人以上",
                "corporationNatureValue": "其他企业",
            },
            "title": "英特尔（中国）有限公司",
            "positionType": "2",
            "startTime": "2026-04-28 14:55:48",
            "endTime":   "2026-06-30 00:00:00",
            "positionNum": 1,
            "education": "本科,硕士,博士",
            "cityName": "上海市,广东省深圳市,北京市",
            "majorName": "不限专业",
            "url": "/f/recruitmentinfo/show?recruitmentId=intern-002",
        },
        {
            "id": "intern-003",
            "corporationinfo": {
                "name": "新疆某公司",
                "corporationScaleValue": "10人-50人",
                "corporationNatureValue": "国有企业",
            },
            "title": "新疆某公司",
            "positionType": "2",
            "startTime": "2026-04-28 10:00:00",
            "endTime":   "2026-09-30 00:00:00",
            "positionNum": 1,
            "education": "硕士,博士",
            "cityName": "乌鲁木齐市",
            "majorName": "测绘工程",
            "url": "/f/recruitmentinfo/show?recruitmentId=intern-003",
        },
    ]
}

FAIR_RESPONSE = {
    "state": 1, "msg": "操作成功",
    "object": [
        {
            "id": "fair-001",
            "corporationinfo": {"name": "中铁十四局集团有限公司"},
            "title": "中铁十四局集团2026届应届毕业生宣讲会",
            "startTime": "2026-05-07 15:00:00",
            "startTimeExport": "2026-05-07 15:00",
            "fieldExport": "第二就业宣讲厅（九教东101）",
            "place": "",
            "isExpired": "1",
            "forwardDay": 3,
            "url": "/f/recruitmentFair/show?recruitmentFairId=fair-001",
        },
        {
            "id": "fair-002",
            "corporationinfo": {"name": "天津云圣智能科技有限责任公司"},
            "title": "云圣智能2026届校园专场招聘会",
            "startTime": "2026-03-27 17:19:00",
            "startTimeExport": "2026-03-27 17:19",
            "fieldExport": "https://ikingtec.com/",
            "place": "https://ikingtec.com/",
            "isExpired": "1",
            "forwardDay": -38,  # already past
            "url": "/f/recruitmentFair/show?recruitmentFairId=fair-002",
        },
        {
            "id": "fair-003",
            "corporationinfo": {"name": "字节跳动"},
            "title": "字节跳动校园招聘会",
            "startTime": "2026-05-09 14:00:00",
            "startTimeExport": "2026-05-09 14:00",
            "fieldExport": "线上 zoom",
            "place": "https://meet.example.com/abc",
            "isExpired": "0",
            "forwardDay": 5,
            "url": "/f/recruitmentFair/show?recruitmentFairId=fair-003",
        },
        {
            "id": "fair-004",
            "corporationinfo": {"name": "远期公司"},
            "title": "远期公司年度宣讲",
            "startTime": "2026-09-01 10:00:00",
            "startTimeExport": "2026-09-01 10:00",
            "fieldExport": "某地点",
            "place": "某地点",
            "isExpired": "0",
            "forwardDay": 90,  # too far in future
            "url": "/f/recruitmentFair/show?recruitmentFairId=fair-004",
        },
    ]
}


def mock_response(json_data):
    m = MagicMock()
    m.status_code = 200
    m.json = MagicMock(return_value=json_data)
    m.raise_for_status = MagicMock()
    return m


# ════════════════════════════════════════════════════════════════
# Test 1: BJTUInternshipSource basic flow
# ════════════════════════════════════════════════════════════════
print("=== Test 1: BJTUInternshipSource record conversion ===")
src = bot.BJTUInternshipSource({
    "name": "BJTU实习",
    "type": "bjtu_internship",
    "max_per_run": 8,
    "first_run_preview": 0,
})

with patch.object(bot.requests, "post", return_value=mock_response(INTERN_RESPONSE)):
    items, updates = src.fetch({})

# First run with first_run_preview=0 means baseline only
assert len(items) == 0
key = "bjtu_internship:BJTU实习"
assert key in updates
assert len(updates[key]["seen_ids"]) == 3
print(f"  Baseline: {len(updates[key]['seen_ids'])} ids recorded, no items pushed")
print("PASS\n")

# ── Test 1b: second run with 1 new record ──
print("=== Test 1b: second run with 1 new record ===")
NEW_RESP = {
    "state": 1, "msg": "操作成功",
    "object": [
        {
            "id": "intern-NEW",
            "corporationinfo": {
                "name": "美团",
                "corporationScaleValue": "500人以上",
                "corporationNatureValue": "其他企业",
            },
            "title": "美团",
            "positionType": "2",
            "startTime": "2026-05-04 10:00:00",
            "endTime":   "2026-08-30 00:00:00",
            "positionNum": 5,
            "education": "本科",
            "cityName": "北京市",
            "majorName": "计算机类",
            "url": "/f/recruitmentinfo/show?recruitmentId=intern-NEW",
        }
    ] + INTERN_RESPONSE["object"]
}

with patch.object(bot.requests, "post", return_value=mock_response(NEW_RESP)):
    items2, updates2 = src.fetch(updates)

assert len(items2) == 1
item = items2[0]
assert item["title"] == "美团 · 北京市 · 本科"
assert item["url"].startswith("https://job.bjtu.edu.cn")
assert "截止" in item["date"]
assert item["_meta"]["company"] == "美团"
assert item["source_type"] == "bjtu_internship"
print(f"  New item: {item['title']}")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 2: city blocklist filter
# ════════════════════════════════════════════════════════════════
print("=== Test 2: city blocklist filter ===")
src_filtered = bot.BJTUInternshipSource({
    "name": "BJTU实习",
    "type": "bjtu_internship",
    "max_per_run": 8,
    "first_run_preview": 5,  # show all on first run for this test
    "skip_cities": ["乌鲁木齐"],
})

with patch.object(bot.requests, "post", return_value=mock_response(INTERN_RESPONSE)):
    items_f, _ = src_filtered.fetch({})

assert len(items_f) == 2, f"expected 2 items (after filter), got {len(items_f)}"
companies = [it["_meta"]["company"] for it in items_f]
assert "新疆某公司" not in companies
print(f"  3 input -> 2 kept (Wulumuqi filtered): {companies}")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 3: BJTUFairSource forward_day filtering + online detection
# ════════════════════════════════════════════════════════════════
print("=== Test 3: BJTUFairSource forward_day filtering ===")
fair_src = bot.BJTUFairSource({
    "name": "BJTU宣讲会",
    "type": "bjtu_fair",
    "max_per_run": 8,
    "first_run_preview": 10,
    "max_forward_days": 30,
})

with patch.object(bot.requests, "get", return_value=mock_response(FAIR_RESPONSE)):
    fair_items, _ = fair_src.fetch({})

# Expected: fair-001 (3 days, kept), fair-003 (5 days, kept)
# Filtered: fair-002 (-38 days), fair-004 (90 days)
assert len(fair_items) == 2
ids_kept = [it["item_id"] for it in fair_items]
assert "fair:fair-001" in ids_kept
assert "fair:fair-003" in ids_kept
assert "fair:fair-002" not in ids_kept
assert "fair:fair-004" not in ids_kept

fair_001 = [it for it in fair_items if it["item_id"] == "fair:fair-001"][0]
assert "3天后" in fair_001["title"]
fair_003 = [it for it in fair_items if it["item_id"] == "fair:fair-003"][0]
assert fair_003["_meta"]["is_online"] is True
print(f"  4 input -> 2 kept ({ids_kept})")
print(f"  fair-003 correctly identified as online")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 4: failure isolation
# ════════════════════════════════════════════════════════════════
print("=== Test 4: API failure handled gracefully ===")
src = bot.BJTUInternshipSource({"name": "BJTU实习", "type": "bjtu_internship"})
with patch.object(bot.requests, "post", side_effect=Exception("network down")):
    items, updates = src.fetch({})
assert items == []
assert updates == {}
print("  Empty result, no state mutation")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 5: API state=0
# ════════════════════════════════════════════════════════════════
print("=== Test 5: API returns state=0 (semantic error) ===")
err_resp = {"state": 0, "msg": "未授权", "object": None}
with patch.object(bot.requests, "post", return_value=mock_response(err_resp)):
    items, _ = src.fetch({})
assert items == []
print("  Empty result on state=0")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 6: build_sources dispatches by type
# ════════════════════════════════════════════════════════════════
print("=== Test 6: build_sources dispatches by type ===")
cfg = {
    "accounts": [],
    "websites": [],
    "jsonapi_sources": [
        {"name": "实习", "type": "bjtu_internship"},
        {"name": "宣讲会", "type": "bjtu_fair"},
        {"name": "未知", "type": "unknown_type"},  # should skip with warning
    ]
}
sources = bot.build_sources(cfg)
assert len(sources) == 2  # unknown_type skipped
assert isinstance(sources[0], bot.BJTUInternshipSource)
assert isinstance(sources[1], bot.BJTUFairSource)
print(f"  Built {len(sources)} sources, unknown_type correctly skipped")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 7: prompt dispatch (smoke test)
# ════════════════════════════════════════════════════════════════
print("=== Test 7: prompts dispatch by source_type ===")
llm_cfg = {"dashscope_api_key": "sk-xxxxxxxx", "model": "qwen-turbo"}  # placeholder triggers stub return
for stype in ["imap", "web", "bjtu_internship", "bjtu_fair"]:
    item = {
        "source_type": stype,
        "source_name": "test",
        "title": "测试标题",
        "from_name": "tester", "from_addr": "t@x.com",
        "body": "测试内容",
        "date": "2026-05-01",
        "_meta": {"company": "C", "nature": "外企", "scale": "X", "city": "北京",
                  "education": "本科", "forward_day": 3, "is_online": False},
    }
    result = bot.summarize_item(item, llm_cfg)
    assert "summary" in result and "level" in result and "reason" in result
print("  All 4 source_types handled without crash")
print("PASS\n")

# ════════════════════════════════════════════════════════════════
# Test 8: IMAP regression check
# ════════════════════════════════════════════════════════════════
print("=== Test 8: IMAP regression check ===")


def fake_email_bytes(uid):
    msg = EmailMessage()
    msg['Subject'] = f'测试主题 #{uid}'
    msg['From']    = f'"老师{uid}" <prof{uid}@bjtu.edu.cn>'
    msg['Date']    = 'Mon, 01 May 2026 10:30:00 +0800'
    msg.set_content(f'第 {uid} 封测试邮件。')
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self, host, port):
        self.uids = [100, 101, 102, 103]
    def login(self, u, p): return ('OK', [b''])
    def select(self, f, readonly=False): return ('OK', [b'4'])
    def uid(self, cmd, *args):
        if cmd == 'SEARCH':
            return ('OK', [b' '.join(str(u).encode() for u in self.uids if u > 100)])
        if cmd == 'FETCH':
            uid = int(args[0].decode())
            return ('OK', [(b'1', fake_email_bytes(uid))])
    def list(self): return ('OK', [b'(\\HasNoChildren) "/" "INBOX"'])
    def logout(self): pass


ACCOUNT = {
    'name': 'TestBox', 'host': 'imap.example.com', 'port': 993,
    'user': 'test@example.com', 'password': 'fake',
    'webmail_url': 'https://mail.example.com/',
    'folders': ['INBOX'], 'initial_mode': 'recent24h',
}

state = {'sources': {'imap:TestBox:INBOX': {'last_uid': 100}}}

with patch.object(bot.imaplib, 'IMAP4_SSL', FakeIMAP), \
     patch.object(bot.time, 'sleep'):
    src = bot.IMAPSource(ACCOUNT)
    state_slice = bot.slice_state_for(src, state)
    items, _ = src.fetch(state_slice)

assert len(items) == 3
assert items[0]["source_type"] == "imap"
assert items[0]["source_name"] == "TestBox"
assert items[0]["title"].startswith("测试主题")
print(f"  IMAP returned {len(items)} items, fields OK")
print("PASS\n")

print("=" * 55)
print("ALL JSONAPI / REGRESSION TESTS PASSED ✓")
print("=" * 55)
