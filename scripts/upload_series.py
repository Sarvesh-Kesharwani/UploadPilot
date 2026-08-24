import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = r"D:\UploadPilot\.tmp\dump_profile"
USER = "kUkae5qhjJSmvKji6QNF5EGYLdU2"
SPACE = sys.argv[1]
ROOT = Path(r"D:\1. The Downloads\Movies & TV Shows") if len(sys.argv) < 3 else Path(sys.argv[2])
LOG = r"D:\UploadPilot\logs\pipeline.log"
EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".pdf", ".txt"}


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [series] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def row_kind_title(row):
    try:
        html = row.inner_html(timeout=1500)
    except Exception:
        html = ""
    try:
        text = row.inner_text(timeout=1500).strip()
    except Exception:
        text = ""
    kind = "folder" if "lucide-folder" in html else ("video" if "lucide-play" in html else "?")
    title = text.splitlines()[0].strip() if text else ""
    return kind, title


def upload_file(page, space_url, file_path, target_title):
    p = page
    p.goto(space_url, wait_until="domcontentloaded")
    p.wait_for_timeout(3000)
    if "/signin" in p.url:
        return "not logged in"
    try:
        p.wait_for_selector("tbody tr", timeout=25000, state="attached")
    except Exception:
        pass
    rows = p.locator("tbody tr")
    before = 0
    for i in range(rows.count()):
        kind, _ = row_kind_title(rows.nth(i))
        if kind == "video":
            before += 1
    # open upload menu
    chosen = False
    for attempt in range(3):
        add = None
        for sel in [
            'main button:has(span:text-is("Add Content"))',
            'main button:has-text("Add Content")',
            'button:has-text("Add Content")',
        ]:
            a = p.locator(sel).first
            try:
                a.wait_for(state="visible", timeout=5000)
                add = a
                break
            except Exception:
                continue
        if add is None:
            return "add content not found"
        add.click()
        p.wait_for_timeout(900 + attempt * 400)
        up = None
        for sel in [
            '[role="dialog"] div.cursor-pointer:has(h3:text-is("Upload"))',
            '[role="dialog"] div.group:has(h3:text-is("Upload"))',
            'div.cursor-pointer:has(h3:text-is("Upload"))',
            'h3:text-is("Upload")',
        ]:
            u = p.locator(sel).first
            try:
                u.wait_for(state="visible", timeout=4000)
                up = u
                break
            except Exception:
                continue
        if up is None:
            try:
                p.keyboard.press("Escape")
            except Exception:
                pass
            p.wait_for_timeout(500)
            continue
        try:
            with p.expect_file_chooser(timeout=30000) as fc_info:
                up.click()
            fc_info.value.set_files(str(file_path))
            chosen = True
            break
        except Exception:
            try:
                p.keyboard.press("Escape")
            except Exception:
                pass
            p.wait_for_timeout(500)
    if not chosen:
        return "upload menu flow failed"
    log(f"chooser accepted: {file_path.name}")
    # wait for new video row
    deadline = time.time() + 1500
    new_row = False
    while time.time() < deadline:
        if "/content/" in p.url:
            p.goto(space_url, wait_until="domcontentloaded")
            p.wait_for_timeout(2000)
        rows = p.locator("tbody tr")
        try:
            cnt = rows.count()
        except Exception:
            cnt = 0
        videos = 0
        for i in range(cnt):
            kind, _ = row_kind_title(rows.nth(i))
            if kind == "video":
                videos += 1
        if videos > before:
            new_row = True
            break
        p.wait_for_timeout(3000)
    if not new_row:
        return "row not detected in time"
    p.wait_for_timeout(6000)
    # rename newest video row (first video row) to target title
    rows = p.locator("tbody tr")
    target_row = None
    for i in range(rows.count()):
        kind, _ = row_kind_title(rows.nth(i))
        if kind == "video":
            target_row = rows.nth(i)
            break
    if target_row is None:
        return "no video row after upload"
    try:
        target_row.hover()
    except Exception:
        pass
    kebab = None
    for sel in [
        'td:last-child svg[aria-label="optionsMenu.openMenu"]',
        'td:last-child svg.lucide-ellipsis-vertical',
        'td:last-child button',
        'button:has(svg.lucide-ellipsis)',
    ]:
        k = target_row.locator(sel).first
        try:
            k.wait_for(state="visible", timeout=2500)
            kebab = k
            break
        except Exception:
            continue
    if kebab is None:
        return "kebab not found"
    kebab.click()
    p.wait_for_timeout(500)
    item = None
    for sel in [
        '[role="menuitem"]:has-text("Rename")',
        '[role="menuitem"]:has-text("Edit")',
        'button:has-text("Rename")',
    ]:
        it = p.locator(sel).first
        try:
            it.wait_for(state="visible", timeout=2500)
            item = it
            break
        except Exception:
            continue
    if item is None:
        return "rename menu not found"
    item.click()
    p.wait_for_timeout(700)
    active = None
    for sel in [
        'tbody tr input[type="text"]:visible',
        'tbody tr [contenteditable="true"]',
        'input[type="text"]:focus',
        '[contenteditable="true"]:focus',
    ]:
        a = p.locator(sel).first
        try:
            a.wait_for(state="visible", timeout=2500)
            active = a
            break
        except Exception:
            continue
    if active is None:
        return "edit input not found"
    active.press("Control+A")
    active.press("Delete")
    active.type(target_title, delay=15)
    p.wait_for_timeout(400)
    try:
        p.mouse.click(80, 400)
    except Exception:
        pass
    p.wait_for_timeout(2500)
    # verify
    rows = p.locator("tbody tr")
    for i in range(rows.count()):
        kind, title = row_kind_title(rows.nth(i))
        if kind == "video" and title.strip() == target_title:
            return "ok"
    return "rename verify failed"


