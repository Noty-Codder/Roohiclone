"""VC logger - one engine for the main bot and every clone bot.

Per chat   : /vclogger on | off | (status)
             -> "who joined / left the voice chat" notices (name, id, username,
                total people in VC, join count) that delete themselves after a few
                seconds. The switch is saved in MongoDB, so it survives restarts.
Logger grp : VC started / ended / invited, assistant joined / left (+ tracks played).
             Honours the global /logger enable|disable switch.

Join / leave events come from the assistant account through py-tgcalls
(call_participant updates), so they work for the main assistants and for the
own-assistant of a clone. Everything is wrapped so a py-tgcalls API change can
never crash the bot - the logger simply stays silent and logs one warning.
"""

import asyncio
import html
import time

from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from RoohiClone.core.mongo import mongodb
from RoohiClone.logging import LOGGER
from roohifont import fancy, quote

vcdb = mongodb.vclogger

_state = {}        # chat_id -> bot_id that owns the logger (0 = main bot)   [cache of the DB]
_session = {}      # chat_id -> {"since": ts, "tracks": n}                   [assistant session]
_vc_open = {}      # chat_id -> ts the voice chat was started
_join_count = {}   # (chat_id, user_id) -> n
_user_cache = {}   # user_id -> (name, username)
_attached = set()  # id() of PyTgCalls instances that already have the handlers
NOTICE_LIFETIME = 25     # started / ended / invited notices
JOIN_LIFETIME = 10       # join / leave notices


# ------------------------------------------------------------------ switch (MongoDB)
async def is_vclog_on(chat_id) -> bool:
    chat_id = int(chat_id)
    if chat_id not in _state:
        try:
            doc = await vcdb.find_one({"chat_id": chat_id})
            _state[chat_id] = int(doc.get("bot_id", 0)) if doc else None
        except Exception:
            _state[chat_id] = None
    return _state[chat_id] is not None


async def set_vclog(chat_id, on: bool, bot_id=0):
    chat_id = int(chat_id)
    if on:
        _state[chat_id] = int(bot_id)
    else:
        _state[chat_id] = None
        for key in [k for k in _join_count if k[0] == chat_id]:
            _join_count.pop(key, None)
    try:
        if on:
            await vcdb.update_one({"chat_id": chat_id}, {"$set": {"bot_id": int(bot_id)}}, upsert=True)
        else:
            await vcdb.delete_one({"chat_id": chat_id})
    except Exception as e:
        LOGGER(__name__).warning(f"vclogger db error: {e}")


def _bot_for(chat_id):
    """Pyrogram client that should post the notices of this chat (main bot or the clone)."""
    from RoohiClone import app
    bot_id = _state.get(int(chat_id)) or 0
    if bot_id and bot_id != getattr(app, "id", 0):
        try:
            from RoohiClone.plugins.tools.clone import ACTIVE_CLONES
            client = ACTIVE_CLONES.get(bot_id)
            if client is not None:
                return client
        except Exception:
            pass
    return app


# ------------------------------------------------------------------ helpers
def _dur(sec):
    sec = max(0, int(sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ʜ {m}ᴍ {s}s"
    if m:
        return f"{m}ᴍ {s}s"
    return f"{s}s"


def _now_str():
    return time.strftime("%d %b %Y • %H:%M:%S UTC", time.gmtime())


def _chat_line(chat):
    title = html.escape(getattr(chat, "title", None) or "Unknown")
    return f"{title} [<code>{chat.id}</code>]"


def _link_markup(chat):
    username = getattr(chat, "username", None)
    if not username:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(fancy("Open group"), url=f"https://t.me/{username}")]])


async def _logger_on() -> bool:
    if not config.LOGGER_ID:
        return False
    try:
        from RoohiClone.utils.database import is_on_off
        return bool(await is_on_off(2))
    except Exception:
        return True


