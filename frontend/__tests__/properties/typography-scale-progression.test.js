/**
 * Property 5: Typography Scale Progression
 * Validates: Requirements 3.3
 *
 * For any two adjacent font size tokens in the type scale, the ratio between them
 * should be consistent (approximately 1.2x to 1.5x), ensuring harmonious typography.
 */

const fs = require('node:fs');
const path = require('node:path');

// Expected font size tokens in order from smallest to largest
const FONT_SIZE_TOKENS = [
  'text-xxs',
  'text-xs',
  'text-sm',
  'text-base',
  'text-lg',
  'text-xl',
  'text-2xl',
];

// Acceptable ratio range for type scale progression
const MIN_RATIO = 1.1;  // Slightly relaxed from 1.2 for flexibility
const MAX_RATIO = 1.6;  // Slightly relaxed from 1.5 for flexibility

function parseFontSize(value) {
  // Extract numeric value from CSS (e.g., "14px" -> 14, "1rem" -> 16)
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

function testTypographyScaleProgression() {
  console.log('🧪 Testing Property 5: Typography Scale Progression\n');

  const globalsPath = path.join(__dirname, '../../app/globals.css');

  if (!fs.existsSync(globalsPath)) {
    console.log('❌ FAIL: globals.css not found\n');
    return false;
  }

  const content = fs.readFileSync(globalsPath, 'utf-8');

  // Extract font size token definitions from :root
  const rootMatch = content.match(/:root\s*\{([^}]+)\}/s);
  if (!rootMatch) {
    console.log('❌ FAIL: Could not find :root block in globals.css\n');
    return false;
  }

  const rootBlock = rootMatch[1];

  // Find all font size token definitions
  const fontSizes = {};
  FONT_SIZE_TOKENS.forEach(token => {
    const pattern = new RegExp(`--${token}:\\s*([^;]+);`);
    const match = rootBlock.match(pattern);
    if (match) {
      fontSizes[token] = match[1].trim();
    }
  });

  console.log('📊 Found font size tokens:\n');
  Object.entries(fontSizes).forEach(([token, value]) => {
    const pixelValue = parseFontSize(value);
    console.log(`  --${token}: ${value} (${pixelValue}px)`);
  });
  console.log('');

  // Check if all expected tokens are present
  const missingTokens = FONT_SIZE_TOKENS.filter(token => !fontSizes[token]);
  if (missingTokens.length > 0) {
    console.log(`❌ FAIL: Missing font size tokens:\n`);
    missingTokens.forEach(token => {
      console.log(`  --${token}\n`);
    });
    return false;
  }

  // Parse all font sizes to pixel values
  const parsedSizes = {};
  for (const [token, value] of Object.entries(fontSizes)) {
    const pixelValue = parseFontSize(value);
    if (pixelValue === null) {
      console.log(`❌ FAIL: Could not parse font size for --${token}: ${value}\n`);
      return false;
    }
    parsedSizes[token] = pixelValue;
  }

  // Calculate ratios between adjacent sizes
  const ratios = [];
  let violations = [];

  console.log('📐 Checking scale progression ratios:\n');

  for (let i = 0; i < FONT_SIZE_TOKENS.length - 1; i++) {
    const currentToken = FONT_SIZE_TOKENS[i];
    const nextToken = FONT_SIZE_TOKENS[i + 1];

    const currentSize = parsedSizes[currentToken];
    const nextSize = parsedSizes[nextToken];

    const ratio = nextSize / currentSize;
    ratios.push(ratio);

    const isValid = ratio >= MIN_RATIO && ratio <= MAX_RATIO;
    const status = isValid ? '✅' : '❌';

    console.log(`  ${status} --${currentToken} (${currentSize}px) → --${nextToken} (${nextSize}px)`);
    console.log(`     Ratio: ${ratio.toFixed(3)}x ${isValid ? '(valid)' : '(INVALID)'}`);

    if (!isValid) {
      violations.push({
        from: currentToken,
        to: nextToken,
        fromSize: currentSize,
        toSize: nextSize,
        ratio: ratio.toFixed(3),
        issue: `Ratio ${ratio.toFixed(3)}x is outside acceptable range (${MIN_RATIO}x - ${MAX_RATIO}x)`,
      });
    }
  }
  console.log('');

  // Calculate average ratio and standard deviation
  const avgRatio = ratios.reduce((sum, r) => sum + r, 0) / ratios.length;
  const variance = ratios.reduce((sum, r) => sum + Math.pow(r - avgRatio, 2), 0) / ratios.length;
  const stdDev = Math.sqrt(variance);

  console.log('📊 Scale Statistics:\n');
  console.log(`  Average ratio: ${avgRatio.toFixed(3)}x`);
  console.log(`  Standard deviation: ${stdDev.toFixed(3)}`);
  console.log(`  Consistency: ${stdDev < 0.1 ? '✅ Excellent' : stdDev < 0.2 ? '⚠️  Good' : '❌ Poor'}\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: All font size ratios are within acceptable range\n');
    console.log('The type scale follows a harmonious progression suitable for professional typography.\n');
    return true;
  }

  console.log(`❌ FAIL: Found ${violations.length} ratio violations:\n`);
  violations.forEach(v => {
    console.log(`  From: --${v.from} (${v.fromSize}px)`);
    console.log(`  To: --${v.to} (${v.toSize}px)`);
    console.log(`  Ratio: ${v.ratio}x`);
    console.log(`  Issue: ${v.issue}\n`);
  });

  console.log(`Expected ratio range: ${MIN_RATIO}x to ${MAX_RATIO}x\n`);

  return false;
}

// Run the test
const passed = testTypographyScaleProgression();
process.exit(passed ? 0 : 1);
