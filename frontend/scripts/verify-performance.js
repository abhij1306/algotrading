#!/usr/bin/env node

/**
 * Performance Validation Script
 *
 * Validates:
 * - CSS bundle size
 * - Font loading configuration
 * - Theme switch performance (checks for CSS variable usage)
 */

const fs = require('node:fs');
const path = require('node:path');

// Performance targets
const MAX_CSS_SIZE_KB = 50;
const MAX_FONT_FILES = 10;

console.log('🧪 Performance Validation\n');

// 1. Check CSS bundle size
console.log('1️⃣  Checking CSS Bundle Size...\n');

const globalsPath = path.join(__dirname, '..', 'app', 'globals.css');
const globalsStats = fs.statSync(globalsPath);
const globalsSizeKB = (globalsStats.size / 1024).toFixed(2);

console.log(`  globals.css: ${globalsSizeKB} KB`);

if (globalsSizeKB > MAX_CSS_SIZE_KB) {
  console.log(`  ❌ FAIL: CSS bundle exceeds ${MAX_CSS_SIZE_KB} KB target\n`);
} else {
  console.log(`  ✅ PASS: CSS bundle is within ${MAX_CSS_SIZE_KB} KB target\n`);
}

// 2. Check font loading configuration
console.log('2️⃣  Checking Font Loading Configuration...\n');

const layoutPath = path.join(__dirname, '..', 'app', 'layout.tsx');
const layoutContent = fs.readFileSync(layoutPath, 'utf-8');

const hasFontImport = layoutContent.includes('next/font') || layoutContent.includes('@next/font');
const hasSansFont = layoutContent.includes('IBM_Plex_Sans') || layoutContent.includes('IBM Plex Sans');
const hasMonoFont = layoutContent.includes('DM_Mono') || layoutContent.includes('DM Mono');
const hasFontDisplay = layoutContent.includes('display:') || layoutContent.includes('swap');

const fontIssues = [];
const fontPasses = [];

if (hasFontImport) {
  fontPasses.push('Font imports configured');
} else {
  fontIssues.push('No font imports found');
}

if (hasSansFont) {
  fontPasses.push('IBM Plex Sans configured');
} else {
  fontIssues.push('IBM Plex Sans not configured');
}

if (hasMonoFont) {
  fontPasses.push('Monospace font configured');
} else {
  fontIssues.push('Monospace font not configured');
}

if (hasFontDisplay) {
  fontPasses.push('Font display strategy configured');
} else {
  fontIssues.push('Font display strategy not configured');
}

if (fontIssues.length === 0) {
  console.log('  ✅ PASS: Font loading properly configured');
  fontPasses.forEach(pass => console.log(`    ✓ ${pass}`));
  console.log();
} else {
  console.log('  ⚠️  WARNINGS: Font loading issues found');
  fontIssues.forEach(issue => console.log(`    ! ${issue}`));
  console.log();
}

// 3. Check theme switch performance (CSS variables)
console.log('3️⃣  Checking Theme Switch Performance...\n');

const globalsContent = fs.readFileSync(globalsPath, 'utf-8');

// Count CSS variables
const cssVarMatches = globalsContent.match(/--[a-z-]+:/g);
const cssVarCount = cssVarMatches ? cssVarMatches.length : 0;

// Check for .dark class
const hasDarkClass = globalsContent.includes('.dark {');

// Check for theme toggle implementation
const hasThemeProvider = layoutContent.includes('ThemeProvider') ||
                         layoutContent.includes('theme') ||
                         layoutContent.includes('dark');

console.log(`  CSS Variables: ${cssVarCount}`);
console.log(`  Dark Mode Class: ${hasDarkClass ? '✓' : '✗'}`);
console.log(`  Theme Provider: ${hasThemeProvider ? '✓' : '✗'}`);

const themeIssues = [];

if (!hasDarkClass) {
  themeIssues.push('No .dark class found for theme switching');
}

if (cssVarCount === 0) {
  themeIssues.push('No CSS variables found - theme switching may not work');
}

