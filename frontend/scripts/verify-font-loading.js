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

// Check IBM Plex Sans font import
const hasSansImport = /import.*IBM_Plex_Sans.*from.*next\/font\/google/.test(layoutContent);
console.log(`${hasSansImport ? '✅' : '❌'} IBM Plex Sans imported from next/font/google`);
if (!hasSansImport) allPassed = false;

// Check DM Mono font import
const hasMonoImport = /import.*DM_Mono.*from.*next\/font\/google/.test(layoutContent);
console.log(`${hasMonoImport ? '✅' : '❌'} DM Mono imported from next/font/google`);
if (!hasMonoImport) allPassed = false;

// Check sans configuration
const hasSansConfig = /variable:\s*["']--font-sans["']/.test(layoutContent);
console.log(`${hasSansConfig ? '✅' : '❌'} Sans font configured with --font-sans variable`);
if (!hasSansConfig) allPassed = false;

// Check mono configuration
const hasMonoConfig = /variable:\s*["']--font-mono["']/.test(layoutContent);
console.log(`${hasMonoConfig ? '✅' : '❌'} Mono font configured with --font-mono variable`);
if (!hasMonoConfig) allPassed = false;

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
