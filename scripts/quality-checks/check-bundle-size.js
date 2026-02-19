#!/usr/bin/env node
/**
 * Bundle Size Checker
 *
 * Analyzes Next.js build output to ensure no pages exceed 200KB First Load JS.
 * Fails CI if violations are found.
 *
 * Requirements:
 * - Parse Next.js build output (Requirement 9.3)
 * - Identify pages exceeding 200KB (Requirement 9.3)
 * - Fail CI if violations found (Requirement 9.3)
 */

const fs = require('fs');
const path = require('path');

const MAX_FIRST_LOAD_KB = 200;
const FRONTEND_DIR = path.join(__dirname, '../frontend');
const NEXT_DIR = path.join(FRONTEND_DIR, '.next');
const BUILD_MANIFEST_PATH = path.join(NEXT_DIR, 'build-manifest.json');
const APP_BUILD_MANIFEST_PATH = path.join(NEXT_DIR, 'app-build-manifest.json');

function formatBytes(bytes) {
  return (bytes / 1024).toFixed(2) + ' KB';
}

function getFileSize(filePath) {
  try {
    if (fs.existsSync(filePath)) {
      const stats = fs.statSync(filePath);
      return stats.size;
    }
  } catch (error) {
    console.warn(`⚠️  Warning: Could not read file ${filePath}: ${error.message}`);
  }
  return 0;
}

function analyzeAppRouterBuild() {
  console.log('📊 Analyzing Next.js App Router build...\n');

  // Check for app-build-manifest.json (App Router)
  if (!fs.existsSync(APP_BUILD_MANIFEST_PATH)) {
    console.log('ℹ️  App Router manifest not found, checking Pages Router...\n');
    return null;
  }

  try {
    const appManifest = JSON.parse(fs.readFileSync(APP_BUILD_MANIFEST_PATH, 'utf8'));
    const violations = [];
    const pages = [];

    // Analyze each page in the app directory
    if (appManifest.pages) {
      for (const [pagePath, pageData] of Object.entries(appManifest.pages)) {
        let totalSize = 0;
        const files = [];

        // Collect all chunks for this page
        if (Array.isArray(pageData)) {
          for (const file of pageData) {
            const filePath = path.join(NEXT_DIR, file);
            const size = getFileSize(filePath);
            totalSize += size;
            if (size > 0) {
              files.push({ path: file, size });
            }
          }
        }

        const sizeKB = totalSize / 1024;
        const status = sizeKB > MAX_FIRST_LOAD_KB ? '❌' : '✅';

        console.log(`${status} ${pagePath}: ${formatBytes(totalSize)}`);

        pages.push({ path: pagePath, size: totalSize, sizeKB });

        if (sizeKB > MAX_FIRST_LOAD_KB) {
          violations.push({
            page: pagePath,
            size: totalSize,
            sizeKB: sizeKB.toFixed(2),
            files,
          });
        }
      }
    }

    return { violations, pages };
  } catch (error) {
    console.error(`❌ Error parsing App Router manifest: ${error.message}`);
    return null;
  }
}

function analyzePagesRouterBuild() {
  console.log('📊 Analyzing Next.js Pages Router build...\n');

  if (!fs.existsSync(BUILD_MANIFEST_PATH)) {
    return null;
  }

  try {
    const manifest = JSON.parse(fs.readFileSync(BUILD_MANIFEST_PATH, 'utf8'));
    const violations = [];
    const pages = [];

    // Get root/main files that are loaded on every page
    const rootFiles = manifest.rootMainFiles || [];
    let rootSize = 0;

    for (const file of rootFiles) {
      const filePath = path.join(NEXT_DIR, file);
      rootSize += getFileSize(filePath);
    }

    console.log(`📦 Root bundle size: ${formatBytes(rootSize)}\n`);

    // Analyze each page
    const pageEntries = Object.keys(manifest.pages || {});

    for (const page of pageEntries) {
      const pageFiles = manifest.pages[page] || [];
      let pageSize = 0;
      const files = [];

      // Calculate page-specific size
      for (const file of pageFiles) {
        const filePath = path.join(NEXT_DIR, file);
        const size = getFileSize(filePath);
        pageSize += size;
        if (size > 0) {
          files.push({ path: file, size });
        }
      }

      // First Load JS = root bundle + page-specific bundle
      const totalSize = rootSize + pageSize;
      const sizeKB = totalSize / 1024;
      const status = sizeKB > MAX_FIRST_LOAD_KB ? '❌' : '✅';

      console.log(`${status} ${page}: ${formatBytes(totalSize)} (root: ${formatBytes(rootSize)} + page: ${formatBytes(pageSize)})`);

      pages.push({ path: page, size: totalSize, sizeKB });

      if (sizeKB > MAX_FIRST_LOAD_KB) {
        violations.push({
          page,
          size: totalSize,
          sizeKB: sizeKB.toFixed(2),
          rootSize: rootSize,
          pageSize: pageSize,
          files,
        });
      }
    }

    return { violations, pages };
  } catch (error) {
    console.error(`❌ Error parsing Pages Router manifest: ${error.message}`);
    return null;
  }
}

