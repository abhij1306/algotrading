/* eslint-disable */
/**
 * Property 13: Component Library Usage
 * Validates: Requirements 6.6
 *
 * For any page file, UI elements should use components from the component library
 * rather than custom styled divs/buttons.
 */

const fs = require('fs');
const path = require('path');

// Pattern to find custom styled elements that should use component library
const CUSTOM_BUTTON_PATTERN = /<button[^>]*className="[^"]*\b(px-|py-|bg-|rounded)[^"]*"[^>]*>/gi;
const CUSTOM_CARD_PATTERN = /<div[^>]*className="[^"]*\b(p-\d+|rounded-lg|border|shadow)[^"]*"[^>]*>/gi;

// UI components that should be imported
const UI_COMPONENTS = ['Button', 'Card', 'Input', 'Badge'];

function getAllPageFiles(dir, fileList = []) {
  const files = fs.readdirSync(dir);

  files.forEach(file => {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);

    if (stat.isDirectory()) {
      if (!file.startsWith('.') && file !== 'node_modules' && file !== 'components') {
        getAllPageFiles(filePath, fileList);
      }
    } else if ((file === 'page.tsx' || file === 'layout.tsx') && !filePath.includes('components')) {
      fileList.push(filePath);
    }
  });

  return fileList;
}

function testComponentLibraryUsage() {
  console.log('🧪 Testing Property 13: Component Library Usage\n');

  const appDir = path.join(__dirname, '../../app');
  const pageFiles = getAllPageFiles(appDir);

  let violations = [];
  let warnings = [];
  let totalChecks = 0;

  pageFiles.forEach(filePath => {
    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Check if page imports UI components
    const hasUIImport = content.includes('from "@/components/ui"') || content.includes("from '@/components/ui'");

    // Check for custom styled buttons
    const buttonMatches = [...content.matchAll(CUSTOM_BUTTON_PATTERN)];
    buttonMatches.forEach(match => {
      const lineNumber = content.substring(0, match.index).split('\n').length;
      const line = content.split('\n')[lineNumber - 1];

      // Check if Button component is imported
      const hasButtonImport = content.includes('Button');

      if (!hasButtonImport) {
        violations.push({
          file: path.relative(appDir, filePath),
          line: lineNumber,
          issue: 'Custom styled button found',
          context: line.trim().substring(0, 100),
          suggestion: 'Import and use Button component from @/components/ui',
          severity: 'error',
        });
      } else {
        warnings.push({
          file: path.relative(appDir, filePath),
          line: lineNumber,
          issue: 'Custom styled button found (Button component is imported)',
          context: line.trim().substring(0, 100),
          suggestion: 'Consider using Button component instead',
          severity: 'warning',
        });
      }
    });

    // Check for custom styled cards
    const cardMatches = [...content.matchAll(CUSTOM_CARD_PATTERN)];
    let cardCount = 0;
    cardMatches.forEach(match => {
      const lineNumber = content.substring(0, match.index).split('\n').length;
      const line = content.split('\n')[lineNumber - 1];

      // Skip if it's clearly not a card (e.g., layout containers)
      if (line.includes('container') || line.includes('wrapper') || line.includes('grid') || line.includes('flex')) {
        return;
      }

      cardCount++;

      // Check if Card component is imported
      const hasCardImport = content.includes('Card');

      if (cardCount > 3 && !hasCardImport) {
        warnings.push({
          file: path.relative(appDir, filePath),
          line: lineNumber,
          issue: 'Multiple custom styled card-like divs found',
          context: line.trim().substring(0, 100),
          suggestion: 'Consider importing and using Card component from @/components/ui',
          severity: 'warning',
        });
      }
    });

    // Check if page has UI elements but no UI imports
    const hasUIElements = content.includes('<button') || content.includes('<input') || buttonMatches.length > 0;
    if (hasUIElements && !hasUIImport) {
      violations.push({
        file: path.relative(appDir, filePath),
        line: 1,
        issue: 'Page has UI elements but does not import from component library',
        suggestion: 'Import UI components from @/components/ui',
        severity: 'error',
      });
    }
  });

  console.log(`✅ Checked ${totalChecks} page files\n`);

  if (violations.length === 0 && warnings.length === 0) {
    console.log('✅ PASS: All pages use component library\n');
    return true;
  }

  let hasErrors = false;

  if (violations.length > 0) {
    hasErrors = true;
    console.log(`❌ ERRORS: Found ${violations.length} component library violations:\n`);
    violations.forEach(v => {
      console.log(`  File: ${v.file}:${v.line}`);
      console.log(`  Issue: ${v.issue}`);
      console.log(`  Suggestion: ${v.suggestion}\n`);
    });
  }

  if (warnings.length > 0) {
    console.log(`⚠️  WARNINGS: Found ${warnings.length} potential improvements:\n`);
    warnings.slice(0, 5).forEach(w => {
      console.log(`  File: ${w.file}:${w.line}`);
      console.log(`  Issue: ${w.issue}`);
      console.log(`  Context: ${w.context}...`);
      console.log(`  Suggestion: ${w.suggestion}\n`);
    });
    if (warnings.length > 5) {
      console.log(`  ... and ${warnings.length - 5} more warnings\n`);
    }
  }

  return !hasErrors;
}

// Run the test
const passed = testComponentLibraryUsage();
process.exit(passed ? 0 : 1);
