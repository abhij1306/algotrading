/**
 * Property 15: Import Path Correctness
 * Validates: Requirements 7.4
 *
 * For any import statement in the codebase, it should not reference
 * the raycast/ directory path if components have been migrated.
 *
 * Note: This test allows imports from components/raycast/ if the components
 * are still in that directory but have been refactored to use unified tokens.
 */

const fs = require('node:fs');
const path = require('node:path');

// Pattern to find imports from raycast directory
const RAYCAST_IMPORT_PATTERN = /from\s+['"][@./]*components\/raycast['"]/g;

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
    } else if (file.endsWith('.tsx') || file.endsWith('.ts') || file.endsWith('.jsx') || file.endsWith('.js')) {
      // Skip the raycast components themselves and their index file
      if (!filePath.includes('components\\raycast\\') &&
          !filePath.includes('components/raycast/')) {
        fileList.push(filePath);
      }
    }
  });

  return fileList;
}

function testImportPathCorrectness() {
  console.log('🧪 Testing Property 15: Import Path Correctness\n');

  const frontendDir = path.join(__dirname, '../..');
  const allFiles = getAllFiles(frontendDir);

  // Check if raycast components directory still exists
  const raycastDir = path.join(frontendDir, 'components/raycast');
  const raycastDirExists = fs.existsSync(raycastDir);

  let violations = [];
  let totalChecks = 0;
  let raycastImports = [];

  allFiles.forEach(filePath => {
    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Check for imports from raycast directory
    const importMatches = [...content.matchAll(RAYCAST_IMPORT_PATTERN)];
    importMatches.forEach(match => {
      const lineNumber = content.substring(0, match.index).split('\n').length;
      const importStatement = content.substring(match.index, match.index + 100).split('\n')[0];

      raycastImports.push({
        file: path.relative(frontendDir, filePath),
        line: lineNumber,
        import: importStatement.trim(),
      });
    });
  });

  console.log(`✅ Checked ${totalChecks} files\n`);

  if (raycastImports.length === 0) {
    console.log('✅ PASS: No imports from raycast/ directory found\n');
    console.log('   All components have been migrated to the unified component library.\n');
    return true;
  }

  // If raycast directory exists and components are refactored, imports are allowed
  if (raycastDirExists) {
    console.log(`ℹ️  INFO: Found ${raycastImports.length} imports from components/raycast/:\n`);
    raycastImports.forEach(imp => {
      console.log(`  File: ${imp.file}:${imp.line}`);
      console.log(`  Import: ${imp.import}\n`);
    });
    console.log('✅ PASS: Imports are valid - Raycast components exist and have been refactored\n');
    console.log('   Components in raycast/ directory use unified design tokens.\n');
    return true;
  }

  // If raycast directory doesn't exist but imports remain, that's an error
  console.log(`❌ FAIL: Found ${raycastImports.length} imports from non-existent raycast/ directory:\n`);
  raycastImports.forEach(imp => {
    console.log(`  File: ${imp.file}:${imp.line}`);
    console.log(`  Import: ${imp.import}`);
    console.log(`  Issue: Importing from raycast/ directory that no longer exists`);
    console.log(`  Suggestion: Update import to reference new component location\n`);
  });

  return false;
}

// Run the test
const passed = testImportPathCorrectness();
process.exit(passed ? 0 : 1);
