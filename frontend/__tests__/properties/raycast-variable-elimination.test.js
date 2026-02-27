/**
 * Property 3: Raycast Variable Elimination
 * Validates: Requirements 2.7, 6.7, 9.7
 *
 * For any file in the codebase, it should contain zero references to CSS variables
 * matching the pattern --raycast-*.
 */

const fs = require('node:fs');
const path = require('node:path');

// Pattern to find Raycast CSS variables
const RAYCAST_VAR_PATTERN = /var\(--raycast-[a-z-]+\)/g;
const RAYCAST_CLASS_PATTERN = /--raycast-[a-z-]+/g;

function getAllFiles(dir, fileList = []) {
  const files = fs.readdirSync(dir);

  files.forEach(file => {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);

    if (stat.isDirectory()) {
      // Skip node_modules, .next, and other build directories
      if (!file.startsWith('.') && file !== 'node_modules' && file !== 'dist' && file !== 'build') {
        getAllFiles(filePath, fileList);
      }
    } else if (file.endsWith('.tsx') || file.endsWith('.ts') || file.endsWith('.css') || file.endsWith('.jsx') || file.endsWith('.js')) {
      // Skip legacy Raycast files and migration map
      if (!filePath.includes('globals-raycast.css') &&
          !filePath.includes('migration-map.ts') &&
          !filePath.includes('RAYCAST_COMPONENT_AUDIT.md')) {
        fileList.push(filePath);
      }
    }
  });

  return fileList;
}

function testRaycastVariableElimination() {
  console.log('🧪 Testing Property 3: Raycast Variable Elimination\n');

  const frontendDir = path.join(__dirname, '../..');
  const allFiles = getAllFiles(frontendDir);

  let violations = [];
  let totalChecks = 0;

  allFiles.forEach(filePath => {
    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Check for var(--raycast-*) references
    const varMatches = [...content.matchAll(RAYCAST_VAR_PATTERN)];
    varMatches.forEach(match => {
      const lineNumber = content.substring(0, match.index).split('\n').length;
      violations.push({
        file: path.relative(frontendDir, filePath),
        line: lineNumber,
        issue: `Raycast CSS variable found: ${match[0]}`,
        suggestion: 'Replace with unified design token from migration-map.ts',
        severity: 'error',
      });
    });

    // Check for --raycast- class references (in CSS files)
    if (filePath.endsWith('.css')) {
      const classMatches = [...content.matchAll(RAYCAST_CLASS_PATTERN)];
      classMatches.forEach(match => {
        const lineNumber = content.substring(0, match.index).split('\n').length;
        violations.push({
          file: path.relative(frontendDir, filePath),
          line: lineNumber,
          issue: `Raycast CSS variable found: ${match[0]}`,
          suggestion: 'Replace with unified design token',
          severity: 'error',
        });
      });
    }
  });

  console.log(`✅ Checked ${totalChecks} files\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: No Raycast CSS variables found in codebase\n');
    return true;
  }

  console.log(`❌ FAIL: Found ${violations.length} Raycast CSS variable references:\n`);
  violations.forEach(v => {
    console.log(`  File: ${v.file}:${v.line}`);
    console.log(`  Issue: ${v.issue}`);
    console.log(`  Suggestion: ${v.suggestion}\n`);
  });

  return false;
}

// Run the test
const passed = testRaycastVariableElimination();
process.exit(passed ? 0 : 1);
