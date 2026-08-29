import enum
from typing import Optional
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Enum, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TransactionStatus(enum.Enum):
    PENDING = "PENDING"

    # ── Authorise-then-capture ──────────────────────────────────────────────
    #: A hold is placed on the card; no money has moved. The gateway delivers
    #: the crypto from here, and only then takes the money — so a delivery
    #: failure releases a hold instead of owing a refund. That inversion is
    #: what removes REVERSAL_FAILED from the ordinary path.
    FIAT_AUTHORIZED = "FIAT_AUTHORIZED"
    #: Crypto delivered and the hold converted to a real debit.
    FIAT_CAPTURED = "FIAT_CAPTURED"
    #: The hold was released without ever becoming a debit. Nothing is owed:
    #: the cardholder sees a pending amount disappear, not a refund.
    AUTH_VOIDED = "AUTH_VOIDED"

    # ── Capture-at-authorisation (legacy, for acquirers without 0100/0220) ──
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
    TransactionStatus.FIAT_AUTHORIZED,
    TransactionStatus.FIAT_CAPTURED,
    TransactionStatus.FIAT_UNKNOWN,
)

#: States that require a human to move money by hand.
MANUAL_INTERVENTION_STATES = (
    TransactionStatus.REVERSAL_FAILED,
    TransactionStatus.FIAT_UNKNOWN,
)


class IllegalTransition(RuntimeError):
    """An attempt to move a transaction into a state it cannot reach from here.

    Raised rather than logged: a payment that reaches an impossible state has a
    corrupt record of where its money is, and continuing on that basis is worse
    than failing loudly.
    """

    def __init__(self, tx_id, current, target):
        super().__init__(
            f"Transaction {tx_id} cannot move from {current.value} to {target.value}."
        )
        self.current = current
        self.target = target


#: The legal transition graph. Every state change in the system goes through
#: :meth:`Transaction.transition_to`, so this table is the whole truth about
#: how a payment may move — rather than ten assignment sites each guarded by
#: its own ad-hoc check, which is what made "does this case work?" unanswerable.
#:
#: Two entries look surprising and are deliberate:
#:   * CRYPTO_FAILED and REVERSAL_FAILED may reach CRYPTO_SENT. The on-chain
#:     check sometimes finds a transfer that did land after all, and the record
#:     must be allowed to tell the truth.
#:   * CRYPTO_SENT may fall back to CRYPTO_FAILED. Reconciliation compares
#:     against the chain, and the chain wins.
LEGAL_TRANSITIONS = {
    TransactionStatus.PENDING: {
        TransactionStatus.FIAT_AUTHORIZED,
        TransactionStatus.FIAT_APPROVED,
        TransactionStatus.FIAT_DECLINED,
        TransactionStatus.FIAT_UNKNOWN,
    },
    # A hold is in place and nothing has been debited. Every exit is cheap.
    #
    # Two states are deliberately absent, and the reachability tests enforce it.
    #
    # CRYPTO_FAILED exists to trigger a reversal, and a hold is released with a
    # void rather than refunded. CRYPTO_SENT is absent because a delivery is
    # only complete once the money is collected: a refused capture is retried,
    # not recorded as finished. Allowing either would reopen a route to
    # REVERSAL_FAILED — a cardholder out of pocket on a payment that was never
    # actually debited, which is the failure this ordering exists to remove.
    TransactionStatus.FIAT_AUTHORIZED: {
        TransactionStatus.FIAT_CAPTURED,
        TransactionStatus.AUTH_VOIDED,
    },
    # Money taken, crypto already delivered before it was.
    TransactionStatus.FIAT_CAPTURED: {
        TransactionStatus.CRYPTO_SENT,
        # A reorg can undo a transfer the capture was based on.
        TransactionStatus.CRYPTO_FAILED,
    },
    TransactionStatus.FIAT_APPROVED: {
        TransactionStatus.CRYPTO_SENT,
        TransactionStatus.CRYPTO_FAILED,
        TransactionStatus.REVERSED,
        TransactionStatus.REVERSAL_FAILED,
    },
    TransactionStatus.FIAT_UNKNOWN: {
        TransactionStatus.REVERSED,
        TransactionStatus.REVERSAL_FAILED,
        # The acquirer's settlement file can prove the authorisation never
        # landed, which makes it a decline after the fact.
        TransactionStatus.FIAT_DECLINED,
    },
    TransactionStatus.CRYPTO_FAILED: {
        TransactionStatus.REVERSED,
        TransactionStatus.REVERSAL_FAILED,
        TransactionStatus.CRYPTO_SENT,
    },
    TransactionStatus.REVERSAL_FAILED: {
        TransactionStatus.REVERSED,
        TransactionStatus.CRYPTO_SENT,
    },
    TransactionStatus.CRYPTO_SENT: {
        TransactionStatus.CRYPTO_FAILED,
    },
    # Terminal. Money has settled in one direction and stays settled.
    TransactionStatus.REVERSED: set(),
    TransactionStatus.FIAT_DECLINED: set(),
    #: Terminal and, unlike REVERSED, costless: no debit ever occurred.
    TransactionStatus.AUTH_VOIDED: set(),
}

