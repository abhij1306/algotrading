# Code Quality Guide (Solo Version)

Quick reference for code quality standards in SmartTrader.

## Quick Commands

```bash
# Setup
pip install pre-commit
pre-commit install

# Frontend
cd frontend && npm run lint        # Check
npm run lint:fix                   # Fix auto-fixable

# Backend
cd backend && ruff check .         # Check
ruff check . --fix                 # Fix auto-fixable

# Pre-commit (run all)
pre-commit run --all-files

# Bypass if needed (emergency only)
git commit --no-verify -m "message"
```

## Standards

### Frontend (TypeScript/React)

| Rule | Good | Bad |
|------|------|-----|
| Colors | `className="bg-background"` | `className="bg-[#050505]"` |
| Text sizes | `className="text-sm"` | `className="text-[11px]"` |
| No console.log | `console.warn()` or `console.error()` | `console.log()` |
| No any types | `const data: ApiResponse` | `const data: any` |

### Backend (Python)

| Rule | Good | Bad |
|------|------|-----|
| Symbol format | `symbol_master.to_db(sym)` | `sym.replace('NSE:', '')` |
| Type hints | `def func(x: int) -> str:` | `def func(x):` |

## Pre-commit Hooks

Runs automatically on commit:
- ESLint (frontend)
- Ruff (backend)
- Symbol format check
- Trailing whitespace fix

## CI Pipeline

Runs on PR/push to main:
- Frontend: lint, type-check, build
- Backend: ruff, symbol format check

## Troubleshooting

**Hook fails:** Run `pre-commit run --all-files` to see full output

**TypeScript errors:** Run `cd frontend && npx tsc --noEmit`

**Ruff not found:** `pip install ruff`
