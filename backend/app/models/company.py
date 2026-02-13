from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from ..base import Base

class Company(Base):
    """Company master table"""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(200))
    sector = Column(String(100))
    industry = Column(String(100))
    market_cap = Column(Float)
    is_fno = Column(Boolean, default=False)  # F&O eligible
    is_active = Column(Boolean, default=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    historical_prices = relationship("HistoricalPrice", back_populates="company", cascade="all, delete-orphan")
    financial_statements = relationship("FinancialStatement", back_populates="company", cascade="all, delete-orphan")
    quarterly_results = relationship("QuarterlyResult", back_populates="company", cascade="all, delete-orphan")
    learning_artifacts = relationship("LearningArtifact", back_populates="company", cascade="all")

class LearningArtifact(Base):
    __tablename__ = "learning_artifacts"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True) # Can be null for general market learning
    agent_id = Column(String(50), nullable=False) # 'data_agent', 'strategy_agent', etc.
    artifact_type = Column(String(50), nullable=False) # 'insight', 'pattern', 'model_v1'
    content = Column(JSON, nullable=False)
    version = Column(Integer, default=1)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    company = relationship("Company", back_populates="learning_artifacts")
