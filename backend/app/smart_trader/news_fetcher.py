"""
News fetcher for market news from Google News and Yahoo Finance
Fetches latest market news and stores in database
"""
import feedparser
import yfinance as yf
from datetime import datetime, timedelta
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from ..database import MarketNews, SessionLocal
import re
import pytz


class NewsFetcher:
    """Fetches market news from multiple sources"""
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
        
    def fetch_google_news(self, query: str = "Indian stock market", limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch news from Google News RSS"""
        try:
            # Google News RSS feed
            rss_url = f"https://news.google.com/rss/search?q={query.replace(' ', '+')}&hl=en-IN&gl=IN&ceid=IN:en"
            
            feed = feedparser.parse(rss_url)
            
            articles = []
            ist = pytz.timezone('Asia/Kolkata')
            
            for entry in feed.entries[:limit]:
                # Extract stock symbols from title (basic regex)
                symbols = self._extract_symbols(entry.title + " " + entry.get('summary', ''))
                
                # Convert published time to IST
                if hasattr(entry, 'published_parsed'):
                    pub_time = datetime(*entry.published_parsed[:6], tzinfo=pytz.UTC)
                    pub_time_ist = pub_time.astimezone(ist)
                else:
                    pub_time_ist = datetime.now(ist)
                
                articles.append({
                    'title': entry.title,
                    'summary': entry.get('summary', entry.title),
                    'source': 'Google News',
                    'url': entry.link,
                    'published_at': pub_time_ist.replace(tzinfo=None),  # Store as naive datetime in IST
                    'symbols': ','.join(symbols) if symbols else None,
                    'sentiment': 'neutral'  # TODO: Add sentiment analysis
                })
            
            return articles
            
        except Exception as e:
            print(f"Error fetching Google News: {e}")
            return []
    
    def fetch_yahoo_finance_news(self, symbols: List[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch news for specific stocks from Yahoo Finance"""
        try:
            if not symbols:
                # Default to major Indian stocks
                symbols = ['RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'HDFCBANK.NS', 'ICICIBANK.NS']
            
            articles = []
            ist = pytz.timezone('Asia/Kolkata')
            
            for symbol in symbols[:5]:  # Limit to 5 symbols to avoid rate limits
                try:
                    ticker = yf.Ticker(symbol)
                    news = ticker.news
                    
                    for item in news[:limit]:
                        # Clean symbol (remove .NS suffix)
                        clean_symbol = symbol.replace('.NS', '')
                        
                        # Convert timestamp to IST
                        timestamp = item.get('providerPublishTime', datetime.now(pytz.UTC).timestamp())
                        pub_time = datetime.fromtimestamp(timestamp, tz=pytz.UTC)
                        pub_time_ist = pub_time.astimezone(ist)
                        
                        articles.append({
                            'title': item.get('title', ''),
                            'summary': item.get('summary', item.get('title', '')),
                            'source': item.get('publisher', 'Yahoo Finance'),
                            'url': item.get('link', ''),
                            'published_at': pub_time_ist.replace(tzinfo=None),  # Store as naive datetime in IST
                            'symbols': clean_symbol,
                            'sentiment': 'neutral'
                        })
                        
                except Exception as e:
                    print(f"Error fetching news for {symbol}: {e}")
                    continue
            
            return articles
            
        except Exception as e:
            print(f"Error fetching Yahoo Finance news: {e}")
            return []
    
    def _extract_symbols(self, text: str) -> List[str]:
        """Extract stock symbols from text using basic pattern matching"""
        # Common Indian stock symbols (NSE F&O stocks)
        common_symbols = [
            'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'HINDUNILVR',
            'ITC', 'SBIN', 'BHARTIARTL', 'KOTAKBANK', 'LT', 'AXISBANK',
            'ASIANPAINT', 'MARUTI', 'TITAN', 'BAJFINANCE', 'WIPRO', 'TECHM',
            'ULTRACEMCO', 'SUNPHARMA', 'NESTLEIND', 'POWERGRID', 'NTPC',
            'ONGC', 'TATASTEEL', 'TATAMOTORS', 'ADANIPORTS', 'JSWSTEEL',
            'NIFTY', 'SENSEX', 'BANKNIFTY'
        ]
        
        found_symbols = []
        text_upper = text.upper()
        
        for symbol in common_symbols:
            if symbol in text_upper:
                found_symbols.append(symbol)
        
        return list(set(found_symbols))  # Remove duplicates
    
    def save_news(self, articles: List[Dict[str, Any]]) -> int:
        """Save news articles to database"""
        saved_count = 0
        
        try:
            for article in articles:
                # Check if article already exists (by URL)
                existing = self.db.query(MarketNews).filter(
                    MarketNews.url == article['url']
                ).first()
                
                if not existing:
                    news = MarketNews(
                        title=article['title'],
                        summary=article['summary'],
                        source=article['source'],
                        url=article['url'],
                        published_at=article['published_at'],
                        symbols=article['symbols'],
                        sentiment=article['sentiment']
                    )
                    self.db.add(news)
                    saved_count += 1
            
            self.db.commit()
            return saved_count
            
        except Exception as e:
            print(f"Error saving news: {e}")
            self.db.rollback()
            return 0
    
    def fetch_and_save_all(self) -> Dict[str, int]:
        """Fetch from all sources and save to database"""
        print("Fetching market news...")
        
        # Fetch from Google News
        google_articles = self.fetch_google_news("Indian stock market NSE BSE", limit=30)
        google_saved = self.save_news(google_articles)
        
        # Fetch from Yahoo Finance
        yahoo_articles = self.fetch_yahoo_finance_news(limit=5)
        yahoo_saved = self.save_news(yahoo_articles)
        
        print(f"Saved {google_saved} Google News articles, {yahoo_saved} Yahoo Finance articles")
        
        return {
            'google_news': google_saved,
            'yahoo_finance': yahoo_saved,
            'total': google_saved + yahoo_saved
        }
    
    def get_latest_news(self, limit: int = 50) -> List[MarketNews]:
        """Get latest news from database"""
        return self.db.query(MarketNews).order_by(
            MarketNews.published_at.desc()
        ).limit(limit).all()
    
    def get_news_by_symbol(self, symbol: str, limit: int = 20) -> List[MarketNews]:
        """Get news for a specific stock symbol"""
        return self.db.query(MarketNews).filter(
            MarketNews.symbols.like(f'%{symbol}%')
        ).order_by(
            MarketNews.published_at.desc()
        ).limit(limit).all()
    
    def cleanup_old_news(self, days: int = 7):
        """Delete news older than specified days"""
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        deleted = self.db.query(MarketNews).filter(
            MarketNews.published_at < cutoff_date
        ).delete()
        self.db.commit()
        print(f"Deleted {deleted} old news articles")
        return deleted


# Singleton instance
_news_fetcher = None

def get_news_fetcher(db: Session = None) -> NewsFetcher:
    """Get or create news fetcher instance"""
    global _news_fetcher
    if _news_fetcher is None or db is not None:
        _news_fetcher = NewsFetcher(db)
    return _news_fetcher
