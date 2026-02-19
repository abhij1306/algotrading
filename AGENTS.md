# SmartTrader - Agent & Development Guide

> **Philosophy**: Build with AI agents as first-class users. Every feature should be accessible via both UI and programmatic interfaces.

---

## Quick Start

### For Developers

```bash
# Backend
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m app.main

# Frontend
cd frontend
npm install
npm run dev
```

### For AI Agents

Use the Compound Engineering workflows:

| Command | Purpose |
|---------|---------|
| `@agent plan <feature>` | Create implementation plan |
| `@agent work <task>` | Execute planned work |
| `@agent review` | Multi-agent code review |
| `@agent compound` | Document learnings |

---

## Project Overview

**SmartTrader** is a quantitative trading platform for the Indian NSE market with:

- **Backtesting Engine** - Test strategies on historical data
- **Stock Screener** - Filter stocks by technical indicators
- **Live Trading** - Paper and live trading via Fyers broker
- **Options Trading** - Option chain analysis and Greeks calculation
- **Real-time Data** - WebSocket streaming from Fyers

---

## Architecture

### Tech Stack

**Backend:**
- Python 3.11+ with FastAPI
- PostgreSQL / SQLite for data storage
- Fyers API for broker integration
- WebSocket for real-time data

**Frontend:**
- Next.js 14 with App Router
- TypeScript + React
- Tailwind CSS (Unified token design system)
- Recharts for visualization

### Directory Structure

```
backend/
├── app/
│   ├── models/          # ORM models
│   ├── routers/         # API endpoints
│   ├── services/        # Business logic
│   ├── strategies/      # Trading strategies
│   └── brokers/         # Broker integrations
├── scripts/             # Maintenance scripts
└── tests/               # Test suite

frontend/
├── app/                 # Next.js pages
├── components/          # React components
├── lib/                 # Utilities
└── stores/              # State management

docs/
├── learnings/           # Compounded knowledge
└── *.md                 # Architecture docs
```

---

## Key Concepts

### 1. Symbol Management

**SymbolMaster** (`services/symbol_master.py`) handles all symbol format conversions:

```python
from app.services.symbol_master import symbol_master

# Convert between formats
symbol_master.to_fyers("SBIN")      # → "NSE:SBIN-EQ"
symbol_master.to_db("NSE:SBIN-EQ")  # → "SBIN"
symbol_master.to_display("SBIN")    # → "SBIN"
```

### 2. Index Universe

**IndexUniverseLoader** (`services/index_universe_loader.py`) manages 33+ NSE indices:

```python
from app.services.index_universe_loader import index_universe_loader

# Load all indices from CSV files
index_universe_loader.load_all()

# Get symbols for an index
symbols = index_universe_loader.get_index_symbols('NIFTY50')
# Returns: ['SBIN', 'RELIANCE', ...] - 51 symbols
```

### 3. Data Flow

**Daily Stock Data:**
```
Bhavcopy CSV → /api/upload/bhavcopy → Company + HistoricalPrice tables
```

**Index Membership:**
```
NSE CSV → nse_data/index_universe/constituents/ → IndexUniverseLoader
```

**Live Market Data:**
```
Fyers WebSocket → LiveMarketService → Frontend via WebSocket
```

---

## API Reference

### Core Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/screener/results` | GET | Get filtered stocks |
| `/api/screener/indices` | GET | List available indices |
| `/api/market/quotes/live` | GET | Get live quotes |
| `/api/market/watchlist` | GET/POST/DELETE | Manage watchlist |
| `/api/backtest/v2/run` | POST | Run backtest |
| `/api/trading/order` | POST | Place order |
| `/api/trading/positions` | GET | Get positions |
| `/api/options/chain` | GET | Get option chain |
| `/api/websocket/stream` | WS | Real-time data stream |

### Authentication

Fyers broker authentication:

```python
# 1. Get auth URL
GET /api/auth/fyers/url

# 2. User authorizes on Fyers website

# 3. Exchange auth code for token
POST /api/auth/fyers/token
Body: { "auth_code": "..." }

# 4. Token stored in fyers/config/access_token.json
```

---

## Development Workflows

### Adding a New Feature

1. **Plan** - Create design doc (or execution doc under `docs/execution/`)
2. **Design** - Follow [Design System](docs/DESIGN_SYSTEM.md) guidelines
3. **Implement** - Follow TDD approach, use real data only
4. **Test** - Add unit and integration tests
5. **Review** - Run `@agent review`
6. **Document** - Update relevant docs
7. **Compound** - Document learnings in `docs/learnings/`