function checkBuildOutput() {
  console.log('🔍 Checking bundle sizes...\n');

  // Check if .next directory exists
  if (!fs.existsSync(NEXT_DIR)) {
    console.error('❌ Error: .next directory not found.');
    console.error('   Run "npm run build" in the frontend directory first.\n');
    process.exit(1);
  }

  // Check if this is a production build
  const buildIdPath = path.join(NEXT_DIR, 'BUILD_ID');
  if (!fs.existsSync(buildIdPath)) {
    console.error('❌ Error: BUILD_ID not found.');
    console.error('   This appears to be a development build.');
    console.error('   Run "npm run build" for a production build.\n');
    process.exit(1);
  }

  // Check for development build indicators
  const traceFile = path.join(NEXT_DIR, 'trace');
  if (fs.existsSync(traceFile)) {
    console.warn('⚠️  Warning: This appears to be a development build.');
    console.warn('   Development builds include extra debugging code and are much larger.');
    console.warn('   For accurate bundle size analysis, run: npm run build\n');
    console.warn('   Continuing with analysis, but results may not reflect production sizes...\n');
  }

  // Try App Router first, then Pages Router
  let result = analyzeAppRouterBuild();
  if (!result) {
    result = analyzePagesRouterBuild();
  }

  if (!result) {
    console.error('❌ Error: Could not find build manifests.');
    console.error('   Ensure you have run "npm run build" successfully.\n');
    process.exit(1);
  }

  const { violations, pages } = result;

  // Report results
  console.log('\n' + '='.repeat(70));
  console.log(`\n📈 Summary: Analyzed ${pages.length} page(s)\n`);

  if (violations.length > 0) {
    console.log(`❌ Found ${violations.length} page(s) exceeding ${MAX_FIRST_LOAD_KB}KB:\n`);

    for (const violation of violations) {
      console.log(`Page: ${violation.page}`);
      console.log(`Size: ${violation.sizeKB} KB (limit: ${MAX_FIRST_LOAD_KB} KB)`);
      console.log(`Exceeded by: ${(violation.sizeKB - MAX_FIRST_LOAD_KB).toFixed(2)} KB`);

      if (violation.rootSize !== undefined) {
        console.log(`Root bundle: ${formatBytes(violation.rootSize)}`);
        console.log(`Page bundle: ${formatBytes(violation.pageSize)}`);
      }

      if (violation.files && violation.files.length > 0) {
        console.log(`Files (${violation.files.length}):`);
        // Sort by size descending to show largest files first
        violation.files
          .sort((a, b) => b.size - a.size)
          .slice(0, 10) // Show top 10 largest files
          .forEach(file => console.log(`  - ${file.path} (${formatBytes(file.size)})`));

        if (violation.files.length > 10) {
          console.log(`  ... and ${violation.files.length - 10} more files`);
        }
      }
      console.log();
    }

    console.log('💡 Suggestions to reduce bundle size:');
    console.log('  1. Use dynamic imports for heavy components:');
    console.log('     const Chart = dynamic(() => import("./Chart"), { ssr: false })');
    console.log('  2. Code-split chart libraries (Recharts, D3, etc.)');
    console.log('  3. Check for duplicate dependencies in package.json');
    console.log('  4. Use tree-shaking friendly imports:');
    console.log('     import { Button } from "components" (not import * as Components)');
    console.log('  5. Analyze bundle with: npm run build && npm run analyze\n');

    process.exit(1);
  } else {
    console.log(`✅ All pages are within the ${MAX_FIRST_LOAD_KB}KB limit\n`);

    // Show largest pages for awareness
    if (pages.length > 0) {
      const sortedPages = pages.sort((a, b) => b.size - a.size).slice(0, 5);
      console.log('📊 Largest pages:');
      sortedPages.forEach(page => {
        console.log(`  ${page.path}: ${formatBytes(page.size)}`);
      });
      console.log();
    }

    process.exit(0);
  }
}

// Run the check
checkBuildOutput();
