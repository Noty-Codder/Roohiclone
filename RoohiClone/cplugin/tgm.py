from pyrogram import Client, filters

from RoohiClone.plugins.tools.telegraph import handle_tgm, handle_tgt
from config import BANNED_USERS


@Client.on_message(filters.command(["tgm", "telegraph"]) & ~BANNED_USERS)
async def clone_tgm(client, message):
    await handle_tgm(client, message)


@Client.on_message(filters.command(["tgt"]) & ~BANNED_USERS)
async def clone_tgt(client, message):
    await handle_tgt(client, message)
