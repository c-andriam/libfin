import logging
from typing import Callable, Awaitable, Optional, List

from libfin import iso8583
from libfin.engine.context import TransactionContext

LOGGER = logging.getLogger(__name__)

# Type aliases
HandlerFunc = Callable[[TransactionContext], Awaitable[None]]
MiddlewareFunc = Callable[[TransactionContext, HandlerFunc], Awaitable[None]]


class Route:
    """
    A single routing rule that maps a condition to a handler.
    """
    def __init__(self, mti: Optional[str] = None,
                 processing_code: Optional[str] = None,
                 handler: HandlerFunc = None):
        self.mti = mti
        self.processing_code = processing_code
        self.handler = handler

    def matches(self, ctx: TransactionContext) -> bool:
        if self.mti and ctx.request.get('MTI') != self.mti:
            return False
        if self.processing_code and ctx.request.get('DE3') != self.processing_code:
            return False
        return True


class Iso8583Router:
    """
    Routes ISO8583 messages to the appropriate handler based on MTI,
    processing code, or custom conditions. Supports middleware chains.

    Usage::

        router = Iso8583Router()

        @router.on_mti('0200')
        async def handle_authorization(ctx: TransactionContext):
            ctx.create_response()
            ctx.set_response_code('00')  # Approved

        @router.on_mti('0800')
        async def handle_network_management(ctx: TransactionContext):
            ctx.create_response()
            ctx.set_response_code('00')

    """
    def __init__(self, encoding: str = None, iso_config: dict = None):
        self.routes: List[Route] = []
        self.middlewares: List[MiddlewareFunc] = []
        self.default_handler: Optional[HandlerFunc] = None
        self.encoding = encoding
        self.iso_config = iso_config

    # ── Decorator-based registration ──

    def on_mti(self, mti: str):
        """
        Decorator to register a handler for a specific MTI.

        Usage::

            @router.on_mti('0200')
            async def handle_auth(ctx):
                ...
        """
        def decorator(func: HandlerFunc):
            self.routes.append(Route(mti=mti, handler=func))
            return func
        return decorator

    def on_processing_code(self, processing_code: str, mti: str = None):
        """
        Decorator to register a handler for a specific processing code (DE3),
        optionally scoped to a specific MTI.

        Usage::

            @router.on_processing_code('000000', mti='0200')
            async def handle_purchase(ctx):
                ...
        """
        def decorator(func: HandlerFunc):
            self.routes.append(Route(mti=mti, processing_code=processing_code, handler=func))
            return func
        return decorator

    def on_default(self, func: HandlerFunc):
        """
        Decorator to register a default (fallback) handler for unmatched messages.

        Usage::

            @router.on_default
            async def fallback(ctx):
                ctx.create_response()
                ctx.set_response_code('96')  # System malfunction
        """
        self.default_handler = func
        return func

    def use(self, middleware: MiddlewareFunc):
        """
        Register a middleware function. Middlewares are executed in order
        before the matched handler, forming a chain.

        A middleware signature is::

            async def my_middleware(ctx: TransactionContext, next_handler):
                # Pre-processing
                await next_handler(ctx)
                # Post-processing

        Usage::

            @router.use
            async def log_transaction(ctx, next_handler):
                LOGGER.info(f"Incoming MTI={ctx.mti}")
                await next_handler(ctx)
                LOGGER.info(f"Response DE39={ctx.response.get('DE39')}")
        """
        self.middlewares.append(middleware)
        return middleware

    # ── Core dispatch ──

    def _find_handler(self, ctx: TransactionContext) -> Optional[HandlerFunc]:
        """
        Finds the first matching handler for the given context.
        More specific routes (with both mti + processing_code) are checked first.
        """
        # Sort routes: more specific first (those with both mti and processing_code)
        sorted_routes = sorted(self.routes,
                               key=lambda r: (r.mti is not None) + (r.processing_code is not None),
                               reverse=True)
        for route in sorted_routes:
            if route.matches(ctx):
                return route.handler
        return self.default_handler

    async def dispatch(self, ctx: TransactionContext):
        """
        Dispatches a TransactionContext through the middleware chain
        then to the matched handler.
        """
        handler = self._find_handler(ctx)
        if handler is None:
            LOGGER.warning(f"No handler found for MTI={ctx.mti}")
            return

        # Build the middleware chain (inside-out)
        async def final_handler(c: TransactionContext):
            await handler(c)

        chain = final_handler
        for mw in reversed(self.middlewares):
            prev_chain = chain

            async def make_chain(middleware=mw, next_h=prev_chain):
                async def chained(c: TransactionContext):
                    await middleware(c, next_h)
                return chained
            chain = await make_chain()

        await chain(ctx)

    async def handle_raw_message(self, raw_message: bytes) -> Optional[bytes]:
        """
        Full pipeline: parse raw bytes → route → build response → serialize.
        This method is designed to be used as the handler for Iso8583Server.

        :param raw_message: raw ISO8583 message bytes
        :return: serialized response bytes, or None if no response
        """
        try:
            message_dict = iso8583.loads(raw_message, encoding=self.encoding, iso_config=self.iso_config)
        except Exception as e:
            LOGGER.error(f"Failed to parse incoming ISO8583 message: {e}")
            return None

        ctx = TransactionContext(message_dict)
        await self.dispatch(ctx)

        if ctx.response:
            try:
                return iso8583.dumps(ctx.response, encoding=self.encoding, iso_config=self.iso_config)
            except Exception as e:
                LOGGER.error(f"Failed to serialize response: {e}")
                return None
        return None