if (themeIssues.length === 0) {
  console.log('\n  ✅ PASS: Theme switching configured for optimal performance');
  console.log('    ✓ Uses CSS variables for instant theme switching');
  console.log('    ✓ No JavaScript-based color calculations needed\n');
} else {
  console.log('\n  ⚠️  WARNINGS: Theme switching issues found');
  themeIssues.forEach(issue => console.log(`    ! ${issue}`));
  console.log();
}

// 4. Check for performance anti-patterns
console.log('4️⃣  Checking for Performance Anti-Patterns...\n');

const antiPatterns = [];

// Check for inline styles in components
const componentsDir = path.join(__dirname, '..', 'components');
let inlineStyleCount = 0;

function scanForInlineStyles(dir) {
  const files = fs.readdirSync(dir);

  for (const file of files) {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);

    if (stat.isDirectory()) {
      scanForInlineStyles(filePath);
    } else if (file.endsWith('.tsx') || file.endsWith('.jsx')) {
      const content = fs.readFileSync(filePath, 'utf-8');
      const matches = content.match(/style=\{\{/g);
      if (matches) {
        inlineStyleCount += matches.length;
      }
    }
  }
}

scanForInlineStyles(componentsDir);

if (inlineStyleCount > 0) {
  antiPatterns.push(`Found ${inlineStyleCount} inline style objects (prefer Tailwind classes)`);
}

// Check for hardcoded colors in components
let hardcodedColorCount = 0;

function scanForHardcodedColors(dir) {
  const files = fs.readdirSync(dir);

  for (const file of files) {
    const filePath = path.join(dir, file);
    const stat = fs.statSync(filePath);

    if (stat.isDirectory()) {
      scanForHardcodedColors(filePath);
    } else if (file.endsWith('.tsx') || file.endsWith('.jsx')) {
      const content = fs.readFileSync(filePath, 'utf-8');
      // Look for hex colors in className or style attributes
      const matches = content.match(/#[0-9a-fA-F]{6}/g);
      if (matches) {
        hardcodedColorCount += matches.length;
      }
    }
  }
}

scanForHardcodedColors(componentsDir);

if (hardcodedColorCount > 10) { // Allow some for charts/visualizations
  antiPatterns.push(`Found ${hardcodedColorCount} hardcoded colors (prefer design tokens)`);
}

if (antiPatterns.length === 0) {
  console.log('  ✅ PASS: No performance anti-patterns detected\n');
} else {
  console.log('  ⚠️  WARNINGS: Performance anti-patterns detected');
  antiPatterns.forEach(pattern => console.log(`    ! ${pattern}`));
  console.log();
}

// Summary
console.log('📊 Summary:\n');

const cssSizePass = globalsSizeKB <= MAX_CSS_SIZE_KB;
const fontPass = fontIssues.length === 0;
const themePass = themeIssues.length === 0;
const antiPatternPass = antiPatterns.length === 0;

console.log(`  CSS Bundle Size: ${cssSizePass ? '✅ PASS' : '❌ FAIL'} (${globalsSizeKB} KB / ${MAX_CSS_SIZE_KB} KB)`);
console.log(`  Font Loading: ${fontPass ? '✅ PASS' : '⚠️  WARNINGS'} (${fontPasses.length} checks passed)`);
console.log(`  Theme Switching: ${themePass ? '✅ PASS' : '⚠️  WARNINGS'} (${cssVarCount} CSS variables)`);
console.log(`  Anti-Patterns: ${antiPatternPass ? '✅ PASS' : '⚠️  WARNINGS'}`);

const totalIssues = (cssSizePass ? 0 : 1);
const totalWarnings = fontIssues.length + themeIssues.length + antiPatterns.length;

if (totalIssues > 0) {
  console.log(`\n❌ FAIL: ${totalIssues} critical performance issues found`);
  process.exit(1);
} else if (totalWarnings > 0) {
  console.log(`\n⚠️  PASS with warnings: ${totalWarnings} potential improvements`);
  process.exit(0);
} else {
  console.log('\n✅ PASS: All performance checks passed');
  process.exit(0);
}
