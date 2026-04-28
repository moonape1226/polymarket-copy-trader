"""
Redeems resolved Polymarket positions on-chain via the Gnosis Safe proxy wallet.

When a market resolves:
  - Winning positions: redeemable for 1 pUSD per share
  - Losing positions: redeemable for 0 pUSD (just clears them from the wallet)

Called periodically from main.py to sweep up any settled positions.
"""

import logging
import os
import requests
from typing import List, Dict, Any

from web3 import Web3
from eth_account import Account
from eth_abi import encode as abi_encode
from eth_utils import keccak

logger = logging.getLogger(__name__)

# ── Polygon RPC ────────────────────────────────────────────────────────────────
RPC_URL = "https://polygon-bor-rpc.publicnode.com"

# ── Polymarket contract addresses on Polygon ───────────────────────────────────
CTF_ADDRESS  = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ZERO_BYTES32   = b"\x00" * 32
ZERO_ADDRESS   = "0x0000000000000000000000000000000000000000"

_zero_balance_cache: set = set()

# ── Minimal ABIs ───────────────────────────────────────────────────────────────
CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "getCollectionId",
        "type": "function",
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSet",           "type": "uint256"},
        ],
        "outputs": [{"type": "bytes32"}],
        "stateMutability": "view",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id",    "type": "uint256"},
        ],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
    },
]

SAFE_ABI = [
    {"name": "nonce",        "type": "function", "inputs": [],
     "outputs": [{"type": "uint256"}], "stateMutability": "view"},
    {"name": "execTransaction", "type": "function", "inputs": [
        {"name": "to",              "type": "address"},
        {"name": "value",           "type": "uint256"},
        {"name": "data",            "type": "bytes"},
        {"name": "operation",       "type": "uint8"},
        {"name": "safeTxGas",       "type": "uint256"},
        {"name": "baseGas",         "type": "uint256"},
        {"name": "gasPrice",        "type": "uint256"},
        {"name": "gasToken",        "type": "address"},
        {"name": "refundReceiver",  "type": "address"},
        {"name": "signatures",      "type": "bytes"},
    ], "outputs": [{"type": "bool"}], "stateMutability": "payable"},
]

# ── Safe EIP-712 constants ─────────────────────────────────────────────────────
SAFE_TX_TYPEHASH = keccak(
    b"SafeTx(address to,uint256 value,bytes data,uint8 operation,"
    b"uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,"
    b"address gasToken,address refundReceiver,uint256 nonce)"
)
DOMAIN_SEPARATOR_TYPEHASH = keccak(
    b"EIP712Domain(uint256 chainId,address verifyingContract)"
)


def _hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str.removeprefix("0x"))


def _safe_tx_hash(safe_address: str, to: str, data: bytes, nonce: int, chain_id: int) -> bytes:
    """Compute the EIP-712 hash of a Gnosis Safe transaction."""
    safe_tx_hash = keccak(abi_encode(
        ["bytes32", "address", "uint256", "bytes32", "uint8",
         "uint256", "uint256", "uint256", "address", "address", "uint256"],
        [SAFE_TX_TYPEHASH, to, 0, keccak(data), 0, 0, 0, 0,
         "0x0000000000000000000000000000000000000000",
         "0x0000000000000000000000000000000000000000", nonce],
    ))
    domain_separator = keccak(abi_encode(
        ["bytes32", "uint256", "address"],
        [DOMAIN_SEPARATOR_TYPEHASH, chain_id, safe_address],
    ))
    return keccak(b"\x19\x01" + domain_separator + safe_tx_hash)


def _fetch_redeemable_positions(proxy_address: str) -> List[Dict[str, Any]]:
    """Return positions where redeemable=True from the Polymarket data API."""
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": proxy_address, "sizeThreshold": ".01"},
            timeout=10,
        )
        resp.raise_for_status()
        return [p for p in resp.json() if p.get("redeemable")]
    except Exception as e:
        logger.error(f"Failed to fetch positions for redemption: {e}")
        return []


def redeem_resolved_positions(private_key: str, proxy_address: str) -> int:
    """
    Scans for redeemable positions and redeems them on-chain.
    Returns the number of positions successfully redeemed.
    """
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        logger.error("Cannot connect to Polygon RPC for redemption")
        return 0

    proxy = Web3.to_checksum_address(proxy_address)
    account = Account.from_key(private_key)
    chain_id = w3.eth.chain_id  # 137

    ctf  = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
    safe = w3.eth.contract(address=proxy, abi=SAFE_ABI)

    positions = _fetch_redeemable_positions(proxy_address)
    if not positions:
        return 0

    positions = [p for p in positions
                 if (p['conditionId'], p.get('outcomeIndex', 0)) not in _zero_balance_cache]
    if not positions:
        return 0

    logger.info(f"Found {len(positions)} redeemable position(s)")
    gas_price = w3.eth.gas_price
    redeemed = 0

    for pos in positions:
        condition_id  = pos["conditionId"]          # hex string "0x..."
        outcome_index = int(pos.get("outcomeIndex", 0))
        title         = pos.get("title", condition_id[:20])
        outcome_label = pos.get("outcome", "")

        # outcomeIndex 0 → YES → indexSet 1  (binary 01)
        # outcomeIndex 1 → NO  → indexSet 2  (binary 10)
        index_set = 1 << outcome_index

        try:
            condition_bytes = _hex_to_bytes(condition_id)

            # Skip if we have no tokens to redeem (API can lag behind on-chain state)
            collection_id = ctf.functions.getCollectionId(ZERO_BYTES32, condition_bytes, index_set).call()
            pos_id = int.from_bytes(keccak(bytes.fromhex(USDC_ADDRESS[2:]) + collection_id), "big")
            balance = ctf.functions.balanceOf(proxy, pos_id).call()
            if balance == 0:
                _zero_balance_cache.add((condition_id, outcome_index))
                logger.info(f"Skipping {title}: already redeemed (balance=0 on-chain)")
                continue

            # Build redeemPositions calldata
            calldata = ctf.functions.redeemPositions(
                USDC_ADDRESS, ZERO_BYTES32, condition_bytes, [index_set]
            ).build_transaction({"gas": 0, "gasPrice": 0, "nonce": 0, "from": proxy})["data"]
            calldata_bytes = _hex_to_bytes(calldata)

            # Get Safe nonce
            nonce = safe.functions.nonce().call()

            # Compute Safe tx hash and sign (raw EIP-712 hash, no prefix)
            tx_hash = _safe_tx_hash(proxy, CTF_ADDRESS, calldata_bytes, nonce, chain_id)
            sig_obj = Account._sign_hash(tx_hash, private_key)
            signature = sig_obj.signature

            # Build and send execTransaction
            tx = safe.functions.execTransaction(
                CTF_ADDRESS, 0, calldata_bytes, 0,
                0, 0, 0,
                ZERO_ADDRESS,
                ZERO_ADDRESS,
                signature,
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gasPrice": gas_price,
                "chainId": chain_id,
            })

            # Estimate gas
            tx["gas"] = w3.eth.estimate_gas(tx)

            signed_tx = account.sign_transaction(tx)
            tx_hash_sent = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash_sent, timeout=120)

            if receipt.status == 1:
                logger.info(
                    f"Redeemed: {title} ({outcome_label}) "
                    f"tx={tx_hash_sent.hex()[:16]}..."
                )
                redeemed += 1
            else:
                logger.warning(
                    f"Redemption tx reverted: {title} "
                    f"tx={tx_hash_sent.hex()[:16]}..."
                )

        except Exception as e:
            logger.error(f"Failed to redeem {title}: {e}")

    return redeemed
