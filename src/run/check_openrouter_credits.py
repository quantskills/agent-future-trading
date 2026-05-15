import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
DEFAULT_ENV_KEY = "OPENROUTER_API_KEY"


def load_api_key(env_key: str) -> str:
    """Load the OpenRouter API key from the configured AgentQuant env files."""
    candidates = []
    explicit_env = os.getenv("AGENTQUANT_ENV_FILE")
    if explicit_env:
        candidates.append(Path(explicit_env))
    candidates.extend(
        [
            PROJECT_ROOT / ".env",
            PROJECT_ROOT.parent / f"{PROJECT_ROOT.name}.env",
        ]
    )
    for path in candidates:
        if path.exists():
            load_dotenv(dotenv_path=path, override=False)
    api_key = os.getenv(env_key, "").strip()
    if not api_key or api_key == "your-openrouter-api-key":
        searched = ", ".join(str(path) for path in candidates)
        raise RuntimeError(f"{env_key} is not set. Searched: {searched}")
    return api_key


def parse_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise RuntimeError(f"OpenRouter response field '{field_name}' is not numeric: {value!r}") from exc


def fetch_openrouter_credits(api_key: str, timeout: float) -> Dict[str, Decimal]:
    response = requests.get(
        OPENROUTER_CREDITS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        timeout=timeout,
    )

    if response.status_code == 401:
        raise RuntimeError("OpenRouter authentication failed with 401. Check OPENROUTER_API_KEY.")
    if response.status_code == 403:
        raise RuntimeError(
            "OpenRouter returned 403 for the credits endpoint. "
            "This endpoint requires a management key."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"OpenRouter credits request failed: {response.status_code} {response.text}") from exc

    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected OpenRouter response payload: {payload!r}")

    total_credits = parse_decimal(data.get("total_credits"), "total_credits")
    total_usage = parse_decimal(data.get("total_usage"), "total_usage")
    return {
        "total_credits": total_credits,
        "total_usage": total_usage,
        "remaining_credits": total_credits - total_usage,
    }


def format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check OpenRouter account credits.")
    parser.add_argument(
        "--env-key",
        default=DEFAULT_ENV_KEY,
        help=f"Environment variable containing the OpenRouter API key. Defaults to {DEFAULT_ENV_KEY}.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = load_api_key(args.env_key)
    credits = fetch_openrouter_credits(api_key=api_key, timeout=args.timeout)

    if args.json:
        print(json.dumps({key: str(value) for key, value in credits.items()}, indent=2))
        return 0

    print("OpenRouter credits")
    print(f"  Total credits:     {format_decimal(credits['total_credits'])}")
    print(f"  Total usage:       {format_decimal(credits['total_usage'])}")
    print(f"  Remaining credits: {format_decimal(credits['remaining_credits'])}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
