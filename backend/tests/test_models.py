"""
Comprehensive tests for database models
Tests model creation, constraints, and relationships
"""
import pytest
from datetime import date, datetime
from sqlalchemy.exc import IntegrityError

from app.models.index_membership import IndexMembership
from app.models.portfolio import (
    PortfolioPolicy, ResearchPortfolio, UserPortfolio,
    PortfolioPosition, ComputedRiskMetric, UserStockPortfolio,
    PortfolioDailyState, LivePortfolioState
)
from app.models.price import HistoricalPrice, IntradayCandle


class TestIndexMembership:
    """Tests for IndexMembership model"""

    def test_create_index_membership(self, db_session):
        """Test creating an index membership record"""
        membership = IndexMembership(
            index_name='NIFTY50',
            symbol='SBIN',
            start_date=date(2020, 1, 1),
            end_date=None,
            weight=2.5,
            company_name='State Bank of India'
        )
        db_session.add(membership)
        db_session.commit()

        assert membership.id is not None
        assert membership.index_name == 'NIFTY50'
        assert membership.symbol == 'SBIN'
        assert membership.end_date is None

    def test_active_membership(self, db_session):
        """Test querying active memberships"""
        membership = IndexMembership(
            index_name='BANKNIFTY',
            symbol='HDFC',
            start_date=date(2020, 1, 1),
            end_date=None
        )
        db_session.add(membership)
        db_session.commit()

        # Query active memberships
        active = db_session.query(IndexMembership).filter(
            IndexMembership.end_date.is_(None)
        ).all()

        assert len(active) >= 1
        assert membership in active

    def test_unique_constraint(self, db_session):
        """Test unique constraint on index_name, symbol, start_date"""
        membership1 = IndexMembership(
            index_name='NIFTY50',
            symbol='TCS',
            start_date=date(2020, 1, 1)
        )
        db_session.add(membership1)
        db_session.commit()

        # Try to add duplicate
        membership2 = IndexMembership(
            index_name='NIFTY50',
            symbol='TCS',
            start_date=date(2020, 1, 1)
        )
        db_session.add(membership2)

        with pytest.raises(IntegrityError):
            db_session.commit()


class TestPortfolioPolicy:
    """Tests for PortfolioPolicy model"""

    def test_create_policy(self, db_session):
        """Test creating a portfolio policy"""
        policy = PortfolioPolicy(
            name='Conservative Policy',
            cash_reserve_percent=30.0,
            daily_stop_loss_percent=1.0,
            max_equity_exposure_percent=70.0,
            max_strategy_allocation_percent=20.0,
            allocation_sensitivity='LOW',
            correlation_penalty='HIGH'
        )
        db_session.add(policy)
        db_session.commit()

        assert policy.id is not None
        assert policy.name == 'Conservative Policy'
        assert policy.cash_reserve_percent == 30.0

    def test_default_values(self, db_session):
        """Test default values for policy"""
        policy = PortfolioPolicy(name='Default Policy')
        db_session.add(policy)
        db_session.commit()

        assert policy.cash_reserve_percent == 20.0
        assert policy.daily_stop_loss_percent == 2.0
        assert policy.allocation_sensitivity == 'MEDIUM'


class TestResearchPortfolio:
    """Tests for ResearchPortfolio model"""

    def test_create_research_portfolio(self, db_session):
        """Test creating a research portfolio"""
        # First create a policy
        policy = PortfolioPolicy(name='Test Policy')
        db_session.add(policy)
        db_session.commit()

        # Create portfolio
        portfolio = ResearchPortfolio(
            name='Test Portfolio',
            policy_id=policy.id,
            status='RESEARCH',
            description='Test portfolio for backtesting',
            benchmark='NIFTY 50',
            initial_capital=100000.0,
            composition=[
                {'strategy_id': 'STRATEGY1', 'allocation_percent': 50.0},
                {'strategy_id': 'STRATEGY2', 'allocation_percent': 50.0}
            ]
        )
        db_session.add(portfolio)
        db_session.commit()

        assert portfolio.id is not None
        assert portfolio.name == 'Test Portfolio'
        assert len(portfolio.composition) == 2

    def test_portfolio_policy_relationship(self, db_session):
        """Test relationship between portfolio and policy"""
        policy = PortfolioPolicy(name='Relationship Test Policy')
        db_session.add(policy)
        db_session.commit()

        portfolio = ResearchPortfolio(
            name='Related Portfolio',
            policy_id=policy.id,
            composition=[]
        )
        db_session.add(portfolio)
        db_session.commit()

        # Test relationship
        assert portfolio.policy.name == 'Relationship Test Policy'
        assert portfolio in policy.portfolios