async def _delete_later(msg, delay):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def is_chat_admin(client, chat_id, user) -> bool:
    """Group admins / owner / sudoers / bot owner. Works even when nothing is playing."""
    if user is None:
        return False
    try:
        from RoohiClone.misc import SUDOERS
        if user.id in SUDOERS or user.id == config.OWNER_ID:
            return True
    except Exception:
        pass
    try:
        member = await client.get_chat_member(chat_id, user.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


# ------------------------------------------------------------------ /vclogger command (main + clones)
async def handle_command(client, message):
    chat_id = message.chat.id
    arg = message.command[1].lower() if len(message.command) > 1 else ""

    if arg in ("on", "enable", "off", "disable") and not await is_chat_admin(client, chat_id, message.from_user):
        return await message.reply_text(quote(f"<b>❌ {fancy('Only group admins can change this.')}</b>"))

    me = client.me or await client.get_me()
    from RoohiClone import app
    bot_id = 0 if me.id == getattr(app, "id", 0) else me.id

    if arg in ("on", "enable"):
        await set_vclog(chat_id, True, bot_id)
        return await message.reply_text(quote(
            f"<b>🎙 {fancy('VC logger')} : ✅ {fancy('enabled')}</b>\n"
            f"<b>{fancy('You will see who joins / leaves the voice chat.')}</b>"))
    if arg in ("off", "disable"):
        await set_vclog(chat_id, False)
        return await message.reply_text(quote(f"<b>🎙 {fancy('VC logger')} : ❌ {fancy('disabled')}</b>"))

    on = await is_vclog_on(chat_id)
    state = f"✅ {fancy('enabled')}" if on else f"❌ {fancy('disabled')}"
    await message.reply_text(quote(
        f"<b>🎙 {fancy('VC logger')} : {state}</b>\n"
        f"<b>{fancy('Use')}</b> <code>/vclogger on</code> | <code>/vclogger off</code>"))


# ------------------------------------------------------------------ who joined / left (py-tgcalls participant events)
def _assistant_ids():
    ids = set()
    try:
        from RoohiClone import userbot
        for name in ("one", "two", "three", "four", "five"):
            me = getattr(getattr(userbot, name, None), "me", None)
            if me:
                ids.add(me.id)
    except Exception:
        pass
    try:
        from RoohiClone.core.call import ROOHI
        ids.update(ROOHI.custom_assistants.keys())
    except Exception:
        pass
    return ids


async def _user_info(chat_id, user_id):
    if user_id in _user_cache:
        return _user_cache[user_id]
    name, username = "User", "ɴσηє"
    try:
        member = await _bot_for(chat_id).get_chat_member(chat_id, user_id)
        user = member.user
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or "User"
        username = f"@{user.username}" if user.username else "ɴσηє"
    except Exception:
        pass
    _user_cache[user_id] = (name, username)
    if len(_user_cache) > 5000:
        _user_cache.clear()
    return name, username


async def _send_participant(call, chat_id, user_id, joined):
    if not await is_vclog_on(chat_id):
        return
    if user_id in _assistant_ids():
        return
    bot = _bot_for(chat_id)
    name, username = await _user_info(chat_id, user_id)
    mention = f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'
    tag = "#JoinVideoChat" if joined else "#LeaveVideoChat"
    dot = "🟢" if joined else "🔴"
    lines = [
        f"<b>ɴᴀᴍᴇ ➛</b> {mention}",
        f"<b>ɪᴅ ➛</b> <code>{user_id}</code>",
        f"<b>ᴜsᴇʀɴᴀᴍᴇ ➛</b> {username}",
    ]
    try:
        total = len(await call.get_participants(chat_id))
        lines.append(f"<b>👥 ᴛᴏᴛᴀʟ ɪɴ ᴠᴄ ➛</b> {total}")
    except Exception:
        pass
    if joined:
        key = (chat_id, user_id)
        _join_count[key] = _join_count.get(key, 0) + 1
        if _join_count[key] > 1:
            lines.append(f"<b>🔁 ᴊᴏɪɴ ᴄᴏᴜɴᴛ ➛</b> {_join_count[key]}")
        if len(_join_count) > 5000:
            _join_count.clear()
    text = f"{dot} <b>{tag}</b>\n<blockquote>" + "\n".join(lines) + "</blockquote>"
    try:
        msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        asyncio.create_task(_delete_later(msg, JOIN_LIFETIME))
    except Exception as e:
        LOGGER(__name__).debug(f"vc join/leave notice failed: {e}")


def attach(call):
    """Register the join / leave listeners on one PyTgCalls instance (safe to call twice)."""
    if call is None or id(call) in _attached:
        return
    try:
        from pytgcalls import filters as fl
        from pytgcalls.types import GroupCallParticipant

        @call.on_update(fl.call_participant(GroupCallParticipant.Action.JOINED))
        async def _joined(client, update):
            try:
                await _send_participant(client, update.chat_id, update.participant.user_id, True)
            except Exception as e:
                LOGGER(__name__).debug(f"vc joined handler: {e}")

        @call.on_update(fl.call_participant(GroupCallParticipant.Action.LEFT))
        async def _left(client, update):
            try:
                await _send_participant(client, update.chat_id, update.participant.user_id, False)
            except Exception as e:
                LOGGER(__name__).debug(f"vc left handler: {e}")

        _attached.add(id(call))
    except Exception as e:
        _attached.add(id(call))
        LOGGER(__name__).warning(f"VC join/leave logger not available with this py-tgcalls: {e}")


# ------------------------------------------------------------------ logger-group events
HEAD = {
    "started": ("🎙", "ᴠᴏɪᴄᴇ ᴄʜᴀᴛ sᴛᴀʀᴛᴇᴅ"),
    "ended": ("🔇", "ᴠᴏɪᴄᴇ ᴄʜᴀᴛ ᴇɴᴅᴇᴅ"),
    "invited": ("📨", "ᴍᴇᴍʙᴇʀs ɪɴᴠɪᴛᴇᴅ ᴛᴏ ᴠᴄ"),
    "joined": ("🎧", "ᴀssɪsᴛᴀɴᴛ ᴊᴏɪɴᴇᴅ ᴠᴄ"),
    "left": ("👋", "ᴀssɪsᴛᴀɴᴛ ʟᴇꜰᴛ ᴠᴄ"),
}


def build_text(kind, chat, by=None, duration=None, users=None, tracks=None):
    icon, head = HEAD[kind]
    lines = [f"<b>💬 ᴄʜᴀᴛ :</b> {_chat_line(chat)}"]
    if by:
        lines.append(f"<b>👤 ʙʏ :</b> {by}")
    if users:
        lines.append(f"<b>👥 ᴜsᴇʀs :</b> {users}")
    if duration is not None:
        lines.append(f"<b>⏱ ᴅᴜʀᴀᴛɪᴏɴ :</b> {_dur(duration)}")
    if tracks is not None:
        lines.append(f"<b>🎵 ᴛʀᴀᴄᴋs ᴘʟᴀʏᴇᴅ :</b> {tracks}")
    lines.append(f"<b>🕒 ᴛɪᴍᴇ :</b> {_now_str()}")
    return f"<blockquote><b>{icon} {head}</b></blockquote>\n<blockquote>" + "\n".join(lines) + "</blockquote>"


async def log_event(kind, chat, by=None, duration=None, users=None, tracks=None, notice_client=None, notify_chat=True):
    """Send one VC event to the logger group and (if switched on) to the chat itself."""
    from RoohiClone import app

    text = build_text(kind, chat, by=by, duration=duration, users=users, tracks=tracks)
    markup = _link_markup(chat)

    if await _logger_on() and int(chat.id) != int(config.LOGGER_ID):
        try:
            await app.send_message(config.LOGGER_ID, text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True, reply_markup=markup)
        except Exception as e:
            LOGGER(__name__).warning(f"vc log send failed: {e}")

    if notify_chat and kind in ("started", "ended", "invited") and await is_vclog_on(chat.id):
        try:
            msg = await (notice_client or app).send_message(chat.id, text, parse_mode=ParseMode.HTML)
            asyncio.create_task(_delete_later(msg, NOTICE_LIFETIME))
        except Exception:
            pass


async def on_started(client, message):
    mark_vc_start(message.chat.id)
    by = message.from_user.mention if message.from_user else None
    await log_event("started", message.chat, by=by, notice_client=client)


async def on_ended(client, message):
    dur = getattr(message.video_chat_ended, "duration", None)
    age = vc_age(message.chat.id)
    by = message.from_user.mention if message.from_user else None
    await log_event("ended", message.chat, by=by, duration=dur if dur is not None else age, notice_client=client)


async def on_invited(client, message):
    try:
        users = ", ".join(u.mention for u in message.video_chat_members_invited.users[:10])
    except Exception:
        users = None
    by = message.from_user.mention if message.from_user else None
    await log_event("invited", message.chat, by=by, users=users, notice_client=client)


# ------------------------------------------------------------------ assistant session (called from call.py)
def session_start(chat_id) -> bool:
    """True only for the first call of a new session."""
    chat_id = int(chat_id)
    if chat_id in _session:
        return False
    _session[chat_id] = {"since": time.time(), "tracks": 1}
    return True


def session_track(chat_id):
    s = _session.get(int(chat_id))
    if s:
        s["tracks"] += 1


def session_end(chat_id):
    return _session.pop(int(chat_id), None)


async def _chat_obj(chat_id):
    from RoohiClone import app
    try:
        return await app.get_chat(int(chat_id))
    except Exception:
        class _C:  # minimal stand-in when the chat can't be fetched
            id = int(chat_id)
            title = "Unknown"
            username = None
        return _C()


async def assistant_joined(chat_id):
    try:
        await log_event("joined", await _chat_obj(chat_id), notify_chat=False)
    except Exception as e:
        LOGGER(__name__).warning(f"vc join log failed: {e}")


async def assistant_left(chat_id, session):
    try:
        await log_event("left", await _chat_obj(chat_id), duration=time.time() - session["since"],
                        tracks=session["tracks"], notify_chat=False)
    except Exception as e:
        LOGGER(__name__).warning(f"vc leave log failed: {e}")


def mark_vc_start(chat_id):
    _vc_open[int(chat_id)] = time.time()
    if len(_vc_open) > 5000:
        _vc_open.clear()


def vc_age(chat_id):
    t = _vc_open.pop(int(chat_id), None)
    return None if t is None else time.time() - t
