from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bn_monitor import models


async def upsert_symbols(session: AsyncSession, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    stmt = insert(models.symbols).values(rows)
    update_cols = {
        col.name: getattr(stmt.excluded, col.name)
        for col in models.symbols.c
        if col.name not in {"exchange", "market_type", "symbol"}
    }
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["exchange", "market_type", "symbol"], set_=update_cols
    ))


async def upsert_kline(session: AsyncSession, row: dict[str, Any]) -> None:
    stmt = insert(models.futures_kline_1m).values(row)
    update_cols = {
        col.name: getattr(stmt.excluded, col.name)
        for col in models.futures_kline_1m.c
        if col.name not in {"ts", "symbol", "created_at"}
    }
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["ts", "symbol"], set_=update_cols
    ))


async def upsert_open_interest(session: AsyncSession, row: dict[str, Any]) -> None:
    stmt = insert(models.futures_open_interest).values(row)
    update_cols = {
        col.name: getattr(stmt.excluded, col.name)
        for col in models.futures_open_interest.c
        if col.name not in {"ts", "symbol", "created_at"}
    }
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["ts", "symbol"], set_=update_cols
    ))


async def upsert_mark_price(session: AsyncSession, row: dict[str, Any]) -> None:
    stmt = insert(models.futures_mark_price).values(row)
    update_cols = {
        col.name: getattr(stmt.excluded, col.name)
        for col in models.futures_mark_price.c
        if col.name not in {"ts", "symbol", "created_at"}
    }
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["ts", "symbol"], set_=update_cols
    ))


async def upsert_funding_history(session: AsyncSession, row: dict[str, Any]) -> None:
    stmt = insert(models.futures_mark_price).values(row)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["ts", "symbol"],
            set_={
                "mark_price": stmt.excluded.mark_price,
                "funding_rate": stmt.excluded.funding_rate,
            },
        )
    )


async def insert_liquidation_snapshot(session: AsyncSession, row: dict[str, Any]) -> None:
    await session.execute(insert(models.liquidation_snapshots).values(row))


async def upsert_indicator(session: AsyncSession, row: dict[str, Any]) -> None:
    table_columns = {column.name for column in models.indicator_snapshot_1m.c}
    stmt = insert(models.indicator_snapshot_1m).values(
        {key: value for key, value in row.items() if key in table_columns}
    )
    update_cols = {
        col.name: getattr(stmt.excluded, col.name)
        for col in models.indicator_snapshot_1m.c
        if col.name not in {"ts", "symbol", "created_at"}
    }
    await session.execute(stmt.on_conflict_do_update(
        index_elements=["ts", "symbol"], set_=update_cols
    ))


async def upsert_market_factor(session: AsyncSession, row: dict[str, Any]) -> None:
    stmt = insert(models.market_factor_1m).values(row)
    update_cols = {
        col.name: getattr(stmt.excluded, col.name)
        for col in models.market_factor_1m.c
        if col.name not in {"ts", "created_at"}
    }
    await session.execute(stmt.on_conflict_do_update(index_elements=["ts"], set_=update_cols))


async def insert_alert(
    session: AsyncSession,
    alert: dict[str, Any],
    mode: str,
    delivery_status: str,
    discord_sent_at: datetime | None = None,
) -> tuple[datetime, int] | None:
    stmt = (
        insert(models.alerts)
        .values({**alert, "mode": mode, "delivery_status": delivery_status, "discord_sent_at": discord_sent_at})
        .on_conflict_do_nothing(index_elements=["ts", "symbol", "alert_type", "mode"])
        .returning(models.alerts.c.ts, models.alerts.c.id)
    )
    result = await session.execute(stmt)
    row = result.one_or_none()
    return (row.ts, row.id) if row else None


async def find_recent_active_alert(
    session: AsyncSession,
    symbol: str,
    alert_type: str,
    mode: str,
    since: datetime,
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(models.alerts)
            .where(
                and_(
                    models.alerts.c.symbol == symbol,
                    models.alerts.c.alert_type == alert_type,
                    models.alerts.c.mode == mode,
                    models.alerts.c.state.in_(["open", "escalated"]),
                    models.alerts.c.ts >= since,
                )
            )
            .order_by(models.alerts.c.ts.desc())
            .limit(1)
        )
    ).mappings().first()
    return dict(row) if row else None


async def resolve_inactive_alerts(
    session: AsyncSession,
    symbols: list[str],
    alert_types: list[str],
    active_keys: set[tuple[str, str]],
    mode: str,
) -> int:
    count = 0
    for symbol in symbols:
        inactive_types = [alert_type for alert_type in alert_types if (symbol, alert_type) not in active_keys]
        if not inactive_types:
            continue
        result = await session.execute(
            update(models.alerts)
            .where(
                and_(
                    models.alerts.c.symbol == symbol,
                    models.alerts.c.alert_type.in_(inactive_types),
                    models.alerts.c.mode == mode,
                    models.alerts.c.state.in_(["open", "escalated"]),
                )
            )
            .values(state="resolved")
        )
        count += result.rowcount or 0
    return count


async def expire_stale_alerts(session: AsyncSession, before: datetime) -> int:
    result = await session.execute(
        update(models.alerts)
        .where(and_(models.alerts.c.state.in_(["open", "escalated"]), models.alerts.c.ts < before))
        .values(state="expired")
    )
    return result.rowcount or 0


async def update_alert_delivery(
    session: AsyncSession,
    alert_ts: datetime,
    alert_id: int,
    delivery_status: str,
    discord_sent_at: datetime | None = None,
) -> None:
    await session.execute(
        update(models.alerts)
        .where(and_(models.alerts.c.ts == alert_ts, models.alerts.c.id == alert_id))
        .values(delivery_status=delivery_status, discord_sent_at=discord_sent_at)
    )


async def update_cooldown(
    session: AsyncSession, key: str, now: datetime, score: Decimal, count_1h: int
) -> None:
    stmt = insert(models.alert_cooldowns).values(
        key=key, last_sent_at=now, last_score=score, count_1h=count_1h, updated_at=now
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "last_sent_at": stmt.excluded.last_sent_at,
                "last_score": stmt.excluded.last_score,
                "count_1h": stmt.excluded.count_1h,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    )


async def get_cooldown(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(models.alert_cooldowns).where(models.alert_cooldowns.c.key == key)
        )
    ).mappings().first()
    return dict(row) if row else None
