"""
Centralised gateway configuration.

Every tunable of the gateway is declared here exactly once, so that:

  * the same code path runs in simulation and in production — only the values
    change (bank host, RPC url, credentials). There is no ``if simulation:``
    branch in the business logic, which is what makes simulation runs
    meaningful as a rehearsal for production;
  * ``scripts/preflight_check.sh`` and ``/health/ready`` can enumerate what is
    required before a production launch;
  * a production process refuses to start with simulation leftovers.
"""

import logging
import os
from decimal import Decimal
from typing import List, Optional

LOGGER = logging.getLogger(__name__)

MODE_SIMULATION = "simulation"
MODE_PRODUCTION = "production"

#: Value used in ``.env.prod.example`` for every field an operator must fill in.
PLACEHOLDER_PREFIX = "REPLACE_ME"


class ConfigError(RuntimeError):
    """Raised when the configuration cannot support the requested mode."""


def _env(name: str, default: str = "") -> str:
    # env_file loaders do not strip trailing comments, and a stray comment in a
    # value silently corrupts addresses and URLs. Strip defensively.
    raw = os.environ.get(name, default)
    return raw.split("#", 1)[0].strip() if raw else raw


def _env_int(name: str, default: int) -> int:
    value = _env(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}")


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def _env_decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(_env(name, default))
    except Exception:
        raise ConfigError(f"{name} must be a decimal number")


