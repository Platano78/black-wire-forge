"""Browser gate for the sequence list and the guide conversation per sequence.

Modelled on tests/test_picture_ui.py. Creates seed data, drives the Cutting room
through sections A–E, and reports PASS/FAIL per check without crashing on a
missing element (short timeouts + try/except, text_of-style helpers).

Run ONLY as:
    flock /models/scratch/bwf-personas/browser.lock timeout 600 python3 tests/test_seq_list_ui.py
"""
import json
import os
import re
import sys
import time
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
import _picture_server as ps  # noqa: E402

# -- helpers ------------------------------------------------------------------

NOW = time.time()


def api(path):
    """GET a JSON endpoint and return the parsed object, or None on failure."""
    try:
        return json.loads(urllib.request.urlopen(URL + path, timeout=5).read())
    except Exception:
        return None


def seq_rows():
    """GET /api/sequences, or [] on failure."""
    r = api("api/sequences")
    return r if isinstance(r, list) else []


def guide_msgs(page):
    """Return the textContent of every #guideLog .guide-msg .guide-text."""
    try:
        return page.eval_on_selector_all("#guideLog .guide-msg .guide-text", "els => els.map(e => e.textContent)")
    except Exception:
        return []


def safe_click(sel, page, timeout=5000):
    """Click sel or return False."""
    try:
        el = page.query_selector(sel)
        if el is None:
            return False
        el.click()
        return True
    except Exception:
        return False


def safe_fill(sel, text, page, timeout=5000):
    """Fill sel with text or return False."""
    try:
        el = page.query_selector(sel)
        if el is None:
            return False
        el.click()
        el.fill(text)
        return True
    except Exception:
        return False


def safe_press(sel, key, page):
    """Press key on sel or return False."""
    try:
        el = page.query_selector(sel)
        if el is None:
            return False
        el.press(key)
        return True
    except Exception:
        return False


