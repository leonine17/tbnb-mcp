"""MCP server that verifies builders and initiates real tBNB payouts."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from typing import Any

import httpx
from dotenv import load_dotenv
from eth_account import Account
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from web3 import Web3

load_dotenv()

VERIFICATION_URL = os.getenv(
    "VERIFICATION_SERVICE_URL", "http://localhost:8080/verify"
)
BSC_RPC_URL = os.getenv("BSC_RPC_URL")
TREASURY_SECRET = os.getenv("TREASURY_PRIVATE_KEY")
DEFAULT_PAYOUT_AMOUNT = Decimal(os.getenv("DEFAULT_PAYOUT_AMOUNT", "0.3"))
PAYOUT_GAS_LIMIT = int(os.getenv("PAYOUT_GAS_LIMIT", "21000"))

if not BSC_RPC_URL or not TREASURY_SECRET:
    raise RuntimeError(
        "BSC_RPC_URL and TREASURY_PRIVATE_KEY must be configured in the environment."
    )

Account.enable_unaudited_hdwallet_features()


def _derive_account(secret: str) -> Account:
    """Interpret env secret as either mnemonic or raw private key."""
    normalized = secret.replace(",", " ").strip()
    if len(normalized.split()) >= 12 and all(normalized.split()):
        return Account.from_mnemonic(normalized)
    return Account.from_key(secret.strip())


treasury_account = _derive_account(TREASURY_SECRET)
treasury_private_key = treasury_account.key

w3 = Web3(Web3.HTTPProvider(BSC_RPC_URL))
if not w3.is_connected():
    raise RuntimeError("Unable to connect to BSC RPC endpoint.")

CHAIN_ID = w3.eth.chain_id

app = FastAPI(title="tBNB MCP Server", version="0.2.0")


class DisbursementRequest(BaseModel):
    builder_id: str = Field(..., description="Verified identity in Discord/Telegram")
    wallet_address: str = Field(..., description="Checksum wallet address")
    github_username: str = Field(..., description="GitHub username for verification")
    channel: str = Field(..., description="Support channel (discord, telegram, web)")


class DisbursementResponse(BaseModel):
    request_id: str
    status: str
    message: str
    tx_hash: str | None = None
    verification: dict[str, Any]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def verify_wallet(payload: DisbursementRequest) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            VERIFICATION_URL,
            json={
                "wallet_address": payload.wallet_address,
                "github_username": payload.github_username,
                "requester_id": payload.builder_id,
                "channel": payload.channel,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def record_payout(github_user_id: int) -> None:
    """Record successful payout in verification service."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{VERIFICATION_URL.rstrip('/verify')}/record-payout",
            json={"github_user_id": github_user_id},
        )
        resp.raise_for_status()


def _send_tbnb(wallet_address: str, amount: Decimal) -> str:
    """Send tBNB to the requested wallet and return the transaction hash."""
    checksum_address = Web3.to_checksum_address(wallet_address)
    value_wei = w3.to_wei(amount, "ether")
    if value_wei <= 0:
        raise ValueError("DEFAULT_PAYOUT_AMOUNT must be positive.")

    nonce = w3.eth.get_transaction_count(treasury_account.address)
    gas_price = w3.eth.gas_price

    tx = {
        "to": checksum_address,
        "value": value_wei,
        "nonce": nonce,
        "gas": PAYOUT_GAS_LIMIT,
        "gasPrice": gas_price,
        "chainId": CHAIN_ID,
    }

    signed = treasury_account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    if receipt.status != 1:
        raise RuntimeError("On-chain transfer failed.")

    return w3.to_hex(tx_hash)


async def initiate_payout(wallet_address: str) -> str:
    return await asyncio.to_thread(
        _send_tbnb, wallet_address, DEFAULT_PAYOUT_AMOUNT
    )


@app.post("/requests", response_model=DisbursementResponse)
async def request_tbnb(payload: DisbursementRequest) -> DisbursementResponse:
    verification = await verify_wallet(payload)

    if not verification.get("verified"):
        reason = verification.get("reason", "Unknown verification failure")
        raise HTTPException(
            status_code=403,
            detail=f"Verification failed: {reason}",
        )

    request_id = str(uuid.uuid4())
    try:
        tx_hash = await initiate_payout(payload.wallet_address)
        
        # Record successful payout for rate limiting
        github_user_id = verification.get("github_user_id")
        if github_user_id:
            try:
                await record_payout(github_user_id)
            except Exception as exc:
                # Log but don't fail the request - payout already succeeded
                print(f"Warning: Failed to record payout: {exc}")
    except Exception as exc:  # pragma: no cover - surfaced to clients
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return DisbursementResponse(
        request_id=request_id,
        status="approved",
        message="Disbursement submitted to BSC testnet",
        tx_hash=tx_hash,
        verification=verification,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090)

