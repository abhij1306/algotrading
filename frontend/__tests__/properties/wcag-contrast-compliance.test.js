/**
 * Property 8: WCAG Contrast Compliance
 * Validates: Requirements 4.9, 9.6
 *
 * For any text color and background color combination in the design system,
 * the contrast ratio should meet WCAG AA standards (4.5:1 for normal text, 3:1 for large text).
 */

const fs = require('node:fs');
const path = require('node:path');

// WCAG AA contrast requirements
const WCAG_AA_NORMAL_TEXT = 4.5;
const WCAG_AA_LARGE_TEXT = 3.0;

// Text/background combinations to test
const COLOR_COMBINATIONS = [
  // Light mode combinations
  { mode: 'light', text: 'foreground', bg: 'background', size: 'normal' },
  { mode: 'light', text: 'foreground-secondary', bg: 'background', size: 'normal' },
  { mode: 'light', text: 'foreground-tertiary', bg: 'background', size: 'normal' },
  { mode: 'light', text: 'foreground', bg: 'surface', size: 'normal' },
  { mode: 'light', text: 'primary-foreground', bg: 'primary', size: 'normal' },
  { mode: 'light', text: 'profit', bg: 'profit-bg', size: 'normal' },
  { mode: 'light', text: 'loss', bg: 'loss-bg', size: 'normal' },
  { mode: 'light', text: 'warning', bg: 'warning-bg', size: 'normal' },

  // Dark mode combinations
  { mode: 'dark', text: 'foreground', bg: 'background', size: 'normal' },
  { mode: 'dark', text: 'foreground-secondary', bg: 'background', size: 'normal' },
  { mode: 'dark', text: 'foreground-tertiary', bg: 'background', size: 'normal' },
  { mode: 'dark', text: 'foreground', bg: 'surface', size: 'normal' },
  { mode: 'dark', text: 'primary-foreground', bg: 'primary', size: 'normal' },
  { mode: 'dark', text: 'profit', bg: 'profit-bg', size: 'normal' },
  { mode: 'dark', text: 'loss', bg: 'loss-bg', size: 'normal' },
  { mode: 'dark', text: 'warning', bg: 'warning-bg', size: 'normal' },
];

/**
 * Parse hex color to RGB
 */
function hexToRgb(hex) {
  // Remove # if present
  hex = hex.replace('#', '');

  // Handle 3-digit hex
  if (hex.length === 3) {
    hex = hex.split('').map(c => c + c).join('');
  }

  const r = Number.parseInt(hex.substring(0, 2), 16);
  const g = Number.parseInt(hex.substring(2, 4), 16);
  const b = Number.parseInt(hex.substring(4, 6), 16);

  return { r, g, b };
}

/**
 * Parse rgba color to RGB with alpha
 */
function rgbaToRgb(rgba) {
  const match = rgba.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
  if (!match) return null;

  return {
    r: Number.parseInt(match[1]),
    g: Number.parseInt(match[2]),
    b: Number.parseInt(match[3]),
    a: match[4] ? Number.parseFloat(match[4]) : 1.0,
  };
}

/**
 * Composite rgba color over a background color
 */
function compositeRgba(foreground, background) {
  const alpha = foreground.a !== undefined ? foreground.a : 1.0;

  return {
    r: Math.round(foreground.r * alpha + background.r * (1 - alpha)),
    g: Math.round(foreground.g * alpha + background.g * (1 - alpha)),
    b: Math.round(foreground.b * alpha + background.b * (1 - alpha)),
  };
}

/**
 * Calculate relative luminance
 */
function getLuminance(rgb) {
  const { r, g, b } = rgb;

  // Convert to 0-1 range
  const rsRGB = r / 255;
  const gsRGB = g / 255;
  const bsRGB = b / 255;

  // Apply gamma correction
  const rLinear = rsRGB <= 0.03928 ? rsRGB / 12.92 : Math.pow((rsRGB + 0.055) / 1.055, 2.4);
  const gLinear = gsRGB <= 0.03928 ? gsRGB / 12.92 : Math.pow((gsRGB + 0.055) / 1.055, 2.4);
  const bLinear = bsRGB <= 0.03928 ? bsRGB / 12.92 : Math.pow((bsRGB + 0.055) / 1.055, 2.4);

  // Calculate luminance
  return 0.2126 * rLinear + 0.7152 * gLinear + 0.0722 * bLinear;
}

/**
 * Calculate contrast ratio between two colors
 */
function getContrastRatio(color1, color2) {
  const lum1 = getLuminance(color1);
  const lum2 = getLuminance(color2);

  const lighter = Math.max(lum1, lum2);
  const darker = Math.min(lum1, lum2);

  return (lighter + 0.05) / (darker + 0.05);
}

/**
 * Parse color value from CSS
 */
function parseColor(value) {
  value = value.trim();

  if (value.startsWith('#')) {
    return hexToRgb(value);
  }

  if (value.startsWith('rgb')) {
    return rgbaToRgb(value);
  }

  return null;
}

