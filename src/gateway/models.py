import enum
from sqlalchemy import Column, String, Float, Enum, Integer, DateTime, Numeric, Index
from sqlalchemy.orm import declarative_base
from datetime import datetime, timezone

Base = declarative_base()

class TransactionStatus(enum.Enum):
    PENDING = "PENDING"
    FIAT_APPROVED = "FIAT_APPROVED"
    FIAT_DECLINED = "FIAT_DECLINED"
    CRYPTO_SENT = "CRYPTO_SENT"
    CRYPTO_FAILED = "CRYPTO_FAILED"
    REVERSED = "REVERSED"

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    amount = Column(Numeric(precision=18, scale=2), nullable=False) # Precision > Float
    masked_pan = Column(String(16), nullable=False)
    target_wallet = Column(String(42), nullable=False)
    
    idempotency_key = Column(String(36), unique=True, nullable=True)
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING, nullable=False, index=True)
    
    stan = Column(String(6), nullable=True, index=True) # Trace number for bank
    action_code = Column(String(2), nullable=True) # DE39 response
    
    crypto_tx_hash = Column(String(66), nullable=True) # e.g. 0x...
    error_message = Column(String, nullable=True)

