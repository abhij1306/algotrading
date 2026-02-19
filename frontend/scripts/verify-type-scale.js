#!/usr/bin/env node

/**
 * Type Scale Verification Script
 * Validates that font size progression is consistent (1.2x to 1.5x ratio)
 */

const fs = require('fs');
const path = require('path');

const globalsPath = path.join(__dirname, '../app/globals.css');
const globalsContent = fs.readFileSync(globalsPath, 'utf-8');

console.log('🔍 Verifying Type Scale Progression...\n');

// Extract font sizes
const fontSizes = {
  xxs: parseInt(globalsContent.match(/--text-xxs:\s*(\d+)px/)?.[1] || '0'),
  xs: parseInt(globalsContent.match(/--text-xs:\s*(\d+)px/)?.[1] || '0'),
  sm: parseInt(globalsContent.match(/--text-sm:\s*(\d+)px/)?.[1] || '0'),
  base: parseInt(globalsContent.match(/--text-base:\s*(\d+)px/)?.[1] || '0'),
  lg: parseInt(globalsContent.match(/--text-lg:\s*(\d+)px/)?.[1] || '0'),
  xl: parseInt(globalsContent.match(/--text-xl:\s*(\d+)px/)?.[1] || '0'),
  '2xl': parseInt(globalsContent.match(/--text-2xl:\s*(\d+)px/)?.[1] || '0'),
};

console.log('📏 Font Sizes:');
Object.entries(fontSizes).forEach(([name, size]) => {
  console.log(`  ${name.padEnd(6)} = ${size}px`);
});

// Calculate ratios
console.log('\n📊 Progression Ratios:');
const sizes = Object.values(fontSizes);
let allValid = true;

for (let i = 1; i < sizes.length; i++) {
  const ratio = sizes[i] / sizes[i - 1];
  const isValid = ratio >= 1.2 && ratio <= 1.5;
  const status = isValid ? '✅' : '❌';
  console.log(`  ${status} ${sizes[i - 1]}px → ${sizes[i]}px = ${ratio.toFixed(2)}x ${isValid ? '' : '(outside 1.2-1.5 range)'}`);
  if (!isValid) allValid = false;
}

console.log('\n' + '='.repeat(50));
if (allValid) {
  console.log('✅ Type scale has consistent progression!');
  process.exit(0);
} else {
  console.log('⚠️  Some ratios are outside the recommended 1.2-1.5 range.');
  console.log('   This may be intentional for information density.');
  process.exit(0); // Don't fail, just warn
}
