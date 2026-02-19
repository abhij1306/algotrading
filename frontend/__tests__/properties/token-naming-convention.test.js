/* eslint-disable */
/**
 * Property 2: Token Naming Convention
 * Validates: Requirements 2.6
 *
 * For any CSS variable reference in the codebase, it should follow the unified naming pattern
 * --color-*, --spacing-*, --radius-*, --shadow-*, --font-*, or --text-*.
 */

const fs = require('fs');
const path = require('path');

// Valid token prefixes for the unified design system
const VALID_TOKEN_PREFIXES = [
  'color',
  'spacing',
  'radius',
  'shadow',
  'font',
  'text',
  'leading',
];

// Pattern to match CSS variable references
const CSS_VAR_PATTERN = /var\(--([a-z0-9-]+)\)/g;

// Forbidden patterns (old Raycast variables)
const FORBIDDEN_PATTERNS = [
  /--raycast-/,
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
    } else if (
      file.endsWith('.tsx') ||
      file.endsWith('.ts') ||
      file.endsWith('.css') ||
      file.endsWith('.jsx') ||
      file.endsWith('.js')
    ) {
      fileList.push(filePath);
    }
  });

  return fileList;
}

function isValidTokenName(tokenName) {
  // Check if token starts with a valid prefix
  return VALID_TOKEN_PREFIXES.some(prefix => tokenName.startsWith(`${prefix}-`));
}

function isForbiddenToken(tokenName) {
  return FORBIDDEN_PATTERNS.some(pattern => pattern.test(tokenName));
}

function testTokenNamingConvention() {
  console.log('🧪 Testing Property 2: Token Naming Convention\n');

  const frontendDir = path.join(__dirname, '../..');
  const allFiles = getAllFiles(frontendDir);

  let violations = [];
  let forbiddenTokens = [];
  let validTokens = new Set();
  let totalChecks = 0;
  let filesChecked = 0;

  allFiles.forEach(filePath => {
    const content = fs.readFileSync(filePath, 'utf-8');
    filesChecked++;

    // Skip test files and migration map
    if (filePath.includes('__tests__') || filePath.includes('migration-map')) {
      return;
    }

    // Find all CSS variable references
    const matches = [...content.matchAll(CSS_VAR_PATTERN)];

    matches.forEach(match => {
      const tokenName = match[1];
      totalChecks++;

      // Check for forbidden patterns (Raycast variables)
      if (isForbiddenToken(tokenName)) {
        const lineNumber = content.substring(0, match.index).split('\n').length;
        const line = content.split('\n')[lineNumber - 1];

        forbiddenTokens.push({
          file: path.relative(frontendDir, filePath),
          line: lineNumber,
          token: `--${tokenName}`,
          context: line.trim(),
          issue: 'Forbidden token pattern (Raycast variable)',
          severity: 'error',
        });
        return;
      }

      // Check if token follows unified naming convention
      if (!isValidTokenName(tokenName)) {
        const lineNumber = content.substring(0, match.index).split('\n').length;
        const line = content.split('\n')[lineNumber - 1];

        // Skip if it's in a comment
        if (line.trim().startsWith('//') || line.trim().startsWith('*')) {
          return;
        }

        violations.push({
          file: path.relative(frontendDir, filePath),
          line: lineNumber,
          token: `--${tokenName}`,
          context: line.trim(),
          issue: `Token does not follow unified naming convention`,
          suggestion: `Use one of: ${VALID_TOKEN_PREFIXES.map(p => `--${p}-*`).join(', ')}`,
          severity: 'error',
        });
      } else {
        validTokens.add(`--${tokenName}`);
      }
    });
  });

  console.log(`✅ Checked ${totalChecks} CSS variable references in ${filesChecked} files\n`);
  console.log(`📊 Found ${validTokens.size} unique valid tokens\n`);

  if (violations.length === 0 && forbiddenTokens.length === 0) {
    console.log('✅ PASS: All CSS variables follow the unified naming convention\n');

    // Show sample of valid tokens
    console.log('Sample of valid tokens found:');
    const sampleTokens = Array.from(validTokens).slice(0, 10);
    sampleTokens.forEach(token => {
      console.log(`  ✅ ${token}`);
    });
    if (validTokens.size > 10) {
      console.log(`  ... and ${validTokens.size - 10} more\n`);
    }

    return true;
  }

  let hasErrors = false;

  if (forbiddenTokens.length > 0) {
    hasErrors = true;
    console.log(`❌ CRITICAL: Found ${forbiddenTokens.length} forbidden tokens (Raycast variables):\n`);
    forbiddenTokens.slice(0, 10).forEach(v => {
      console.log(`  File: ${v.file}:${v.line}`);
      console.log(`  Token: ${v.token}`);
      console.log(`  Context: ${v.context}`);
      console.log(`  Issue: ${v.issue}\n`);
    });
    if (forbiddenTokens.length > 10) {
      console.log(`  ... and ${forbiddenTokens.length - 10} more\n`);
    }
  }

  if (violations.length > 0) {
    hasErrors = true;
    console.log(`❌ ERRORS: Found ${violations.length} naming convention violations:\n`);
    violations.slice(0, 10).forEach(v => {
      console.log(`  File: ${v.file}:${v.line}`);
      console.log(`  Token: ${v.token}`);
      console.log(`  Context: ${v.context}`);
      console.log(`  Issue: ${v.issue}`);
      console.log(`  Suggestion: ${v.suggestion}\n`);
    });
    if (violations.length > 10) {
      console.log(`  ... and ${violations.length - 10} more\n`);
    }
  }

  return !hasErrors;
}

// Run the test
const passed = testTokenNamingConvention();
process.exit(passed ? 0 : 1);
