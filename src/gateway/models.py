import enum
from datetime import datetime, timezone

from sqlalchemy import Column, String, Enum, Integer, DateTime, Numeric, Index
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TransactionStatus(enum.Enum):
    PENDING = "PENDING"
    FIAT_APPROVED = "FIAT_APPROVED"
    FIAT_DECLINED = "FIAT_DECLINED"
    CRYPTO_SENT = "CRYPTO_SENT"
    CRYPTO_FAILED = "CRYPTO_FAILED"
    REVERSED = "REVERSED"
    #: Fiat was captured, crypto failed and the reversal was refused by the
    #: bank. Money is owed to the cardholder: this state must page a human.
    REVERSAL_FAILED = "REVERSAL_FAILED"
    #: The acquirer never answered the 0200. We do not know whether the
    #: cardholder was debited, so a reversal is sent and the row is flagged.
    FIAT_UNKNOWN = "FIAT_UNKNOWN"


#: States that mean money left the cardholder's account but the customer has
#: not been served yet. The reconciliation job watches these.
UNSETTLED_STATES = (
    TransactionStatus.FIAT_APPROVED,
    TransactionStatus.FIAT_UNKNOWN,
)

#: States that require a human to move money by hand.
MANUAL_INTERVENTION_STATES = (
    TransactionStatus.REVERSAL_FAILED,
    TransactionStatus.FIAT_UNKNOWN,
)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    amount = Column(Numeric(precision=18, scale=2), nullable=False)
    currency = Column(String(3), nullable=False, default="840")
    masked_pan = Column(String(19), nullable=False)
    target_wallet = Column(String(42), nullable=False)

    idempotency_key = Column(String(64), unique=True, nullable=True)
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING, nullable=False)

    stan = Column(String(6), nullable=True)
    rrn = Column(String(12), nullable=True)  # DE37, the acquirer's handle on the auth
    auth_code = Column(String(6), nullable=True)  # DE38
    action_code = Column(String(3), nullable=True)  # DE39

    crypto_tx_hash = Column(String(66), nullable=True)
    #: Token amount actually sent, in the ERC-20's smallest unit.
    crypto_amount_units = Column(Numeric(precision=38, scale=0), nullable=True)
    #: Nonce the transfer was signed with. Replacing a transaction stuck in the
    #: mempool requires rebroadcasting at the same nonce, and the process that
    #: chose it is usually long gone by then.
    crypto_nonce = Column(Integer, nullable=True)
    #: How many times this transfer has been re-priced and rebroadcast.
    crypto_replacements = Column(Integer, nullable=False, default=0)

    reversal_stan = Column(String(6), nullable=True)
    reversal_attempts = Column(Integer, nullable=False, default=0)

    error_message = Column(String(512), nullable=True)

    __table_args__ = (
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_stan", "stan"),
        Index("ix_transactions_created_at", "created_at"),
        Index("ix_transactions_crypto_tx_hash", "crypto_tx_hash"),
        # The reconciliation job scans by (status, created_at) together.
        Index("ix_transactions_status_created_at", "status", "created_at"),
    )

    def mark_completed(self, status: TransactionStatus) -> None:
        self.status = status
        self.completed_at = utcnow()
