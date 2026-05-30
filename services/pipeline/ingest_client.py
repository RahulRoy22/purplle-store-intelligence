from __future__ import annotations

import asyncio

import httpx


class IngestError(Exception):
    """Raised when a batch cannot be committed after all retries."""


class IngestClient:
    """
    Async HTTP client that POSTs event batches to /events/ingest.

    Splits large payloads into chunks of at most `batch_size` events and
    retries each chunk on HTTP 503 (transient server unavailability),
    sleeping `retry_delays[i]` seconds before attempt i+1.

    Non-retriable statuses (anything that is not 200 or 503) raise
    IngestError immediately — no retry is attempted.
    """

    def __init__(
        self,
        base_url: str,
        batch_size: int = 500,
        retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0),
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._batch_size = batch_size
        self._retry_delays = retry_delays

    async def send(self, events: list[dict]) -> None:
        if not events:
            return

        async with httpx.AsyncClient() as client:
            for start in range(0, len(events), self._batch_size):
                chunk = events[start : start + self._batch_size]
                await self._send_chunk(client, chunk)

    async def _send_chunk(self, client: httpx.AsyncClient, chunk: list[dict]) -> None:
        url = f"{self._base_url}/events/ingest"
        payload = {"events": chunk}

        # First attempt
        response = await client.post(url, json=payload)

        if response.status_code == 200:
            return

        if response.status_code != 503:
            raise IngestError(
                f"Non-retriable HTTP {response.status_code} from {url}"
            )

        # Retry loop — only reached on 503
        for delay in self._retry_delays:
            if delay > 0:
                await asyncio.sleep(delay)
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                return
            if response.status_code != 503:
                raise IngestError(
                    f"Non-retriable HTTP {response.status_code} from {url}"
                )

        raise IngestError(
            f"Batch failed after {1 + len(self._retry_delays)} attempts "
            f"(last status: {response.status_code})"
        )
