"""FastAPI webhook endpoint receiving TradingView alerts.

TradingView fires one webhook per alert; the alert message is the JSON
document built by ``tradingview/pine_script.pine``. The endpoint:

1. authenticates the shared secret (constant-time comparison),
2. parses and structurally validates the payload,
3. enqueues it for asynchronous processing and returns immediately.

TradingView allows only a few seconds for webhook delivery, so no slow work
(validation, screenshots, Telegram) happens on the request path — the
:class:`strategy.pipeline.SignalPipeline` worker does all of that.
"""

from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response, status
from pydantic import BaseModel, Field, ValidationError, field_validator

from config.settings import Settings
from strategy.models import Candle, Direction, TamadSetup

logger = logging.getLogger(__name__)


class CandlePayload(BaseModel):
    t: int = Field(gt=0, description="bar open time, ms since epoch")
    o: float = Field(gt=0)
    h: float = Field(gt=0)
    l: float = Field(gt=0)
    c: float = Field(gt=0)

    def to_candle(self) -> Candle:
        return Candle(open_time_ms=self.t, open=self.o, high=self.h, low=self.l, close=self.c)


class AlertPayload(BaseModel):
    """Schema of the JSON alert emitted by the Pine script."""

    secret: str = ""
    strategy: str
    version: int = 1
    exchange: str
    symbol: str
    timeframe: str
    direction: Direction
    level: float = Field(gt=0)
    sr_type: str
    sr_level: float = Field(gt=0)
    candles: list[CandlePayload] = Field(min_length=3, max_length=3)
    entry: float = Field(gt=0)
    sl: float = Field(gt=0)
    tp2: float
    tp3: float
    detected_at_ms: int = Field(gt=0)

    @field_validator("strategy")
    @classmethod
    def _must_be_tamad(cls, v: str) -> str:
        if v.upper() != "TAMAD":
            raise ValueError(f"unexpected strategy {v!r}")
        return v

    @field_validator("timeframe")
    @classmethod
    def _timeframe_minutes(cls, v: str) -> str:
        if not v.isdigit() or int(v) <= 0:
            raise ValueError(f"timeframe must be minutes, got {v!r}")
        return v

    def to_setup(self) -> TamadSetup:
        c1, c2, c3 = (c.to_candle() for c in self.candles)
        return TamadSetup(
            exchange=self.exchange,
            symbol=self.symbol,
            timeframe_minutes=int(self.timeframe),
            direction=self.direction,
            candle1=c1,
            candle2=c2,
            candle3=c3,
            level=self.level,
            sr_type=self.sr_type,
            sr_level=self.sr_level,
            entry=self.entry,
            stop_loss=self.sl,
            tp2=self.tp2,
            tp3=self.tp3,
            detected_at=datetime.fromtimestamp(
                self.detected_at_ms / 1000, tz=timezone.utc
            ),
            raw_payload=self.model_dump(exclude={"secret"}),
        )


def create_app(settings: Settings, pipeline) -> FastAPI:
    """Build the FastAPI application around a signal pipeline.

    ``pipeline`` is anything exposing ``enqueue``/``record_malformed``/
    ``stats`` — in production a :class:`strategy.pipeline.SignalPipeline`.
    """
    app = FastAPI(
        title="Tamad Strategy Scanner", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", **pipeline.stats()}

    @app.post("/webhook/tradingview")
    async def tradingview_webhook(request: Request) -> Response:
        body = await request.body()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("webhook: non-JSON body rejected (%d bytes)", len(body))
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

        secret = str(data.get("secret", "")) if isinstance(data, dict) else ""
        if not settings.app.webhook_secret or not hmac.compare_digest(
            secret, settings.app.webhook_secret
        ):
            logger.warning("webhook: rejected request with bad secret")
            return Response(status_code=status.HTTP_403_FORBIDDEN)

        try:
            payload = AlertPayload.model_validate(data)
        except ValidationError as exc:
            logger.warning("webhook: malformed alert payload: %s", exc)
            pipeline.record_malformed(data, str(exc))
            return Response(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

        logger.info(
            "webhook: accepted %s %s %sm alert",
            payload.direction.value,
            payload.symbol,
            payload.timeframe,
        )
        await pipeline.enqueue(payload)
        return Response(status_code=status.HTTP_202_ACCEPTED)

    return app
