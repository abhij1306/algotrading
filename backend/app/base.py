from sqlalchemy.orm import declarative_base

# hat_live: the stock data, written only by the data pipeline and importers (docs/architecture.md,
# Storage). Every other connection to it is read-only.
Base = declarative_base()

# hat_state: the trading and runtime state (signals, orders, positions, events, job runs, ...).
StateBase = declarative_base()
