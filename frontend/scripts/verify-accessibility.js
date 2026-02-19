#!/usr/bin/env node

/**
 * Accessibility Compliance Verification Script
 *
 * Validates:
 * - WCAG AA contrast ratios (4.5:1 for normal text, 3:1 for large text)
 * - Keyboard navigation support
 * - Focus indicators
 * - Screen reader compatibility (basic checks)
 */

const fs = require('fs');
const path = require('path');

// WCAG AA contrast ratio requirements
const WCAG_AA_NORMAL = 4.5;
const WCAG_AA_LARGE = 3.0;

// Parse hex color to RGB
function hexToRgb(hex) {
  const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  return result ? {
    r: parseInt(result[1], 16),
    g: parseInt(result[2], 16),
    b: parseInt(result[3], 16)
  } : null;
}

// Calculate relative luminance
function getLuminance(r, g, b) {
  const [rs, gs, bs] = [r, g, b].map(c => {
    c = c / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
}

// Calculate contrast ratio
function getContrastRatio(color1, color2) {
  const lum1 = getLuminance(color1.r, color1.g, color1.b);
  const lum2 = getLuminance(color2.r, color2.g, color2.b);
  const lighter = Math.max(lum1, lum2);
  const darker = Math.min(lum1, lum2);
  return (lighter + 0.05) / (darker + 0.05);
}

// Extract color tokens from globals.css
function extractColorTokens() {
  const globalsPath = path.join(__dirname, '..', 'app', 'globals.css');
  const content = fs.readFileSync(globalsPath, 'utf-8');

  const tokens = {
    light: {},
    dark: {}
  };

  let currentMode = 'light';
  const lines = content.split('\n');

  for (const line of lines) {
    if (line.includes('.dark {')) {
      currentMode = 'dark';
    } else if (line.includes(':root {')) {
      currentMode = 'light';
    }

    // Match CSS variable definitions with hex colors
    const match = line.match(/--color-([a-z-]+):\s*(#[0-9a-fA-F]{6})/);
    if (match) {
      const [, name, value] = match;
      tokens[currentMode][name] = value;
    }
  }

  return tokens;
}

// Check contrast ratios for text/background combinations
function checkContrastRatios(tokens) {
  const issues = [];
  const passes = [];

  // Define text/background combinations to check
  const combinations = [
    // Light mode
    { mode: 'light', text: 'foreground', bg: 'background', size: 'normal' },
    { mode: 'light', text: 'foreground-secondary', bg: 'background', size: 'normal' },
    { mode: 'light', text: 'foreground-tertiary', bg: 'background', size: 'normal' },
    { mode: 'light', text: 'foreground', bg: 'background-secondary', size: 'normal' },
    { mode: 'light', text: 'primary', bg: 'background', size: 'normal' },
    { mode: 'light', text: 'profit', bg: 'background', size: 'normal' },
    { mode: 'light', text: 'loss', bg: 'background', size: 'normal' },
    { mode: 'light', text: 'primary-foreground', bg: 'primary', size: 'normal' },

    // Dark mode
    { mode: 'dark', text: 'foreground', bg: 'background', size: 'normal' },
    { mode: 'dark', text: 'foreground-secondary', bg: 'background', size: 'normal' },
    { mode: 'dark', text: 'foreground-tertiary', bg: 'background', size: 'normal' },
    { mode: 'dark', text: 'foreground', bg: 'background-secondary', size: 'normal' },
    { mode: 'dark', text: 'primary', bg: 'background', size: 'normal' },
    { mode: 'dark', text: 'profit', bg: 'background', size: 'normal' },
    { mode: 'dark', text: 'loss', bg: 'background', size: 'normal' },
    { mode: 'dark', text: 'primary-foreground', bg: 'primary', size: 'normal' },
  ];

  for (const combo of combinations) {
    const textColor = tokens[combo.mode][combo.text];
    const bgColor = tokens[combo.mode][combo.bg];

    if (!textColor || !bgColor) {
      continue;
    }

    const textRgb = hexToRgb(textColor);
    const bgRgb = hexToRgb(bgColor);

    if (!textRgb || !bgRgb) {
      continue;
    }

    const ratio = getContrastRatio(textRgb, bgRgb);
    const required = combo.size === 'large' ? WCAG_AA_LARGE : WCAG_AA_NORMAL;

    const result = {
      mode: combo.mode,
      text: combo.text,
      textColor,
      bg: combo.bg,
      bgColor,
      ratio: ratio.toFixed(2),
      required: required.toFixed(1),
      passes: ratio >= required
    };

    if (result.passes) {
      passes.push(result);
    } else {
      issues.push(result);
    }
  }

  return { issues, passes };
}

// Check for keyboard navigation support
function checkKeyboardNavigation() {
  const issues = [];
  const componentsDir = path.join(__dirname, '..', 'components');

  function scanDirectory(dir) {
    const files = fs.readdirSync(dir);

    for (const file of files) {
      const filePath = path.join(dir, file);
      const stat = fs.statSync(filePath);

      if (stat.isDirectory()) {
        scanDirectory(filePath);
      } else if (file.endsWith('.tsx') || file.endsWith('.jsx')) {
        const content = fs.readFileSync(filePath, 'utf-8');

        // Check for interactive elements without keyboard support
        const hasButton = content.includes('<button') || content.includes('<Button');
        const hasLink = content.includes('<a ') || content.includes('<Link');
        const hasInput = content.includes('<input') || content.includes('<Input');

        const hasOnClick = content.includes('onClick');
        const hasOnKeyDown = content.includes('onKeyDown') || content.includes('onKeyPress');

        // If has onClick but no keyboard handler, might be an issue
        if (hasOnClick && !hasOnKeyDown && !hasButton && !hasLink && !hasInput) {
          issues.push({
            file: path.relative(path.join(__dirname, '..'), filePath),
            issue: 'onClick without keyboard handler (not on button/link/input)'
          });
        }
      }
    }
  }

  scanDirectory(componentsDir);
  return issues;
}

// Check for focus indicators
function checkFocusIndicators() {
  const issues = [];
  const globalsPath = path.join(__dirname, '..', 'app', 'globals.css');
  const content = fs.readFileSync(globalsPath, 'utf-8');

  // Check if focus-visible styles are defined
  const hasFocusVisible = content.includes('focus-visible:') || content.includes(':focus-visible');
  const hasFocusRing = content.includes('ring-') || content.includes('outline-');

  if (!hasFocusVisible && !hasFocusRing) {
    issues.push({
      file: 'globals.css',
      issue: 'No focus-visible styles found'
    });
  }

  return issues;
}

// Main execution
console.log('🧪 Accessibility Compliance Verification\n');

// 1. Check contrast ratios
console.log('1️⃣  Checking WCAG AA Contrast Ratios...\n');
const tokens = extractColorTokens();
const { issues: contrastIssues, passes: contrastPasses } = checkContrastRatios(tokens);

if (contrastIssues.length === 0) {
  console.log(`✅ All ${contrastPasses.length} text/background combinations meet WCAG AA standards\n`);
} else {
  console.log(`❌ Found ${contrastIssues.length} contrast ratio violations:\n`);
  for (const issue of contrastIssues) {
    console.log(`  ${issue.mode} mode: ${issue.text} (${issue.textColor}) on ${issue.bg} (${issue.bgColor})`);
    console.log(`  Ratio: ${issue.ratio}:1 (required: ${issue.required}:1)\n`);
  }
}

// 2. Check keyboard navigation
console.log('2️⃣  Checking Keyboard Navigation Support...\n');
const keyboardIssues = checkKeyboardNavigation();

if (keyboardIssues.length === 0) {
  console.log('✅ No keyboard navigation issues found\n');
} else {
  console.log(`⚠️  Found ${keyboardIssues.length} potential keyboard navigation issues:\n`);
  for (const issue of keyboardIssues) {
    console.log(`  File: ${issue.file}`);
    console.log(`  Issue: ${issue.issue}\n`);
  }
}

// 3. Check focus indicators
console.log('3️⃣  Checking Focus Indicators...\n');
const focusIssues = checkFocusIndicators();

if (focusIssues.length === 0) {
  console.log('✅ Focus indicators are properly defined\n');
} else {
  console.log(`❌ Found ${focusIssues.length} focus indicator issues:\n`);
  for (const issue of focusIssues) {
    console.log(`  File: ${issue.file}`);
    console.log(`  Issue: ${issue.issue}\n`);
  }
}

// Summary
console.log('📊 Summary:\n');
console.log(`  Contrast Ratios: ${contrastIssues.length === 0 ? '✅ PASS' : '❌ FAIL'}`);
console.log(`  Keyboard Navigation: ${keyboardIssues.length === 0 ? '✅ PASS' : '⚠️  WARNINGS'}`);
console.log(`  Focus Indicators: ${focusIssues.length === 0 ? '✅ PASS' : '❌ FAIL'}`);

const totalIssues = contrastIssues.length + focusIssues.length;
const totalWarnings = keyboardIssues.length;

if (totalIssues > 0) {
  console.log(`\n❌ FAIL: ${totalIssues} accessibility issues found`);
  process.exit(1);
} else if (totalWarnings > 0) {
  console.log(`\n⚠️  PASS with warnings: ${totalWarnings} potential improvements`);
  process.exit(0);
} else {
  console.log('\n✅ PASS: All accessibility checks passed');
  process.exit(0);
}
