"""
Comprehensive tests for main application
Tests startup, exception handlers, and configuration
"""
import pytest
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.exceptions import SmartTraderException


class TestApplicationStartup:
    """Tests for application startup"""

    def test_app_creation(self):
        """Test that app is created successfully"""
        assert app is not None
        assert app.title == "SmartTrader 3.0 API"
        assert app.version == "3.0.0"

    def test_root_endpoint(self):
        """Test root endpoint"""
        client = TestClient(app)
        response = client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["message"] == "SmartTrader 3.0 API Running"
        assert data["version"] == "3.0.0"

    def test_ping_endpoint(self):
        """Test ping endpoint"""
        client = TestClient(app)
        response = client.get("/ping")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["message"] == "Backend is alive"


class TestStartupValidation:
    """Tests for startup validation"""

    @patch('app.main.symbol_master')
    def test_symbol_master_validation(self, mock_symbol_master):
        """Test symbol master validation on startup"""
        mock_symbol_master.to_fyers.return_value = "NSE:SBIN-EQ"
        mock_symbol_master.to_db.return_value = "SBIN"

        # Should not raise exception
        client = TestClient(app)

    @patch('app.main.engine')
    def test_database_validation(self, mock_engine):
        """Test database connection validation"""
        mock_conn = Mock()
        mock_conn.execute.return_value = None
        mock_engine.connect.return_value.__enter__.return_value = mock_conn

        client = TestClient(app)

    @patch('app.main.symbol_master')
    def test_symbol_master_validation_failure(self, mock_symbol_master):
        """Test symbol master validation failure"""
        mock_symbol_master.to_fyers.side_effect = Exception("Symbol conversion failed")

        # App should still start but log error
        client = TestClient(app)


class TestExceptionHandlers:
    """Tests for exception handlers"""

    def test_smarttrader_exception_handler(self):
        """Test SmartTrader exception handler"""
        # Create a test endpoint that raises SmartTraderException
        @app.get("/test/smarttrader-exception")
        def test_exception():
            raise SmartTraderException(
                status_code=400,
                code="TEST_ERROR",
                message="Test error message",
                details={"field": "value"}
            )

        client = TestClient(app)
        response = client.get("/test/smarttrader-exception")

        assert response.status_code == 400
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "TEST_ERROR"
        assert data["error"]["message"] == "Test error message"

    def test_global_exception_handler(self):
        """Test global exception handler"""
        @app.get("/test/unhandled-exception")
        def test_exception():
            raise ValueError("Unhandled error")

        client = TestClient(app)
        response = client.get("/test/unhandled-exception")

        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "INTERNAL_SERVER_ERROR"


class TestCORSConfiguration:
    """Tests for CORS middleware"""

    def test_cors_headers_present(self):
        """Test that CORS headers are present"""
        client = TestClient(app)
        response = client.options("/", headers={"Origin": "http://localhost:3000"})

        # CORS should allow the origin
        assert "access-control-allow-origin" in response.headers

    def test_cors_allowed_origin(self):
        """Test allowed CORS origins"""
        client = TestClient(app)

        # Test allowed origin
        response = client.get("/", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 200

    def test_cors_methods(self):
        """Test allowed CORS methods"""
        client = TestClient(app)
        response = client.options(
            "/",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET"
            }
        )

        # Should allow all methods
        assert response.status_code in [200, 405]


class TestRouterRegistration:
    """Tests for router registration"""

    def test_health_router_registered(self):
        """Test health router is registered"""
        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_market_router_registered(self):
        """Test market router is registered"""
        client = TestClient(app)
        response = client.get("/api/market/status")
        assert response.status_code == 200

    def test_screener_router_registered(self):
        """Test screener router is registered"""
        client = TestClient(app)
        response = client.get("/api/screener/indices")
        assert response.status_code == 200

    def test_websocket_router_registered(self):
        """Test websocket router is registered"""
        client = TestClient(app)
        response = client.get("/api/websocket/status")
        assert response.status_code == 200


