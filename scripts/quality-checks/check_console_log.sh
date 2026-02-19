#!/bin/bash
# Console.log Checker
#
# Detects console.log statements in production code.
# Allows console.warn and console.error.
# Excludes test files and stories.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

# Find console.log statements
violations=$(grep -rn "console\.log" frontend/app frontend/components frontend/lib frontend/stores \
  --include="*.ts" \
  --include="*.tsx" \
  --exclude="*.test.ts" \
  --exclude="*.test.tsx" \
  --exclude="*.stories.tsx" \
  2>/dev/null || true)

if [ -n "$violations" ]; then
  echo -e "${RED}❌ Found console.log statements in production code:${NC}\n"
  echo "$violations"
  echo -e "\n${RED}Use console.error or console.warn instead, or remove the statement.${NC}"
  exit 1
else
  echo -e "${GREEN}✅ No console.log violations found${NC}"
  exit 0
fi