class TestUserPortfolio:
    """Tests for UserPortfolio model"""

    def test_create_user_portfolio(self, db_session):
        """Test creating a user portfolio"""
        portfolio = UserPortfolio(
            user_id='user123',
            portfolio_name='My Portfolio',
            description='Personal investment portfolio'
        )
        db_session.add(portfolio)
        db_session.commit()

        assert portfolio.id is not None
        assert portfolio.user_id == 'user123'
        assert portfolio.portfolio_name == 'My Portfolio'

    def test_default_user_id(self, db_session):
        """Test default user_id"""
        portfolio = UserPortfolio(portfolio_name='Default User Portfolio')
        db_session.add(portfolio)
        db_session.commit()

        assert portfolio.user_id == 'default_user'


class TestHistoricalPrice:
    """Tests for HistoricalPrice model"""

    def test_create_historical_price(self, db_session):
        """Test creating a historical price record"""
        from app.models.company import Company

        # Create a company first
        company = Company(
            symbol='TESTSTOCK',
            name='Test Stock Ltd',
            sector='Technology',
            is_active=True
        )
        db_session.add(company)
        db_session.commit()

        # Create price record
        price = HistoricalPrice(
            company_id=company.id,
            date=date(2024, 1, 1),
            open=100.0,
            high=105.0,
            low=99.0,
            close=103.0,
            volume=1000000,
            ema_20=102.0,
            ema_50=101.0,
            rsi_14=55.0,
            atr_14=2.5,
            source='fyers'
        )
        db_session.add(price)
        db_session.commit()

        assert price.id is not None
        assert price.close == 103.0
        assert price.source == 'fyers'

    def test_technical_indicators(self, db_session):
        """Test technical indicator fields"""
        from app.models.company import Company

        company = Company(
            symbol='TECHTEST',
            name='Tech Test Ltd',
            sector='IT',
            is_active=True
        )
        db_session.add(company)
        db_session.commit()

        price = HistoricalPrice(
            company_id=company.id,
            date=date(2024, 1, 1),
            open=100.0,
            high=105.0,
            low=99.0,
            close=103.0,
            volume=1000000,
            macd=1.5,
            macd_signal=1.2,
            macd_histogram=0.3,
            stoch_k=65.0,
            stoch_d=60.0,
            bb_upper=110.0,
            bb_middle=105.0,
            bb_lower=100.0,
            adx=25.0,
            obv=5000000
        )
        db_session.add(price)
        db_session.commit()

        assert price.macd == 1.5
        assert price.stoch_k == 65.0
        assert price.adx == 25.0

    def test_unique_company_date_constraint(self, db_session):
        """Test unique constraint on company_id and date"""
        from app.models.company import Company

        company = Company(
            symbol='UNIQUETEST',
            name='Unique Test Ltd',
            sector='Finance',
            is_active=True
        )
        db_session.add(company)
        db_session.commit()

        # Add first price
        price1 = HistoricalPrice(
            company_id=company.id,
            date=date(2024, 1, 1),
            open=100.0,
            high=105.0,
            low=99.0,
            close=103.0,
            volume=1000000
        )
        db_session.add(price1)
        db_session.commit()

        # Try to add duplicate date
        price2 = HistoricalPrice(
            company_id=company.id,
            date=date(2024, 1, 1),
            open=101.0,
            high=106.0,
            low=100.0,
            close=104.0,
            volume=1100000
        )
        db_session.add(price2)

        with pytest.raises(IntegrityError):
            db_session.commit()