def create_folder(page, space_id, name, parent=None):
    return page.evaluate(
        """async (a) => {
            const r = await fetch(a.url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'include',
                body: JSON.stringify(a.body)
            });
            let t = '';
            try { t = await r.text(); } catch (e) {}
            return {s: r.status, b: t.slice(0, 400)};
        }""",
        {
            "url": f"https://api.youlearn.ai/space/{space_id}/folders",
            "body": {
                "user_id": USER,
                "name": name,
                "parent_folder_id": parent,
                "icon": None,
                "icon_color": None,
            },
        },
    )


def list_folders(page, space_id):
    r = page.evaluate(
        "async (u) => { const r = await fetch(u, {credentials: 'include'}); return await r.text(); }",
        f"https://api.youlearn.ai/space/{space_id}/folders/{USER}",
    )
    try:
        return json.loads(r)
    except Exception:
        return []


def main():
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            headless=True,
            viewport={"width": 1440, "height": 950},
            args=["--disable-blink-features=AutomationControlled", "--disable-gpu"],
        )
        page = ctx.new_page()
        page.goto("https://app.youlearn.ai/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if "/signin" in page.url:
            log("NOT LOGGED IN")
            ctx.close()
            return
        if "Sign in" in page.inner_text("body") or page.locator("button:has-text('Sign in')").count() > 0:
            log("SESSION NOT VALID (sign-in page)")
            ctx.close()
            return

        # build folder tree from ROOT relative subdirs (first level => folder, nested => child)
        files = []
        for dp, dn, fn in os.walk(ROOT):
            for f in fn:
                p = Path(dp) / f
                if p.suffix.lower() in EXTS:
                    files.append(p)
        files.sort(key=lambda p: p.as_posix().lower())
        log(f"total uploadable files: {len(files)}")

        # map: relative dir path -> folder id
        folder_ids = {}
        existing = list_folders(page, SPACE)
        by_name = {}
        for f in existing:
            by_name.setdefault(f.get("name"), []).append(f)
        for p in files:
            rel_dir = p.parent.relative_to(ROOT).as_posix() if p.parent != ROOT else "."
            if rel_dir == ".":
                continue
            parts = rel_dir.split("/")
            parent = None
            cur = ""
            for part in parts:
                cur = f"{cur}/{part}" if cur else part
                if cur not in folder_ids:
                    fid = None
                    for cand in by_name.get(part, []):
                        cand_parent = cand.get("parent_folder")
                        cand_parent_id = cand_parent.get("id") if isinstance(cand_parent, dict) else cand_parent
                        if cand_parent_id == parent:
                            fid = cand["_id"]
                            break
                    if fid is None:
                        r = create_folder(page, SPACE, part, parent)
                        if r["s"] == 201:
                            fid = json.loads(r["b"])["_id"]
                            log(f"folder created: {part} -> {fid}")
                        else:
                            log(f"folder create failed: {part} {r['s']}")
                    if fid:
                        folder_ids[cur] = fid
                    else:
                        folder_ids[cur] = None
                parent = folder_ids.get(cur)

        ok = 0
        fail = []
        for p in files:
            rel_dir = p.parent.relative_to(ROOT).as_posix() if p.parent != ROOT else "."
            fid = folder_ids.get(rel_dir) if rel_dir != "." else None
            space_url = f"https://app.youlearn.ai/space/{SPACE}"
            if fid:
                space_url += f"?folderId={fid}"
            target = p.name
            res = upload_file(page, space_url, p, target)
            if res == "ok":
                ok += 1
                log(f"OK: {p.name}")
            else:
                fail.append((str(p), res))
                log(f"FAIL: {p.name} -> {res}")
        log(f"series done: ok={ok} failed={len(fail)}")
        for f, r in fail[:30]:
            log(f"  failed file: {f} -> {r}")
        ctx.close()


if __name__ == "__main__":
    main()
