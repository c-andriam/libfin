"""
Tests for the network protocol, server, and client modules.
"""
import asyncio
import struct
import pytest
import pytest_asyncio

from libfin.network.protocol import Iso8583Protocol
from libfin.network.server import Iso8583Server
from libfin.network.client import Iso8583Client
from libfin import iso8583


# ── Protocol Tests ──

class TestIso8583Protocol:
    """Tests for the low-level framing protocol."""

    def test_pack_message_2byte_header(self):
        """Pack a message with a 2-byte length header."""
        proto = Iso8583Protocol(length_header_size=2)
        payload = b'HELLO'
        packed = proto.pack_message(payload)
        assert packed == struct.pack("!H", 5) + b'HELLO'

    def test_pack_message_4byte_header(self):
        """Pack a message with a 4-byte length header."""
        proto = Iso8583Protocol(length_header_size=4)
        payload = b'HELLO'
        packed = proto.pack_message(payload)
        assert packed == struct.pack("!I", 5) + b'HELLO'

    @pytest.mark.asyncio
    async def test_read_message_2byte_header(self):
        """Read a framed message from a stream with 2-byte header."""
        proto = Iso8583Protocol(length_header_size=2)
        payload = b'TEST_DATA_12345'
        framed = proto.pack_message(payload)

        reader = asyncio.StreamReader()
        reader.feed_data(framed)
        reader.feed_eof()

        result = await proto.read_message(reader)
        assert result == payload

    @pytest.mark.asyncio
    async def test_read_message_4byte_header(self):
        """Read a framed message from a stream with 4-byte header."""
        proto = Iso8583Protocol(length_header_size=4)
        payload = b'TEST_DATA_67890'
        framed = proto.pack_message(payload)

        reader = asyncio.StreamReader()
        reader.feed_data(framed)
        reader.feed_eof()

        result = await proto.read_message(reader)
        assert result == payload

    @pytest.mark.asyncio
    async def test_read_message_eof_returns_none(self):
        """If the stream is closed before a header, return None."""
        proto = Iso8583Protocol(length_header_size=2)
        reader = asyncio.StreamReader()
        reader.feed_eof()

        result = await proto.read_message(reader)
        assert result is None

    @pytest.mark.asyncio
    async def test_read_multiple_messages(self):
        """Read multiple consecutive framed messages from a stream."""
        proto = Iso8583Protocol(length_header_size=2)
        messages = [b'MSG_ONE', b'MSG_TWO', b'MSG_THREE']

        reader = asyncio.StreamReader()
        for msg in messages:
            reader.feed_data(proto.pack_message(msg))
        reader.feed_eof()

        results = []
        for _ in messages:
            result = await proto.read_message(reader)
            results.append(result)
        assert results == messages

    def test_unsupported_header_size_raises(self):
        """Unsupported header sizes should raise ValueError."""
        proto = Iso8583Protocol(length_header_size=3)
        with pytest.raises(ValueError):
            proto.pack_message(b'DATA')


# ── Server + Client Integration Tests ──

class TestServerClientIntegration:
    """End-to-end tests: server receives ISO8583, processes it, client gets response."""

    @pytest.mark.asyncio
    async def test_echo_server(self):
        """Server echoes back whatever it receives."""
        async def echo_handler(msg: bytes) -> bytes:
            return msg

        server = Iso8583Server('127.0.0.1', 0, echo_handler)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            proto = Iso8583Protocol()

            payload = b'ECHO_TEST_PAYLOAD'
            writer.write(proto.pack_message(payload))
            await writer.drain()

            response = await proto.read_message(reader)
            assert response == payload

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_iso8583_roundtrip(self):
        """
        Full ISO8583 roundtrip: client sends a 0200 authorization request,
        server parses it, builds a 0210 response with code '00', client receives it.
        """
        async def auth_handler(msg: bytes) -> bytes:
            msg_dict = iso8583.loads(msg)
            assert msg_dict['MTI'] == '0200'
            response = {
                'MTI': '0210',
                'DE39': '00',
            }
            return iso8583.dumps(response)

        server = Iso8583Server('127.0.0.1', 0, auth_handler)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            proto = Iso8583Protocol()

            request = iso8583.dumps({'MTI': '0200', 'DE2': '4444555566667777'})
            writer.write(proto.pack_message(request))
            await writer.drain()

            response_bytes = await asyncio.wait_for(proto.read_message(reader), timeout=5.0)
            response_dict = iso8583.loads(response_bytes)

            assert response_dict['MTI'] == '0210'
            assert response_dict['DE39'] == '00'

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_multiple_concurrent_messages(self):
        """
        Send 50 messages concurrently and verify all responses are received
        without mixing or losing any transaction.
        """
        async def numbered_handler(msg: bytes) -> bytes:
            # Echo back the same message
            return msg

        server = Iso8583Server('127.0.0.1', 0, numbered_handler)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]

        num_messages = 50
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            proto = Iso8583Protocol()

            # Send all messages
            sent_payloads = []
            for i in range(num_messages):
                payload = f'MSG_{i:04d}'.encode()
                sent_payloads.append(payload)
                writer.write(proto.pack_message(payload))
            await writer.drain()

            # Receive all responses
            received_payloads = []
            for _ in range(num_messages):
                resp = await asyncio.wait_for(proto.read_message(reader), timeout=5.0)
                assert resp is not None
                received_payloads.append(resp)

            assert sent_payloads == received_payloads

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_client_disconnect_graceful(self):
        """Server handles client disconnection without crashing."""
        handler_called = asyncio.Event()

        async def slow_handler(msg: bytes) -> bytes:
            handler_called.set()
            return msg

        server = Iso8583Server('127.0.0.1', 0, slow_handler)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            proto = Iso8583Protocol()

            writer.write(proto.pack_message(b'HELLO'))
            await writer.drain()

            await asyncio.wait_for(handler_called.wait(), timeout=5.0)

            # Close abruptly
            writer.close()
            await writer.wait_closed()

            # Give server a moment to clean up
            await asyncio.sleep(0.1)

            # Server should still be running and accept new connections
            reader2, writer2 = await asyncio.open_connection('127.0.0.1', port)
            writer2.write(proto.pack_message(b'AFTER_DISCONNECT'))
            await writer2.drain()
            resp = await asyncio.wait_for(proto.read_message(reader2), timeout=5.0)
            assert resp == b'AFTER_DISCONNECT'

            writer2.close()
            await writer2.wait_closed()
        finally:
            await server.stop()
