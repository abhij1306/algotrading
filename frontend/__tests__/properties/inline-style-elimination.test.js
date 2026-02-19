/* eslint-disable */
/**
 * Property 12: Inline Style Elimination
 * Validates: Requirements 5.8, 6.5
 *
 * For any component or page file, inline style attributes should not contain
 * CSS variable references (use Tailwind classes instead).
 */

const fs = require('fs');
const path = require('path');

// Pattern to find inline style attributes with CSS variables
const INLINE_STYLE_WITH_VAR_PATTERN = /style=\{[^}]*var\([^)]+\)[^}]*\}/g;

// Pattern to find any inline style attribute
const INLINE_STYLE_PATTERN = /style=\{/g;

function testInlineStyleElimination() {
  console.log('🧪 Testing Property 12: Inline Style Elimination\n');

  const componentsDir = path.join(__dirname, '../../components/ui');
  const componentFiles = fs.readdirSync(componentsDir)
    .filter(f => f.endsWith('.tsx') || f.endsWith('.ts'));

  let violations = [];
  let warnings = [];
  let totalChecks = 0;

  componentFiles.forEach(file => {
    const filePath = path.join(componentsDir, file);
    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Check for inline styles with CSS variables (VIOLATION)
    const varMatches = [...content.matchAll(INLINE_STYLE_WITH_VAR_PATTERN)];
    varMatches.forEach(match => {
      // Extract the var() references
      const vars = match[0].match(/var\([^)]+\)/g);
      violations.push({
        file,
        issue: `Inline style with CSS variable: ${vars?.join(', ')}`,
        suggestion: 'Replace with Tailwind classes that use design tokens',
        severity: 'error',
      });
    });

    // Check for any inline styles (WARNING - acceptable for dynamic values)
    const styleMatches = [...content.matchAll(INLINE_STYLE_PATTERN)];
    if (styleMatches.length > 0) {
      // Get context around each match
      styleMatches.forEach(match => {
        const index = match.index || 0;
        const start = Math.max(0, index - 50);
        const end = Math.min(content.length, index + 150);
        const context = content.substring(start, end);

        // Check if it's for dynamic values (width, height props)
        const isDynamic =
          context.includes('width:') ||
          context.includes('height:') ||
          context.includes('...style');

        if (!isDynamic) {
          warnings.push({
            file,
            issue: 'Inline style attribute found',
            context: context.replace(/\n/g, ' ').trim(),
            suggestion: 'Consider using Tailwind classes instead',
            severity: 'warning',
          });
        }
      });
    }
  });

  console.log(`✅ Checked ${totalChecks} component files\n`);

  if (violations.length === 0 && warnings.length === 0) {
    console.log('✅ PASS: No inline styles with CSS variables found\n');
    return true;
  }

  let hasErrors = false;

  if (violations.length > 0) {
    hasErrors = true;
    console.log(`❌ ERRORS: Found ${violations.length} inline styles with CSS variables:\n`);
    violations.forEach(v => {
      console.log(`  File: ${v.file}`);
      console.log(`  Issue: ${v.issue}`);
      console.log(`  Suggestion: ${v.suggestion}\n`);
    });
  }

  if (warnings.length > 0) {
    console.log(`⚠️  WARNINGS: Found ${warnings.length} inline style attributes:\n`);
    warnings.forEach(w => {
      console.log(`  File: ${w.file}`);
      console.log(`  Issue: ${w.issue}`);
      console.log(`  Context: ${w.context.substring(0, 100)}...`);
      console.log(`  Suggestion: ${w.suggestion}\n`);
    });
    console.log('Note: Inline styles for dynamic values (width, height) are acceptable.\n');
  }

  return !hasErrors;
}

// Run the test
const passed = testInlineStyleElimination();
process.exit(passed ? 0 : 1);
