from pyrogram import Client, filters

from RoohiClone.utils import vclog
from config import BANNED_USERS


@Client.on_message(filters.command(["vclogger", "vclog"]) & filters.group & ~BANNED_USERS)
async def clone_vclogger_cmd(client, message):
    await vclog.handle_command(client, message)


@Client.on_message(filters.video_chat_ended, group=41)
async def clone_vc_ended(client, message):
    await vclog.on_ended(client, message)


@Client.on_message(filters.video_chat_members_invited, group=42)
async def clone_vc_invited(client, message):
    await vclog.on_invited(client, message)


@Client.on_message(filters.video_chat_started, group=22)
async def clone_vc_started_log(client, message):
    await vclog.on_started(client, message)
