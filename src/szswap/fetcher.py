"""Async/sync model discovery client with secret redaction."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from szswap.config import AUTH_MODES, ConfigError, resolve_env_var, validate_base_url


class FetchError(RuntimeError):
    """Raised when fetching models from a provider fails."""


@dataclass
class FetchResult:
    """Result of a successful models list fetch."""

    models: list[str]
    endpoint_used: str
    status_code: int


_AUTH_HEADER_LEAK_RE = re.compile(
    r"(?i)(Bearer\s+|x-api-key\s*[:=]\s*)([^\s\"',;]+)"
)


def _sanitize_error(message: str, secret: str) -> str:
    """Remove any occurrence of the raw secret token from an error string."""
    sanitized = message
    if secret:
        sanitized = sanitized.replace(secret, "[REDACTED]")
    sanitized = _AUTH_HEADER_LEAK_RE.sub(r"\1[REDACTED]", sanitized)
    return sanitized


def build_models_urls(base_url: str, models_endpoint: str = "/v1/models") -> list[str]:
    """Build ordered candidate URLs for fetching models, avoiding /v1/v1 duplication."""
    base = validate_base_url(base_url)
    endpoint = (models_endpoint or "/v1/models").strip()
    if endpoint.startswith(("http://", "https://")):
        return [endpoint]
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"

    if base.endswith("/v1") and (endpoint == "/v1" or endpoint.startswith("/v1/")):
        primary = f"{base}{endpoint[3:]}"
    else:
        primary = f"{base}{endpoint}"

    urls: list[str] = [primary]
    if endpoint == "/v1/models":
        fallback = f"{base}/models"
        if fallback not in urls:
            urls.append(fallback)
    return urls


def parse_models_payload(payload: object) -> list[str]:
    """Extract a deduplicated, order-preserving list of model IDs from JSON payload."""
    items: list[object]
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload.get("models"), list):
            items = payload["models"]
        else:
            raise FetchError("Unexpected models payload: expected 'data' or 'models' list.")
    elif isinstance(payload, list):
        items = payload
    else:
        raise FetchError("Unexpected models payload: expected JSON object or array.")

    seen: set[str] = set()
    models: list[str] = []
    for item in items:
        model_id = ""
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            raw = item.get("id") or item.get("name")
            if isinstance(raw, str):
                model_id = raw.strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


async def fetch_models_async(
    base_url: str,
    auth_mode: str,
    auth_env: str,
    models_endpoint: str = "/v1/models",
    timeout: float = 15.0,
    client: httpx.AsyncClient | None = None,
    token: str | None = None,
) -> FetchResult:
    """Fetch available model IDs from a provider asynchronously.

    ``token`` lets callers (e.g. the editor) use a key that has not been stored
    yet; when omitted the key is resolved from the environment/secrets store.
    """
    if auth_mode not in AUTH_MODES:
        raise FetchError(f"Unsupported auth mode '{auth_mode}'.")

    if token is not None:
        token = token.strip()
        if not token:
            raise FetchError("API key is empty.")
    else:
        if not auth_env:
            raise FetchError("Provider auth_env is not configured.")
        token = resolve_env_var(auth_env)
        if not token:
            raise FetchError(
                f"No API key for '{auth_env}': export it or store it in the provider editor."
            )

    try:
        urls = build_models_urls(base_url, models_endpoint)
    except ConfigError as exc:
        raise FetchError(str(exc)) from None

    headers: dict[str, str] = {"Accept": "application/json"}
    if auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["x-api-key"] = token
        headers["anthropic-version"] = "2023-06-01"

    owns_client = client is None
    active_client = client if client is not None else httpx.AsyncClient(timeout=timeout)
    try:
        last_status = 0
        last_url = urls[0]
        for idx, url in enumerate(urls):
            last_url = url
            try:
                response = await active_client.get(url, headers=headers, timeout=timeout)
            except httpx.HTTPError as exc:
                msg = _sanitize_error(f"Request to {url} failed: {exc}", token)
                raise FetchError(msg) from None

            last_status = response.status_code
            if response.status_code == 404 and idx + 1 < len(urls):
                continue

            if response.status_code >= 400:
                snippet = response.text[:200].strip()
                msg = f"HTTP {response.status_code} from {url}"
                if snippet:
                    msg = f"{msg}: {snippet}"
                raise FetchError(_sanitize_error(msg, token))

            try:
                payload = response.json()
            except ValueError:
                raise FetchError(
                    _sanitize_error(f"Invalid JSON response from {url}.", token)
                ) from None

            models = parse_models_payload(payload)
            return FetchResult(
                models=models,
                endpoint_used=url,
                status_code=response.status_code,
            )

        raise FetchError(f"HTTP {last_status} from {last_url}.")
    finally:
        if owns_client:
            await active_client.aclose()


def fetch_models(
    base_url: str,
    auth_mode: str,
    auth_env: str,
    models_endpoint: str = "/v1/models",
    timeout: float = 15.0,
    token: str | None = None,
) -> FetchResult:
    """Synchronous wrapper around fetch_models_async."""
    return asyncio.run(
        fetch_models_async(
            base_url=base_url,
            auth_mode=auth_mode,
            auth_env=auth_env,
            models_endpoint=models_endpoint,
            timeout=timeout,
            token=token,
        )
    )
