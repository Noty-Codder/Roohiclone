from pyrogram import filters

from RoohiClone import app
from RoohiClone.utils import vclog
from config import BANNED_USERS


@app.on_message(filters.command(["vclogger", "vclog"]) & filters.group & ~BANNED_USERS)
async def vclogger_cmd(client, message):
    await vclog.handle_command(client, message)


@app.on_message(filters.video_chat_started, group=40)
async def vc_started(client, message):
    await vclog.on_started(client, message)


@app.on_message(filters.video_chat_ended, group=41)
async def vc_ended(client, message):
    await vclog.on_ended(client, message)


@app.on_message(filters.video_chat_members_invited, group=42)
async def vc_invited(client, message):
    await vclog.on_invited(client, message)