#: States after which nothing further is owed to anyone.
TERMINAL_STATES = frozenset(
    state for state, targets in LEGAL_TRANSITIONS.items() if not targets
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

    #: The rate the customer was quoted, locked at authorisation. Delivery uses
    #: this and never a fresh lookup: a rate that moved between the quote and
    #: the transfer would hand the customer something other than what they
    #: agreed to — and a retry a day later would use a completely different one.
    exchange_rate = Column(Numeric(precision=28, scale=12), nullable=True)
    #: Where that rate came from, so a disputed transaction can be explained.
    exchange_rate_source = Column(String(64), nullable=True)
    #: When the source last updated the rate. An oracle publishes on a
    #: heartbeat, not continuously, so the quote can legitimately rest on a
    #: reading some hours old — recorded rather than inferred.
    exchange_rate_at = Column(DateTime(timezone=True), nullable=True)

    crypto_tx_hash = Column(String(66), nullable=True)
    #: Token amount owed, in the ERC-20's smallest unit. Computed once at
    #: authorisation from the locked rate, then delivered exactly.
    crypto_amount_units = Column(Numeric(precision=38, scale=0), nullable=True)
    #: Nonce the transfer was signed with. Replacing a transaction stuck in the
    #: mempool requires rebroadcasting at the same nonce, and the process that
    #: chose it is usually long gone by then.
    crypto_nonce = Column(Integer, nullable=True)
    #: How many times this transfer has been re-priced and rebroadcast.
    # Both a Python-side and a server-side default, deliberately. Without the
    # server default, autogenerate proposes dropping it on every subsequent
    # migration — churn that eventually gets applied, and then any INSERT not
    # written by SQLAlchemy fails on a NOT NULL column with nothing to fill it.
    crypto_replacements = Column(Integer, nullable=False, default=0, server_default="0")

    #: STAN of the capture, written *before* the message is sent. A capture
    #: that succeeded at the acquirer while this process died on the way to
    #: committing would otherwise be invisible, and the retry would capture a
    #: second time — the cardholder charged twice for one purchase. Its presence
    #: means "a capture was attempted"; reconcile rather than re-send.
    capture_stan = Column(String(6), nullable=True)
    capture_attempts = Column(Integer, nullable=False, default=0, server_default="0")

    reversal_stan = Column(String(6), nullable=True)
    reversal_attempts = Column(Integer, nullable=False, default=0, server_default="0")

    error_message = Column(String(512), nullable=True)

    __table_args__ = (
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_stan", "stan"),
        Index("ix_transactions_created_at", "created_at"),
        Index("ix_transactions_crypto_tx_hash", "crypto_tx_hash"),
        # The reconciliation job scans by (status, created_at) together.
        Index("ix_transactions_status_created_at", "status", "created_at"),
    )

    def transition_to(self, target: TransactionStatus, *, completed: bool = None) -> bool:
        """Move to ``target``, or refuse.

        Returns True when the state changed, False when it was already there —
        so a redelivered task or a duplicate reconciliation pass is a harmless
        no-op rather than an error. Raises :class:`IllegalTransition` for a move
        the graph does not allow.

        ``completed`` stamps ``completed_at``; left unset, it is inferred from
        whether the target settles the transaction.
        """
        if target is self.status:
            return False

        allowed = LEGAL_TRANSITIONS.get(self.status, set())
        if target not in allowed:
            raise IllegalTransition(self.id, self.status, target)

        self.status = target

        if completed is None:
            completed = target in (
                TransactionStatus.CRYPTO_SENT,
                TransactionStatus.REVERSED,
                TransactionStatus.FIAT_DECLINED,
                TransactionStatus.AUTH_VOIDED,
            )
        if completed:
            self.completed_at = utcnow()
        return True

    def can_transition_to(self, target: TransactionStatus) -> bool:
        return target is self.status or target in LEGAL_TRANSITIONS.get(self.status, set())

    #: Retained for callers that predate the state machine.
    def mark_completed(self, status: TransactionStatus) -> None:
        self.transition_to(status, completed=True)


class PaymentLink(Base):
    """A payment the merchant has described, for someone else to pay.

    A 2D link used to carry the order in its query string. That is readable by
    anyone it is forwarded to, and — worse — editable: the payer could turn 300
    into 3, or point the crypto at a wallet of their own, and the gateway would
    have no way to know the link had ever said anything else.

    So the order lives here and the link carries only ``token``. The payer's
    browser never learns the destination wallet, and the amount it is told is
    the amount this row holds. Nothing the payer sends can change either.

    ``token`` is the credential: whoever holds it can pay this link. It is
    generated from ``secrets`` and never derived from the row id, which would
    make links enumerable.
    """

    __tablename__ = "payment_links"

    id = Column(Integer, primary_key=True)
    #: URL-safe, 32 bytes of entropy. Unique so a collision fails loudly at the
    #: database rather than silently overwriting somebody's order.
    token = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    amount = Column(Numeric(18, 2), nullable=False)
    #: ISO 4217 alphabetic, as the merchant supplied it.
    currency = Column(String(3), nullable=False)
    target_wallet = Column(String(42), nullable=False)

    #: NULL means no expiry: the link lives until the merchant retires it.
    #: A date here still works, for a link deliberately given a deadline.
    expires_at = Column(DateTime(timezone=True), nullable=True)
    #: The merchant's switch. Turning a link off is not the same as deleting it:
    #: the row stays, with its payment history, and can be turned back on.
    active = Column(Boolean, nullable=False, default=True, server_default="true")

    #: A link is reusable, so these describe the most recent payment rather than
    #: the only one. The count is what tells a merchant a link is being used
    #: more than they expected — the signal that a shared link has spread.
    used_at = Column(DateTime(timezone=True), nullable=True)
    transaction_id = Column(Integer, nullable=True)
    payment_count = Column(Integer, nullable=False, default=0, server_default="0")

    def is_spendable(self, now: Optional[datetime] = None) -> bool:
        """Payable right now.

        Three conditions, and they are checked in the order a merchant would
        expect to reason about them: switched on, and not past a deadline if
        one was set. Deletion is not represented here because a deleted link
        has no row at all.
        """
        if not self.active:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > (now or utcnow())


class OutboxMessage(Base):
    """A task to publish, recorded in the same transaction as the work it describes.

    The gateway used to commit a payment and then publish its task as two
    separate acts against two separate systems. A process that died between them
    left a card authorised with nothing queued to deliver against it — invisible
    until the reconciliation sweep noticed, minutes later.

    The database and the broker cannot commit together, so the intent is written
    where transactions already exist: here, in the same commit as the payment.
    Either both land or neither does. A relay then moves rows from this table to
    the broker, and a row is only marked published once the broker has accepted
    it — so the failure mode is publishing twice, never losing a message. Both
    tasks are idempotent, which is what makes that trade the right way round.
    """

    __tablename__ = "outbox"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    task_name = Column(String(128), nullable=False)
    #: Task keyword arguments, JSON. Never card data: this table is not the PAN
    #: vault, and a broker payload is not a place for it either.
    payload = Column(Text, nullable=False)
    #: Seconds to wait before the task runs, for retries scheduled ahead.
    countdown = Column(Integer, nullable=False, default=0)

    published_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(512), nullable=True)
    #: Set when a message has failed enough times that a human should look. It
    #: stays in the table rather than being dropped: the payment it describes
    #: still has not been acted on.
    abandoned = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        # The relay's only query: unpublished, not abandoned, oldest first.
        Index("ix_outbox_pending", "published_at", "abandoned", "created_at"),
    )
