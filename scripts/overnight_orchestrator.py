import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = r"D:\UploadPilot\.tmp\dump_profile"
USER = "kUkae5qhjJSmvKji6QNF5EGYLdU2"
SERVER = "http://127.0.0.1:3000"
REPORT = r"D:\UploadPilot\youlearn_upload_report.md"
PIPELINE_LOG = r"D:\UploadPilot\logs\pipeline.log"
MERGED_SKIP = Path(r"D:\.Courses\_merged_skip")
SUPPORTED_EXTS = {".txt", ".pdf", ".mp4", ".mov", ".webm", ".m4v"}
SKIPPED_EXTS = {".mkv", ".avi", ".m2ts", ".ts", ".wmv", ".flv", ".vob"}

COURSES = [
    {
        "name": "FastAPI Udemy",
        "space": "43a99a9c4fbe4d2b",
        "root": r"D:\.Courses\fastapi udemy",
        "cleanup_first": True,
    },
]


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with open(PIPELINE_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def report_append(text):
    with open(REPORT, "a", encoding="utf-8") as f:
        f.write(text + "\n")


class YL:
    def __init__(self, page):
        self.page = page

    def api(self, method, url, body=None):
        return self.page.evaluate(
            """async (a) => {
                const r = await fetch(a.url, {
                    method: a.method,
                    headers: {'Content-Type': 'application/json'},
                    credentials: 'include',
                    body: a.body ? JSON.stringify(a.body) : undefined
                });
                let t = '';
                try { t = await r.text(); } catch (e) {}
                return {s: r.status, b: t};
            }""",
            {"url": url, "method": method, "body": body},
        )

    def space_contents(self, space_id):
        r = self.api(
            "GET",
            f"https://api.youlearn.ai/space/{USER}/{space_id}",
        )
        if r["s"] != 200:
            return [], []
        d = json.loads(r["b"])
        return d.get("contents", []), d.get("space_content_maps", [])

    def folders(self, space_id):
        r = self.api(
            "GET",
            f"https://api.youlearn.ai/space/{space_id}/folders/{USER}",
        )
        if r["s"] != 200:
            return []
        return json.loads(r["b"])

    def create_folder(self, space_id, name, parent=None):
        body = {
            "user_id": USER,
            "name": name,
            "parent_folder_id": parent,
            "icon": None,
            "icon_color": None,
        }
        r = self.api("POST", f"https://api.youlearn.ai/space/{space_id}/folders", body)
        if r["s"] == 201:
            return json.loads(r["b"])["_id"]
        return None

    def move_content(self, content_id, space_id, folder_id=None):
        body = {"user_id": USER, "content_id": content_id, "destination_space_id": space_id}
        if folder_id:
            body["destination_folder_id"] = folder_id
        r = self.api("POST", "https://api.youlearn.ai/content/move", body)
        return r["s"] == 204

    def delete_contents(self, space_id, ids):
        if not ids:
            return
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            self.api(
                "DELETE",
                "https://api.youlearn.ai/content/",
                {
                    "user_id": USER,
                    "space_id": space_id,
                    "content_ids": batch,
                    "delete_from_history": False,
                },
            )


def server_state():
    try:
        with urllib.request.urlopen(f"{SERVER}/api/state", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def server_start(space_url, folder):
    body = json.dumps({"space_url": space_url, "folder": folder, "mode": "smart"}).encode()
    req = urllib.request.Request(
        f"{SERVER}/api/start",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def wait_job(timeout_min=180):
    deadline = time.time() + timeout_min * 60
    last_progress = None
    last_progress_t = time.time()
    while time.time() < deadline:
        st = server_state()
        if st and st.get("job") and st["job"].get("finished_at"):
            return st["job"]
        if st and st.get("job"):
            items = st["job"].get("items", [])
            key = (
                sum(1 for i in items if i.get("status") == "uploaded"),
                sum(1 for i in items if i.get("status") == "failed"),
                sum(1 for i in items if i.get("status") == "skipped"),
                sum(int(i.get("progress_percent") or 0) for i in items),
            )
            if key != last_progress:
                last_progress = key
                last_progress_t = time.time()
            elif time.time() - last_progress_t > 1200:
                log("job stalled 20 min without progress; cancelling to retry")
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(f"{SERVER}/api/cancel", method="POST"), timeout=15
                    )
                except Exception:
                    pass
                time.sleep(30)
                return st["job"]
        time.sleep(20)
    return None


def scan_subfolders(root):
    dirs = []
    for dp, dn, fn in os.walk(root):
        if dp == root:
            dirs.extend(sorted(d for d in dn if not d.startswith(".")))
    return dirs


def run_folder_job(name, space_id, folder_path, sub=None):
    space_url = f"https://app.youlearn.ai/space/{space_id}"
    if sub:
        space_url += f"?folderId={sub}"
    r = None
    for attempt in range(6):
        st = server_state()
        if st and st.get("job") and not st["job"].get("finished_at"):
            log(f"job {name} - server busy, waiting ({attempt})")
            time.sleep(30)
            continue
        log(f"job {name} start folder={folder_path} sub={sub or 'root'}")
        r = server_start(space_url, folder_path)
        if "error" in r and "409" in str(r["error"]):
            time.sleep(30)
            continue
        break
    if r is None or "error" in r:
        log(f"job {name} start error: {r}")
        return None
    job = wait_job(timeout_min=240)
    if job is None:
        log(f"job {name} timed out")
        return None
    items = job.get("items", [])
    up = sum(1 for i in items if i.get("status") == "uploaded")
    sk = sum(1 for i in items if i.get("status") == "skipped")
    fl = [i for i in items if i.get("status") == "failed"]
    log(f"job {name} done: total={len(items)} uploaded={up} skipped={sk} failed={len(fl)}")
    return fl


def main():
    for _ in range(30):
        st = server_state()
        if st is None:
            log("server down; waiting")
            time.sleep(20)
            continue
        job = st.get("job")
        if job is None or job.get("finished_at"):
            break
        time.sleep(20)
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
            log("NOT LOGGED IN - aborting")
            ctx.close()
            return
        yl = YL(page)

        for course in COURSES:
            name = course["name"]
            space = course["space"]
            root = course["root"]
            log(f"=== course {name} ===")

            # Log unsupported files (never uploaded); handled at the end.
            skipped = []
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    p = Path(dp) / f
                    if p.suffix.lower() in SKIPPED_EXTS:
                        skipped.append(p)
            if skipped:
                log(f"{name}: {len(skipped)} unsupported files skipped (.mkv/.avi/etc)")
                report_append(
                    f"- {name}: SKIPPED {len(skipped)} unsupported files -> "
                    + "; ".join(p.name for p in skipped[:20])
                    + (" ..." if len(skipped) > 20 else "")
                )

            if course.get("cleanup_first"):
                contents, maps = yl.space_contents(space)
                titles = [c["title"] for c in contents]
                dup = {k for k, v in Counter(titles).items() if v > 1}
                to_delete = []
                for k in dup:
                    ids = [c["_id"] for c in contents if c["title"] == k]
                    to_delete.extend(ids[1:])
                yl.delete_contents(space, to_delete)
                log(f"{name} cleanup: deleted {len(to_delete)} duplicate rows")
                report_append(f"### {name} cleanup: deleted {len(to_delete)} duplicates")

            subs = scan_subfolders(root)
            folders = {f["name"]: f["_id"] for f in yl.folders(space)}
            for s in subs:
                if s not in folders:
                    fid = yl.create_folder(space, s)
                    if fid:
                        folders[s] = fid
                        log(f"{name}: created folder {s} -> {fid}")

            # move existing contents into matching folders by title prefix
            contents, maps = yl.space_contents(space)
            moved = 0
            for c in contents:
                title = c.get("title", "")
                for s in subs:
                    prefix = f"{s} / "
                    if title.startswith(prefix) and s in folders:
                        if yl.move_content(c["_id"], space, folders[s]):
                            moved += 1
                        break
            if moved:
                log(f"{name}: moved {moved} existing contents into folders")

            # Merged files: create a per-course "Merged" folder, upload the
            # staged merged files from _merged_skip, and move any merged-titled
            # rows uploaded in place into it.
            merged_fid = folders.get("Merged")
            if not merged_fid:
                merged_fid = yl.create_folder(space, "Merged")
                if merged_fid:
                    folders["Merged"] = merged_fid
                    log(f"{name}: created Merged folder -> {merged_fid}")
            merged_dir = MERGED_SKIP / name if (MERGED_SKIP / name).exists() else None
            if merged_dir and any(
                p.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".pdf", ".txt"}
                for p in merged_dir.rglob("*")
                if p.is_file()
            ):
                fl = run_folder_job(f"{name}/Merged", space, str(merged_dir), merged_fid)
                if fl:
                    report_append(f"- {name}/Merged: {len(fl)} failed - retrying")
                    fl2 = run_folder_job(f"{name}/Merged", space, str(merged_dir), merged_fid)
                    if fl2:
                        report_append(
                            "  failed: "
                            + "; ".join(f"{i.get('name')} -> {i.get('error')}" for i in fl2[:10])
                        )
            # upload per subfolder
            for s in subs:
                folder_path = os.path.join(root, s)
                fl = run_folder_job(f"{name}/{s}", space, folder_path, folders.get(s))
                if fl:
                    report_append(f"- {name}/{s}: {len(fl)} failed - retrying")
                    fl2 = run_folder_job(f"{name}/{s}", space, folder_path, folders.get(s))
                    if fl2:
                        report_append(
                            "  failed: "
                            + "; ".join(f"{i.get('name')} -> {i.get('error')}" for i in fl2[:10])
                        )

            # move any merged-titled rows (uploaded in place) into Merged folder
            if merged_fid:
                contents, _maps = yl.space_contents(space)
                for c in contents:
                    title = c.get("title", "")
                    base = os.path.basename(title)
                    if "merged" in base.lower() and "Combine the BIO" not in title:
                        if yl.move_content(c["_id"], space, merged_fid):
                            log(f"{name}: moved merged row {title} -> Merged")

            # Delete local files validated on the space (extraction completed).
            contents, _maps = yl.space_contents(space)
            valid = {
                c["title"]: c
                for c in contents
                if c.get("metadata", {}).get("extraction_status") == "completed"
            }
            local_files = []
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    p = Path(dp) / f
                    if p.suffix.lower() in SUPPORTED_EXTS:
                        local_files.append(p)
            if merged_dir:
                local_files += [
                    p
                    for p in merged_dir.rglob("*")
                    if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
                ]
            deleted = 0
            for p in local_files:
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    rel = p.name
                title = rel.replace("/", " / ")
                if title in valid:
                    try:
                        os.remove(p)
                        deleted += 1
                    except OSError as e:
                        log(f"{name}: delete failed {p.name}: {e}")
            if deleted:
                log(f"{name}: deleted {deleted} validated local files")
                report_append(f"- {name}: DELETED {deleted} local files after validation")

            # top-level loose files (non-recursive) to root
            loose = [
                f
                for f in os.listdir(root)
                if os.path.isfile(os.path.join(root, f))
                and f.lower().endswith((".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".pdf", ".txt"))
            ]
            if loose:
                fl = run_folder_job(f"{name}/root", space, root)
                if fl:
                    report_append(f"- {name}/root: {len(fl)} failed")

            report_append(f"### {name}: folders created={len(subs)}, contents moved={moved}")

        report_append("---\n## Orchestrator finished")
        log("orchestrator finished")
        ctx.close()


if __name__ == "__main__":
    main()
