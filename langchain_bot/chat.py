"""LangChain chat bot to test the MCP payout flow end-to-end.

Usage:
    python chat.py

Environment variables:
    OPENAI_API_KEY           - required for LangChain ChatOpenAI
    MCP_SERVER_URL           - defaults to http://127.0.0.1:8090/requests
"""

from __future__ import annotations

import os
import sys
from typing import Dict

import requests
from dotenv import load_dotenv
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import tool
from langchain_openai import ChatOpenAI

load_dotenv()

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8090/requests")


@tool("issue_tbnb", return_direct=True)
def issue_tbnb(
    wallet_address: str,
    builder_id: str | None = None,
    channel: str = "discord",
) -> str:
    """Send a payout request to the MCP server."""

    builder_id = builder_id or "anonymous"

    if not wallet_address:
        return "wallet_address is required for payouts."

    payload = {
        "builder_id": builder_id,
        "wallet_address": wallet_address,
        "channel": channel,
    }

    try:
        response = requests.post(
            MCP_SERVER_URL,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:  # pragma: no cover - interactive
        return f"Failed to call MCP server: {exc}"

    data = response.json()
    tx_hash = data.get("tx_hash", "unknown")
    return (
        f"Payout approved for {wallet_address}. "
        f"tx_hash={tx_hash}. Verification={data.get('verification')}"
    )


def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is required", file=sys.stderr)
        raise SystemExit(1)

    llm = ChatOpenAI(temperature=0)
    tools = [issue_tbnb]

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are BNB Support AI. When a verified builder requests tBNB "
                "and provides a wallet address, call the issue_tbnb tool with "
                "their builder_id, wallet_address, and channel.",
            ),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )

    agent = create_openai_tools_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

    print("BNB Support AI (type 'exit' to quit)")
    while True:
        user_input = input("builder> ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Goodbye!")
            break
        result = agent_executor.invoke({"input": user_input})
        print(f"assistant> {result['output']}")


if __name__ == "__main__":
    main()

