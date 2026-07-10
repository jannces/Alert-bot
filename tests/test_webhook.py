"""Tests for the TradingView webhook endpoint (auth, parsing, enqueueing)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings
from tradingview.webhook_handler import AlertPayload, create_app
from tests.fixtures import short_payload_dict

SECRET = "test-secret"


class PipelineStub:
    def __init__(self) -> None:
        self.enqueued: list[AlertPayload] = []
        self.malformed: list[tuple[object, str]] = []

    async def enqueue(self, payload: AlertPayload) -> None:
        self.enqueued.append(payload)

    def record_malformed(self, data: object, error: str) -> None:
        self.malformed.append((data, error))

    def stats(self) -> dict:
        return {"queued": len(self.enqueued)}


@pytest.fixture()
def pipeline() -> PipelineStub:
    return PipelineStub()


@pytest.fixture()
def client(pipeline: PipelineStub) -> TestClient:
    settings = Settings()
    settings.app.webhook_secret = SECRET
    return TestClient(create_app(settings, pipeline))


class TestAuthentication:
    def test_valid_secret_accepted(self, client, pipeline):
        response = client.post("/webhook/tradingview", json=short_payload_dict(SECRET))
        assert response.status_code == 202
        assert len(pipeline.enqueued) == 1

    def test_wrong_secret_rejected(self, client, pipeline):
        response = client.post("/webhook/tradingview", json=short_payload_dict("nope"))
        assert response.status_code == 403
        assert not pipeline.enqueued

    def test_missing_secret_rejected(self, client, pipeline):
        payload = short_payload_dict(SECRET)
        del payload["secret"]
        response = client.post("/webhook/tradingview", json=payload)
        assert response.status_code == 403
        assert not pipeline.enqueued

    def test_empty_configured_secret_rejects_everything(self, pipeline):
        settings = Settings()
        settings.app.webhook_secret = ""
        client = TestClient(create_app(settings, pipeline))
        response = client.post("/webhook/tradingview", json=short_payload_dict(""))
        assert response.status_code == 403


class TestPayloadParsing:
    def test_non_json_body_rejected(self, client):
        response = client.post("/webhook/tradingview", content=b"BTCUSDT crossed 110")
        assert response.status_code == 400

    def test_wrong_candle_count_rejected(self, client, pipeline):
        payload = short_payload_dict(SECRET)
        payload["candles"] = payload["candles"][:2]
        response = client.post("/webhook/tradingview", json=payload)
        assert response.status_code == 422
        assert len(pipeline.malformed) == 1

    def test_unknown_strategy_rejected(self, client):
        payload = short_payload_dict(SECRET)
        payload["strategy"] = "OTHER"
        assert client.post("/webhook/tradingview", json=payload).status_code == 422

    def test_bad_direction_rejected(self, client):
        payload = short_payload_dict(SECRET)
        payload["direction"] = "SIDEWAYS"
        assert client.post("/webhook/tradingview", json=payload).status_code == 422

    def test_payload_converts_to_setup(self):
        payload = AlertPayload.model_validate(short_payload_dict(SECRET))
        setup = payload.to_setup()
        assert setup.symbol == "BTCUSDT.P"
        assert setup.pair == "BTCUSDT"
        assert setup.timeframe_minutes == 15
        assert setup.timeframe_label == "15m"
        assert setup.candle3.close == setup.entry
        assert "secret" not in setup.raw_payload

    def test_health_endpoint(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
