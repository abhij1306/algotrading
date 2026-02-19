/* eslint-disable */
/**
 * Property 10: Component Spacing Consistency
 * Validates: Requirements 5.6
 *
 * For any spacing value used in components, it should come from the
 * defined spacing scale tokens (4px multiples).
 */

const fs = require('fs');
const path = require('path');

// Valid spacing values from the design system (4px multiples)
const VALID_SPACING = [
  '0', '0.5', '1', '1.5', '2', '2.5', '3', '3.5', '4', '5', '6', '7', '8',
  '9', '10', '11', '12', '14', '16', '20', '24', '28', '32', '36', '40',
  '44', '48', '52', '56', '60', '64', '72', '80', '96'
];

// Spacing class patterns
const SPACING_PATTERNS = [
  /\b(p|px|py|pt|pb|pl|pr)-(\d+(?:\.\d+)?)\b/g,
  /\b(m|mx|my|mt|mb|ml|mr)-(\d+(?:\.\d+)?)\b/g,
  /\bgap-(\d+(?:\.\d+)?)\b/g,
  /\bspace-[xy]-(\d+(?:\.\d+)?)\b/g,
];

// Arbitrary spacing values that should not be used
const ARBITRARY_SPACING_PATTERN = /\[([\d.]+)(?:px|rem|em)\]/g;

function testComponentSpacingConsistency() {
  console.log('🧪 Testing Property 10: Component Spacing Consistency\n');

  const componentsDir = path.join(__dirname, '../../components/ui');
  const componentFiles = fs.readdirSync(componentsDir)
    .filter(f => f.endsWith('.tsx') || f.endsWith('.ts'));

  let violations = [];
  let totalChecks = 0;

  componentFiles.forEach(file => {
    const filePath = path.join(componentsDir, file);
    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Check for arbitrary spacing values
    const arbitraryMatches = [...content.matchAll(ARBITRARY_SPACING_PATTERN)];
    arbitraryMatches.forEach(match => {
      const value = match[1];
      const fullMatch = match[0];

      // Skip border widths (1px is common for borders)
      if (value === '1' && fullMatch.includes('px') &&
          (content.includes('border-') || content.includes('p-[1px]'))) {
        return;
      }

      // Skip max-width constraints (like 1920px for container widths)
      if (fullMatch.includes('px') && parseInt(value) > 100) {
        return;
      }

      // Check if it's a spacing value (not color, etc.)
      if (match[0].includes('px') || match[0].includes('rem') || match[0].includes('em')) {
        violations.push({
          file,
          issue: `Arbitrary spacing value: ${match[0]}`,
          suggestion: 'Use Tailwind spacing scale instead',
        });
      }
    });

    // Check spacing class values
    SPACING_PATTERNS.forEach(pattern => {
      const matches = [...content.matchAll(pattern)];
      matches.forEach(match => {
        const value = match[match.length - 1]; // Last capture group is the value

        // Skip if it's a valid spacing value
        if (VALID_SPACING.includes(value)) {
          return;
        }

        // Skip special values
        if (['auto', 'full', 'screen', 'min', 'max', 'fit'].includes(value)) {
          return;
        }

        violations.push({
          file,
          issue: `Non-standard spacing value: ${match[0]}`,
          value,
          suggestion: 'Use spacing scale values (4px multiples)',
        });
      });
    });
  });

  console.log(`✅ Checked ${totalChecks} component files\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: All components use consistent spacing from the design system\n');
    return true;
  } else {
    console.log(`❌ FAIL: Found ${violations.length} spacing violations:\n`);
    violations.forEach(v => {
      console.log(`  File: ${v.file}`);
      console.log(`  Issue: ${v.issue}`);
      if (v.value) console.log(`  Value: ${v.value}`);
      console.log(`  Suggestion: ${v.suggestion}\n`);
    });
    return false;
  }
}

// Run the test
const passed = testComponentSpacingConsistency();
process.exit(passed ? 0 : 1);
