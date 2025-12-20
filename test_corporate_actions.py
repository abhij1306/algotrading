import pandas as pd
import pytest
from scripts.data_pipeline.apply_corporate_actions import parse_factor, adjust_prices

def test_parse_factor_split():
    """Test parsing of split from purpose string"""
    purpose = "Split from FV 10 to FV 2"
    action_type, factor = parse_factor(purpose)
    assert action_type == "split"
    assert factor == 5.0

def test_parse_factor_bonus():
    """Test parsing of bonus from purpose string"""
    purpose = "Bonus 1:1"
    action_type, factor = parse_factor(purpose)
    assert action_type == "bonus"
    assert factor == 2.0
    
    purpose = "Bonus 5:1" # 5 bonus shares for every 1 held
    action_type, factor = parse_factor(purpose)
    assert action_type == "bonus"
    assert factor == 6.0 # (5+1)/1

def test_adjust_prices_bonus():
    """Test price adjustment for a 1:1 bonus"""
    # Create sample data
    prices = pd.DataFrame({
        'trade_date': [pd.to_datetime('2017-09-06').date(), pd.to_datetime('2017-09-07').date()],
        'open': [1600.0, 800.0],
        'high': [1650.0, 820.0],
        'low': [1580.0, 790.0],
        'close': [1620.0, 810.0],
        'volume': [1000000, 2000000]
    })
    
    ca = pd.DataFrame({
        'symbol': ['RELIANCE'],
        'ex_date': [pd.to_datetime('2017-09-07').date()],
        'action_type': ['bonus'],
        'factor': [2.0]
    })
    
    adjusted = adjust_prices(prices, ca)
    
    # Pre-ex-date prices should be halved
    assert adjusted.iloc[0]['close'] == 810.0
    assert adjusted.iloc[0]['open'] == 800.0
    # Pre-ex-date volume should be doubled
    assert adjusted.iloc[0]['volume'] == 2000000
    
    # Ex-date prices and volume should remain same
    assert adjusted.iloc[1]['close'] == 810.0
    assert adjusted.iloc[1]['volume'] == 2000000

def test_reliance_2017_bonus_logic():
    """Specific check for Reliance 2017 bonus logic"""
    # PURPOSE: "Bonus 1:1"
    # EX-DATE: 07-Sep-2017
    action_type, factor = parse_factor("Bonus 1:1")
    assert action_type == "bonus"
    assert factor == 2.0

def test_infy_2018_bonus_logic():
    """Specific check for Infosys 2018 bonus logic"""
    # PURPOSE: "Bonus 1:1"
    # EX-DATE: 04-Sep-2018
    action_type, factor = parse_factor("Bonus 1:1")
    assert action_type == "bonus"
    assert factor == 2.0