class TestIntradayCandle:
    """Tests for IntradayCandle model"""

    def test_create_intraday_candle(self, db_session):
        """Test creating an intraday candle"""
        from app.models.company import Company

        company = Company(
            symbol='INTRATEST',
            name='Intraday Test Ltd',
            sector='Banking',
            is_active=True
        )
        db_session.add(company)
        db_session.commit()

        candle = IntradayCandle(
            company_id=company.id,
            timestamp=datetime(2024, 1, 1, 9, 15, 0),
            timeframe=5,
            open=100.0,
            high=101.0,
            low=99.5,
            close=100.5,
            volume=50000,
            trades=150,
            source='fyers'
        )
        db_session.add(candle)
        db_session.commit()

        assert candle.id is not None
        assert candle.timeframe == 5
        assert candle.trades == 150

    def test_multiple_timeframes(self, db_session):
        """Test storing multiple timeframes for same symbol"""
        from app.models.company import Company

        company = Company(
            symbol='MULTITIME',
            name='Multi Timeframe Ltd',
            sector='IT',
            is_active=True
        )
        db_session.add(company)
        db_session.commit()

        # Add 5-minute candle
        candle_5m = IntradayCandle(
            company_id=company.id,
            timestamp=datetime(2024, 1, 1, 9, 15, 0),
            timeframe=5,
            open=100.0,
            high=101.0,
            low=99.5,
            close=100.5,
            volume=50000
        )
        db_session.add(candle_5m)

        # Add 15-minute candle
        candle_15m = IntradayCandle(
            company_id=company.id,
            timestamp=datetime(2024, 1, 1, 9, 15, 0),
            timeframe=15,
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.5,
            volume=150000
        )
        db_session.add(candle_15m)
        db_session.commit()

        # Query both
        candles = db_session.query(IntradayCandle).filter(
            IntradayCandle.company_id == company.id
        ).all()

        assert len(candles) == 2
        assert candles[0].timeframe != candles[1].timeframe


class TestUserStockPortfolio:
    """Tests for UserStockPortfolio model"""

    def test_create_user_stock_portfolio(self, db_session):
        """Test creating a user stock portfolio"""
        portfolio = UserStockPortfolio(
            portfolio_id='portfolio_001',
            name='Tech Stocks',
            description='Technology sector stocks',
            symbols=['TCS', 'INFY', 'WIPRO', 'HCLTECH']
        )
        db_session.add(portfolio)
        db_session.commit()

        assert portfolio.portfolio_id == 'portfolio_001'
        assert len(portfolio.symbols) == 4
        assert 'TCS' in portfolio.symbols


class TestPortfolioDailyState:
    """Tests for PortfolioDailyState model"""

    def test_create_daily_state(self, db_session):
        """Test creating a daily state record"""
        state = PortfolioDailyState(
            date=date(2024, 1, 1),
            run_id='run_001',
            equity=100000.0,
            drawdown=-2.5,
            volatility=15.0,
            volatility_regime='MEDIUM',
            risk_state='NORMAL',
            risk_state_reason='All metrics within limits',
            strategy_weights={'STRATEGY1': 50.0, 'STRATEGY2': 50.0}
        )
        db_session.add(state)
        db_session.commit()

        assert state.date == date(2024, 1, 1)
        assert state.equity == 100000.0
        assert state.volatility_regime == 'MEDIUM'


class TestLivePortfolioState:
    """Tests for LivePortfolioState model"""

    def test_create_live_state(self, db_session):
        """Test creating a live portfolio state"""
        # Create policy and portfolio first
        policy = PortfolioPolicy(name='Live Test Policy')
        db_session.add(policy)
        db_session.commit()

        portfolio = ResearchPortfolio(
            name='Live Portfolio',
            policy_id=policy.id,
            composition=[]
        )
        db_session.add(portfolio)
        db_session.commit()

        # Create live state
        state = LivePortfolioState(
            portfolio_id=portfolio.id,
            timestamp=datetime(2024, 1, 1, 9, 30, 0),
            total_equity=100000.0,
            cash_balance=20000.0,
            deployed_capital=80000.0,
            current_drawdown_pct=-1.5,
            is_breached=False,
            strategy_performance={'STRATEGY1': 5.2, 'STRATEGY2': 3.1}
        )
        db_session.add(state)
        db_session.commit()

        assert state.id is not None
        assert state.total_equity == 100000.0
        assert state.is_breached is False

    def test_breach_detection(self, db_session):
        """Test breach detection in live state"""
        policy = PortfolioPolicy(name='Breach Test Policy')
        db_session.add(policy)
        db_session.commit()

        portfolio = ResearchPortfolio(
            name='Breach Portfolio',
            policy_id=policy.id,
            composition=[]
        )
        db_session.add(portfolio)
        db_session.commit()

        state = LivePortfolioState(
            portfolio_id=portfolio.id,
            total_equity=100000.0,
            cash_balance=10000.0,
            deployed_capital=90000.0,
            current_drawdown_pct=-5.0,
            is_breached=True,
            breach_details='Daily stop loss exceeded'
        )
        db_session.add(state)
        db_session.commit()

        assert state.is_breached is True
        assert state.breach_details == 'Daily stop loss exceeded'