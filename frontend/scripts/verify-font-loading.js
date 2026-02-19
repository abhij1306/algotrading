#!/usr/bin/env node

/**
 * Font Loading Verification Script
 * Validates that fonts are properly configured in layout.tsx
 */

const fs = require('fs');
const path = require('path');

const layoutPath = path.join(__dirname, '../app/layout.tsx');
const layoutContent = fs.readFileSync(layoutPath, 'utf-8');

console.log('🔍 Verifying Font Loading Configuration...\n');

let allPassed = true;

// Check Inter font import
const hasInterImport = /import.*Inter.*from.*next\/font\/google/.test(layoutContent);
console.log(`${hasInterImport ? '✅' : '❌'} Inter font imported from next/font/google`);
if (!hasInterImport) allPassed = false;

// Check JetBrains Mono font import
const hasJetBrainsImport = /import.*JetBrains_Mono.*from.*next\/font\/google/.test(layoutContent);
console.log(`${hasJetBrainsImport ? '✅' : '❌'} JetBrains Mono font imported from next/font/google`);
if (!hasJetBrainsImport) allPassed = false;

// Check Inter configuration
const hasInterConfig = /variable:\s*["']--font-sans["']/.test(layoutContent);
console.log(`${hasInterConfig ? '✅' : '❌'} Inter configured with --font-sans variable`);
if (!hasInterConfig) allPassed = false;

// Check JetBrains Mono configuration
const hasJetBrainsConfig = /variable:\s*["']--font-mono["']/.test(layoutContent);
console.log(`${hasJetBrainsConfig ? '✅' : '❌'} JetBrains Mono configured with --font-mono variable`);
if (!hasJetBrainsConfig) allPassed = false;

// Check display: swap
const hasDisplaySwap = /display:\s*["']swap["']/.test(layoutContent);
console.log(`${hasDisplaySwap ? '✅' : '❌'} Font display set to 'swap' for optimal loading`);
if (!hasDisplaySwap) allPassed = false;

// Check globals.css import
const hasGlobalsCss = /import.*["']\.\/globals\.css["']/.test(layoutContent);
console.log(`${hasGlobalsCss ? '✅' : '❌'} globals.css imported (not globals-raycast.css)`);
if (!hasGlobalsCss) allPassed = false;

console.log('\n' + '='.repeat(50));
if (allPassed) {
  console.log('✅ Font loading is properly configured!');
  process.exit(0);
} else {
  console.log('❌ Font loading configuration has issues.');
  process.exit(1);
}
