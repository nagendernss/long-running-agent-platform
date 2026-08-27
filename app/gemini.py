"""One Gemini client, shared by everything that talks to it.

Both callers - the Agent Brain reading a reply, and the call agent deciding what to
say next - need the same three behaviours, and neither should own its own copy:

* **Rotate keys on a spent quota.** A 429 means the quota is gone; retrying the same
  key with a backoff cannot fix that, and during development it repeatedly downgraded
  extraction to keyword matching for the rest of the run. 401 and 403 are the same
  shape of problem. Rotation is immediate and does not sleep.
* **Retry the provider's own bad moments.** A 500 or 503 is not the key's fault, so
  that is retried on the same key, honouring Retry-After when it is sent.
* **Structured output.** Every call here asks for JSON against a schema, because
  parsing prose out of a model is how you get a workflow that behaves subtly wrong.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Sequence

import httpx

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.6-flash"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
# A different key can fix these; retrying the same one cannot.
ROTATE_STATUSES = frozenset({401, 403, 429})
RETRY_DELAYS = (1.0, 4.0)  # short: callers are on a live path


class GeminiClient:
    def __init__(
        self,
        api_keys: str | Sequence[str],
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
        retry_delays: Sequence[float] = RETRY_DELAYS,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_keys: tuple[str, ...] = (api_keys,) if isinstance(api_keys, str) else tuple(api_keys)
        if not self.api_keys:
            raise ValueError("GeminiClient needs at least one API key")
        self.model = model
        self.timeout = timeout
        self._retry_delays = tuple(retry_delays)
        self._client = client
        self._key_index = 0  # sticky: stay on whichever key last worked

    @property
    def api_key(self) -> str:
        return self.api_keys[self._key_index]

    def _rotate_key(self) -> bool:
        if len(self.api_keys) < 2:
            return False
        self._key_index = (self._key_index + 1) % len(self.api_keys)
        return True

    async def generate_json(
        self, *, system: str, user: str, schema: dict[str, Any], temperature: float = 0.0
    ) -> dict[str, Any]:
        last: Exception | None = None
        keys_tried = 1
        for attempt in range(len(self._retry_delays) + 1):
            try:
                return await self._post(system, user, schema, temperature)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in ROTATE_STATUSES and keys_tried < len(self.api_keys) and self._rotate_key():
                    keys_tried += 1
                    log.warning("gemini key returned %s, trying the next key", status)
                    continue
                if status not in RETRY_STATUSES or attempt == len(self._retry_delays):
                    raise
                last = exc
                delay = self._retry_delays[attempt]
                retry_after = exc.response.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = min(float(retry_after), 10.0)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == len(self._retry_delays):
                    raise
                last = exc
                delay = self._retry_delays[attempt]
            log.info("gemini call failed (%s), retrying in %.1fs", type(last).__name__, delay)
            await asyncio.sleep(delay)
        raise last  # unreachable, keeps type checkers happy

    async def _post(self, system: str, user: str, schema: dict[str, Any], temperature: float) -> dict[str, Any]:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": temperature,
            },
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        url = f"{API_ROOT}/{self.model}:generateContent"
        if self._client is not None:
            response = await self._client.post(url, json=body, headers=headers, timeout=self.timeout)
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = next(p["text"] for p in reversed(parts) if "text" in p)
        return json.loads(text)
