"""
Test Dashboard Endpoints
"""

import logging

import requests

logger = logging.getLogger(__name__)


def test_dashboard() -> None:
    base_url = "http://localhost:8000/api/market"

    logger.info("Testing /market-overview...")
    try:
        resp = requests.get(f"{base_url}/market-overview", timeout=5)
        logger.info("Status: %s", resp.status_code)
        if resp.status_code == 200:
            data = resp.json()
            logger.info(
                "Market Status: %s", data.get("market_status", {}).get("market_status")
            )
            logger.info("Indices keys: %s", list(data.get("indices", {}).keys()))
            for idx, val in data.get('indices', {}).items():
                logger.info(" - %s: %s (Source: %s)", idx, val.get("price"), val.get("source"))
    except Exception as e:
        logger.exception("Failed market overview request: %s", e)

    logger.info("Testing /top-gainers...")
    try:
        resp = requests.get(f"{base_url}/top-gainers?limit=5&index=NIFTY50", timeout=5)
        logger.info("Status: %s", resp.status_code)
        if resp.status_code == 200:
            data = resp.json()
            logger.info("Is Live: %s", data.get("is_live"))
            logger.info("Count: %s", data.get("count"))
            for g in data.get('data', []):
                logger.info(
                    " - %s: %s (%.2f%%) [Source: %s]",
                    g["symbol"],
                    g["price"],
                    g["change_pct"],
                    g["source"],
                )
    except Exception as e:
        logger.exception("Failed top gainers request: %s", e)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    test_dashboard()
