#!/usr/bin/env node

/**
 * Typography System Verification Script
 * Validates that all typography tokens are properly defined in globals.css
 */

const fs = require('node:fs');
const path = require('node:path');

const globalsPath = path.join(__dirname, '../app/globals.css');
const globalsContent = fs.readFileSync(globalsPath, 'utf-8');

// Required typography tokens
const requiredTokens = {
  fontFamilies: ['--font-sans', '--font-mono'],
  fontSizes: ['--text-xxs', '--text-xs', '--text-sm', '--text-base', '--text-lg', '--text-xl', '--text-2xl'],
  lineHeights: ['--leading-tight', '--leading-normal', '--leading-relaxed'],
  fontWeights: ['--font-normal', '--font-medium', '--font-semibold', '--font-bold'],
};

let allPassed = true;

console.log('🔍 Verifying Typography System...\n');

// Check font families
console.log('📝 Font Families:');
requiredTokens.fontFamilies.forEach(token => {
  const regex = new RegExp(`${token}:\\s*var\\(${token}\\)|${token}:\\s*["']`, 'g');
  const found = regex.test(globalsContent);
  console.log(`  ${found ? '✅' : '❌'} ${token}`);
  if (!found) allPassed = false;
});

// Check font sizes
console.log('\n📏 Font Sizes:');
requiredTokens.fontSizes.forEach(token => {
  const regex = new RegExp(`${token}:\\s*\\d+px`, 'g');
  const found = regex.test(globalsContent);
  console.log(`  ${found ? '✅' : '❌'} ${token}`);
  if (!found) allPassed = false;
});

// Check line heights
console.log('\n📐 Line Heights:');
requiredTokens.lineHeights.forEach(token => {
  const regex = new RegExp(`${token}:\\s*[\\d.]+`, 'g');
  const found = regex.test(globalsContent);
  console.log(`  ${found ? '✅' : '❌'} ${token}`);
  if (!found) allPassed = false;
});

// Check font weights
console.log('\n⚖️  Font Weights:');
requiredTokens.fontWeights.forEach(token => {
  const regex = new RegExp(`${token}:\\s*\\d+`, 'g');
  const found = regex.test(globalsContent);
  console.log(`  ${found ? '✅' : '❌'} ${token}`);
  if (!found) allPassed = false;
});

// Check font features
console.log('\n✨ Font Features:');
const fontFeatures = [
  { name: 'Tabular Numbers (tnum)', pattern: /tnum/ },
  { name: 'Ligatures (liga)', pattern: /liga/ },
  { name: 'Font Smoothing', pattern: /-webkit-font-smoothing:\s*antialiased/ },
  { name: 'Text Rendering', pattern: /text-rendering:\s*optimizeLegibility/ },
];

fontFeatures.forEach(({ name, pattern }) => {
  const found = pattern.test(globalsContent);
  console.log(`  ${found ? '✅' : '❌'} ${name}`);
  if (!found) allPassed = false;
});

// Check mono-num class
console.log('\n💰 Financial Data Typography:');
const monoNumClass = /\.mono-num\s*{[^}]*font-family:\s*var\(--font-mono\)[^}]*font-variant-numeric:\s*tabular-nums/s;
const hasMonoNum = monoNumClass.test(globalsContent);
console.log(`  ${hasMonoNum ? '✅' : '❌'} .mono-num class with tabular numbers`);
if (!hasMonoNum) allPassed = false;

// Summary
console.log('\n' + '='.repeat(50));
if (allPassed) {
  console.log('✅ All typography tokens are properly defined!');
  process.exit(0);
} else {
  console.log('❌ Some typography tokens are missing or incorrect.');
  process.exit(1);
}
