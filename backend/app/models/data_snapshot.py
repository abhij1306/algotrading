from datetime import datetime

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from ..base import Base


class DatasetRun(Base):
    """Tracks each Phase-1 dataset build run."""

    __tablename__ = "dataset_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), unique=True, nullable=False, index=True)
    asof_date = Column(Date, nullable=False, index=True)
    mode = Column(String(20), nullable=False)  # full | incremental
    status = Column(String(20), nullable=False, index=True)  # running | completed | failed
    source_manifest_hash = Column(String(64))
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime)
    details_json = Column(Text)


class DatasetArtifact(Base):
    """Metadata index for generated curated artifacts."""

    __tablename__ = "dataset_artifacts"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), ForeignKey("dataset_runs.run_id"), nullable=False, index=True)
    dataset_name = Column(String(100), nullable=False, index=True)
    artifact_path = Column(String(500), nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    min_date = Column(Date)
    max_date = Column(Date)
    checksum = Column(String(64))
    metadata_json = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "dataset_name", name="uq_dataset_artifact_run_name"),
    )


class SnapshotIndexStock(Base):
    """Serving index for daily stock snapshot rows."""

    __tablename__ = "snapshot_index_stock"

    id = Column(Integer, primary_key=True, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    artifact_path = Column(String(500), nullable=False)
    run_id = Column(String(64), ForeignKey("dataset_runs.run_id"), nullable=False, index=True)
    row_pointer = Column(String(120))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("snapshot_date", "symbol", name="uq_snapshot_stock_date_symbol"),
        Index("ix_snapshot_stock_date_symbol", "snapshot_date", "symbol"),
    )


class SnapshotIndexUniverse(Base):
    """Serving index for daily universe snapshot rows."""

    __tablename__ = "snapshot_index_universe"

    id = Column(Integer, primary_key=True, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    universe_id = Column(String(40), nullable=False, index=True)
    artifact_path = Column(String(500), nullable=False)
    run_id = Column(String(64), ForeignKey("dataset_runs.run_id"), nullable=False, index=True)
    version = Column(String(40), nullable=False, default="v1")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "snapshot_date",
            "universe_id",
            "version",
            name="uq_snapshot_universe_date_id_ver",
        ),
        Index("ix_snapshot_universe_date_id", "snapshot_date", "universe_id"),
    )
