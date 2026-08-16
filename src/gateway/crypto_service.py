import asyncio
import logging
import os
from decimal import Decimal
from web3 import Web3
from web3.exceptions import Web3Exception

LOGGER = logging.getLogger(__name__)

# Minimal ERC-20 ABI for transfers and decimals
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    }
]

class CryptoService:
    """
    Service for executing real cryptocurrency transfers using Web3.py.
    Integrates with HashiCorp Vault for secure private key management.
    """
    def __init__(self, rpc_urls: list[str] = None, private_key: str = None):
        primary_rpc = os.environ.get("WEB3_RPC_URL", "http://127.0.0.1:8545")
        backup_rpc = os.environ.get("WEB3_RPC_URL_BACKUP")
        
        self.rpc_urls = rpc_urls or [rpc for rpc in [primary_rpc, backup_rpc] if rpc]
        
        # Try to connect to the first available RPC
        self.w3 = None
        for url in self.rpc_urls:
            w3 = Web3(Web3.HTTPProvider(url))
            if w3.is_connected():
                self.w3 = w3
                self.active_rpc = url
                LOGGER.info(f"Connected to Web3 RPC: {url}")
                break
                
        if not self.w3:
            LOGGER.error("Could not connect to any Web3 RPC providers.")
            self.w3 = Web3(Web3.HTTPProvider(self.rpc_urls[0])) # Fallback
            
        self._nonce_lock = asyncio.Lock() # TODO: Replace with Redis distributed lock for multi-worker
        
        # Priority: explicit arg > Vault > env var
        self.private_key = private_key or self._fetch_key_from_vault() or os.environ.get("WEB3_PRIVATE_KEY")
        
        if self.private_key:
            self.account = self.w3.eth.account.from_key(self.private_key)
            LOGGER.info(f"CryptoService initialized. Wallet: {self.account.address}")
        else:
            self.account = None
            LOGGER.warning("No private key available (Vault or ENV). Crypto transfers will fail.")

    def _fetch_key_from_vault(self) -> str | None:
        """Attempt to retrieve the Web3 private key from HashiCorp Vault."""
        vault_addr = os.environ.get("VAULT_ADDR")
        vault_token = os.environ.get("VAULT_TOKEN")
        
        if not vault_addr or not vault_token:
            return None

        try:
            import hvac
            client = hvac.Client(url=vault_addr, token=vault_token)
            
            if not client.is_authenticated():
                return None
            
            secret = client.secrets.kv.v2.read_secret_version(path="gateway/web3", mount_point="secret")
            return secret["data"]["data"].get("private_key")
        except Exception:
            return None

    def is_connected(self) -> bool:
        return self.w3.is_connected()

    async def transfer_erc20_token(self, token_address: str, to_address: str, amount_fiat: float, exchange_rate: float = 1.0) -> str:
        """
        Calculates the crypto amount based on fiat and transfers ERC20 tokens.
        Waits for N block confirmations for production safety.
        """
        if not self.account:
            raise ValueError("Service not configured with a private key.")
            
        if not self.is_connected():
            raise ConnectionError("Cannot connect to Web3 RPC.")

        token_address = self.w3.to_checksum_address(token_address)
        to_address = self.w3.to_checksum_address(to_address)
        
        contract = self.w3.eth.contract(address=token_address, abi=ERC20_ABI)

        # Dynamic decimals
        decimals = contract.functions.decimals().call()
        multiplier = Decimal(str(10 ** decimals))
        
        crypto_amount = Decimal(str(amount_fiat)) / Decimal(str(exchange_rate))
        token_amount = int(crypto_amount * multiplier)

        LOGGER.info(f"Preparing to send {crypto_amount} ERC20 to {to_address} (Fiat: {amount_fiat})")

        async with self._nonce_lock:
            nonce = self.w3.eth.get_transaction_count(self.account.address, 'pending')
            
            base_fee = self.w3.eth.get_block('latest')['baseFeePerGas']
            max_priority_fee = self.w3.to_wei(2, 'gwei')
            max_fee = base_fee * 2 + max_priority_fee

            tx = contract.functions.transfer(to_address, token_amount).build_transaction({
                'nonce': nonce,
                'from': self.account.address,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': max_priority_fee,
                'chainId': self.w3.eth.chain_id
            })

            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)

            try:
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)
                tx_hash_hex = self.w3.to_hex(tx_hash)
                LOGGER.info(f"ERC20 Transaction broadcasted. Hash: {tx_hash_hex}")
                
                # Wait for receipt
                receipt = await asyncio.to_thread(self.w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
                
                if receipt.status != 1:
                    raise RuntimeError(f"ERC20 Transaction failed on-chain: {tx_hash_hex}")
                    
                # Wait for extra confirmations (e.g., 2 blocks) for safety
                CONFIRMATIONS_REQUIRED = int(os.environ.get("WEB3_CONFIRMATIONS", "2"))
                target_block = receipt.blockNumber + CONFIRMATIONS_REQUIRED
                
                LOGGER.info(f"Tx included in block {receipt.blockNumber}. Waiting for {CONFIRMATIONS_REQUIRED} confirmations...")
                
                while True:
                    current_block = self.w3.eth.block_number
                    if current_block >= target_block:
                        break
                    await asyncio.sleep(2)
                    
                LOGGER.info(f"Transaction {tx_hash_hex} fully confirmed.")
                return tx_hash_hex
                
            except Web3Exception as e:
                LOGGER.error(f"Web3 transaction failed: {e}")
                raise
