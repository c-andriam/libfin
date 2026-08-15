import asyncio
import struct
import logging
from typing import Optional

LOGGER = logging.getLogger(__name__)


class Iso8583Protocol:
    """
    Handles TCP framing for ISO8583 messages.
    Supports a configurable byte length header (usually 2 or 4 bytes).
    """
    def __init__(self, length_header_size: int = 2):
        self.length_header_size = length_header_size

    async def read_message(self, reader: asyncio.StreamReader) -> Optional[bytes]:
        """
        Reads a single framed message from the reader.
        Returns the raw message bytes, or None if connection is closed.
        """
        try:
            # 1. Read the length header
            header_bytes = await reader.readexactly(self.length_header_size)
        except asyncio.IncompleteReadError:
            # Connection closed gracefully
            return None
        except ConnectionError as e:
            LOGGER.error(f"Connection error while reading header: {e}")
            return None

        if self.length_header_size == 2:
            msg_length = struct.unpack("!H", header_bytes)[0]
        elif self.length_header_size == 4:
            msg_length = struct.unpack("!I", header_bytes)[0]
        else:
            raise ValueError(f"Unsupported length header size: {self.length_header_size}")

        try:
            # 2. Read the exact message length
            msg_bytes = await reader.readexactly(msg_length)
            return msg_bytes
        except asyncio.IncompleteReadError as e:
            LOGGER.error(f"Incomplete read: expected {msg_length} bytes, got {len(e.partial)}")
            return None

    def pack_message(self, msg_bytes: bytes) -> bytes:
        """
        Packs a raw message with its length header.
        """
        msg_length = len(msg_bytes)
        if self.length_header_size == 2:
            header = struct.pack("!H", msg_length)
        elif self.length_header_size == 4:
            header = struct.pack("!I", msg_length)
        else:
            raise ValueError(f"Unsupported length header size: {self.length_header_size}")

        return header + msg_bytes
