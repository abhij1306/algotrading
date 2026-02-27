/**
 * Property 14: Raycast Component Refactoring
 * Validates: Requirements 7.2
 *
 * For any component file that was in the raycast/ directory and is kept,
 * it should use unified design tokens exclusively.
 */

const fs = require('node:fs');
const path = require('node:path');

// Pattern to find Raycast CSS variables
const RAYCAST_VAR_PATTERN = /var\(--raycast-[a-z-]+\)/g;
const RAYCAST_STYLE_PATTERN = /style=\{\{[^}]*--raycast-[^}]*\}\}/g;

function testRaycastComponentRefactoring() {
  console.log('🧪 Testing Property 14: Raycast Component Refactoring\n');

  const raycastComponentsDir = path.join(__dirname, '../../components/raycast');

  // Check if directory exists
  if (!fs.existsSync(raycastComponentsDir)) {
    console.log('✅ PASS: No Raycast components directory found (all components migrated)\n');
    return true;
  }

  const componentFiles = fs.readdirSync(raycastComponentsDir)
    .filter(file => file.endsWith('.tsx') || file.endsWith('.ts'))
    .map(file => path.join(raycastComponentsDir, file));

  let violations = [];
  let totalChecks = 0;

  componentFiles.forEach(filePath => {
    const content = fs.readFileSync(filePath, 'utf-8');
    const fileName = path.basename(filePath);
    totalChecks++;

    // Check for var(--raycast-*) references
    const varMatches = [...content.matchAll(RAYCAST_VAR_PATTERN)];
    varMatches.forEach(match => {
      const lineNumber = content.substring(0, match.index).split('\n').length;
      violations.push({
        file: fileName,
        line: lineNumber,
        issue: `Raycast CSS variable found: ${match[0]}`,
        suggestion: 'Replace with unified design token (e.g., var(--color-background))',
        severity: 'error',
      });
    });

    // Check for inline styles with Raycast variables
    const styleMatches = [...content.matchAll(RAYCAST_STYLE_PATTERN)];
    styleMatches.forEach(match => {
      const lineNumber = content.substring(0, match.index).split('\n').length;
      violations.push({
        file: fileName,
        line: lineNumber,
        issue: `Inline style with Raycast variable found`,
        suggestion: 'Replace with Tailwind classes or unified design tokens',
        severity: 'error',
      });
    });
  });

  console.log(`✅ Checked ${totalChecks} Raycast component files\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: All Raycast components use unified design tokens exclusively\n');
    return true;
  }

  console.log(`❌ FAIL: Found ${violations.length} Raycast variable references in components:\n`);
  violations.forEach(v => {
    console.log(`  File: ${v.file}:${v.line}`);
    console.log(`  Issue: ${v.issue}`);
    console.log(`  Suggestion: ${v.suggestion}\n`);
  });

  return false;
}

// Run the test
const passed = testRaycastComponentRefactoring();
process.exit(passed ? 0 : 1);
