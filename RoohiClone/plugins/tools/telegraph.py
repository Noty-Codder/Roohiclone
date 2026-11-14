"""/tgm  - reply to a photo / video / gif / sticker / file -> public link
   /tgt  - reply to text (or /tgt <text>) -> telegra.ph page

Telegraph shut its anonymous *image* upload, so /tgm uploads to free file hosts and
tries them one after the other (catbox, litterbox, envs.sh, tmpfiles, uguu, 0x0.st).
The file is handled in RAM only - nothing is written to the (Heroku) disk.
If every host fails the reply lists each host's error so the cause is visible.
"""

import asyncio
import json
import os
import time

import aiohttp
from pyrogram import filters
from pyrogram.types import Message

from RoohiClone import app
from RoohiClone.logging import LOGGER
from RoohiClone.utils.roohi import btn, rows_to_kb
from roohifont import fancy, quote
from config import BANNED_USERS

MAX_SIZE = 20 * 1024 * 1024          # 20 MB
COOLDOWN = 8                         # seconds per user
UA = {"User-Agent": "RoohiCloneBot/1.0 (+telegram)"}
_last_use = {}
_tg_token = {"value": None}


# ------------------------------------------------------------------ uploaders
def _short(e):
    return f"{type(e).__name__}: {str(e)[:60]}"


async def _post_bytes(session, url, field, data_bytes, filename, extra=None):
    form = aiohttp.FormData()
    for k, v in (extra or {}).items():
        form.add_field(k, v)
    form.add_field(field, data_bytes, filename=filename, content_type="application/octet-stream")
    async with session.post(url, data=form, headers=UA) as resp:
        return resp.status, (await resp.text()).strip()


def _pick_link(name, status, text):
    """Turn a host's answer into a link (or None)."""
    if status not in (200, 201):
        return None
    if name == "tmpfiles":
        try:
            url = json.loads(text)["data"]["url"]
            return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1) if "/dl/" not in url else url
        except Exception:
            return None
    if name == "uguu":
        try:
            return json.loads(text)["files"][0]["url"]
        except Exception:
            return None
    first = text.split()[0] if text else ""
    return first if first.startswith("http") else None


HOSTS = (
    # name, url, field, extra form fields
    ("catbox", "https://catbox.moe/user/api.php", "fileToUpload", {"reqtype": "fileupload"}),
    ("litterbox", "https://litterbox.catbox.moe/resources/internals/api.php", "fileToUpload",
     {"reqtype": "fileupload", "time": "72h"}),
    ("envs.sh", "https://envs.sh", "file", None),
    ("tmpfiles", "https://tmpfiles.org/api/v1/upload", "file", None),
    ("uguu", "https://uguu.se/upload", "files[]", None),
    ("0x0.st", "https://0x0.st", "file", None),
)


async def upload_bytes(data_bytes, filename):
    """Try every host in turn. Returns (link, host, errors)."""
    errors = []
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for name, url, field, extra in HOSTS:
            try:
                status, text = await _post_bytes(session, url, field, data_bytes, filename, extra)
                link = _pick_link(name, status, text)
                if link:
                    return link, name, errors
                errors.append(f"{name}: HTTP {status}")
            except Exception as e:
                errors.append(f"{name}: {_short(e)}")
    return None, None, errors


async def _telegraph_page(title, text):
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if not _tg_token["value"]:
            async with session.get("https://api.telegra.ph/createAccount",
                                   params={"short_name": "RoohiClone", "author_name": "RoohiClone"}) as r:
                data = await r.json()
            if not data.get("ok"):
                raise RuntimeError(data)
            _tg_token["value"] = data["result"]["access_token"]
        content = [{"tag": "p", "children": [line]} for line in text.split("\n") if line.strip()] or \
                  [{"tag": "p", "children": ["-"]}]
        async with session.post("https://api.telegra.ph/createPage", data={
            "access_token": _tg_token["value"],
            "title": title[:200] or "RoohiClone",
            "author_name": "RoohiClone",
            "content": json.dumps(content),
        }) as r:
            data = await r.json()
        if not data.get("ok"):
            _tg_token["value"] = None
            raise RuntimeError(data)
        return data["result"]["url"]