### Code Style

**Python:**
- Use type hints for all functions
- Follow PEP 8 (enforced by ruff)
- Prefer explicit error handling
- Document complex business logic

**TypeScript:**
- Use functional components with hooks
- Prefer explicit types over `any`
- Follow Tailwind utility-first approach
- Keep components under 300 lines

### Code Quality Standards

SmartTrader enforces strict code quality standards through automated tooling. All standards are checked via pre-commit hooks and CI pipelines.

#### Pre-commit Hooks

**Setup:**
```bash
# Install pre-commit (one-time setup)
pip install pre-commit
pre-commit install

# Or use setup script
./scripts/setup-quality-checks.sh  # Linux/Mac
./scripts/setup-quality-checks.bat # Windows
```

**What Gets Checked:**
- ESLint with custom rules (frontend)
- Ruff linting (backend)
- Symbol format validation
- Console.log detection
- TypeScript type safety

**Bypass (Emergency Only):**
```bash
git commit --no-verify -m "emergency fix"
```

#### ESLint Custom Rules

**Frontend code must follow these rules:**

1. **No Hardcoded Colors** - Use design system
   ```typescript
   // ❌ Bad
   <div className="bg-[#050505] text-gray-400">

   // ✅ Good
   <div className="bg-background text-muted">
   ```

2. **No Arbitrary Text Sizes** - Use design system scale
   ```typescript
   // ❌ Bad
   <p className="text-[11px] tracking-[0.15em]">

   // ✅ Good
   <p className="text-xs tracking-tight">
   ```

3. **Use Format Utilities** - Centralized number formatting
   ```typescript
   // ❌ Bad
   {price.toFixed(2)}
   {(change * 100).toFixed(2)}%

   // ✅ Good
   {formatCurrency(price)}
   {formatPercentage(change)}
   ```

4. **Use UI Components** - Standardized components
   ```typescript
   // ❌ Bad
   <button className="px-4 py-2 bg-primary">

   // ✅ Good
   <Button variant="default">
   ```

**Run checks manually:**
```bash
cd frontend
npm run lint        # Check for violations
npm run lint:fix    # Auto-fix where possible
```

#### Symbol Format Requirements

**All symbol conversions MUST use SymbolMaster:**

```python
# ❌ Bad - Direct string manipulation
symbol = fyers_symbol.replace('NSE:', '').replace('-EQ', '')
parts = symbol.split(':')

# ✅ Good - Use SymbolMaster
from app.services.symbol_master import symbol_master

db_symbol = symbol_master.to_db(fyers_symbol)
fyers_symbol = symbol_master.to_fyers(db_symbol)
display_symbol = symbol_master.to_display(db_symbol)
```

**Why This Matters:**
- Prevents data corruption from inconsistent formats
- Centralizes conversion logic for maintainability
- Handles edge cases (F&O symbols, special characters)

**Automated Check:**
```bash
cd backend
python scripts/check_symbol_format.py
```

The pre-commit hook automatically runs this check and blocks commits with violations.

#### TypeScript Type Safety

**No `any` types allowed:**

```typescript
// ❌ Bad
const data: any = await response.json();
function process(input: any) { }

// ✅ Good
interface ApiResponse {
  symbol: string;
  price: number;
}
const data: ApiResponse = await response.json();
function process(input: StockData) { }
```

**Use type guards for unknown types:**
```typescript
function isStockData(data: unknown): data is StockData {
  return (
    typeof data === 'object' &&
    data !== null &&
    'symbol' in data &&
    'price' in data
  );
}
```

#### Dead Code Prevention

**No console.log in production:**
```typescript
// ❌ Bad
console.log('Debug info:', data);

// ✅ Good - Use appropriate logging
console.error('Failed to fetch:', error);
console.warn('Deprecated API used');
```

**No TODO/FIXME without issues:**
```typescript
// ❌ Bad
// TODO: Fix this later

// ✅ Good
// TODO(#123): Implement caching for performance
```

#### Bundle Size Limits

**Pages must not exceed 200KB First Load JS:**

```bash
cd frontend
npm run build
node scripts/check-bundle-size.js
```

