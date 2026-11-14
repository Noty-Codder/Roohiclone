"""Auto cleaner - keeps the (Heroku) disk small.

Every CLEAN_INTERVAL_HOURS (default 2) it deletes
  * player-card thumbnails  (cache/)
  * finished downloads      (downloads/)
  * speed-change leftovers  (playback/)
and posts a short report in the logger group. Files that belong to a song that is
playing or waiting in a queue are never touched.
"""

import asyncio
import gc
import os
import shutil
import time
from os import getenv

from RoohiClone.logging import LOGGER

INTERVAL = max(1, int(float(getenv("CLEAN_INTERVAL_HOURS", "2")) * 3600))

# folder, minimum file age in seconds, label for the report
TARGETS = (
    ("cache", 600, "thumbnails"),
    ("downloads", 1800, "downloads"),
    ("playback", 1800, "speed files"),
)

_last = {"time": 0.0, "files": 0, "bytes": 0}


def _mb(n):
    return f"{n / 1048576:.1f} MB"


def _protected():
    """Absolute paths of every file used by an active / queued track."""
    keep = set()
    try:
        from RoohiClone.misc import db
        for queue in list(db.values()):
            for track in list(queue or []):
                for key in ("file", "speed_path"):
                    value = track.get(key)
                    if value and os.path.exists(str(value)):
                        keep.add(os.path.abspath(str(value)))
    except Exception:
        pass
    return keep


def sweep():
    """Blocking sweep. Returns {label: (files, bytes)}."""
    now = time.time()
    keep = _protected()
    report = {}
    for folder, min_age, label in TARGETS:
        files = size = 0
        if os.path.isdir(folder):
            for root, dirs, names in os.walk(folder, topdown=False):
                for name in names:
                    path = os.path.join(root, name)
                    try:
                        if os.path.abspath(path) in keep:
                            continue
                        st = os.stat(path)
                        if now - st.st_mtime < min_age:
                            continue
                        os.remove(path)
                        files += 1
                        size += st.st_size
                    except OSError:
                        continue
                if root != folder:
                    try:
                        os.rmdir(root)          # only succeeds when empty
                    except OSError:
                        pass
        report[label] = (files, size)
    return report


def _system_line():
    parts = []
    try:
        total, used, free = shutil.disk_usage(".")
        parts.append(f"<b>💾 ᴅɪsᴋ :</b> {_mb(used)} / {_mb(total)}")
    except Exception:
        pass
    try:
        import psutil
        parts.append(f"<b>🧠 ʀᴀᴍ :</b> {_mb(psutil.Process().memory_info().rss)}")
    except Exception:
        pass
    return "\n".join(parts)


async def run_cleanup(notify=True):
    loop = asyncio.get_running_loop()
    report = await loop.run_in_executor(None, sweep)
    gc.collect()
    total_files = sum(f for f, _ in report.values())
    total_bytes = sum(b for _, b in report.values())
    _last.update(time=time.time(), files=total_files, bytes=total_bytes)
    LOGGER(__name__).info(f"🧹 Auto clean: {total_files} files, {_mb(total_bytes)} freed.")

    if notify:
        try:
            await _send_report(report, total_files, total_bytes)
        except Exception as e:
            LOGGER(__name__).warning(f"Cleaner report failed: {e}")
    return total_files, total_bytes


async def _send_report(report, total_files, total_bytes):
    import config
    from RoohiClone import app

    if not config.LOGGER_ID:
        return
    icons = {"thumbnails": "🖼", "downloads": "🎵", "speed files": "⚡"}
    lines = "\n".join(
        f"<b>{icons.get(label, '•')} {label} :</b> {files} ꜰɪʟᴇs · {_mb(size)}"
        for label, (files, size) in report.items()
    )
    hours = INTERVAL / 3600
    text = (
        "<blockquote><b>🧹 ᴀᴜᴛᴏ ᴄʟᴇᴀɴ • ᴄᴏᴍᴘʟᴇᴛᴇ</b></blockquote>\n"
        f"<blockquote>{lines}\n"
        f"<b>✅ ꜰʀᴇᴇᴅ :</b> {total_files} ꜰɪʟᴇs · {_mb(total_bytes)}\n"
        f"{_system_line()}\n"
        f"<b>⏰ ɴᴇxᴛ ᴄʟᴇᴀɴ :</b> {hours:g}ʜ</blockquote>"
    )
    await app.send_message(config.LOGGER_ID, text, disable_web_page_preview=True)


async def _loop():
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            await run_cleanup()
        except Exception as e:
            LOGGER(__name__).warning(f"Auto clean failed: {e}")


_task = None


def start_cleaner():
    """Start the background loop once (call after the event loop is running)."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())
        LOGGER(__name__).info(f"🧹 Auto cleaner started (every {INTERVAL / 3600:g}h).")