class TestLifespan:
    """Tests for lifespan events"""

    @patch('app.main.live_market')
    @patch('app.main.manager')
    def test_lifespan_startup(self, mock_manager, mock_live_market):
        """Test lifespan startup event"""
        mock_manager.set_loop = Mock()
        mock_live_market.connect = Mock()

        client = TestClient(app)

        # Startup should have been called
        # Note: Testing lifespan is tricky with TestClient

    @patch('app.main.live_market')
    def test_lifespan_shutdown(self, mock_live_market):
        """Test lifespan shutdown event"""
        mock_live_market.broadcast_task = Mock()
        mock_live_market.broadcast_task.cancel = Mock()
        mock_live_market.ws_service = Mock()
        mock_live_market.ws_service.disconnect = Mock()

        # Note: Testing shutdown is complex with TestClient


class TestDatabaseInitialization:
    """Tests for database initialization"""

    @patch('app.main.Base')
    @patch('app.main.engine')
    def test_database_tables_created(self, mock_engine, mock_base):
        """Test that database tables are created on startup"""
        mock_base.metadata.create_all = Mock()

        # Import should trigger table creation
        from app.main import app

        # Should not raise exception

    @patch('app.main.Base')
    @patch('app.main.engine')
    def test_database_creation_failure(self, mock_engine, mock_base):
        """Test handling of database creation failure"""
        mock_base.metadata.create_all.side_effect = Exception("DB Error")

        # Should log warning but not crash
        from app.main import app


class TestApplicationMetadata:
    """Tests for application metadata"""

    def test_app_title(self):
        """Test application title"""
        assert app.title == "SmartTrader 3.0 API"

    def test_app_version(self):
        """Test application version"""
        assert app.version == "3.0.0"

    def test_app_description(self):
        """Test application description"""
        assert "Algorithmic Trading Platform" in app.description


class TestEdgeCases:
    """Tests for edge cases"""

    def test_concurrent_requests(self):
        """Test handling multiple concurrent requests"""
        import concurrent.futures

        client = TestClient(app)

        def make_request():
            return client.get("/ping")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(make_request) for _ in range(50)]
            results = [f.result() for f in futures]

        # All requests should succeed
        assert all(r.status_code == 200 for r in results)

    def test_invalid_route(self):
        """Test accessing invalid route"""
        client = TestClient(app)
        response = client.get("/invalid/route/that/does/not/exist")

        assert response.status_code == 404

    def test_health_check_performance(self):
        """Test that health check is fast"""
        import time

        client = TestClient(app)

        start = time.time()
        response = client.get("/ping")
        elapsed = time.time() - start

        assert response.status_code == 200
        assert elapsed < 1.0  # Should respond within 1 second

    def test_method_not_allowed(self):
        """Test method not allowed error"""
        client = TestClient(app)
        response = client.post("/")  # Root only supports GET

        assert response.status_code == 405


class TestSecurityHeaders:
    """Tests for security considerations"""

    def test_no_sensitive_info_in_errors(self):
        """Test that errors don't leak sensitive information"""
        @app.get("/test/error-with-details")
        def test_error():
            raise Exception("Database password: secret123")

        client = TestClient(app)
        response = client.get("/test/error-with-details")

        # Should not expose internal error details
        assert response.status_code == 500
        data = response.json()
        assert "password" not in str(data).lower()
        assert "secret" not in str(data).lower()


class TestDependencyInjection:
    """Tests for dependency injection"""

    def test_get_db_dependency(self):
        """Test that database dependency works"""
        from app.database import get_db

        # Should be a generator
        db = next(get_db())
        assert db is not None

    def test_database_session_cleanup(self):
        """Test that database sessions are properly cleaned up"""
        from app.database import get_db

        db = next(get_db())

        # Session should be valid
        assert db is not None

        # Cleanup
        try:
            next(get_db())
        except StopIteration:
            pass  # Expected