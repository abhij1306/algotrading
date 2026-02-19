# Canonical API Namespace Map (Phase-1 Freeze)

This file defines the canonical endpoint surface to be preserved during PRD v2 Phase-1 rebuild.

## WebSocket Core
- `GET /api/websocket/status`
- `POST /api/websocket/connect`
- `POST /api/websocket/subscribe`
- `POST /api/websocket/disconnect`
- `WS /api/websocket/stream`

## Dashboard / Market
- `GET /api/market/global`
- `GET /api/market/sentiment`
- `GET /api/market/condition`
- `GET /api/market/options-sentiment`

## Screener
- `GET /api/screener/indices`
- `GET /api/screener/results`

## Terminal
- `GET /api/terminal/chart`
- `GET /api/terminal/orders`
- `GET /api/terminal/positions`
- `POST /api/terminal/order`
- `GET /api/terminal/paper/positions`
- `POST /api/terminal/paper/order`

## Backtest
- `GET /api/backtest/status`
- `POST /api/backtest/run`
- `GET /api/backtest/result/{job_id}`

## Data Snapshot (Phase-1 Pipeline)
- `GET /api/data/snapshot/stock`
- `GET /api/data/snapshot/universe`
- `GET /api/data/snapshot/status`

## Deviation Policy
- Any endpoint outside this surface is considered legacy unless explicitly justified in `decision-log.md`.
- Legacy paths may remain temporarily only if required by existing UI during migration slices.
