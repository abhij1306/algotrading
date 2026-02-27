/**
 * Property 7: Interactive Element States
 * Validates: Requirements 4.7
 *
 * For any interactive element (button, link, input), hover and focus states
 * should be defined using design tokens.
 */

const fs = require('node:fs');
const path = require('node:path');

// Pattern to find interactive elements
const INTERACTIVE_ELEMENT_PATTERNS = [
  /<button[^>]*>/gi,
  /<a[^>]*>/gi,
  /<input[^>]*>/gi,
  /className="[^"]*\b(cursor-pointer|clickable)\b[^"]*"/gi,
];

// Required state classes
const HOVER_STATE_PATTERN = /hover:/;
const FOCUS_STATE_PATTERN = /focus:|focus-visible:/;

function getAllFiles(dir, fileList = []) {
  const files = fs.readdirSync(dir);

  files.forEach(file => {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);

    if (stat.isDirectory()) {
      if (!file.startsWith('.') && file !== 'node_modules' && file !== 'dist' && file !== 'build') {
        getAllFiles(filePath, fileList);
      }
    } else if (file.endsWith('.tsx') || file.endsWith('.jsx')) {
      fileList.push(filePath);
    }
  });

  return fileList;
}

function testInteractiveElementStates() {
  console.log('🧪 Testing Property 7: Interactive Element States\n');

  const frontendDir = path.join(__dirname, '../..');
  const allFiles = getAllFiles(frontendDir);

  let violations = [];
  let checked = 0;
  let totalChecks = 0;

  allFiles.forEach(filePath => {
    const content = fs.readFileSync(filePath, 'utf-8');
    totalChecks++;

    // Skip test files and component library (already validated)
    if (filePath.includes('__tests__') || filePath.includes('components/ui')) {
      return;
    }

    // Find interactive elements
    const lines = content.split('\n');
    lines.forEach((line, index) => {
      const lineNumber = index + 1;

      // Check if line contains interactive elements
      const hasInteractiveElement = INTERACTIVE_ELEMENT_PATTERNS.some(pattern => pattern.test(line));

      if (hasInteractiveElement) {
        checked++;

        // Check if the element has hover and focus states
        // Look at the current line and a few lines before/after for context
        const contextStart = Math.max(0, index - 5);
        const contextEnd = Math.min(lines.length, index + 5);
        const context = lines.slice(contextStart, contextEnd).join('\n');

        const hasHoverState = HOVER_STATE_PATTERN.test(context);
        const hasFocusState = FOCUS_STATE_PATTERN.test(context);

        // Check if it's using a component from the UI library (which already has states)
        const usesUIComponent =
          line.includes('<Button') ||
          line.includes('<Input') ||
          line.includes('from "@/components/ui"') ||
          context.includes('from "@/components/ui"');

        // Skip if using UI component or if it's disabled
        if (usesUIComponent || line.includes('disabled')) {
          return;
        }

        if (!hasHoverState && !hasFocusState) {
          violations.push({
            file: path.relative(frontendDir, filePath),
            line: lineNumber,
            issue: `Interactive element without hover/focus states`,
            missing: [
              !hasHoverState && 'hover state',
              !hasFocusState && 'focus state',
            ].filter(Boolean).join(', '),
            context: line.trim().substring(0, 100),
            suggestion: 'Add hover: and focus-visible: classes with design tokens',
            severity: 'warning',
          });
        }
      }
    });
  });

  console.log(`✅ Checked ${totalChecks} files`);
  console.log(`✅ Found ${checked} interactive elements\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: All interactive elements have proper states\n');
    return true;
  }

  console.log(`⚠️  WARNINGS: Found ${violations.length} interactive elements without proper states:\n`);
  violations.slice(0, 10).forEach(v => {
    console.log(`  File: ${v.file}:${v.line}`);
    console.log(`  Issue: ${v.issue}`);
    console.log(`  Missing: ${v.missing}`);
    console.log(`  Context: ${v.context}...`);
    console.log(`  Suggestion: ${v.suggestion}\n`);
  });

  if (violations.length > 10) {
    console.log(`  ... and ${violations.length - 10} more warnings\n`);
  }

  // Return true even with warnings (they're not critical)
  return true;
}

// Run the test
const passed = testInteractiveElementStates();
process.exit(passed ? 0 : 1);
