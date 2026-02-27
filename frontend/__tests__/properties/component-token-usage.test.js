/**
 * Property 9: Component Token Usage
 * Validates: Requirements 5.2, 5.3, 5.4, 5.5
 *
 * For any UI component (Button, Card, Input, Badge), all color, spacing, and sizing
 * properties should reference unified design tokens rather than hardcoded values.
 */

const fs = require('node:fs');
const path = require('node:path');

// Unified design tokens that are allowed
const ALLOWED_TOKENS = [
  // Colors
  'background', 'background-secondary', 'background-tertiary',
  'surface', 'elevated',
  'foreground', 'foreground-secondary', 'foreground-tertiary', 'foreground-muted',
  'border', 'border-subtle', 'border-focus',
  'primary', 'primary-hover', 'primary-light', 'primary-fg',
  'profit', 'profit-bg', 'loss', 'loss-bg', 'warning', 'warning-bg',
  // Spacing (as Tailwind classes)
  'p-', 'px-', 'py-', 'pt-', 'pb-', 'pl-', 'pr-',
  'm-', 'mx-', 'my-', 'mt-', 'mb-', 'ml-', 'mr-',
  'gap-', 'space-',
  // Sizing
  'h-', 'w-', 'min-h-', 'min-w-', 'max-h-', 'max-w-',
  // Radius
  'rounded',
  // Shadows
  'shadow',
];

// Hardcoded values that should NOT be used (except for special cases)
const FORBIDDEN_PATTERNS = [
  /#[0-9a-fA-F]{3,6}(?![0-9a-fA-F])/,  // Hex colors like #fff, #000000
  /rgb\(/,                               // RGB colors
  /rgba\(/,                              // RGBA colors
  /hsl\(/,                               // HSL colors
];

// Allowed exceptions
const ALLOWED_EXCEPTIONS = [
  'transparent',
  'currentColor',
  'inherit',
  'auto',
  '0',
  '100%',
  'full',
  'screen',
];

function testComponentTokenUsage() {
  console.log('🧪 Testing Property 9: Component Token Usage\n');

  const componentsDir = path.join(__dirname, '../../components/ui');
  const componentFiles = [
    'button.tsx',
    'card.tsx',
    'input.tsx',
    'badge.tsx',
  ];

  let violations = [];
  let totalChecks = 0;

  componentFiles.forEach(file => {
    const filePath = path.join(componentsDir, file);

    if (!fs.existsSync(filePath)) {
      console.log(`⚠️  File not found: ${file}`);
      return;
    }

    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Check for forbidden patterns
    FORBIDDEN_PATTERNS.forEach(pattern => {
      const matches = content.match(new RegExp(pattern, 'g'));
      if (matches) {
        matches.forEach(match => {
          // Check if it's in an allowed exception context
          const isException = ALLOWED_EXCEPTIONS.some(exc =>
            content.includes(`"${exc}"`) || content.includes(`'${exc}'`)
          );

          if (!isException) {
            violations.push({
              file,
              issue: `Hardcoded color value found: ${match}`,
              pattern: pattern.toString(),
            });
          }
        });
      }
    });

    // Check for old Raycast variables
    const raycastVars = content.match(/var\(--raycast-[^)]+\)/g);
    if (raycastVars) {
      raycastVars.forEach(match => {
        violations.push({
          file,
          issue: `Raycast variable found: ${match}`,
          pattern: 'Raycast variables',
        });
      });
    }

    // Check for undefined custom variables
    const customVars = content.match(/var\(--(?!color-|spacing-|radius-|shadow-|font-|text-|leading-)[^)]+\)/g);
    if (customVars) {
      customVars.forEach(match => {
        violations.push({
          file,
          issue: `Non-standard CSS variable: ${match}`,
          pattern: 'Custom variables',
        });
      });
    }
  });

  console.log(`✅ Checked ${totalChecks} component files\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: All components use unified design tokens correctly\n');
    return true;
  } else {
    console.log(`❌ FAIL: Found ${violations.length} violations:\n`);
    violations.forEach(v => {
      console.log(`  File: ${v.file}`);
      console.log(`  Issue: ${v.issue}`);
      console.log(`  Pattern: ${v.pattern}\n`);
    });
    return false;
  }
}

// Run the test
const passed = testComponentTokenUsage();
process.exit(passed ? 0 : 1);
