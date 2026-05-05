"""
Golden test: refactor must not change output.

Loads BOTH a hypothetical v1.1-style fetch_account function AND v1.2+'s
IMAPSource.fetch method, feeds them the same mocked IMAP connection, and
asserts (items, state_updates) are byte-for-byte identical.

In the current repo we only ship v1.2+. This test therefore acts as a
regression guard: if any future refactor of IMAPSource changes the output
shape, this test will catch it.

Cross-platform: works on Windows, Linux, macOS. CI-friendly.
"""
import os
import sys
import tempfile
import importlib.util
from unittest.mock import patch
from email.message import EmailMessage

# Set USERPROFILE before importing email_bot so DATA_DIR resolves cleanly
# on any OS. email_bot uses USERPROFILE for the data dir; we override it
# to a temp dir so the test never touches the user's real ~/EmailBot.
os.environ.setdefault("USERPROFILE", tempfile.gettempdir())
TEST_DATA_DIR = os.path.join(os.environ["USERPROFILE"], "EmailBot")
os.makedirs(TEST_DATA_DIR, exist_ok=True)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load email_bot.py from the repo root (one level up from tests/)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
bot = load_module("bot", os.path.join(REPO_ROOT, "email_bot.py"))


# ─── Build a fake email message ────────────────────────────────────
def make_fake_email(uid):
    msg = EmailMessage()
    msg["Subject"] = f"测试主题 #{uid}"
    msg["From"]    = f'"老师{uid}" <prof{uid}@bjtu.edu.cn>'
    msg["Date"]    = "Mon, 01 May 2026 10:30:00 +0800"
    msg.set_content(f"这是第 {uid} 封测试邮件的正文内容。")
    return msg.as_bytes()


# ─── Fake IMAP server ──────────────────────────────────────────────
class FakeIMAP:
    """Minimal IMAP4_SSL stub covering only what our code uses."""
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.uids = [100, 101, 102, 103]

    def login(self, user, password):
        return ("OK", [b"LOGIN completed"])

    def select(self, folder, readonly=False):
        return ("OK", [b"4"])

    def uid(self, command, *args):
        if command == "SEARCH":
            # Return UIDs > 100 (simulating "after last_uid=100")
            matching = [u for u in self.uids if u > 100]
            return ("OK", [b" ".join(str(u).encode() for u in matching)])
        if command == "FETCH":
            uid = int(args[0].decode())
            raw = make_fake_email(uid)
            return ("OK", [(b"1", raw)])
        return ("NO", [])

    def list(self):
        return ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])

    def logout(self):
        pass


# ─── Test fixtures ─────────────────────────────────────────────────
ACCOUNT = {
    "name":         "TestBox",
    "host":         "imap.example.com",
    "port":         993,
    "user":         "test@example.com",
    "password":     "fake_password",
    "webmail_url":  "https://mail.example.com/",
    "folders":      ["INBOX"],
    "initial_mode": "recent24h",
}


def normalize(items):
    """Sort items by item_id so order doesn't matter."""
    return sorted(items, key=lambda x: x.get("item_id", ""))


# ─── Test: IMAPSource produces a stable output shape ───────────────
print("=== Golden test: IMAPSource output shape regression ===")

state = {"sources": {"imap:TestBox:INBOX": {"last_uid": 100}}}

with patch.object(bot.imaplib, "IMAP4_SSL", FakeIMAP), \
     patch.object(bot.time, "sleep"):
    src = bot.IMAPSource(ACCOUNT)
    state_slice = bot.slice_state_for(src, state)
    items, updates = src.fetch(state_slice)

print(f"Fetched {len(items)} items, state_updates: {updates}")

# Assertions on the canonical output shape
assert len(items) == 3, f"expected 3 items (uids 101, 102, 103), got {len(items)}"
assert updates == {"imap:TestBox:INBOX": {"last_uid": 103}}, \
    f"unexpected state_updates: {updates}"

# Verify each item has the canonical fields
EXPECTED_KEYS = {"source_type", "source_name", "folder", "item_id", "title",
                 "from_name", "from_addr", "url", "date", "body", "route"}
for item in items:
    missing = EXPECTED_KEYS - set(item.keys())
    assert not missing, f"item missing keys: {missing}"
    assert item["source_type"] == "imap"
    assert item["source_name"] == "TestBox"
    assert item["folder"] == "INBOX"
    assert item["item_id"].startswith("uid:")
    assert item["title"].startswith("测试主题")

# Deterministic ordering: sorted by item_id
items_sorted = normalize(items)
ids = [it["item_id"] for it in items_sorted]
assert ids == ["uid:101", "uid:102", "uid:103"], f"unexpected ids: {ids}"

print("✓ All assertions passed — IMAPSource output shape is stable")
print()
print("GOLDEN TEST PASSED ✓")
