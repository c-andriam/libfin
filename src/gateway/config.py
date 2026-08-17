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
        #: Bitmap encoding on the wire. ISO 8583 permits both; which one your
        #: acquirer expects is part of their dialect, and getting it wrong means
        #: every message is rejected as unparseable. Cross-checking libfin
        #: against an independent implementation is what surfaced this as a
        #: decision rather than a default nobody had looked at.
        self.acquirer_hex_bitmap: bool = _env_bool("ACQUIRER_HEX_BITMAP", False)
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
        self.exchange_rate: Decimal = _env_decimal("EXCHANGE_RATE", "1.0")

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
        self.stale_transaction_minutes: int = _env_int("STALE_TRANSACTION_MINUTES", 15)
        # create_all is convenient in simulation; production uses migrations.
        self.auto_create_schema: bool = _env_bool("AUTO_CREATE_SCHEMA", not self.is_production)
        #: Permits a production-mode run against a simulated acquirer, for
        #: rehearsals. There is no public ISO 8583 test host to point at, so the
        #: alternative would be never exercising the production configuration at
        #: all. scripts/preflight_check.sh refuses this outright, so it cannot
        #: reach a system that serves real cardholders.
        self.allow_simulated_acquirer: bool = _env_bool("ALLOW_SIMULATED_ACQUIRER", False)

    # ── Derived properties ──────────────────────────────────────────────────

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
        if any(marker in url for url in self.web3_rpc_urls for marker in ("anvil", "hardhat")):
            problems.append("WEB3_RPC_URL points at a local test chain.")
        if self.web3_chain_id in (31337, 1337):
            problems.append(f"WEB3_CHAIN_ID={self.web3_chain_id} is a local test chain.")

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
