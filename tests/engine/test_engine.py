"""
Tests for the engine module: TransactionContext and Iso8583Router.
"""
import asyncio
import pytest
import pytest_asyncio

from libfin.engine.context import TransactionContext
from libfin.engine.router import Iso8583Router


# ── TransactionContext Tests ──

class TestTransactionContext:

    def test_create_context_from_dict(self):
        msg = {'MTI': '0200', 'DE2': '4444555566667777', 'DE11': 123456}
        ctx = TransactionContext(msg)
        assert ctx.mti == '0200'
        assert ctx.request['DE2'] == '4444555566667777'
        assert ctx.response is None

    def test_create_response_mti_conversion(self):
        ctx = TransactionContext({'MTI': '0200', 'DE11': 123456, 'DE37': 'RRN123456789'})
        resp = ctx.create_response()
        assert resp['MTI'] == '0210'
        assert resp['DE11'] == 123456
        assert resp['DE37'] == 'RRN123456789'

    def test_create_response_0800_to_0810(self):
        ctx = TransactionContext({'MTI': '0800', 'DE11': 1})
        resp = ctx.create_response()
        assert resp['MTI'] == '0810'

    def test_create_response_0400_to_0410(self):
        ctx = TransactionContext({'MTI': '0400', 'DE11': 99})
        resp = ctx.create_response()
        assert resp['MTI'] == '0410'

    def test_set_response_code(self):
        ctx = TransactionContext({'MTI': '0200'})
        ctx.create_response()
        ctx.set_response_code('00')
        assert ctx.response['DE39'] == '00'

    def test_set_response_code_auto_creates_response(self):
        ctx = TransactionContext({'MTI': '0200'})
        ctx.set_response_code('05')
        assert ctx.response is not None
        assert ctx.response['DE39'] == '05'


# ── Iso8583Router Tests ──

class TestIso8583Router:

    @pytest.mark.asyncio
    async def test_route_by_mti(self):
        router = Iso8583Router()
        handled = {}

        @router.on_mti('0200')
        async def handle_0200(ctx: TransactionContext):
            handled['0200'] = True
            ctx.create_response()
            ctx.set_response_code('00')

        @router.on_mti('0800')
        async def handle_0800(ctx: TransactionContext):
            handled['0800'] = True
            ctx.create_response()
            ctx.set_response_code('00')

        ctx_200 = TransactionContext({'MTI': '0200', 'DE11': 1})
        await router.dispatch(ctx_200)
        assert handled.get('0200') is True
        assert ctx_200.response['MTI'] == '0210'
        assert ctx_200.response['DE39'] == '00'

        ctx_800 = TransactionContext({'MTI': '0800', 'DE11': 2})
        await router.dispatch(ctx_800)
        assert handled.get('0800') is True

    @pytest.mark.asyncio
    async def test_route_by_processing_code(self):
        router = Iso8583Router()
        handled = {}

        @router.on_processing_code('000000', mti='0200')
        async def handle_purchase(ctx: TransactionContext):
            handled['purchase'] = True
            ctx.create_response()
            ctx.set_response_code('00')

        @router.on_processing_code('010000', mti='0200')
        async def handle_cash_advance(ctx: TransactionContext):
            handled['cash_advance'] = True
            ctx.create_response()
            ctx.set_response_code('00')

        ctx = TransactionContext({'MTI': '0200', 'DE3': '000000'})
        await router.dispatch(ctx)
        assert handled.get('purchase') is True
        assert handled.get('cash_advance') is None

    @pytest.mark.asyncio
    async def test_default_handler(self):
        router = Iso8583Router()

        @router.on_default
        async def fallback(ctx: TransactionContext):
            ctx.create_response()
            ctx.set_response_code('96')  # System malfunction

        ctx = TransactionContext({'MTI': '9999'})
        await router.dispatch(ctx)
        assert ctx.response['DE39'] == '96'

    @pytest.mark.asyncio
    async def test_no_handler_no_crash(self):
        router = Iso8583Router()
        ctx = TransactionContext({'MTI': '0200'})
        await router.dispatch(ctx)
        assert ctx.response is None

    @pytest.mark.asyncio
    async def test_middleware_chain(self):
        router = Iso8583Router()
        call_order = []

        @router.use
        async def middleware_1(ctx, next_handler):
            call_order.append('mw1_before')
            await next_handler(ctx)
            call_order.append('mw1_after')

        @router.use
        async def middleware_2(ctx, next_handler):
            call_order.append('mw2_before')
            await next_handler(ctx)
            call_order.append('mw2_after')

        @router.on_mti('0200')
        async def handler(ctx: TransactionContext):
            call_order.append('handler')
            ctx.create_response()
            ctx.set_response_code('00')

        ctx = TransactionContext({'MTI': '0200'})
        await router.dispatch(ctx)

        assert call_order == ['mw1_before', 'mw2_before', 'handler', 'mw2_after', 'mw1_after']

    @pytest.mark.asyncio
    async def test_handle_raw_message_roundtrip(self):
        """Test the full pipeline: raw bytes in → parsed → routed → response bytes out."""
        from libfin import iso8583

        router = Iso8583Router()

        @router.on_mti('0200')
        async def approve(ctx: TransactionContext):
            ctx.create_response()
            ctx.set_response_code('00')

        request_dict = {'MTI': '0200', 'DE2': '4444555566667777'}
        raw_request = iso8583.dumps(request_dict)

        raw_response = await router.handle_raw_message(raw_request)
        assert raw_response is not None

        response_dict = iso8583.loads(raw_response)
        assert response_dict['MTI'] == '0210'
        assert response_dict['DE39'] == '00'