class Settings:
    """Immutable snapshot of the environment, validated for the running mode."""

    def __init__(self) -> None:
        # ── Mode ────────────────────────────────────────────────────────────
        # ENVIRONMENT stays for backwards compatibility with the dev compose
        # file; GATEWAY_MODE is the explicit switch.
        environment = _env("ENVIRONMENT", "development").lower()
        self.environment: str = environment
        self.mode: str = _env(
            "GATEWAY_MODE",
            MODE_PRODUCTION if environment == "production" else MODE_SIMULATION,
        ).lower()
        if self.mode not in (MODE_SIMULATION, MODE_PRODUCTION):
            raise ConfigError(f"GATEWAY_MODE must be 'simulation' or 'production', got {self.mode!r}")

        # ── Infrastructure ──────────────────────────────────────────────────
        self.database_url: str = _env("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        self.database_ssl: bool = _env_bool("DATABASE_SSL", self.is_production)
        #: How far to trust the database's certificate.
        #:   disable      no TLS
        #:   require      encrypt, do not verify the certificate
        #:   verify-full  encrypt and verify against DATABASE_SSL_CA_FILE
        #: `ssl: True` in asyncpg means verify-full, which fails against the
        #: self-signed certificate the bundled Postgres uses — so the mode has
        #: to be a decision, not a side effect.
        self.database_ssl_mode: str = _env(
            "DATABASE_SSL_MODE", "require" if self.database_ssl else "disable"
        ).lower()
        self.database_ssl_ca_file: str = _env("DATABASE_SSL_CA_FILE")
        # Applied in every mode, so a load test in simulation exercises the
        # pool production will actually run. Postgres' own max_connections must
        # exceed (pool_size + max_overflow) x number of API workers.
        self.db_pool_size: int = _env_int("DB_POOL_SIZE", 10)
        self.db_max_overflow: int = _env_int("DB_MAX_OVERFLOW", 10)
        self.db_pool_timeout: int = _env_int("DB_POOL_TIMEOUT", 10)
        self.redis_url: str = _env("REDIS_URL", "redis://localhost:6379/0")
        self.celery_broker_url: str = _env("CELERY_BROKER_URL", self.redis_url)
        self.vault_addr: str = _env("VAULT_ADDR")
        self.vault_token: str = _env("VAULT_TOKEN")

        # ── Bank acquirer (ISO 8583) ────────────────────────────────────────
        self.bank_host: str = _env("BANK_HOST", "127.0.0.1")
        self.bank_port: int = _env_int("BANK_PORT", 9000)
        self.bank_use_tls: bool = _env_bool("BANK_USE_TLS", False)
        self.bank_tls_insecure: bool = _env_bool("BANK_TLS_INSECURE", False)
        self.bank_tls_ca_file: str = _env("BANK_TLS_CA_FILE")
        self.bank_tls_client_cert: str = _env("BANK_TLS_CLIENT_CERT")
        self.bank_tls_client_key: str = _env("BANK_TLS_CLIENT_KEY")
        self.bank_timeout_sec: float = float(_env("BANK_TIMEOUT_SEC", "10"))
        self.bank_echo_interval_sec: int = _env_int("BANK_ECHO_INTERVAL_SEC", 60)

        # Card acceptor identification. Real acquirers reject a 0200 that does
        # not carry these, so they are required in production.
        self.acquirer_terminal_id: str = _env("ACQUIRER_TERMINAL_ID")
        self.acquirer_merchant_id: str = _env("ACQUIRER_MERCHANT_ID")
        self.acquirer_currency: str = _env("ACQUIRER_CURRENCY", "840")
        self.acquirer_country: str = _env("ACQUIRER_COUNTRY", "840")
        # Point of service data code: card-not-present / e-commerce by default.
        self.acquirer_pos_data: str = _env("ACQUIRER_POS_DATA", "810101Y00000")
        self.acquirer_processing_code: str = _env("ACQUIRER_PROCESSING_CODE", "000000")
        self.acquirer_send_cvv: bool = _env_bool("ACQUIRER_SEND_CVV", True)
        #: How money is taken.
        #:   auth_capture  0100 hold, deliver, then 0220 capture — a failed
        #:                 delivery releases a hold and owes nothing.
        #:   purchase      0200 takes the money up front; every failure after
        #:                 it needs a reversal, and a refused reversal leaves a
        #:                 cardholder out of pocket.
        #: auth_capture is the safer order and the default. Use purchase only
        #: if your acquirer cannot separate the two.
        self.acquirer_capture_mode: str = _env("ACQUIRER_CAPTURE_MODE", "auth_capture").lower()
        if self.acquirer_capture_mode not in ("auth_capture", "purchase"):
            raise ConfigError(
                f"ACQUIRER_CAPTURE_MODE must be 'auth_capture' or 'purchase', "
                f"got {self.acquirer_capture_mode!r}"
            )
        #: Bitmap encoding on the wire. ISO 8583 permits both; which one your
        #: acquirer expects is part of their dialect, and getting it wrong means
        #: every message is rejected as unparseable. Cross-checking libfin
        #: against an independent implementation is what surfaced this as a
        #: decision rather than a default nobody had looked at.
        self.acquirer_hex_bitmap: bool = _env_bool("ACQUIRER_HEX_BITMAP", False)
        #: Which ISO 8583 variant the acquirer's host speaks. Sets field
        #: lengths, character encoding and bitmap form together — they are not
        #: independent choices. See src/gateway/iso_dialect.py for the sources
        #: each profile was checked against.
        self.acquirer_dialect: str = _env("ACQUIRER_DIALECT", "iso87").lower()
        #: Acquiring institution identification code (DE32) and card acceptor
        #: name/location (DE43). Both mandatory; both assigned by the acquirer.
        self.acquirer_institution_id: str = _env("ACQUIRER_INSTITUTION_ID")
        #: DE43 components. Built positionally rather than configured as one
        #: forty-character blob: a blob that is off by a character puts the city
        #: in the middle of the merchant name, which nothing rejects and the
        #: cardholder reads on their statement.
        self.acquirer_name: str = _env("ACQUIRER_NAME")
        self.acquirer_city: str = _env("ACQUIRER_CITY")
        self.acquirer_state: str = _env("ACQUIRER_STATE")
        #: Two-letter country code for DE43. ACQUIRER_COUNTRY is the numeric
        #: ISO 4217-style code used elsewhere; these are not the same field.
        self.acquirer_country_alpha: str = _env("ACQUIRER_COUNTRY_ALPHA", "US")
        #: Point of service condition code (DE25). 59 identifies a transaction
        #: as electronic commerce, which drives interchange and liability.
        self.acquirer_pos_condition: str = _env("ACQUIRER_POS_CONDITION", "59")

        #: Retained for deployments that already build DE43 themselves.
        self.acquirer_name_location: str = _env("ACQUIRER_NAME_LOCATION")
        #: Merchant category code (DE18), mandatory. 6051 is the usual
        #: quasi-cash code for crypto purchases, but the acquirer assigns it.
        self.acquirer_merchant_category: str = _env("ACQUIRER_MERCHANT_CATEGORY", "6051")
        #: Message encoding. latin_1 for ASCII hosts, cp500 for EBCDIC ones
        #: (still common on mainframe-backed acquirers).
        self.acquirer_encoding: str = _env("ACQUIRER_ENCODING", "latin_1")

        # ── Web3 ────────────────────────────────────────────────────────────
        rpc_urls = [
            _env("WEB3_RPC_URL", "http://127.0.0.1:8545"),
            _env("WEB3_RPC_URL_BACKUP"),
        ]
        self.web3_rpc_urls: List[str] = [u for u in rpc_urls if u]
        self.web3_chain_id: Optional[int] = (
            _env_int("WEB3_CHAIN_ID", 0) or None
        )
        self.web3_confirmations: int = _env_int("WEB3_CONFIRMATIONS", 2)
        #: Absolute ceiling on maxFeePerGas, in gwei. Without one, a fee spike
        #: is paid in full and a small transfer can cost more than it moves.
        #: Exceeding it is treated as temporary: the transfer waits for calmer
        #: fees, and is refunded if they never come.
        self.web3_max_fee_gwei: Decimal = _env_decimal("WEB3_MAX_FEE_GWEI", "200")
        #: Multiplier applied when replacing a transaction stuck in the
        #: mempool. Below ~1.1 the network rejects the replacement outright.
        self.web3_replacement_multiplier: Decimal = _env_decimal(
            "WEB3_REPLACEMENT_MULTIPLIER", "1.25"
        )
        self.web3_receipt_timeout_sec: int = _env_int("WEB3_RECEIPT_TIMEOUT_SEC", 180)
        self.web3_confirmation_timeout_sec: int = _env_int("WEB3_CONFIRMATION_TIMEOUT_SEC", 600)
        self.web3_private_key: str = _env("WEB3_PRIVATE_KEY")
        self.erc20_token_address: str = _env(
            "ERC20_TOKEN_ADDRESS", "0xdAC17F958D2ee523a2206206994597C13D831ec7"
        )
        #: Where the rate comes from. "fixed" uses EXCHANGE_RATE below, which
        #: is correct only for a stablecoin at parity; "chainlink" reads the
        #: on-chain oracles over the Web3 connection already held.
        self.rate_source: str = _env("RATE_SOURCE", "fixed").lower()
        self.exchange_rate: Decimal = _env_decimal("EXCHANGE_RATE", "1.0")
        #: The pair to price the settlement token in, e.g. EUR/USD when taking
        #: euros and settling in a dollar stablecoin.
        #: Written FIAT/TOKEN — the currency taken, and the token delivered.
        #: EUR/USDT means euros in, USDT out.
        self.rate_pair: str = _env("RATE_PAIR", "USD/USDT")
        #: The currency oracles publish against. Both legs of a cross are read
        #: against it, so it has to be the one your feeds actually use.
        self.rate_settlement_currency: str = _env("RATE_SETTLEMENT_CURRENCY", "USD")
        #: Symbol of the token delivered. Used to build the rate pair for
        #: whichever currency the customer is paying in.
        self.rate_token_symbol: str = _env("RATE_TOKEN_SYMBOL", "USDT").upper()
        #: RPC used for reading price oracles, when it differs from the one
        #: transfers are signed against. Reading a feed costs nothing and
        #: changes nothing, so a simulation settling on a local chain can still
        #: price against the real oracles — which is the only way the live rate
        #: path gets exercised through the whole stack rather than by a script.
        self.rate_rpc_url: str = _env("RATE_RPC_URL")
        #: How far a second, independent source may disagree before the quote is
        #: refused. Two sources disagreeing means at least one is wrong and
        #: there is no way to tell which. Zero disables the check.
        self.rate_cross_check_tolerance: Decimal = _env_decimal("RATE_CROSS_CHECK_TOLERANCE", "0.02")
        #: Set to "none" to run on the oracle alone.
        self.rate_cross_check_source: str = _env("RATE_CROSS_CHECK_SOURCE", "coingecko").lower()
        #: How long a quote is reused. An oracle on a daily heartbeat does not
        #: need reading per request, and the off-chain check has a rate limit a
        #: busy gateway would exhaust in a minute.
        self.rate_cache_seconds: int = _env_int("RATE_CACHE_SECONDS", 30)
        #: Margin over mid-market. The gateway never quotes mid: every payment
        #: would then be a coin flip on which way the market moved between the
        #: quote and the transfer, with nothing to absorb the losing half.
        self.rate_spread: Decimal = _env_decimal("RATE_SPREAD", "0.01")
        #: A quote is refused if the rate moved more than this since the last
        #: one. A sharp market move and a broken oracle look identical from
        #: here, and only one of them is safe to deliver against.
        self.rate_max_movement: Decimal = _env_decimal("RATE_MAX_MOVEMENT", "0.10")
        #: Multiplier on a feed's heartbeat to decide staleness. Above 1 by
        #: design: a threshold *below* the heartbeat rejects readings that are
        #: working exactly as intended and takes the gateway down by itself.
        self.rate_staleness_margin: Decimal = _env_decimal("RATE_STALENESS_MARGIN", "1.5")
        #: Per-pair overrides, "EUR/USD:90000,USDT/USD:90000".
        self.rate_staleness_overrides: dict = {}
        for entry in _env("RATE_STALENESS_OVERRIDES").split(","):
            if ":" in entry:
                pair, seconds = entry.split(":", 1)
                try:
                    self.rate_staleness_overrides[pair.strip()] = int(seconds)
                except ValueError:
                    raise ConfigError(f"RATE_STALENESS_OVERRIDES entry is not a number: {entry!r}")
        #: Per-pair plausible ranges, "EUR/USD:0.5:2.0". Wide enough that
        #: ordinary volatility passes and a decimal-place error does not.
        self.rate_bounds: dict = {}
        for entry in _env("RATE_BOUNDS", "USDT/USD:0.5:1.5,EUR/USD:0.5:2.0,GBP/USD:0.5:2.5").split(","):
            parts = entry.split(":")
            if len(parts) == 3:
                try:
                    self.rate_bounds[parts[0].strip()] = (Decimal(parts[1]), Decimal(parts[2]))
                except Exception:
                    raise ConfigError(f"RATE_BOUNDS entry is malformed: {entry!r}")

        # ── Gateway security & limits ───────────────────────────────────────
        self.api_key: str = _env("GATEWAY_API_KEY")
        self.cors_origins: List[str] = [
            o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()
        ]
        self.rate_limit_per_minute: int = _env_int("RATE_LIMIT_PER_MINUTE", 5)
        #: Addresses or CIDR blocks whose ``X-Forwarded-For`` may be believed.
        #: "*" trusts any peer, which is only safe when nothing but your own
        #: proxy can reach the API port — otherwise a client forges the header
        #: and walks around the rate limit.
        self.trusted_proxies: List[str] = [
            p.strip() for p in _env("TRUSTED_PROXIES", "*").split(",") if p.strip()
        ]
        self.amount_min: Decimal = _env_decimal("AMOUNT_MIN", "1.00")
        self.amount_max: Decimal = _env_decimal("AMOUNT_MAX", "10000.00")

        # ── Behavioural safety switches ─────────────────────────────────────
        # When Redis is unreachable the circuit breaker cannot know the shared
        # state. In production we refuse the payment rather than debit fiat we
        # may not be able to honour on-chain.
        self.circuit_breaker_fail_closed: bool = _env_bool(
            "CIRCUIT_BREAKER_FAIL_CLOSED", self.is_production
        )
        self.circuit_breaker_threshold: int = _env_int("CIRCUIT_BREAKER_THRESHOLD", 3)
        self.circuit_breaker_recovery_sec: int = _env_int("CIRCUIT_BREAKER_RECOVERY_SEC", 30)
        # How long a PAN token stays available for a reversal.
        self.pan_token_ttl_sec: int = _env_int("PAN_TOKEN_TTL_SEC", 86400)
        self.reconciliation_interval_sec: int = _env_int("RECONCILIATION_INTERVAL_SEC", 900)
        #: How often the outbox relay runs. Short: an unpublished row is a
        #: payment that has been taken and not yet acted on.
        self.outbox_relay_interval_sec: int = _env_int("OUTBOX_RELAY_INTERVAL_SEC", 30)
        # ── Data retention ──────────────────────────────────────────────
        #: Days before the identifying fields are stripped while the financial
        #: record is kept. 90 is a common default; your regulator may differ.
        self.retention_redact_days: int = _env_int("RETENTION_REDACT_DAYS", 90)
        #: Days before the row is removed entirely. Must exceed the financial
        #: record-keeping period you are held to — often seven years.
        self.retention_delete_days: int = _env_int("RETENTION_DELETE_DAYS", 2555)
        self.retention_batch_size: int = _env_int("RETENTION_BATCH_SIZE", 1000)
        self.retention_interval_sec: int = _env_int("RETENTION_INTERVAL_SEC", 86400)

        self.log_level: str = _env("LOG_LEVEL", "INFO").upper()
        #: Structured JSON logs. These lines are meant to be queried — "every
        #: CRITICAL naming a manual refund in the last hour" has to be a filter,
        #: not a grep someone remembers to run.
        self.log_json: bool = _env_bool("LOG_JSON", True)
        self.stale_transaction_minutes: int = _env_int("STALE_TRANSACTION_MINUTES", 15)
        # create_all is convenient in simulation; production uses migrations.
        self.auto_create_schema: bool = _env_bool("AUTO_CREATE_SCHEMA", not self.is_production)
        #: Permits a production-mode run against a simulated acquirer, for
        #: rehearsals. There is no public ISO 8583 test host to point at, so the
        #: alternative would be never exercising the production configuration at
        #: all. scripts/preflight_check.sh refuses this outright, so it cannot
        #: reach a system that serves real cardholders.
        self.allow_simulated_acquirer: bool = _env_bool("ALLOW_SIMULATED_ACQUIRER", False)
        #: Permits a production-mode run against a local test chain. Separate
        #: from the acquirer hatch on purpose: rehearsing against a real chain
        #: with a simulated bank is a different proposition from simulating
        #: both, and conflating them would let one flag excuse the other.
        #: Refused outright by scripts/preflight_check.sh.
        self.allow_simulated_chain: bool = _env_bool("ALLOW_SIMULATED_CHAIN", False)

    # ── Derived properties ──────────────────────────────────────────────────

    def rate_bounds_for(self, pair: str):
        """Plausible range for a pair, or (None, None) when none is configured."""
        return self.rate_bounds.get(pair, (None, None))

    @property
    def uses_auth_capture(self) -> bool:
        return self.acquirer_capture_mode == "auth_capture"

    @property
    def is_production(self) -> bool:
        return self.mode == MODE_PRODUCTION

    @property
    def is_simulation(self) -> bool:
        return self.mode == MODE_SIMULATION

    @property
    def docs_enabled(self) -> bool:
        return not self.is_production

    # ── Validation ──────────────────────────────────────────────────────────

    def validate(self) -> List[str]:
        """Return the list of problems that block the current mode.

        Simulation is permissive on purpose: it must run with zero manual
        setup. Production demands every externally-supplied field.
        """
        problems: List[str] = []

        def require(value, name: str, hint: str = "") -> None:
            if not value or str(value).startswith(PLACEHOLDER_PREFIX):
                problems.append(f"{name} is missing or still a placeholder. {hint}".strip())

        if not self.is_production:
            return problems

        require(self.api_key, "GATEWAY_API_KEY", "Generate one with `openssl rand -hex 32`.")
        require(self.database_url, "DATABASE_URL")
        require(self.redis_url, "REDIS_URL")
        require(self.vault_addr, "VAULT_ADDR")
        require(self.vault_token, "VAULT_TOKEN")
        require(self.bank_host, "BANK_HOST", "Point it at your acquirer.")
        require(self.acquirer_terminal_id, "ACQUIRER_TERMINAL_ID", "Supplied by your acquirer.")
        require(self.acquirer_merchant_id, "ACQUIRER_MERCHANT_ID", "Supplied by your acquirer.")
        # Mandatory in an authorisation. A message without them is rejected by
        # the host, so refusing to start is the cheaper failure.
        require(
            self.acquirer_institution_id,
            "ACQUIRER_INSTITUTION_ID",
            "DE32, mandatory. Assigned by your acquirer.",
        )
        if not self.acquirer_name_location:
            require(self.acquirer_name, "ACQUIRER_NAME", "DE43, mandatory.")
            require(self.acquirer_city, "ACQUIRER_CITY", "DE43, mandatory.")
        require(
            self.acquirer_merchant_category,
            "ACQUIRER_MERCHANT_CATEGORY",
            "DE18, mandatory. Confirm the code with your acquirer.",
        )

        from gateway.iso_dialect import DIALECTS

        if self.acquirer_dialect not in DIALECTS:
            problems.append(
                f"ACQUIRER_DIALECT={self.acquirer_dialect!r} is not a known variant "
                f"({', '.join(sorted(DIALECTS))})."
            )

        for url in self.web3_rpc_urls:
            if PLACEHOLDER_PREFIX in url:
                problems.append("WEB3_RPC_URL still contains a placeholder API key.")

        if not self.web3_chain_id:
            problems.append("WEB3_CHAIN_ID must be set in production so a wrong RPC is detected.")

        if "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must not be '*' in production.")

        if not self.bank_use_tls:
            problems.append("BANK_USE_TLS must be true in production (card data in transit).")

        if self.bank_tls_insecure:
            problems.append("BANK_TLS_INSECURE must be false in production.")

        if self.web3_confirmations < 1:
            problems.append("WEB3_CONFIRMATIONS must be >= 1 in production.")

        if self.auto_create_schema:
            problems.append("AUTO_CREATE_SCHEMA must be false in production (use migrations).")

        # Guard against launching production against simulation infrastructure.
        sim_markers = ("bank-simulator", "anvil", "hardhat", "localhost", "127.0.0.1")
        if any(marker == self.bank_host for marker in sim_markers):
            if self.allow_simulated_acquirer:
                # A rehearsal: production everywhere except the bank, because no
                # public ISO 8583 test host exists to point at. The escape hatch
                # is deliberately narrow — it excuses this one check and nothing
                # else — and preflight refuses it outright, so it cannot survive
                # into a real launch.
                LOGGER.critical(
                    f"ALLOW_SIMULATED_ACQUIRER is set: talking to {self.bank_host}, "
                    "which is a simulator. No real card is being charged. This must "
                    "never be set on a system serving real cardholders."
                )
            else:
                problems.append(f"BANK_HOST={self.bank_host} is a simulation host.")
        local_chain = any(
            marker in url for url in self.web3_rpc_urls for marker in ("anvil", "hardhat")
        ) or self.web3_chain_id in (31337, 1337)

        if local_chain:
            if self.allow_simulated_chain:
                LOGGER.critical(
                    f"ALLOW_SIMULATED_CHAIN is set: settling on chain "
                    f"{self.web3_chain_id}, which is a local test network. No real "
                    "value is moving. This must never be set on a system serving "
                    "real cardholders."
                )
            else:
                if self.web3_chain_id in (31337, 1337):
                    problems.append(
                        f"WEB3_CHAIN_ID={self.web3_chain_id} is a local test chain."
                    )
                else:
                    problems.append("WEB3_RPC_URL points at a local test chain.")

        return problems

    def require_valid(self) -> None:
        problems = self.validate()
        if problems:
            raise ConfigError(
                "Configuration is not fit for production:\n  - " + "\n  - ".join(problems)
            )

    def summary(self) -> dict:
        """Non-secret view of the configuration, safe to log or expose."""
        return {
            "mode": self.mode,
            "environment": self.environment,
            "bank_host": self.bank_host,
            "bank_port": self.bank_port,
            "bank_tls": self.bank_use_tls,
            "chain_id": self.web3_chain_id,
            "confirmations": self.web3_confirmations,
            "rpc_count": len(self.web3_rpc_urls),
            "token": self.erc20_token_address,
        }


settings = Settings()
