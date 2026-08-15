import asyncio
import logging
from typing import Callable, Awaitable

from libfin.network.protocol import Iso8583Protocol

LOGGER = logging.getLogger(__name__)

MessageHandler = Callable[[bytes], Awaitable[bytes]]


class Iso8583Server:
    """
    Asyncio TCP server for receiving and responding to ISO8583 messages.
    """
    def __init__(self, host: str, port: int, handler: MessageHandler, length_header_size: int = 2):
        self.host = host
        self.port = port
        self.handler = handler
        self.protocol = Iso8583Protocol(length_header_size)
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(self.handle_client, self.host, self.port)
        LOGGER.info(f"Server started at {self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            LOGGER.info("Server stopped.")

    async def serve_forever(self):
        if not self._server:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info('peername')
        LOGGER.info(f"Client connected: {addr}")

        try:
            while True:
                msg_bytes = await self.protocol.read_message(reader)
                if msg_bytes is None:
                    break

                LOGGER.debug(f"Received message from {addr}: len={len(msg_bytes)}")

                # Dispatch to handler (concurrently or sequentially depending on handler)
                # To avoid blocking other messages on the same connection, we can use create_task
                # but for strict ordering, we await it directly.
                try:
                    response_bytes = await self.handler(msg_bytes)
                    if response_bytes:
                        out_msg = self.protocol.pack_message(response_bytes)
                        writer.write(out_msg)
                        await writer.drain()
                except Exception as e:
                    LOGGER.exception(f"Error handling message from {addr}: {e}")

        finally:
            LOGGER.info(f"Client disconnected: {addr}")
            writer.close()
            await writer.wait_closed()
