"""Stub verification source service.

This FastAPI app simply returns `verified=True` for any wallet request so we can
focus on building the rest of the MCP server. Replace the logic inside
`verify_wallet` once a real verification mechanism is ready.
"""

from fastapi import FastAPI
from pydantic import BaseModel


class VerificationRequest(BaseModel):
    wallet_address: str
    requester_id: str | None = None
    channel: str | None = None


class VerificationResponse(BaseModel):
    wallet_address: str
    verified: bool
    confidence: float
    reason: str


app = FastAPI(title="Verification Source Stub", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Simple health endpoint for container/runtime checks."""
    return {"status": "ok"}


@app.post("/verify", response_model=VerificationResponse)
def verify_wallet(payload: VerificationRequest) -> VerificationResponse:
    """Always return verified=True for now."""
    return VerificationResponse(
        wallet_address=payload.wallet_address,
        verified=True,
        confidence=1.0,
        reason="Stubbed verification service",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)

