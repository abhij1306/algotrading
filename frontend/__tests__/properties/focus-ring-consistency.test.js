/**
 * Property 11: Focus Ring Consistency
 * Validates: Requirements 5.7
 *
 * For any focusable component, the focus ring should use --color-border-focus
 * with consistent styling (2px ring, 20% opacity).
 */

const fs = require('node:fs');
const path = require('node:path');

// Expected focus ring patterns
const VALID_FOCUS_PATTERNS = [
  /focus-visible:ring-2/,
  /focus-visible:ring-primary/,
  /focus-visible:ring-loss/,
  /focus-visible:outline-none/,
];

// Components that should have focus states
const FOCUSABLE_COMPONENTS = [
  'button.tsx',
  'input.tsx',
];

function testFocusRingConsistency() {
  console.log('🧪 Testing Property 11: Focus Ring Consistency\n');

  const componentsDir = path.join(__dirname, '../../components/ui');

  let violations = [];
  let totalChecks = 0;

  FOCUSABLE_COMPONENTS.forEach(file => {
    const filePath = path.join(componentsDir, file);

    if (!fs.existsSync(filePath)) {
      console.log(`⚠️  File not found: ${file}`);
      return;
    }

    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Check if component has focus-visible styles
    const hasFocusVisible = content.includes('focus-visible:');

    if (!hasFocusVisible) {
      violations.push({
        file,
        issue: 'Missing focus-visible styles',
        suggestion: 'Add focus-visible:outline-none and focus-visible:ring-2',
      });
      return;
    }

    // Check for outline-none
    if (!content.includes('focus-visible:outline-none')) {
      violations.push({
        file,
        issue: 'Missing focus-visible:outline-none',
        suggestion: 'Add focus-visible:outline-none to remove default outline',
      });
    }

    // Check for ring-2
    if (!content.includes('focus-visible:ring-2')) {
      violations.push({
        file,
        issue: 'Missing focus-visible:ring-2',
        suggestion: 'Add focus-visible:ring-2 for consistent ring width',
      });
    }

    // Check for ring color using design tokens
    const hasRingColor =
      content.includes('focus-visible:ring-primary') ||
      content.includes('focus-visible:ring-loss') ||
      content.includes('focus-visible:ring-border-focus');

    if (!hasRingColor) {
      violations.push({
        file,
        issue: 'Missing focus ring color from design tokens',
        suggestion: 'Add focus-visible:ring-primary or focus-visible:ring-loss',
      });
    }

    // Check for hardcoded focus colors
    const hardcodedFocus = content.match(/focus-visible:ring-\[#[0-9a-fA-F]+\]/g);
    if (hardcodedFocus) {
      hardcodedFocus.forEach(match => {
        violations.push({
          file,
          issue: `Hardcoded focus ring color: ${match}`,
          suggestion: 'Use design token colors like ring-primary',
        });
      });
    }
  });

  console.log(`✅ Checked ${totalChecks} focusable components\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: All focusable components have consistent focus rings\n');
    return true;
  } else {
    console.log(`❌ FAIL: Found ${violations.length} focus ring violations:\n`);
    violations.forEach(v => {
      console.log(`  File: ${v.file}`);
      console.log(`  Issue: ${v.issue}`);
      console.log(`  Suggestion: ${v.suggestion}\n`);
    });
    return false;
  }
}

// Run the test
const passed = testFocusRingConsistency();
process.exit(passed ? 0 : 1);
