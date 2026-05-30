# PROMPT: Design a pytest-httpx test suite for an async batch HTTP client that
#   POSTs to /events/ingest with configurable batch size and exponential-backoff
#   retry on 503. Include: (1) 503-twice-then-200 retry fixture without real
#   sleeps; (2) non-retriable errors (422, 500) raise IngestError immediately;
#   (3) send([]) makes exactly 0 HTTP requests; (4) batch splitting — N events
#   with batch_size=B makes ceil(N/B) requests, last batch may be smaller.
#
# CHANGES MADE: Used retry_delays=(0, 0) in tests to avoid asyncio.sleep delays.
#   Added @pytest.mark.httpx_mock(assert_all_responses_were_requested=False) on
#   tests that raise NotImplementedError before consuming mock responses, to
#   prevent pytest-httpx teardown errors during RED phase.
"""
Phase 4 Tests — Task 7: IngestClient  (RED phase)

IngestClient is an async HTTP client that POSTs EventIn-shaped dicts
to the /events/ingest endpoint in batches, with configurable batch size
and exponential-backoff retry on transient failures.

Contract:
  IngestClient(base_url, batch_size=500, retry_delays=(1, 2, 4))
    base_url:      str  — e.g. "http://localhost:8000"
    batch_size:    int  — max events per POST; default 500
    retry_delays:  tuple[float, ...]  — sleep seconds between retries;
                   length = max retry attempts. Pass (0,) in tests to
                   avoid real waits.

  async send(events: list[dict]) -> None
    → POSTs events in batches of exactly batch_size (last batch may be smaller)
    → send([]) makes 0 HTTP requests
    → each POST targets  POST {base_url}/events/ingest
    → request body is JSON: {"events": [...]}
    → on HTTP 200 the batch is considered committed; no retry
    → on HTTP 503 the client retries up to len(retry_delays) more times,
      sleeping retry_delays[i] seconds before attempt i+1
    → if all retries are exhausted (still 503), raises IngestError
    → on any non-200/503 status (e.g. 422, 500), raises IngestError immediately
      (no retry — these indicate a malformed batch, not a transient fault)

  IngestError(Exception) — raised when a batch cannot be committed

No live FastAPI server is required.  All HTTP is intercepted via
pytest-httpx's httpx_mock fixture.

Note on @pytest.mark.httpx_mock(assert_all_responses_were_requested=False):
  In RED state the stub raises NotImplementedError before making any HTTP
  requests, so registered mock responses are never consumed. This marker
  silences pytest-httpx's teardown assertion about unconsumed responses,
  keeping RED failures clean: every test fails with NotImplementedError,
  not a mix of FAILED + ERROR.

Prompt used to design the retry fixture (AI-assisted):
  "Design a pytest fixture that simulates a server returning 503 twice
   then 200, to test retry-with-backoff without sleeping."
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from ingest_client import IngestClient, IngestError   # services/pipeline/ingest_client.py


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _events(n: int) -> list[dict]:
    """Return n minimal event-shaped dicts (content irrelevant for routing tests)."""
    return [{"event_id": f"evt-{i:05d}", "visitor_id": f"vis_{i:04d}"} for i in range(n)]


_URL = "http://fake-api:8000"


# ---------------------------------------------------------------------------
# Batch-splitting — number of requests
# ---------------------------------------------------------------------------

class TestBatchSplitting:

    @pytest.mark.asyncio
    async def test_empty_events_makes_no_requests(self, httpx_mock):
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        await client.send([])
        assert httpx_mock.get_requests() == [], "send([]) must make zero HTTP requests"

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_one_event_makes_one_request(self, httpx_mock):
        httpx_mock.add_response(status_code=200, json={"accepted": 1, "duplicate": 0, "rejected": []})
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        await client.send(_events(1))
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_batch_size_events_makes_one_request(self, httpx_mock):
        """Exactly batch_size events → 1 POST (boundary inclusive)."""
        httpx_mock.add_response(status_code=200, json={"accepted": 500})
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        await client.send(_events(500))
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_batch_size_plus_one_makes_two_requests(self, httpx_mock):
        """501 events with batch_size=500 → 2 POSTs (500 + 1)."""
        httpx_mock.add_response(status_code=200, json={"accepted": 500})
        httpx_mock.add_response(status_code=200, json={"accepted": 1})
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        await client.send(_events(501))
        assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_thousand_events_two_requests(self, httpx_mock):
        """1000 events with batch_size=500 → exactly 2 POSTs."""
        httpx_mock.add_response(status_code=200, json={"accepted": 500})
        httpx_mock.add_response(status_code=200, json={"accepted": 500})
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        await client.send(_events(1000))
        assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_1001_events_three_requests(self, httpx_mock):
        """1001 events with batch_size=500 → 3 POSTs (500+500+1)."""
        httpx_mock.add_response(status_code=200, json={"accepted": 500})
        httpx_mock.add_response(status_code=200, json={"accepted": 500})
        httpx_mock.add_response(status_code=200, json={"accepted": 1})
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        await client.send(_events(1001))
        assert len(httpx_mock.get_requests()) == 3

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_custom_batch_size_respected(self, httpx_mock):
        """batch_size=10 with 25 events → 3 POSTs (10+10+5)."""
        for _ in range(3):
            httpx_mock.add_response(status_code=200, json={"accepted": 10})
        client = IngestClient(_URL, batch_size=10, retry_delays=())
        await client.send(_events(25))
        assert len(httpx_mock.get_requests()) == 3


# ---------------------------------------------------------------------------
# Batch payload structure
# ---------------------------------------------------------------------------

class TestBatchPayload:

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_request_body_is_json_with_events_key(self, httpx_mock):
        """POST body must be JSON with an 'events' key containing the batch."""
        httpx_mock.add_response(status_code=200, json={"accepted": 2})
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        await client.send(_events(2))
        req = httpx_mock.get_requests()[0]
        body = json.loads(req.content)
        assert "events" in body, "Request body must have an 'events' key"
        assert len(body["events"]) == 2

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_request_targets_correct_url(self, httpx_mock):
        """POST must go to {base_url}/events/ingest."""
        httpx_mock.add_response(status_code=200, json={"accepted": 1})
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        await client.send(_events(1))
        req = httpx_mock.get_requests()[0]
        assert req.url.path == "/events/ingest"

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_first_batch_contains_correct_events(self, httpx_mock):
        """First batch of a 2-batch send must contain exactly batch_size events."""
        httpx_mock.add_response(status_code=200, json={"accepted": 3})
        httpx_mock.add_response(status_code=200, json={"accepted": 2})
        client = IngestClient(_URL, batch_size=3, retry_delays=())
        evts = _events(5)
        await client.send(evts)
        first_body = json.loads(httpx_mock.get_requests()[0].content)
        second_body = json.loads(httpx_mock.get_requests()[1].content)
        assert len(first_body["events"]) == 3
        assert len(second_body["events"]) == 2


# ---------------------------------------------------------------------------
# Retry on 503
# ---------------------------------------------------------------------------

class TestRetry:

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_success_on_first_attempt_makes_one_request(self, httpx_mock):
        httpx_mock.add_response(status_code=200, json={"accepted": 1})
        client = IngestClient(_URL, batch_size=500, retry_delays=(0,))
        await client.send(_events(1))
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_503_then_200_succeeds(self, httpx_mock):
        """First attempt returns 503; retry returns 200 → no exception raised."""
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(status_code=200, json={"accepted": 1})
        client = IngestClient(_URL, batch_size=500, retry_delays=(0,))
        await client.send(_events(1))          # must not raise
        assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_503_twice_then_200_succeeds(self, httpx_mock):
        """Two transient failures followed by success — 3 total attempts."""
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(status_code=200, json={"accepted": 1})
        client = IngestClient(_URL, batch_size=500, retry_delays=(0, 0))
        await client.send(_events(1))
        assert len(httpx_mock.get_requests()) == 3

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_503_exhausts_retries_raises_ingest_error(self, httpx_mock):
        """503 on every attempt → IngestError raised after retries exhausted."""
        for _ in range(3):   # 1 initial + 2 retries = 3 total attempts
            httpx_mock.add_response(status_code=503)
        client = IngestClient(_URL, batch_size=500, retry_delays=(0, 0))
        with pytest.raises(IngestError):
            await client.send(_events(1))

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_retry_count_matches_retry_delays_length(self, httpx_mock):
        """With retry_delays=(0, 0, 0) (3 retries), client makes 4 total attempts."""
        for _ in range(4):
            httpx_mock.add_response(status_code=503)
        client = IngestClient(_URL, batch_size=500, retry_delays=(0, 0, 0))
        with pytest.raises(IngestError):
            await client.send(_events(1))
        assert len(httpx_mock.get_requests()) == 4   # 1 initial + 3 retries

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_zero_retries_raises_immediately_on_503(self, httpx_mock):
        """retry_delays=() means no retries — 503 raises IngestError immediately."""
        httpx_mock.add_response(status_code=503)
        client = IngestClient(_URL, batch_size=500, retry_delays=())
        with pytest.raises(IngestError):
            await client.send(_events(1))
        assert len(httpx_mock.get_requests()) == 1


# ---------------------------------------------------------------------------
# Non-retriable errors
# ---------------------------------------------------------------------------

class TestNonRetriableErrors:

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_422_raises_ingest_error_immediately(self, httpx_mock):
        """422 Unprocessable = bad payload; must not retry."""
        httpx_mock.add_response(status_code=422, json={"detail": "bad"})
        client = IngestClient(_URL, batch_size=500, retry_delays=(0, 0, 0))
        with pytest.raises(IngestError):
            await client.send(_events(1))
        # Must have made exactly 1 request — no retry
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_500_raises_ingest_error_immediately(self, httpx_mock):
        """500 Internal Server Error is non-retriable."""
        httpx_mock.add_response(status_code=500)
        client = IngestClient(_URL, batch_size=500, retry_delays=(0, 0, 0))
        with pytest.raises(IngestError):
            await client.send(_events(1))
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    async def test_second_batch_failure_raises_after_first_succeeds(self, httpx_mock):
        """
        If the first batch succeeds but the second fails (non-retriable),
        IngestError is raised. The first batch is NOT re-sent.
        """
        httpx_mock.add_response(status_code=200, json={"accepted": 3})
        httpx_mock.add_response(status_code=422, json={"detail": "bad"})
        client = IngestClient(_URL, batch_size=3, retry_delays=())
        with pytest.raises(IngestError):
            await client.send(_events(6))
        assert len(httpx_mock.get_requests()) == 2   # batch 1 OK, batch 2 failed
