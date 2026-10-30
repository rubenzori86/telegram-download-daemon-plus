# -*- coding: utf-8 -*-
import os

from telethon.tl.types import PeerChannel


class DownloadMedia:

    def __init__(self, channel, message_id=None, destination='.'):
        self.channel = int(channel)
        self.message_id = int(message_id) if message_id is not None else None
        self.destination = destination

    async def get_message(self, client):
        if self.message_id is None:
            raise ValueError("message_id cannot be None")
        return await client.get_messages(PeerChannel(self.channel), 1, ids=self.message_id)

    def get_link(self):
        if self.message_id is None:
            return f"https://t.me/c/{self.channel}"
        return f"https://t.me/c/{self.channel}/{self.message_id}"