from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class AlertSummary:
    symbol: str
    alert_type: str
    severity: str
    mode: str
    state: str
    delivery_status: str
    count: int


async def alert_summary(
    session: AsyncSession,
    lookback_hours: int = 24,
    symbol: str | None = None,
    alert_type: str | None = None,
    severity: str | None = None,
) -> list[AlertSummary]:
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    rows = (
        await session.execute(
            text(
                """
                SELECT symbol, alert_type, severity, mode, state, delivery_status, count(*) AS count
                FROM alerts
                WHERE ts >= :since
                  AND (:symbol IS NULL OR symbol = :symbol)
                  AND (:alert_type IS NULL OR alert_type = :alert_type)
                  AND (:severity IS NULL OR severity = :severity)
                GROUP BY symbol, alert_type, severity, mode, state, delivery_status
                ORDER BY symbol, alert_type, severity, mode, state, delivery_status
                """
            ),
            {
                "since": since,
                "symbol": symbol.upper() if symbol else None,
                "alert_type": alert_type,
                "severity": severity.upper() if severity else None,
            },
        )
    ).mappings().all()
    return [
        AlertSummary(
            symbol=row["symbol"],
            alert_type=row["alert_type"],
            severity=row["severity"],
            mode=row["mode"],
            state=row["state"],
            delivery_status=row["delivery_status"],
            count=int(row["count"]),
        )
        for row in rows
    ]
