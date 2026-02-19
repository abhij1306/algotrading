#!/bin/bash
# Setup script for code quality infrastructure
# Run this after cloning the repository

set -e

echo "🚀 Setting up SmartTrader code quality infrastructure..."
echo ""

# Check if we're in the right directory
if [ ! -f "package.json" ] && [ ! -d "frontend" ]; then
    echo "❌ Error: Run this script from the project root directory"
    exit 1
fi

# Install pre-commit
echo "📦 Installing pre-commit..."
if command -v pre-commit &> /dev/null; then
    echo "✅ pre-commit already installed"
else
    pip install pre-commit
fi

# Install pre-commit hooks
echo "🔗 Installing pre-commit hooks..."
pre-commit install

# Make scripts executable
echo "🔧 Making scripts executable..."
chmod +x backend/scripts/check_console_log.sh
chmod +x backend/scripts/check_symbol_format.py
chmod +x backend/scripts/check_todo_fixme.py
chmod +x scripts/check-bundle-size.js

# Install frontend dependencies
echo "📦 Installing frontend dependencies..."
cd frontend
npm install
cd ..

# Install backend dependencies
echo "📦 Installing backend dependencies..."
cd backend
pip install -r requirements.txt
pip install ruff pytest pytest-cov
cd ..

# Run initial checks
echo "🔍 Running initial quality checks..."
echo ""

echo "Frontend checks:"
cd frontend
npm run lint || echo "⚠️  ESLint found issues (expected on first run)"
cd ..

echo ""
echo "Backend checks:"
cd backend
ruff check . || echo "⚠️  Ruff found issues (expected on first run)"
cd ..

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Review any linting errors above"
echo "  2. Run 'pre-commit run --all-files' to test all hooks"
echo "  3. See docs/CODE_QUALITY_SETUP.md for usage guide"
echo ""
