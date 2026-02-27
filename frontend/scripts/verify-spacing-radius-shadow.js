#!/usr/bin/env node

/**
 * Verification Script: Spacing, Radius, and Shadow Tokens
 *
 * Validates Requirements 2.3, 2.4, 2.5:
 * - Spacing scale uses 4px multiples
 * - Border radius tokens are defined
 * - Shadow tokens are defined for light and dark modes
 */

const fs = require('node:fs');
const path = require('node:path');

const GLOBALS_CSS_PATH = path.join(__dirname, '..', 'app', 'globals.css');

function parseGlobalsCSS() {
  const content = fs.readFileSync(GLOBALS_CSS_PATH, 'utf-8');

  // Extract :root tokens
  const rootMatch = content.match(/:root\s*{([^}]+)}/s);
  const rootTokens = rootMatch ? rootMatch[1] : '';

  // Extract .dark tokens
  const darkMatch = content.match(/\.dark\s*{([^}]+)}/s);
  const darkTokens = darkMatch ? darkMatch[1] : '';

  return { rootTokens, darkTokens };
}

function extractTokens(content, pattern) {
  const tokens = {};
  const regex = new RegExp(`--${pattern}-([^:]+):\\s*([^;]+);`, 'g');
  let match;

  while ((match = regex.exec(content)) !== null) {
    tokens[match[1]] = match[2].trim();
  }

  return tokens;
}

function validateSpacingScale(tokens) {
  console.log('\n📏 Validating Spacing Scale (4px multiples)...');

  const errors = [];
  const expectedTokens = ['1', '2', '3', '4', '5', '6', '8', '10', '12'];

  // Check all expected tokens exist
  for (const token of expectedTokens) {
    if (!tokens[token]) {
      errors.push(`Missing spacing token: --spacing-${token}`);
    }
  }

  // Validate each token is a multiple of 4px
  for (const [name, value] of Object.entries(tokens)) {
    const pxMatch = value.match(/^(\d+)px$/);
    if (!pxMatch) {
      errors.push(`Invalid spacing value for --spacing-${name}: ${value} (must be in px)`);
      continue;
    }

    const pixels = Number.parseInt(pxMatch[1], 10);
    if (pixels % 4 !== 0) {
      errors.push(`Spacing --spacing-${name} is ${pixels}px, not a multiple of 4`);
    } else {
      console.log(`  ✓ --spacing-${name}: ${value} (${pixels / 4} × 4px)`);
    }
  }

  return errors;
}

function validateRadiusTokens(tokens) {
  console.log('\n🔘 Validating Border Radius Tokens...');

  const errors = [];
  const expectedTokens = {
    'sm': '4px',
    'md': '6px',
    'lg': '8px',
    'xl': '12px',
    'full': '9999px'
  };

  for (const [name, expectedValue] of Object.entries(expectedTokens)) {
    if (!tokens[name]) {
      errors.push(`Missing radius token: --radius-${name}`);
    } else if (tokens[name] !== expectedValue) {
      errors.push(`Radius --radius-${name} is ${tokens[name]}, expected ${expectedValue}`);
    } else {
      console.log(`  ✓ --radius-${name}: ${tokens[name]}`);
    }
  }

  return errors;
}

function validateShadowTokens(lightTokens, darkTokens) {
  console.log('\n🌓 Validating Shadow Tokens (Light & Dark Modes)...');

  const errors = [];
  const expectedTokens = ['sm', 'md', 'lg', 'xl'];

  // Check light mode shadows
  console.log('\n  Light Mode:');
  for (const token of expectedTokens) {
    if (!lightTokens[token]) {
      errors.push(`Missing light mode shadow token: --shadow-${token}`);
    } else {
      console.log(`    ✓ --shadow-${token}: ${lightTokens[token]}`);
    }
  }

  // Check dark mode shadows
  console.log('\n  Dark Mode:');
  for (const token of expectedTokens) {
    if (!darkTokens[token]) {
      errors.push(`Missing dark mode shadow token: --shadow-${token}`);
    } else {
      console.log(`    ✓ --shadow-${token}: ${darkTokens[token]}`);
    }
  }

  // Validate shadow format (should contain rgba with opacity)
  for (const [name, value] of Object.entries(lightTokens)) {
    if (!value.includes('rgba')) {
      errors.push(`Shadow --shadow-${name} should use rgba for opacity control`);
    }
  }

  return errors;
}

function validateThemeMapping(content) {
  console.log('\n🎨 Validating @theme Mapping...');

  const errors = [];
  const themeMatch = content.match(/@theme\s*{([^}]+)}/s);

  if (!themeMatch) {
    errors.push('Missing @theme directive for Tailwind integration');
    return errors;
  }

  const themeContent = themeMatch[1];

  // Check spacing tokens are mapped
  const spacingTokens = ['1', '2', '3', '4', '5', '6', '8', '10', '12'];
  for (const token of spacingTokens) {
    if (!themeContent.includes(`--spacing-${token}`)) {
      errors.push(`Spacing token --spacing-${token} not mapped in @theme`);
    }
  }

  // Check radius tokens are mapped
  const radiusTokens = ['sm', 'md', 'lg', 'xl', 'full'];
  for (const token of radiusTokens) {
    if (!themeContent.includes(`--radius-${token}`)) {
      errors.push(`Radius token --radius-${token} not mapped in @theme`);
    }
  }

  // Check shadow tokens are mapped
  const shadowTokens = ['sm', 'md', 'lg', 'xl'];
  for (const token of shadowTokens) {
    if (!themeContent.includes(`--shadow-${token}`)) {
      errors.push(`Shadow token --shadow-${token} not mapped in @theme`);
    }
  }

  if (errors.length === 0) {
    console.log('  ✓ All spacing tokens mapped');
    console.log('  ✓ All radius tokens mapped');
    console.log('  ✓ All shadow tokens mapped');
  }

  return errors;
}

function main() {
  console.log('🔍 Verifying Spacing, Radius, and Shadow Tokens\n');
  console.log('=' .repeat(60));

  const content = fs.readFileSync(GLOBALS_CSS_PATH, 'utf-8');
  const { rootTokens, darkTokens } = parseGlobalsCSS();

  // Extract tokens
  const spacingTokens = extractTokens(rootTokens, 'spacing');
  const radiusTokens = extractTokens(rootTokens, 'radius');
  const lightShadowTokens = extractTokens(rootTokens, 'shadow');
  const darkShadowTokens = extractTokens(darkTokens, 'shadow');

  // Run validations
  const allErrors = [
    ...validateSpacingScale(spacingTokens),
    ...validateRadiusTokens(radiusTokens),
    ...validateShadowTokens(lightShadowTokens, darkShadowTokens),
    ...validateThemeMapping(content)
  ];

  // Report results
  console.log('\n' + '='.repeat(60));

  if (allErrors.length === 0) {
    console.log('\n✅ All validations passed!');
    console.log('\nSummary:');
    console.log(`  • ${Object.keys(spacingTokens).length} spacing tokens (4px multiples)`);
    console.log(`  • ${Object.keys(radiusTokens).length} radius tokens`);
    console.log(`  • ${Object.keys(lightShadowTokens).length} shadow tokens (light mode)`);
    console.log(`  • ${Object.keys(darkShadowTokens).length} shadow tokens (dark mode)`);
    console.log('\n✓ Validates Requirements 2.3, 2.4, 2.5');
    process.exit(0);
  } else {
    console.log('\n❌ Validation failed with errors:\n');
    allErrors.forEach(error => console.log(`  • ${error}`));
    process.exit(1);
  }
}

main();
