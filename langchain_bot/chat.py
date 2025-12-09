"""LangChain chat bot to test the MCP payout flow end-to-end.

Usage:
    python chat.py

Environment variables:
    OPENAI_API_KEY           - required for LangChain ChatOpenAI
    MCP_SERVER_URL           - defaults to http://127.0.0.1:8090/requests
"""

from __future__ import annotations

import base64
import os
import re
import sys
from typing import Dict

import httpx
import requests
from dotenv import load_dotenv
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools import tool
from langchain_openai import ChatOpenAI

load_dotenv()

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8090/requests")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # Optional: for higher rate limits

# OpenAI's client will auto-read proxy env vars; clear them here so we don't
# accidentally pass unsupported `proxies` args from environment into the client.
for proxy_var in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "OPENAI_PROXY",
):
    os.environ.pop(proxy_var, None)


def parse_github_repo_url(repo_url: str) -> tuple[str, str] | None:
    """
    Parse GitHub repository URL to extract username and repo name.
    Supports formats:
    - https://github.com/username/repo
    - https://github.com/username/repo/
    - github.com/username/repo
    - username/repo
    Returns (username, repo_name) or None if invalid.
    """
    # Remove trailing slash and whitespace
    repo_url = repo_url.strip().rstrip("/")
    
    # Try to extract username/repo from various URL formats
    patterns = [
        r"github\.com/([^/]+)/([^/]+)",  # github.com/username/repo
        r"^([^/]+)/([^/]+)$",  # username/repo
    ]
    
    for pattern in patterns:
        match = re.search(pattern, repo_url)
        if match:
            return (match.group(1), match.group(2))
    
    return None


def fetch_wallet_from_github(github_username: str, repo_url: str | None = None) -> str | None:
    """
    Fetch wallet address from bsc.address file.
    
    Args:
        github_username: GitHub username
        repo_url: Optional GitHub repository URL (e.g., https://github.com/username/repo)
                  If provided, will check this specific repo first.
                  If not provided, checks the user's most recently updated repo.
    
    Returns wallet address or None if not found.
    """
    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    
    try:
        repo_name = None
        
        # If repo_url is provided, parse it to get username and repo name
        if repo_url:
            parsed = parse_github_repo_url(repo_url)
            if parsed:
                repo_username, repo_name = parsed
                # Use the username from the URL if it matches, otherwise use provided username
                if repo_username.lower() != github_username.lower():
                    # URL username doesn't match provided username - use URL username
                    github_username = repo_username
            else:
                # Invalid repo URL format, fall back to checking user's repos
                repo_url = None
        
        # If we have a specific repo name, use it directly
        if repo_name:
            file_resp = requests.get(
                f"https://api.github.com/repos/{github_username}/{repo_name}/contents/bsc.address",
                headers=headers,
                timeout=10,
            )
            
            if file_resp.status_code == 200:
                file_data = file_resp.json()
                if file_data.get("encoding") == "base64":
                    content = base64.b64decode(file_data["content"]).decode("utf-8")
                    wallet = content.strip()
                    if wallet.startswith("0x") and len(wallet) == 42:
                        return wallet
        
        # Fall back to checking user's most recently updated repository
        repos_resp = requests.get(
            f"https://api.github.com/users/{github_username}/repos",
            headers=headers,
            params={"sort": "updated", "per_page": 1},
            timeout=10,
        )
        repos_resp.raise_for_status()
        repos = repos_resp.json()
        
        if not repos:
            return None
        
        # Get the first repository
        repo_name = repos[0]["name"]
        
        # Try to fetch bsc.address file from root directory
        file_resp = requests.get(
            f"https://api.github.com/repos/{github_username}/{repo_name}/contents/bsc.address",
            headers=headers,
            timeout=10,
        )
        
        if file_resp.status_code == 200:
            file_data = file_resp.json()
            if file_data.get("encoding") == "base64":
                content = base64.b64decode(file_data["content"]).decode("utf-8")
                wallet = content.strip()
                if wallet.startswith("0x") and len(wallet) == 42:
                    return wallet
        
        return None
    except requests.RequestException:
        return None


