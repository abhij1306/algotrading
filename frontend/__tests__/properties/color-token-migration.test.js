/* eslint-disable */
/**
 * Property 4: Color Token Migration Completeness
 * Validates: Requirements 2.8
 *
 * For any style declaration in the codebase, color values should reference design tokens
 * (CSS variables) rather than hardcoded hex, rgb, or named colors.
 */

const fs = require('fs');
const path = require('path');

// Pattern to find hardcoded colors in style attributes and CSS
const HEX_COLOR_PATTERN = /#[0-9a-fA-F]{3,8}/g;
const RGB_COLOR_PATTERN = /rgba?\([^)]+\)/g;
const NAMED_COLOR_PATTERN = /\b(red|blue|green|yellow|orange|purple|pink|gray|grey|black|white|brown|cyan|magenta)\b/gi;

// Allowed exceptions
const ALLOWED_EXCEPTIONS = [
  'transparent',
  'currentColor',
  'inherit',
  'initial',
  'unset',
  'white', // Allowed in specific contexts like text on colored backgrounds
  'black', // Allowed in specific contexts
];

function getAllFiles(dir, fileList = []) {
  const files = fs.readdirSync(dir);

  files.forEach(file => {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);

    if (stat.isDirectory()) {
      if (!file.startsWith('.') && file !== 'node_modules' && file !== 'dist' && file !== 'build') {
        getAllFiles(filePath, fileList);
      }
    } else if (file.endsWith('.tsx') || file.endsWith('.ts') || file.endsWith('.css') || file.endsWith('.jsx') || file.endsWith('.js')) {
      fileList.push(filePath);
    }
  });

  return fileList;
}

function testColorTokenMigration() {
  console.log('🧪 Testing Property 4: Color Token Migration Completeness\n');

  const frontendDir = path.join(__dirname, '../..');
  const allFiles = getAllFiles(frontendDir);

  let violations = [];
  let warnings = [];
  let totalChecks = 0;

  allFiles.forEach(filePath => {
    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Skip test files and migration map
    if (filePath.includes('__tests__') || filePath.includes('migration-map')) {
      return;
    }

    // Check for hex colors
    const hexMatches = [...content.matchAll(HEX_COLOR_PATTERN)];
    hexMatches.forEach(match => {
      const lineNumber = content.substring(0, match.index).split('\n').length;
      const line = content.split('\n')[lineNumber - 1];

      // Skip if it's in a comment
      if (line.trim().startsWith('//') || line.trim().startsWith('*')) {
        return;
      }

      violations.push({
        file: path.relative(frontendDir, filePath),
        line: lineNumber,
        issue: `Hardcoded hex color: ${match[0]}`,
        suggestion: 'Replace with design token CSS variable',
        severity: 'error',
      });
    });

    // Check for RGB colors
    const rgbMatches = [...content.matchAll(RGB_COLOR_PATTERN)];
    rgbMatches.forEach(match => {
      const lineNumber = content.substring(0, match.index).split('\n').length;
      const line = content.split('\n')[lineNumber - 1];

      // Skip if it's in a comment or if it's using CSS variables
      if (line.trim().startsWith('//') || line.trim().startsWith('*') || match[0].includes('var(')) {
        return;
      }

      violations.push({
        file: path.relative(frontendDir, filePath),
        line: lineNumber,
        issue: `Hardcoded RGB color: ${match[0]}`,
        suggestion: 'Replace with design token CSS variable',
        severity: 'error',
      });
    });

    // Check for named colors (with exceptions)
    const namedMatches = [...content.matchAll(NAMED_COLOR_PATTERN)];
    namedMatches.forEach(match => {
      const color = match[0].toLowerCase();

      // Skip allowed exceptions
      if (ALLOWED_EXCEPTIONS.includes(color)) {
        return;
      }

      const lineNumber = content.substring(0, match.index).split('\n').length;
      const line = content.split('\n')[lineNumber - 1];

      // Skip if it's in a comment or not in a style context
      if (line.trim().startsWith('//') || line.trim().startsWith('*')) {
        return;
      }

      // Check if it's in a style context (className, style, or CSS property)
      const context = content.substring(Math.max(0, (match.index || 0) - 50), (match.index || 0) + 50);
      if (context.includes('color:') || context.includes('background') || context.includes('border') || context.includes('className')) {
        warnings.push({
          file: path.relative(frontendDir, filePath),
          line: lineNumber,
          issue: `Named color: ${match[0]}`,
          context: line.trim(),
          suggestion: 'Consider using design token CSS variable',
          severity: 'warning',
        });
      }
    });
  });

  console.log(`✅ Checked ${totalChecks} files\n`);

  if (violations.length === 0 && warnings.length === 0) {
    console.log('✅ PASS: All colors use design tokens\n');
    return true;
  }

  let hasErrors = false;

  if (violations.length > 0) {
    hasErrors = true;
    console.log(`❌ ERRORS: Found ${violations.length} hardcoded colors:\n`);
    violations.forEach(v => {
      console.log(`  File: ${v.file}:${v.line}`);
      console.log(`  Issue: ${v.issue}`);
      console.log(`  Suggestion: ${v.suggestion}\n`);
    });
  }

  if (warnings.length > 0) {
    console.log(`⚠️  WARNINGS: Found ${warnings.length} named colors:\n`);
    warnings.slice(0, 10).forEach(w => {
      console.log(`  File: ${w.file}:${w.line}`);
      console.log(`  Issue: ${w.issue}`);
      console.log(`  Context: ${w.context}\n`);
    });
    if (warnings.length > 10) {
      console.log(`  ... and ${warnings.length - 10} more warnings\n`);
    }
  }

  return !hasErrors;
}

// Run the test
const passed = testColorTokenMigration();
process.exit(passed ? 0 : 1);
