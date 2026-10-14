"""
Gmail MCP server — exposes Gmail searching, email reading, OTP extraction,
and link resolution as tools an agent can drive.

Tools:
    search_emails(query, max_results)       Find recent emails matching Gmail query syntax
    get_email(message_id)                   Read full plain-text body of an email
    get_latest_otp(query, minutes_ago)      Extract numeric/alphanumeric OTP verification codes
    get_latest_link(query, minutes_ago)     Extract verification or password-reset URLs

Reads OAuth credentials from ~/.gemini/antigravity-cli/mcp_oauth_tokens.json and
automatically refreshes tokens with Google's OAuth endpoint when needed.

Transport is stdio: stdout carries JSON-RPC protocol; diagnostics go to stderr.
"""

import os
import sys
import json
import re
import time
import base64
import requests
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer

TOKEN_FILE = os.path.expanduser("~/.gemini/antigravity-cli/mcp_oauth_tokens.json")
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def log(msg: str):
    print(f"[gmail-mcp] {msg}", file=sys.stderr, flush=True)


def load_token_data() -> dict:
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(f"OAuth token file not found at {TOKEN_FILE}")
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_token_data(data: dict):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_valid_access_token() -> str:
    """Returns a valid access token, auto-refreshing if expired."""
    data = load_token_data()
    # Key may be the URL or 'gmail'
    creds_entry = data.get("https://gmailmcp.googleapis.com/mcp/v1") or data.get("gmail")
    if not creds_entry:
        for k, v in data.items():
            if isinstance(v, dict) and "token" in v:
                creds_entry = v
                break

    if not creds_entry or "token" not in creds_entry:
        raise ValueError("No valid OAuth token entry found in token file.")

    token_info = creds_entry["token"]
    access_token = token_info.get("access_token")
    refresh_token = token_info.get("refresh_token")
    client_id = creds_entry.get("client_id")
    client_secret = creds_entry.get("client_secret")

    # Parse expiry safely (can be ISO 8601 string or numeric timestamp)
    expiry = token_info.get("expiry") or 0
    if isinstance(expiry, str):
        try:
            expiry_ts = datetime.fromisoformat(expiry).timestamp()
        except Exception:
            expiry_ts = 0
    else:
        expiry_ts = float(expiry)

    now = time.time()

    # If expired or expiring within 60s, refresh
    if refresh_token and (expiry_ts < now + 60):
        log("Access token expired or expiring soon; refreshing via Google OAuth...")
        try:
            resp = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                new_tokens = resp.json()
                access_token = new_tokens["access_token"]
                token_info["access_token"] = access_token
                token_info["expiry"] = datetime.fromtimestamp(now + new_tokens.get("expires_in", 3600), tz=timezone.utc).isoformat()
                save_token_data(data)
                log("Token refreshed successfully.")
            else:
                log(f"Token refresh failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            log(f"Exception refreshing token: {e}")

    return access_token


def _decode_body(payload: dict) -> str:
    """Recursively extract plain text body from Gmail message payload."""
    body_data = ""
    mime_type = payload.get("mimeType", "")
    parts = payload.get("parts", [])

    if mime_type.startswith("text/plain") and "data" in payload.get("body", {}):
        raw = payload["body"]["data"]
        return base64.urlsafe_b64decode(raw + "==").decode("utf-8", errors="replace")

    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain" and "data" in part.get("body", {}):
            raw = part["body"]["data"]
            return base64.urlsafe_b64decode(raw + "==").decode("utf-8", errors="replace")
        elif "parts" in part:
            sub = _decode_body(part)
            if sub:
                return sub

    # Fallback to html text if no plain text
    if mime_type.startswith("text/html") and "data" in payload.get("body", {}):
        raw = payload["body"]["data"]
        html = base64.urlsafe_b64decode(raw + "==").decode("utf-8", errors="replace")
        # Strip simple HTML tags
        return re.sub(r"<[^>]+>", " ", html)

    return body_data


mcp = MCPServer(
    name="gmail",
    instructions=(
        "Gmail automation MCP server. Search messages, read email bodies, extract "
        "verification OTPs and password reset links for automated job applications."
    ),
)


@mcp.tool(description="Search emails using Gmail query syntax (e.g. 'from:Workday', 'subject:verification', 'newer_than:1d'). Returns metadata and snippets.")
def search_emails(query: str = "newer_than:2d", max_results: int = 5) -> str:
    """Searches messages and returns list of summaries."""
    token = get_valid_access_token()
    url = f"{GMAIL_API_BASE}/messages"
    params = {"q": query, "maxResults": max_results}
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(url, params=params, headers=headers, timeout=10)
    if resp.status_code != 200:
        return json.dumps({"ok": False, "error": resp.text}, indent=2)

    data = resp.json()
    messages_summary = []
    for item in data.get("messages", []):
        m_id = item["id"]
        # Fetch metadata
        m_resp = requests.get(
            f"{GMAIL_API_BASE}/messages/{m_id}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date",
            headers=headers,
            timeout=10,
        )
        if m_resp.status_code == 200:
            m_data = m_resp.json()
            headers_dict = {h["name"].lower(): h["value"] for h in m_data.get("payload", {}).get("headers", [])}
            messages_summary.append({
                "id": m_id,
                "thread_id": m_data.get("threadId"),
                "from": headers_dict.get("from", ""),
                "subject": headers_dict.get("subject", ""),
                "date": headers_dict.get("date", ""),
                "snippet": m_data.get("snippet", ""),
            })

    return json.dumps({
        "ok": True,
        "count": len(messages_summary),
        "query": query,
        "messages": messages_summary,
    }, indent=2, ensure_ascii=False)


@mcp.tool(description="Get full plain-text content and headers of an email by its message ID.")
def get_email(message_id: str) -> str:
    """Fetches full email body and details."""
    token = get_valid_access_token()
    url = f"{GMAIL_API_BASE}/messages/{message_id}?format=full"
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code != 200:
        return json.dumps({"ok": False, "error": resp.text}, indent=2)

    data = resp.json()
    payload = data.get("payload", {})
    headers_dict = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    body_text = _decode_body(payload)

    return json.dumps({
        "ok": True,
        "id": message_id,
        "subject": headers_dict.get("subject", ""),
        "from": headers_dict.get("from", ""),
        "date": headers_dict.get("date", ""),
        "body": body_text.strip() if body_text else data.get("snippet", ""),
    }, indent=2, ensure_ascii=False)


@mcp.tool(description="Extract the latest OTP / verification code from recent emails matching a query (e.g. 'Workday' or 'Paramount').")
def get_latest_otp(query: str = "", minutes_ago: int = 15) -> str:
    """Searches recent emails and regex-extracts verification codes."""
    full_query = f"newer_than:{max(1, minutes_ago)}m"
    if query:
        full_query = f"{query} {full_query}"

    search_res = json.loads(search_emails(query=full_query, max_results=3))
    if not search_res.get("ok") or not search_res.get("messages"):
        return json.dumps({
            "ok": False,
            "error": f"No recent emails found matching '{full_query}'.",
        }, indent=2)

    # Check each recent email
    for msg in search_res["messages"]:
        email_data = json.loads(get_email(msg["id"]))
        body = email_data.get("body", "") + " " + msg.get("snippet", "")

        # Look for explicit code patterns
        patterns = [
            r"(?:code|pin|password|verification)\s*(?:is|:)?\s*([0-9]{4,8})\b",
            r"\b([0-9]{6})\b",  # Standard 6-digit OTP
            r"\b([0-9]{4})\b",  # Standard 4-digit OTP
            r"\b([0-9]{8})\b",  # Standard 8-digit OTP
        ]
        for pat in patterns:
            match = re.search(pat, body, re.IGNORECASE)
            if match:
                code = match.group(1)
                return json.dumps({
                    "ok": True,
                    "code": code,
                    "from": email_data.get("from"),
                    "subject": email_data.get("subject"),
                    "date": email_data.get("date"),
                    "snippet": msg.get("snippet"),
                }, indent=2)

    return json.dumps({
        "ok": False,
        "error": "Emails found, but no clear verification code detected in body.",
    }, indent=2)


@mcp.tool(description="Extract the latest password-reset or action URL from recent emails matching a query.")
def get_latest_link(query: str, minutes_ago: int = 15) -> str:
    """Searches recent emails and extracts action links (password resets, e-signatures, confirmations)."""
    full_query = f"{query} newer_than:{max(1, minutes_ago)}m"
    search_res = json.loads(search_emails(query=full_query, max_results=3))
    if not search_res.get("ok") or not search_res.get("messages"):
        return json.dumps({
            "ok": False,
            "error": f"No recent emails found matching '{full_query}'.",
        }, indent=2)

    for msg in search_res["messages"]:
        email_data = json.loads(get_email(msg["id"]))
        body = email_data.get("body", "")

        # Extract HTTP/HTTPS links
        urls = re.findall(r"https?://[^\s<>\"')]+", body)
        # Filter for action links
        filtered = [
            u for u in urls
            if any(k in u.lower() for k in ["reset", "verify", "confirm", "auth", "token", "login", "password"])
        ]
        target_url = filtered[0] if filtered else (urls[0] if urls else None)

        if target_url:
            return json.dumps({
                "ok": True,
                "url": target_url,
                "all_urls_found": len(urls),
                "from": email_data.get("from"),
                "subject": email_data.get("subject"),
            }, indent=2)

    return json.dumps({
        "ok": False,
        "error": "No links found in matching recent emails.",
    }, indent=2)


def selftest():
    print("Testing search_emails('newer_than:2d', max_results=2):")
    res = search_emails("newer_than:2d", max_results=2)
    print(res)

    parsed = json.loads(res)
    if parsed.get("ok") and parsed.get("messages"):
        mid = parsed["messages"][0]["id"]
        print(f"\nTesting get_email('{mid}'):")
        print(get_email(mid)[:500] + "...")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        mcp.run(transport="stdio")
