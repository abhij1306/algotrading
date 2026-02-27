/**
 * Property 6: Financial Data Typography
 * Validates: Requirements 3.6
 *
 * For any element displaying financial data (prices, percentages, quantities),
 * it should use the monospace font family with tabular number formatting.
 */

const fs = require('node:fs');
const path = require('node:path');

// Pattern to find elements that likely display financial data
const FINANCIAL_DATA_PATTERNS = [
  /className="[^"]*\b(price|return|sharpe|drawdown|profit|loss|pnl|change|percentage)\b[^"]*"/gi,
  /\{[^}]*(price|return|sharpe|drawdown|profit|loss|pnl|change|percentage)[^}]*\}/gi,
];

// Required classes for financial data
const REQUIRED_FONT_MONO = /font-mono/;
const REQUIRED_TABULAR_NUMS = /tabular-nums/;

function getAllFiles(dir, fileList = []) {
  const files = fs.readdirSync(dir);

  files.forEach(file => {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);

    if (stat.isDirectory()) {
      if (!file.startsWith('.') && file !== 'node_modules' && file !== 'dist' && file !== 'build') {
        getAllFiles(filePath, fileList);
      }
    } else if (file.endsWith('.tsx') || file.endsWith('.jsx')) {
      fileList.push(filePath);
    }
  });

  return fileList;
}

function testFinancialDataTypography() {
  console.log('🧪 Testing Property 6: Financial Data Typography\n');

  const frontendDir = path.join(__dirname, '../..');
  const allFiles = getAllFiles(frontendDir);

  let violations = [];
  let checked = 0;
  let totalChecks = 0;

  allFiles.forEach(filePath => {
    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Skip test files
    if (filePath.includes('__tests__')) {
      return;
    }

    // Find elements with financial data indicators
    const lines = content.split('\n');
    lines.forEach((line, index) => {
      const lineNumber = index + 1;

      // Check if line contains financial data indicators
      const hasFinancialData = FINANCIAL_DATA_PATTERNS.some(pattern => pattern.test(line));

      if (hasFinancialData) {
        checked++;

        // Check if the element or its parent has font-mono and tabular-nums
        // Look at the current line and a few lines before/after for context
        const contextStart = Math.max(0, index - 3);
        const contextEnd = Math.min(lines.length, index + 3);
        const context = lines.slice(contextStart, contextEnd).join('\n');

        const hasFontMono = REQUIRED_FONT_MONO.test(context);
        const hasTabularNums = REQUIRED_TABULAR_NUMS.test(context);

        if (!hasFontMono || !hasTabularNums) {
          violations.push({
            file: path.relative(frontendDir, filePath),
            line: lineNumber,
            issue: `Financial data without proper typography`,
            missing: [
              !hasFontMono && 'font-mono',
              !hasTabularNums && 'tabular-nums',
            ].filter(Boolean).join(', '),
            context: line.trim().substring(0, 100),
            suggestion: 'Add font-mono and tabular-nums classes for financial data',
            severity: 'error',
          });
        }
      }
    });
  });

  console.log(`✅ Checked ${totalChecks} files`);
  console.log(`✅ Found ${checked} elements with financial data\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: All financial data uses proper typography\n');
    return true;
  }

  console.log(`❌ FAIL: Found ${violations.length} financial data elements without proper typography:\n`);
  violations.slice(0, 10).forEach(v => {
    console.log(`  File: ${v.file}:${v.line}`);
    console.log(`  Issue: ${v.issue}`);
    console.log(`  Missing: ${v.missing}`);
    console.log(`  Context: ${v.context}...`);
    console.log(`  Suggestion: ${v.suggestion}\n`);
  });

  if (violations.length > 10) {
    console.log(`  ... and ${violations.length - 10} more violations\n`);
  }

  return false;
}

// Run the test
const passed = testFinancialDataTypography();
process.exit(passed ? 0 : 1);