def wait_until_msg_count(page, min_count, timeout=15):
    """Wait until #guideLog .guide-msg count >= min_count and #guideThinking is hidden."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            count = page.eval_on_selector_all("#guideLog .guide-msg", "els => els.length")
            th = page.query_selector("#guideThinking")
            if count >= min_count and (th is None or th.is_hidden()):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def wait_for_selector_safe(sel, page, timeout=3):
    """Try to wait for sel, return False on timeout/exception."""
    try:
        page.wait_for_selector(sel, timeout=timeout * 1000)
        return True
    except Exception:
        return False


def text_of(sel, page=None):
    """Return textContent of a selector, or "" when missing."""
    try:
        el = page.query_selector(sel)
        if el is None:
            return ""
        return el.inner_text()
    except Exception:
        return ""


def has_sel(sel, page):
    """True when the selector exists and is visible."""
    try:
        el = page.query_selector(sel)
        return el is not None and not el.is_hidden()
    except Exception:
        return False


# -- seed data ----------------------------------------------------------------
# Write straight into os.path.join(ui.SCRATCH, "data") BEFORE ui.start().

DATA = os.path.join(ui.SCRATCH, "data")
os.makedirs(os.path.join(DATA, "seq", "s_0000aaaa", "takes"), exist_ok=True)

with open(os.path.join(DATA, "seq", "s_0000aaaa", "takes", "pic1.png"), "wb") as f:
    f.write(ui.gradient_png(160, 90))

seq_a = {
    "id": "s_0000aaaa",
    "schema": 1,
    "rev": 1,
    "title": "Paper plane draft",
    "mode": "storyboard",
    "created": NOW - 3600,
    "updated": NOW - 3600,
    "canvas": {"width": 1024, "height": 576},
    "refs": [],
    "beats": [{"id": "b1", "kind": "film", "text": "A paper airplane leaves a window.", "rev": 1, "slot_id": "v1"}],
    "slots": [
        {
            "id": "p1",
            "lane": "picture",
            "beat_id": None,
            "cap": "image",
            "mode": "t2i",
            "recipe": None,
            "quality": None,
            "values": {},
            "refs": "auto",
            "takes": [
                {
                    "job_id": "pic1",
                    "made": NOW - 3600,
                    "beat_rev": None,
                    "inputs": {"refs": [], "cables": {}},
                    "file": "takes/pic1.png"
                }
            ],
            "pick": "pic1",
            "trim": None,
            "title": None
        },
        {
            "id": "v1",
            "lane": "video",
            "beat_id": "b1",
            "cap": "video",
            "mode": "ltx",
            "recipe": None,
            "quality": None,
            "values": {"prompt": "A paper airplane leaves a window."},
            "refs": "auto",
            "takes": [],
            "pick": None,
            "trim": None,
            "title": None
        }
    ],
    "cables": [],
    "cuts": []
}

seq_b = {
    "id": "s_0000bbbb",
    "schema": 1,
    "rev": 1,
    "title": "Untitled sequence",
    "mode": "sequence",
    "created": NOW - 7200,
    "updated": NOW - 7200,
    "canvas": {"width": 1024, "height": 576},
    "refs": [],
    "beats": [],
    "slots": [],
    "cables": [],
    "cuts": []
}

os.makedirs(os.path.join(DATA, "sequences"), exist_ok=True)
with open(os.path.join(DATA, "sequences", "s_0000aaaa.json"), "w") as f:
    json.dump(seq_a, f)
with open(os.path.join(DATA, "sequences", "s_0000bbbb.json"), "w") as f:
    json.dump(seq_b, f)

# -- fake helper replies ------------------------------------------------------
ps.HELPER_STATE["replies"] = ["Beats one.", "Second answer.", "Third answer."]

# -- start the server ---------------------------------------------------------
JOB = {
    "id": "pic1",
    "lane": "t",
    "lane_name": "Fake lane",
    "kind": "image",
    "mode": "t2i",
    "status": "done",
    "prompt": "a paper plane",
    "seed": 1,
    "created": NOW - 3600,
    "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}]
}

URL = ui.start([JOB])

# -- test driver --------------------------------------------------------------

NEWID = None

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        # Enter the room; wait for the sequence list container
        # The Cutting Room needs JS to fetch /api/engines, discover the cutting room,
        # then seqEnterRoom -> seqLoadList -> renderSeqPickerList.
        # Use domcontentloaded + a short selector wait with try/except so a missing
        # feature is a FAIL line, not a traceback.
        try:
            page.goto(URL + "#room=cutting", wait_until="domcontentloaded")
            # give the room-picker JS time to load
            page.wait_for_timeout(2000)
        except Exception:
            pass
        # short wait for [data-seq-id] — will fail gracefully if feature is missing
        try:
            page.wait_for_selector('[data-seq-id]', timeout=3000)
        except Exception:
            pass

        # ======================================================================
        # A. The list
        # ======================================================================

        # 1. "row: shows the first beat's text"
        beat_text = text_of('.seq-row-beat', page)
        check("A1: row: shows the first beat's text",
              beat_text == "A paper airplane leaves a window.",
              beat_text)

        # 2. "row: shows its shot count"
        meta = text_of('.seq-row-meta', page)
        check("A2: row: shows its shot count",
              "1 shot" in meta and "1 shots" not in meta,
              "meta text: %r" % meta)

        # 3. "row: shows an exact date and time"
        check("A3: row: shows an exact date and time",
              bool(re.search(r'20\d\d\D+\d{2}:\d{2}', meta)),
              "meta text: %r" % meta)

        # 4. "row: shows a thumbnail of the first picked take"
        thumb = page.query_selector('[data-seq-row="s_0000aaaa"] .seq-thumb img')
        thumb_ok = False
        if thumb is not None:
            src = thumb.get_attribute("src") or ""
            if "api/sequence/file?id=s_0000aaaa" in src:
                try:
                    thumb_ok = page.wait_for_function(
                        '() => { const i = document.querySelector(\'[data-seq-row="s_0000aaaa"] .seq-thumb img\'); return i && i.naturalWidth > 0; }',
                        timeout=5000)
                except Exception:
                    thumb_ok = False
        check("A4: row: shows a thumbnail of the first picked take",
              thumb_ok)

        # 5. "row: a sequence with no beats shows no beat line"
        bb_row = page.query_selector('[data-seq-row="s_0000bbbb"]')
        no_beat = bb_row is not None and bb_row.query_selector(".seq-row-beat") is None
        check("A5: row: a sequence with no beats shows no beat line",
              no_beat)

        ui.shot(page, "seqlist-A")

        # ======================================================================
        # B. A blank new sequence
        # ======================================================================

        # 6. Leave #seqNewTitle empty, click #seqNewBtn, wait for #seqOpenNote
        safe_fill("#seqNewTitle", "", page)
        safe_click("#seqNewBtn", page)
        try:
            page.wait_for_selector("#seqOpenNote", timeout=5000)
        except Exception:
            pass

        new_title_text = text_of("#seqOpenTitle", page)
        current_year = time.strftime("%Y")
        check("B6a: blank new sequence: named from the date",
              current_year in new_title_text and "Untitled" not in new_title_text,
              "title text: %r, year: %s" % (new_title_text, current_year))

        # remember NEWID from location.hash
        m = re.search(r'seq=(s_[0-9a-f]{8})', page.url)
        NEWID = m.group(1) if m else None
        check("B6b: blank new sequence: got an id", bool(NEWID), NEWID)

        # 7. "blank new sequence: the newest row is the one that opened"
        rows_now = seq_rows()
        check("B7: blank new sequence: the newest row is the one that opened",
              len(rows_now) > 0 and rows_now[0]["id"] == NEWID,
              "first id: %s" % (rows_now[0]["id"] if rows_now else "none"))

        ui.shot(page, "seqlist-B")

        # ======================================================================
        # C. Rename
        # ======================================================================

        # 8. Click [data-seq-rename="s_0000bbbb"]
        safe_click('[data-seq-rename="s_0000bbbb"]', page)
        time.sleep(0.3)
        check("C8: rename: the row turns into a text box",
              has_sel('[data-seq-rename-input="s_0000bbbb"]', page))

        # 9. Fill with "Harbour test", press Enter
        safe_fill('[data-seq-rename-input="s_0000bbbb"]', "Harbour test", page)
        safe_press('[data-seq-rename-input="s_0000bbbb"]', "Enter", page)
        time.sleep(0.5)

        row_title = text_of('[data-seq-row="s_0000bbbb"] .seq-row-title', page)
        srv_title = api("api/sequence?id=s_0000bbbb").get("title", "")
        check("C9a: rename: the row shows the new name",
              row_title == "Harbour test", "row title: %r" % row_title)
        check("C9b: rename: the server has it",
              srv_title == "Harbour test", "server title: %r" % srv_title)

        # 10. Click #seqOpenTitle; rename open
        safe_click("#seqOpenTitle", page)
        time.sleep(0.3)
        check("C10a: rename open: the title turns into a text box",
              has_sel('#seqOpenNote .seq-rename-input', page))

        safe_fill('#seqOpenNote .seq-rename-input', "Paper plane", page)
        safe_press('#seqOpenNote .seq-rename-input', "Enter", page)
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass

        check("C10b: rename open: the new name shows",
              text_of("#seqOpenTitle", page) == "Paper plane",
              text_of("#seqOpenTitle", page))
        srv_title_newid_c10c = None
        if NEWID:
            srv_title_newid_c10c = api("api/sequence?id=%s" % NEWID).get("title")
        check("C10c: rename open: the server has it",
              srv_title_newid_c10c == "Paper plane",
              srv_title_newid_c10c)

        # 11. Click #seqOpenTitle, fill "zzz", press Escape
        safe_click("#seqOpenTitle", page)
        safe_fill('#seqOpenNote .seq-rename-input', "zzz", page)
        safe_press('#seqOpenNote .seq-rename-input', "Escape", page)
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass

        srv_title_newid = None
        if NEWID:
            srv_title_newid = api("api/sequence?id=%s" % NEWID).get("title")
        check("C11: rename: Escape keeps the old name (server)",
              srv_title_newid == "Paper plane",
              "server title: %r" % srv_title_newid)
        check("C11b: rename: Escape keeps the old name (UI)",
              text_of("#seqOpenTitle", page) == "Paper plane",
              text_of("#seqOpenTitle", page))

        ui.shot(page, "seqlist-C")

        # ======================================================================
        # D. Delete
        # ======================================================================

        # 12. Click [data-seq-delete="s_0000bbbb"] once
        del_btn_ok = safe_click('[data-seq-delete="s_0000bbbb"]', page)
        del_btn = page.query_selector('[data-seq-delete="s_0000bbbb"]')
        del_btn_text = del_btn.inner_text() if del_btn else ""
        check("D12a: delete: asks once more",
              "Click again" in del_btn_text)
        check("D12b: delete: still in list after first click",
              any(s["id"] == "s_0000bbbb" for s in seq_rows()))

        # 13. Click it again
        safe_click('[data-seq-delete="s_0000bbbb"]', page)
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass
        del_ok = page.query_selector('[data-seq-row="s_0000bbbb"]') is None
        del_rows = seq_rows()
        check("D13a: delete: the row goes",
              del_ok)
        check("D13b: delete: the server list no longer has it",
              not any(s["id"] == "s_0000bbbb" for s in del_rows))
        check("D13c: delete: says what happened",
              "Deleted" in text_of("#seqMsg", page),
              text_of("#seqMsg", page))

        # 14. Delete the open one: click [data-seq-delete="NEWID"] twice
        if NEWID:
            safe_click('[data-seq-delete="%s"]' % NEWID, page)
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
            safe_click('[data-seq-delete="%s"]' % NEWID, page)
            try:
                page.wait_for_timeout(3000)
            except Exception:
                pass
            check("D14a: delete open: the sequence closes",
                  page.query_selector("#seqOpenNote") is None)
            check("D14b: delete open: gone from the list",
                  page.query_selector('[data-seq-row="%s"]' % NEWID) is None)
        else:
            check("D14: skip (no NEWID)", False, "NEWID was None")

        ui.shot(page, "seqlist-D")

        # ======================================================================
        # E. The guide conversation belongs to its sequence
        # ======================================================================

        # Re-enter the room and click s_0000aaaa
        try:
            page.goto(URL + "#room=cutting", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception:
            pass
        try:
            page.wait_for_selector('[data-seq-id]', timeout=3000)
        except Exception:
            pass
        safe_click('[data-seq-id="s_0000aaaa"]', page)
        try:
            page.wait_for_selector("#seqOpenNote", timeout=5000)
        except Exception:
            pass

        # 15. Send a message to the guide
        try:
            page.fill("#guideInput", "a paper airplane")
            page.press("#guideInput", "Enter")
        except Exception:
            pass
        wait_until_msg_count(page, 3)
        msgs = guide_msgs(page)
        check("E15: guide: the answer shows",
              any("Beats one." in m for m in msgs),
              msgs)

        # 16. Create a second sequence
        safe_fill("#seqNewTitle", "Second", page)
        safe_click("#seqNewBtn", page)
        try:
            page.wait_for_selector("#seqOpenTitle", timeout=5000)
        except Exception:
            pass
        try:
            page.wait_for_function('() => document.getElementById("seqOpenTitle")?.textContent === "Second"',
                                    timeout=10000)
        except Exception:
            pass
        msgs2 = guide_msgs(page)
        check("E16: guide: another sequence starts its own conversation",
              len(msgs2) == 1,
              "msg count: %d" % len(msgs2))
        try:
            page.fill("#guideInput", "second talk")
            page.press("#guideInput", "Enter")
        except Exception:
            pass
        wait_until_msg_count(page, 3)

        # 17. Switch back to s_0000aaaa
        safe_click('[data-seq-id="s_0000aaaa"]', page)
        page.wait_for_timeout(1000)
        msgs_back = guide_msgs(page)
        check("E17: guide: switching back brings its conversation back",
              any("a paper airplane" in m for m in msgs_back)
              and any("Beats one." in m for m in msgs_back)
              and not any("second talk" in m for m in msgs_back),
              msgs_back)

        # 18. Tab away and back
        try:
            safe_click('[data-room-id="picture"]', page)
            page.wait_for_timeout(1000)
            safe_click('[data-room-id="cutting"]', page)
            page.wait_for_timeout(3000)
            hash_has_seq = "seq=s_0000aaaa" in page.url
            has_open_note = page.query_selector("#seqOpenNote") is not None
            check("E18a: return: the room tab reopens the last sequence",
                  hash_has_seq and has_open_note,
                  "hash has seq: %s, open note: %s" % (hash_has_seq, has_open_note))
            msgs_ret = guide_msgs(page)
            check("E18b: return: with its conversation",
                  any("a paper airplane" in m for m in msgs_ret),
                  msgs_ret)
        except Exception:
            pass

        # 19. Reload
        try:
            page.reload(wait_until="load")
            page.wait_for_timeout(3000)
        except Exception:
            pass
        msgs_reload = guide_msgs(page)
        check("E19: reload: the conversation is still there",
              any("Beats one." in m for m in msgs_reload),
              msgs_reload)

        # 20. Close the sequence, tab away and back
        try:
            safe_click("#seqCloseBtn", page)
            page.wait_for_timeout(500)
            safe_click('[data-room-id="picture"]', page)
            page.wait_for_timeout(500)
            safe_click('[data-room-id="cutting"]', page)
            page.wait_for_timeout(1000)
            check("E20a: return: a closed sequence stays closed (no open note)",
                  page.query_selector("#seqOpenNote") is None)
            has_seq_in_hash = "seq=" in page.url
            check("E20b: return: a closed sequence stays closed (no seq=)",
                  not has_seq_in_hash,
                  "url: %s" % page.url)
        except Exception:
            pass

        ui.shot(page, "seqlist-E")

        browser.close()

finally:
    ui.finish()
