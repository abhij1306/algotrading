"""
OpenRouter API integration for AI-powered sector classification
Uses KAT Coder Pro model via OpenRouter to classify company sectors
"""

import os
import requests
from typing import Optional, Dict
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
# Using free Qwen 2.5 7B Instruct model
OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL', 'qwen/qwen-2.5-7b-instruct:free')

def classify_sector_with_ai(company_name: str, symbol: str) -> Optional[Dict[str, str]]:
    """
    Use OpenRouter AI to classify company sector and industry
    
    Args:
        company_name: Full company name
        symbol: Stock symbol
    
    Returns:
        Dict with 'sector' and 'industry' keys, or None if failed
    """
    
    if not OPENROUTER_API_KEY:
        print("Error: OPENROUTER_API_KEY not set in .env file")
        return None
    
    prompt = f"""Classify the following Indian company into a sector and industry.

Company: {company_name}
Symbol: {symbol}

Respond ONLY with a JSON object in this exact format:
{{"sector": "sector_name", "industry": "industry_name"}}

Use standard sector classifications like:
- Banking
- IT Services
- Pharmaceuticals
- Automobiles
- FMCG
- Telecommunications
- Energy
- Infrastructure
- Real Estate
- Financial Services
- Healthcare
- Consumer Durables
- Textiles
- Metals & Mining

Be concise and use standard industry terminology."""

    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:3000",  # Required by OpenRouter
                "X-Title": "AlgoTrading Sector Classifier"  # Optional but recommended
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "temperature": 0.1,  # Low temperature for consistent classification
                "max_tokens": 100
            },
            timeout=30
        )
        
        response.raise_for_status()
        data = response.json()
        
        # Extract the AI's response
        ai_response = data['choices'][0]['message']['content'].strip()
        
        # Try to parse JSON from response
        import json
        # Remove markdown code blocks if present
        if '```json' in ai_response:
            ai_response = ai_response.split('```json')[1].split('```')[0].strip()
        elif '```' in ai_response:
            ai_response = ai_response.split('```')[1].split('```')[0].strip()
        
        result = json.loads(ai_response)
        
        if 'sector' in result and 'industry' in result:
            return {
                'sector': result['sector'].strip(),
                'industry': result['industry'].strip()
            }
        
        return None
        
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}")
        try:
            print(f"Response: {response.text}")
        except:
            pass
        return None
    except requests.exceptions.RequestException as e:
        print(f"API request failed: {str(e)}")
        return None
    except json.JSONDecodeError as e:
        print(f"Failed to parse AI response as JSON: {str(e)}")
        try:
            print(f"Raw response: {ai_response}")
        except:
            pass
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def batch_classify_sectors(companies: list, batch_size: int = 10) -> Dict[str, Dict[str, str]]:
    """
    Classify multiple companies in batches
    
    Args:
        companies: List of dicts with 'symbol' and 'name' keys
        batch_size: Number to process before pausing
    
    Returns:
        Dict mapping symbol to {sector, industry}
    """
    results = {}
    
    for i, company in enumerate(companies):
        if i > 0 and i % batch_size == 0:
            import time
            print(f"Processed {i} companies, pausing...")
            time.sleep(2)  # Rate limiting
        
        sector_info = classify_sector_with_ai(company['name'], company['symbol'])
        if sector_info:
            results[company['symbol']] = sector_info
    
    return results
