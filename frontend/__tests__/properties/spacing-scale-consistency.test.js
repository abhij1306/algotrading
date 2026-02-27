/**
 * Property 1: Spacing Scale Consistency
 * Validates: Requirements 2.3
 *
 * For any spacing token in the design system, the value should be a multiple of 4 pixels,
 * ensuring consistent spacing throughout the application.
 */

const fs = require('node:fs');
const path = require('node:path');

// Expected spacing tokens and their values (all should be multiples of 4px)
const EXPECTED_SPACING_TOKENS = {
  '--spacing-1': 4,
  '--spacing-2': 8,
  '--spacing-3': 12,
  '--spacing-4': 16,
  '--spacing-5': 20,
  '--spacing-6': 24,
  '--spacing-8': 32,
  '--spacing-10': 40,
  '--spacing-12': 48,
};

function parseSpacingValue(value) {
  // Extract numeric value from CSS (e.g., "4px" -> 4, "1rem" -> 16)
  const match = value.match(/^(\d+(?:\.\d+)?)(px|rem)?$/);
  if (!match) return null;

  const num = Number.parseFloat(match[1]);
  const unit = match[2];

  if (unit === 'rem') {
    // Convert rem to px (assuming 1rem = 16px)
    return num * 16;
  }

  return num;
}

function testSpacingScaleConsistency() {
  console.log('🧪 Testing Property 1: Spacing Scale Consistency\n');

  const globalsPath = path.join(__dirname, '../../app/globals.css');

  if (!fs.existsSync(globalsPath)) {
    console.log('❌ FAIL: globals.css not found\n');
    return false;
  }

  const content = fs.readFileSync(globalsPath, 'utf-8');

  let violations = [];
  let foundTokens = {};
  let totalChecks = 0;

  // Extract spacing token definitions from :root
  const rootMatch = content.match(/:root\s*\{([^}]+)\}/s);
  if (!rootMatch) {
    console.log('❌ FAIL: Could not find :root block in globals.css\n');
    return false;
  }

  const rootBlock = rootMatch[1];

  // Find all spacing token definitions
  const spacingPattern = /--(spacing-\d+):\s*([^;]+);/g;
  let match;

  while ((match = spacingPattern.exec(rootBlock)) !== null) {
    const tokenName = `--${match[1]}`;
    const tokenValue = match[2].trim();
    totalChecks++;

    foundTokens[tokenName] = tokenValue;

    // Parse the value to get numeric pixels
    const pixelValue = parseSpacingValue(tokenValue);

    if (pixelValue === null) {
      violations.push({
        token: tokenName,
        value: tokenValue,
        issue: 'Could not parse spacing value',
        severity: 'error',
      });
      continue;
    }

    // Check if it's a multiple of 4
    if (pixelValue % 4 !== 0) {
      violations.push({
        token: tokenName,
        value: tokenValue,
        pixelValue,
        issue: `Value ${pixelValue}px is not a multiple of 4`,
        severity: 'error',
      });
    }

    // Check if it matches expected value
    if (EXPECTED_SPACING_TOKENS[tokenName]) {
      if (pixelValue !== EXPECTED_SPACING_TOKENS[tokenName]) {
        violations.push({
          token: tokenName,
          value: tokenValue,
          pixelValue,
          expected: EXPECTED_SPACING_TOKENS[tokenName],
          issue: `Expected ${EXPECTED_SPACING_TOKENS[tokenName]}px but got ${pixelValue}px`,
          severity: 'warning',
        });
      }
    }
  }

  console.log(`✅ Checked ${totalChecks} spacing tokens\n`);

  // Check if all expected tokens are present
  const missingTokens = Object.keys(EXPECTED_SPACING_TOKENS).filter(
    token => !foundTokens[token]
  );

  if (missingTokens.length > 0) {
    console.log(`⚠️  WARNING: Missing expected spacing tokens:\n`);
    missingTokens.forEach(token => {
      console.log(`  ${token}: ${EXPECTED_SPACING_TOKENS[token]}px\n`);
    });
  }

  // Report found tokens
  console.log('📊 Found spacing tokens:\n');
  Object.entries(foundTokens).forEach(([token, value]) => {
    const pixelValue = parseSpacingValue(value);
    const isMultipleOf4 = pixelValue && pixelValue % 4 === 0;
    const status = isMultipleOf4 ? '✅' : '❌';
    console.log(`  ${status} ${token}: ${value} (${pixelValue}px)`);
  });
  console.log('');

  if (violations.length === 0) {
    console.log('✅ PASS: All spacing tokens are multiples of 4px\n');
    return true;
  }

  // Report violations
  const errors = violations.filter(v => v.severity === 'error');
  const warnings = violations.filter(v => v.severity === 'warning');

  if (errors.length > 0) {
    console.log(`❌ ERRORS: Found ${errors.length} spacing violations:\n`);
    errors.forEach(v => {
      console.log(`  Token: ${v.token}`);
      console.log(`  Value: ${v.value}`);
      if (v.pixelValue) console.log(`  Pixel Value: ${v.pixelValue}px`);
      console.log(`  Issue: ${v.issue}\n`);
    });
  }

  if (warnings.length > 0) {
    console.log(`⚠️  WARNINGS: Found ${warnings.length} spacing mismatches:\n`);
    warnings.forEach(v => {
      console.log(`  Token: ${v.token}`);
      console.log(`  Value: ${v.value} (${v.pixelValue}px)`);
      console.log(`  Expected: ${v.expected}px`);
      console.log(`  Issue: ${v.issue}\n`);
    });
  }

  return errors.length === 0;
}

// Run the test
const passed = testSpacingScaleConsistency();
process.exit(passed ? 0 : 1);