**Use code splitting for heavy components:**
```typescript
// ❌ Bad - Eager import
import { LineChart } from 'recharts';

// ✅ Good - Dynamic import
const LineChart = dynamic(
  () => import('recharts').then(mod => mod.LineChart),
  { ssr: false, loading: () => <ChartSkeleton /> }
);
```

#### CI Pipeline Checks

**All PRs must pass:**
- ESLint with custom rules
- Ruff linting
- Symbol format validation
- TypeScript type checking
- Bundle size analysis
- Unit and integration tests

**Check status:**
```bash
# Frontend
cd frontend
npm run lint && npm run type-check && npm run build

# Backend
cd backend
ruff check . && pytest
```

#### Quick Reference

| Check | Command | Auto-fix |
|-------|---------|----------|
| Frontend lint | `npm run lint` | `npm run lint:fix` |
| Backend lint | `ruff check .` | `ruff check . --fix` |
| Symbol format | `python scripts/check_symbol_format.py` | Manual |
| Type check | `npm run type-check` | Manual |
| Bundle size | `node scripts/check-bundle-size.js` | Manual |

**Full documentation:** See [CODE_QUALITY.md](docs/CODE_QUALITY.md)

### Testing

```bash
# Backend tests
cd backend
pytest

# Frontend tests
cd frontend
npm test

# E2E tests
npm run test:e2e
```

---

## Performance Guidelines

### Backend

1. **Use database indexes** for frequently queried columns
2. **Batch operations** instead of loops
3. **Cache expensive calculations** with TTL
4. **Use async/await** properly (no blocking in async context)
5. **Connection pooling** for database

### Frontend

1. **Code splitting** with dynamic imports
2. **Memoize** expensive calculations
3. **Debounce** user inputs
4. **Lazy load** images and heavy components
5. **Use WebSocket** for real-time updates (not polling)
6. **Follow Design System** - See [DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md)

---

## Security Best Practices

1. **Never commit secrets** - Use environment variables
2. **Validate all inputs** - Both frontend and backend
3. **Sanitize error messages** - Don't leak stack traces
4. **Rate limiting** on all public endpoints
5. **JWT authentication** for protected routes
6. **Risk checks** before live trading

---

## Troubleshooting

### Common Issues

**Backend won't start:**
```bash
# Check if port 8000 is in use
netstat -ano | findstr :8000

# Check database connection
python -c "from app.database import SessionLocal; db = SessionLocal(); print('OK')"
```

**Frontend build fails:**
```bash
# Clear cache
rm -rf .next node_modules
npm install
npm run build
```

**WebSocket not connecting:**
```bash
# Check Fyers token validity
curl http://localhost:8000/api/auth/fyers/status

# Reconnect if expired
# Visit http://localhost:3000 and reconnect Fyers
```

---

## Contributing

### Before Submitting PR

- [ ] Code follows style guidelines
- [ ] Tests pass (`pytest` and `npm test`)
- [ ] Linting passes (`ruff check .` and `npm run lint`)
- [ ] Pre-commit hooks installed and passing
- [ ] No hardcoded colors or arbitrary text sizes
- [ ] Symbol conversions use SymbolMaster
- [ ] No TypeScript `any` types
- [ ] Bundle size within limits (200KB First Load JS)
- [ ] Documentation updated
- [ ] No secrets committed
- [ ] Performance impact considered

### Commit Message Format

```
type(scope): brief description

Detailed explanation of changes.

Fixes #123
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`

---

## Resources

### Documentation
- [Design System](docs/DESIGN_SYSTEM.md) - UI/UX design guidelines
- [Code Quality](docs/CODE_QUALITY.md) - Quality checks and pre-commit hooks
- [Fyers API Docs](docs/FYERS_API_REFERENCE.md) - Fyers API reference
- [Architecture](docs/ARCHITECTURE.md) - System architecture
- [Data Flow](docs/DATA_ARCHITECTURE.md) - Data architecture
- [Symbol Format Rules](docs/SYMBOL_FORMAT_RULES.md) - Symbol conversions
- [Audit Report](COMPREHENSIVE_AUDIT_2026.md) - Performance audit

### External Links
- [Fyers API](https://myapi.fyers.in/docs/)
- [NSE India](https://www.nseindia.com/)
- [FastAPI Docs](https://fastapi.tiangolo.com/)
- [Next.js Docs](https://nextjs.org/docs)

---

## License

See [LICENSE](LICENSE) file for details.

---

**Last Updated:** February 18, 2026