function testWCAGContrastCompliance() {
  console.log('🧪 Testing Property 8: WCAG Contrast Compliance\n');

  const globalsPath = path.join(__dirname, '../../app/globals.css');

  if (!fs.existsSync(globalsPath)) {
    console.log('❌ FAIL: globals.css not found\n');
    return false;
  }

  const content = fs.readFileSync(globalsPath, 'utf-8');

  // Extract color tokens from :root (light mode)
  const rootMatch = content.match(/:root\s*\{([^}]+)\}/s);
  if (!rootMatch) {
    console.log('❌ FAIL: Could not find :root block\n');
    return false;
  }

  // Extract color tokens from .dark (dark mode)
  const darkMatch = content.match(/\.dark\s*\{([^}]+)\}/s);
  if (!darkMatch) {
    console.log('❌ FAIL: Could not find .dark block\n');
    return false;
  }

  const lightColors = {};
  const darkColors = {};

  // Parse light mode colors
  const colorPattern = /--(color-[a-z-]+):\s*([^;]+);/g;
  let match;

  while ((match = colorPattern.exec(rootMatch[1])) !== null) {
    const tokenName = match[1].replace('color-', '');
    const value = match[2].trim();
    lightColors[tokenName] = value;
  }

  // Parse dark mode colors
  colorPattern.lastIndex = 0;
  while ((match = colorPattern.exec(darkMatch[1])) !== null) {
    const tokenName = match[1].replace('color-', '');
    const value = match[2].trim();
    darkColors[tokenName] = value;
  }

  console.log(`📊 Found ${Object.keys(lightColors).length} light mode colors\n`);
  console.log(`📊 Found ${Object.keys(darkColors).length} dark mode colors\n`);

  let violations = [];
  let warnings = [];
  let passed = 0;

  console.log('🔍 Testing color combinations:\n');

  COLOR_COMBINATIONS.forEach(combo => {
    const colors = combo.mode === 'light' ? lightColors : darkColors;
    const baseBackground = combo.mode === 'light' ? lightColors['background'] : darkColors['background'];

    const textColor = colors[combo.text];
    const bgColor = colors[combo.bg];

    if (!textColor || !bgColor) {
      warnings.push({
        ...combo,
        issue: `Missing color token: ${!textColor ? combo.text : combo.bg}`,
      });
      return;
    }

    const textRgb = parseColor(textColor);
    let bgRgb = parseColor(bgColor);

    if (!textRgb || !bgRgb) {
      warnings.push({
        ...combo,
        textColor,
        bgColor,
        issue: 'Could not parse color values',
      });
      return;
    }

    // If background has alpha, composite it over the base background
    if (bgRgb.a !== undefined && bgRgb.a < 1.0) {
      const baseBgRgb = parseColor(baseBackground);
      if (baseBgRgb) {
        bgRgb = compositeRgba(bgRgb, baseBgRgb);
      }
    }

    const ratio = getContrastRatio(textRgb, bgRgb);
    const required = combo.size === 'large' ? WCAG_AA_LARGE_TEXT : WCAG_AA_NORMAL_TEXT;
    const passes = ratio >= required;

    const status = passes ? '✅' : '❌';
    console.log(`  ${status} [${combo.mode}] --color-${combo.text} on --color-${combo.bg}`);
    console.log(`     Ratio: ${ratio.toFixed(2)}:1 (required: ${required}:1)`);

    if (passes) {
      passed++;
    } else {
      violations.push({
        ...combo,
        textColor,
        bgColor,
        ratio: ratio.toFixed(2),
        required,
        issue: `Contrast ratio ${ratio.toFixed(2)}:1 is below WCAG AA requirement of ${required}:1`,
      });
    }
  });

  console.log('');

  if (warnings.length > 0) {
    console.log(`⚠️  WARNINGS: ${warnings.length} combinations could not be tested:\n`);
    warnings.forEach(w => {
      console.log(`  [${w.mode}] --color-${w.text} on --color-${w.bg}`);
      console.log(`  Issue: ${w.issue}\n`);
    });
  }

  console.log(`📊 Results: ${passed}/${COLOR_COMBINATIONS.length} combinations passed\n`);

  if (violations.length === 0) {
    console.log('✅ PASS: All tested color combinations meet WCAG AA contrast requirements\n');
    return true;
  }

  console.log(`❌ FAIL: Found ${violations.length} contrast violations:\n`);
  violations.forEach(v => {
    console.log(`  Mode: ${v.mode}`);
    console.log(`  Text: --color-${v.text} (${v.textColor})`);
    console.log(`  Background: --color-${v.bg} (${v.bgColor})`);
    console.log(`  Ratio: ${v.ratio}:1`);
    console.log(`  Required: ${v.required}:1`);
    console.log(`  Issue: ${v.issue}\n`);
  });

  return false;
}

// Run the test
const passed = testWCAGContrastCompliance();
process.exit(passed ? 0 : 1);