# ------------------------------------------------------------------ helpers
def _media_info(msg: Message):
    """(media object, extension) for the first uploadable media in msg."""
    if msg.photo:
        return msg.photo, ".jpg"
    if msg.animation:
        return msg.animation, ".mp4"
    if msg.video:
        return msg.video, ".mp4"
    if msg.sticker:
        return msg.sticker, ".tgs" if msg.sticker.is_animated else (".webm" if msg.sticker.is_video else ".webp")
    if msg.audio:
        return msg.audio, os.path.splitext(msg.audio.file_name or "")[1] or ".mp3"
    if msg.voice:
        return msg.voice, ".ogg"
    if msg.video_note:
        return msg.video_note, ".mp4"
    if msg.document:
        return msg.document, os.path.splitext(msg.document.file_name or "")[1] or ".bin"
    return None, None


def _cooldown(user_id):
    now = time.time()
    if now - _last_use.get(user_id, 0) < COOLDOWN:
        return True
    _last_use[user_id] = now
    if len(_last_use) > 2000:
        _last_use.clear()
    return False


# ------------------------------------------------------------------ /tgm
async def handle_tgm(client, message: Message):
    if message.from_user and _cooldown(message.from_user.id):
        return await message.reply_text(fancy("Slow down, try again in a few seconds."))

    target = message.reply_to_message or message
    media, ext = _media_info(target)
    if not media:
        return await message.reply_text(quote(
            f"<b>🖼 {fancy('Reply to a photo / video / gif / file with')} <code>/tgm</code></b>"))
    if (getattr(media, "file_size", 0) or 0) > MAX_SIZE:
        return await message.reply_text(quote(f"<b>❌ {fancy('File is too big (max 20 MB).')}</b>"))

    status = await message.reply_text(quote(f"<b>⬇️ {fancy('Downloading...')}</b>"))
    try:
        buf = await client.download_media(target, in_memory=True)      # RAM only - nothing touches the disk
        data_bytes = bytes(buf.getbuffer()) if buf is not None else b""
        if not data_bytes:
            raise RuntimeError("empty download")
    except Exception as e:
        LOGGER(__name__).warning(f"/tgm download failed: {_short(e)}")
        return await status.edit_text(quote(f"<b>❌ {fancy('Could not download that media from Telegram.')}</b>"))

    await status.edit_text(quote(f"<b>⬆️ {fancy('Uploading...')}</b>"))
    link, host, errors = await upload_bytes(data_bytes, f"kavya_{message.id}{ext}")
    del data_bytes

    if not link:
        LOGGER(__name__).warning("/tgm upload failed: " + " | ".join(errors))
        detail = "\n".join(errors[:6])
        return await status.edit_text(quote(
            f"<b>❌ {fancy('Upload failed on every host.')}</b>\n<code>{detail}</code>"))

    await status.edit_text(
        quote(f"<b>✅ {fancy('Uploaded')} ({host})</b>\n\n<code>{link}</code>"),
        disable_web_page_preview=True,
        reply_markup=rows_to_kb([[btn("Open link", url=link)]]),
    )


# ------------------------------------------------------------------ /tgt
async def handle_tgt(client, message: Message):
    if message.from_user and _cooldown(message.from_user.id):
        return await message.reply_text(fancy("Slow down, try again in a few seconds."))

    reply = message.reply_to_message
    text = (reply.text or reply.caption) if reply else None
    if not text and len(message.command) > 1:
        text = message.text.split(None, 1)[1]
    if not text:
        return await message.reply_text(quote(
            f"<b>📝 {fancy('Reply to a text with')} <code>/tgt</code> {fancy('or use')} <code>/tgt your text</code></b>"))

    title = text.strip().split("\n")[0][:60] or "RoohiClone"
    try:
        link = await _telegraph_page(title, text)
    except Exception:
        return await message.reply_text(quote(f"<b>❌ {fancy('Could not create the page. Try again later.')}</b>"))
    await message.reply_text(
        quote(f"<b>✅ {fancy('Page created')}</b>\n\n<code>{link}</code>"),
        disable_web_page_preview=True,
        reply_markup=rows_to_kb([[btn("Open page", url=link)]]),
    )


@app.on_message(filters.command(["tgm", "telegraph"]) & ~BANNED_USERS)
async def tgm_upload(client, message: Message):
    await handle_tgm(client, message)


@app.on_message(filters.command(["tgt"]) & ~BANNED_USERS)
async def tgt_page(client, message: Message):
    await handle_tgt(client, message)