@tool("issue_tbnb", return_direct=True)
def issue_tbnb(
    github_username: str,
    repo_url: str | None = None,
    builder_id: str | None = None,
    channel: str = "discord",
) -> str:
    """Request tBNB payout for a GitHub user.
    
    Automatically fetches the wallet address from the user's GitHub repository
    (bsc.address file) and initiates verification and payout.
    
    Args:
        github_username: GitHub username for verification (required)
        repo_url: Optional GitHub repository URL (e.g., https://github.com/username/repo)
                  If provided, will check this specific repository for bsc.address file.
                  If not provided, checks the user's most recently updated repository.
        builder_id: Optional builder identifier
        channel: Support channel (default: discord)
    """

    builder_id = builder_id or "anonymous"
    
    if not github_username:
        return "github_username is required for verification."

    # Fetch wallet address from GitHub repo
    wallet_address = fetch_wallet_from_github(github_username, repo_url)
    if not wallet_address:
        if repo_url:
            return (
                f"Could not find wallet address in the specified repository ({repo_url}). "
                f"Please ensure you have a file named 'bsc.address' in the root directory "
                f"of that repository containing your BSC wallet address."
            )
        else:
            return (
                f"Could not find wallet address in your GitHub repository. "
                f"Please ensure you have a file named 'bsc.address' in the root directory "
                f"of one of your public repositories containing your BSC wallet address. "
                f"You can also provide a specific repository URL using the repo_url parameter."
            )

    payload = {
        "builder_id": builder_id,
        "wallet_address": wallet_address,
        "github_username": github_username,
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
        error_detail = str(exc)
        if hasattr(exc, "response") and exc.response is not None:
            try:
                error_data = exc.response.json()
                error_detail = error_data.get("detail", error_detail)
            except:
                pass
        return f"Failed to process payout: {error_detail}"

    data = response.json()
    tx_hash = data.get("tx_hash", "unknown")
    return (
        f"Payout approved! Sent tBNB to {wallet_address}. "
        f"Transaction hash: {tx_hash}. "
        f"Verification: {data.get('verification', {}).get('reason', 'N/A')}"
    )


def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is required", file=sys.stderr)
        raise SystemExit(1)

    # Create httpx clients without proxies to avoid the 'proxies' argument error
    # trust_env=False prevents reading proxy env vars
    sync_client = httpx.Client(trust_env=False)
    async_client = httpx.AsyncClient(trust_env=False)
    llm = ChatOpenAI(
        temperature=0,
        http_client=sync_client,
        http_async_client=async_client,
    )
    tools = [issue_tbnb]

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """You are BNB Support AI, helping verified builders obtain tBNB for development.

IMPORTANT: When users ask "how to get tBNB", "how do I get tBNB", "how to obtain tBNB", or similar questions about getting started, you MUST share the detailed step-by-step tutorial below. Provide comprehensive instructions with explanations.

HOW TO OBTAIN tBNB - DETAILED STEP-BY-STEP TUTORIAL:
Share this complete tutorial when users ask how to get tBNB:

STEP 1: Verify Your GitHub Account Meets Requirements
Before you begin, ensure your GitHub account meets these requirements:
- Your GitHub account must be at least 30 days old (we verify this automatically)
- You must have at least 1 public repository (private repos won't work)
- The account must be active and accessible

STEP 2: Set Up Your Wallet Address File
This is the most important step. You need to add your BSC wallet address to your GitHub repository:

a) Choose or Create a Repository:
   - You can use any existing public repository, or create a new one
   - The repository MUST be public (not private) so our system can read the file

b) Create the bsc.address File:
   - Navigate to the root directory of your repository (not in any subfolder)
   - Create a new file named exactly: bsc.address
   - The filename is case-sensitive, so it must be lowercase "bsc.address"

c) Add Your Wallet Address:
   - Open the bsc.address file
   - Inside the file, put ONLY your BSC wallet address
   - Example: 0x76c97a633c9b635bbfe3d0fc63b24196e63415ce
   - Do NOT add any extra text, comments, or formatting - just the wallet address
   - Make sure there are no spaces before or after the address

d) Commit and Push:
   - Save the file
   - Commit the changes with a message like "Add bsc.address file"
   - Push the commit to your repository
   - Verify the file is visible on GitHub by checking your repository online

STEP 3: Request tBNB
Once your bsc.address file is set up and pushed to GitHub:

- Simply provide your GitHub username to me
- You can say: "I want tBNB, my GitHub is @yourusername"
- Or provide a specific repository: "Check https://github.com/yourusername/yourrepo"
- I will automatically:
  * Fetch your wallet address from the bsc.address file
  * Verify your GitHub account (checking account age, repository count, and rate limits)
  * Process the tBNB payout if all checks pass

STEP 4: Receive Your tBNB
After verification:
- tBNB will be automatically sent to the wallet address in your bsc.address file
- You'll receive a transaction hash to track the transfer on BSC testnet
- You can view the transaction on BscScan testnet explorer using the hash
- Important: Each GitHub user can collect tBNB once per 24 hours

RATE LIMIT INFORMATION:
When users ask "why can't I receive more tBNB?" or "when can I get more?", provide detailed explanation:
"You can collect tBNB once per 24 hours per GitHub account. This rate limit is enforced to prevent abuse and ensure fair distribution. If you just collected tBNB, you'll need to wait 24 hours from your last successful payout before you can request again. The system tracks this automatically using your GitHub user ID."

WORKFLOW:
- When a builder requests tBNB, automatically call issue_tbnb with their GitHub username
- If they provide a repo URL, include it as repo_url parameter
- The tool automatically fetches wallet, verifies account, and processes payout
- If wallet is not found, share the detailed tutorial above (especially Step 2)

RESPONSE GUIDELINES:
- When users ask "how to get tBNB" or similar, ALWAYS share the full detailed tutorial
- For rate limit questions, provide the detailed explanation above
- Be thorough and helpful, explaining each step clearly
- If users encounter issues, guide them through troubleshooting""",
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

