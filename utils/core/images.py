"""Image generation.

One entry point - :func:`generate_image` - turns a text prompt into an image for
the ``,imagegen`` command, the AI's ``generate_image`` tool and the image buttons
the embed builder can add. Providers are tried in order, and only the last one
has to work for an image to come back:

1. **Cloudflare Workers AI** (``@cf/black-forest-labs/flux-1-schnell``) whenever
   ``CLOUDFLARE_AI_TOKEN`` is set. The account's free Workers AI allowance is
   10,000 Neurons a day - roughly 200 images at the model's 4 default steps - and
   it runs on the same Cloudflare account the bot already has for its R2 bucket,
   so ``R2_ACCOUNT_ID`` supplies the account id when ``CLOUDFLARE_ACCOUNT_ID`` is
   not set. Docs: https://developers.cloudflare.com/workers-ai/models/flux-1-schnell

2. **Pollinations, keyed** (``gen.pollinations.ai``). Honours ``model`` /
   ``width`` / ``height`` and gives the best quality, but it needs pollen on the
   account, so a key with no balance answers ``HTTP 402`` to every prompt it has
   not already cached.

3. **Pollinations, anonymous** (``image.pollinations.ai``). Free and needs no key
   at all, but renders with Sana and only takes the prompt and a seed. It serves
   about one fresh image every 20 seconds and answers an empty-bodied ``402``
   while it cools down, so this attempt is the one that gets retried.

Docs for the keyed endpoint: https://gen.pollinations.ai — ``GET /image/{prompt}``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp

#: Pollinations API key, baked in directly.
API_KEY = "sk_o6OIlM6wlijuXBZsR250VHlMD4cjI8os"
#: Base image endpoint. The prompt is appended, URL-encoded.
IMAGE_ENDPOINT = "https://gen.pollinations.ai/image"
#: Free, anonymous image endpoint (Sana only, no key and no credit).
FREE_IMAGE_ENDPOINT = "https://image.pollinations.ai/prompt"
#: Default model alias; ``flux`` is the quick, always-available general model.
DEFAULT_IMAGE_MODEL = "flux"

#: Cloudflare Workers AI root; the account id and model build the run URL.
CLOUDFLARE_AI_BASE = "https://api.cloudflare.com/client/v4/accounts"
#: FLUX.1 [schnell] - fast, free on the Workers AI allowance and much better
#: than anything else on a free tier. Overridable with ``CLOUDFLARE_AI_MODEL``.
DEFAULT_CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
#: Diffusion steps: 4 is the model's default, and each extra step costs neurons.
CLOUDFLARE_IMAGE_STEPS = 4

#: Generation can take a little while on busier models.
IMAGE_TIMEOUT_SECONDS = 90
#: Wait before retrying a request that failed for a transient reason (dropped
#: connection, or the free tier's soft rate limit). Repeats multiply this.
IMAGE_RETRY_SECONDS = 5.0
#: How many extra goes the free anonymous endpoint gets. Its limit rolls about
#: every 30 seconds, so the waits below land goes at roughly 0s, 5s, 15s and 30s.
FREE_IMAGE_RETRIES = 3
#: Refuse anything larger than this so a rogue response can't blow up memory.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: Statuses worth another go: ``402``/``429`` are how the endpoints say "not
#: right now" (the free tier uses an empty-bodied 402 as its rate limit) and the
#: 5xx family is the service being briefly unavailable.
_TRANSIENT_STATUSES = frozenset({402, 429, 500, 502, 503, 504})

_CONTENT_TYPE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}


def _env(name: str, default: str = "") -> str:
    """A trimmed environment variable, or ``default``."""
    value = os.getenv(name)
    return (value if value is not None else default).strip()


def api_key() -> str | None:
    """The Pollinations key, or ``None`` when it is not configured."""
    return API_KEY.strip() or None


def cloudflare_ai() -> tuple[str, str, str] | None:
    """``(account_id, token, model)`` for Workers AI, or ``None`` when unset.

    The token is the only thing that has to be provided: Workers AI's endpoint
    takes the same account id the R2 bucket already uses.
    """
    token = _env("CLOUDFLARE_AI_TOKEN")
    account_id = _env("CLOUDFLARE_ACCOUNT_ID") or _env("R2_ACCOUNT_ID")
    if not token or not account_id:
        return None
    model = _env("CLOUDFLARE_AI_MODEL") or DEFAULT_CLOUDFLARE_IMAGE_MODEL
    return account_id, token, model


def _error_detail(body: str) -> str:
    """One short line from an error body (both APIs answer with JSON)."""
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return text[:200]

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
        if isinstance(error, str):
            return error[:200]
        if data.get("message"):
            return str(data["message"])[:200]
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("message"):
                return str(first["message"])[:200]
            return str(first)[:200]
    return text[:200]


def _failure_reason(exc: BaseException) -> str:
    """A readable reason for a failed request, even when the error is blank."""
    text = str(exc).strip() or type(exc).__name__
    return f"the image request failed ({text})"


@dataclass(slots=True)
class GeneratedImage:
    """One rendered image, plus enough metadata to attach it to Discord."""

    data: bytes
    content_type: str = "image/png"

    @property
    def extension(self) -> str:
        base = self.content_type.split(";", 1)[0].strip().lower()
        return _CONTENT_TYPE_EXTENSIONS.get(base, "png")

    @property
    def filename(self) -> str:
        return f"image.{self.extension}"


def _build_image(data: bytes, content_type: str) -> tuple[GeneratedImage | None, str | None]:
    """Validate one response body and wrap it, or explain why it is unusable."""
    if not data:
        return None, "an empty image came back"
    if len(data) > MAX_IMAGE_BYTES:
        return None, "the generated image was too large to post"
    return GeneratedImage(data=data, content_type=content_type or "image/png"), None


async def _cloudflare_image(
    session: aiohttp.ClientSession, prompt: str, *, seed: int | None = None
) -> tuple[GeneratedImage | None, str | None, bool]:
    """One image from Cloudflare Workers AI, or ``(None, reason, transient)``."""
    config = cloudflare_ai()
    if config is None:
        return None, "Cloudflare Workers AI is not configured", False
    account_id, token, model = config

    payload: dict[str, Any] = {"prompt": prompt, "steps": CLOUDFLARE_IMAGE_STEPS}
    if seed is not None:
        payload["seed"] = int(seed)

    try:
        async with session.post(
            f"{CLOUDFLARE_AI_BASE}/{account_id}/ai/run/{model}",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            status = response.status
            body = await response.text()
    except (aiohttp.ClientError, TimeoutError) as exc:
        return None, _failure_reason(exc), True
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a reason
        return None, _failure_reason(exc), True

    if status != 200:
        detail = _error_detail(body)
        return (
            None,
            f"Cloudflare returned HTTP {status}" + (f": {detail}" if detail else ""),
            status in _TRANSIENT_STATUSES,
        )

    try:
        data = json.loads(body)
    except ValueError:
        return None, "Cloudflare returned an unreadable response", False
    if not isinstance(data, dict):
        return None, "Cloudflare returned an unreadable response", False

    result = data.get("result")
    encoded = result.get("image") if isinstance(result, dict) else None
    if not encoded:
        detail = _error_detail(body)
        return (
            None,
            "Cloudflare returned no image" + (f": {detail}" if detail else ""),
            False,
        )

    try:
        # flux-1-schnell hands the render back as a base64 JPEG.
        raw = base64.b64decode(str(encoded))
    except ValueError:
        return None, "Cloudflare returned an undecodable image", False
    return (*_build_image(raw, "image/jpeg"), False)


async def _fetch_image(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
) -> tuple[GeneratedImage | None, str | None, bool]:
    """One Pollinations request.

    Returns ``(image, None, False)`` on success, or ``(None, reason, transient)``
    where ``transient`` marks a failure worth another go: a dropped connection,
    or a "not right now" status like the free tier's empty-bodied 402.
    """
    try:
        async with session.get(url, params=params, headers=headers) as response:
            if response.status != 200:
                detail = _error_detail(await response.text())
                return (
                    None,
                    f"Pollinations returned HTTP {response.status}"
                    + (f": {detail}" if detail else ""),
                    response.status in _TRANSIENT_STATUSES,
                )
            data = await response.read()
            content_type = response.headers.get("Content-Type", "image/png")
    except (aiohttp.ClientError, TimeoutError) as exc:
        return None, _failure_reason(exc), True
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a reason
        return None, _failure_reason(exc), True

    return (*_build_image(data, content_type), False)


def _pollinations_attempts(
    prompt: str,
    *,
    model: str,
    width: int | None,
    height: int | None,
    seed: int | None,
) -> list[tuple[str, dict[str, str], dict[str, str], int]]:
    """Pollinations requests to make, as ``(url, params, headers, retries)``.

    The keyed endpoint goes first. The anonymous one only accepts the prompt and
    a seed - it answers 402 to ``model``/``width``/``height`` - so it is sent
    those two only, with the retries it needs for its rolling rate limit.
    """
    quoted = urllib.parse.quote(prompt, safe="")
    attempts: list[tuple[str, dict[str, str], dict[str, str], int]] = []

    key = api_key()
    if key is not None:
        params: dict[str, str] = {"model": model or DEFAULT_IMAGE_MODEL}
        if width:
            params["width"] = str(int(width))
        if height:
            params["height"] = str(int(height))
        if seed is not None:
            params["seed"] = str(int(seed))
        # No retries here: a key that is out of pollen answers 402 every time.
        attempts.append(
            (
                f"{IMAGE_ENDPOINT}/{quoted}",
                params,
                {"Authorization": f"Bearer {key}"},
                0,
            )
        )

    free_params: dict[str, str] = {}
    if seed is not None:
        free_params["seed"] = str(int(seed))
    attempts.append(
        (f"{FREE_IMAGE_ENDPOINT}/{quoted}", free_params, {}, FREE_IMAGE_RETRIES)
    )
    return attempts


async def generate_image(
    prompt: str,
    *,
    model: str = DEFAULT_IMAGE_MODEL,
    width: int | None = None,
    height: int | None = None,
    seed: int | None = None,
) -> tuple[GeneratedImage | None, str | None]:
    """Render ``prompt`` and return ``(image, None)`` or ``(None, reason)``.

    Cloudflare Workers AI is used when it is configured, then Pollinations. The
    ``model``/``width``/``height`` arguments are Pollinations options; Workers AI
    always renders with its own FLUX model.

    The Pollinations key travels in the ``Authorization`` header rather than the
    query string, so it never ends up baked into a URL that could be handed to a
    user.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "an image prompt is required"

    last_error = "the image request failed"
    timeout = aiohttp.ClientTimeout(total=IMAGE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if cloudflare_ai() is not None:
            image, error, _ = await _cloudflare_image(session, prompt, seed=seed)
            if image is not None:
                return image, None
            last_error = error or last_error

        attempts = _pollinations_attempts(
            prompt, model=model, width=width, height=height, seed=seed
        )
        for url, params, headers, retries in attempts:
            for attempt in range(retries + 1):
                image, error, transient = await _fetch_image(
                    session, url, params, headers
                )
                if image is not None:
                    return image, None
                last_error = error or last_error
                if not transient or attempt >= retries:
                    break
                # Space the retries out, since the free tier's limit rolls.
                await asyncio.sleep(IMAGE_RETRY_SECONDS * (attempt + 1))
    return None, last_error
