# Screener Page Manual Test Plan

**Task:** 7.4 Test Screener page
**Requirements:** 9.1, 9.2, 9.4, 9.5
**Date:** $(Get-Date)

## Test Environment
- URL: http://localhost:3001/screener
- Browser: Chrome/Edge (latest)

## Test Cases

### 1. Page Rendering - Light Mode ✓
**Requirement:** 9.1 - Verify page renders correctly in light mode

**Steps:**
1. Open http://localhost:3001/screener in light mode
2. Verify header displays "Stock Screener" title
3. Verify search input is visible and functional
4. Verify universe selector dropdown is visible
5. Verify table headers are visible and properly aligned
6. Verify table data loads and displays correctly
7. Verify pagination controls appear (if > 25 results)

**Expected Results:**
- All elements render with proper spacing and alignment
- Text is readable with proper contrast
- Colors use design tokens (no hardcoded colors)
- No layout shifts or visual glitches

**Status:** [ ] Pass [ ] Fail

**Notes:**
_______________________________________________________________________

---

### 2. Page Rendering - Dark Mode ✓
**Requirement:** 9.2 - Verify page renders correctly in dark mode

**Steps:**
1. Switch to dark mode (toggle in header)
2. Verify all elements render correctly
3. Check background colors are deep blacks (#0a0a0a, #141414)
4. Check text colors are refined grays (#fafafa, #a0a0a0)
5. Check borders are subtle (#2a2a2a)
6. Verify profit/loss colors are vibrant (#00d084, #ff4757)

**Expected Results:**
- All elements render with Linear-inspired dark theme
- Text maintains proper contrast ratios
- Interactive elements have visible hover states
- No color bleeding or contrast issues

**Status:** [ ] Pass [ ] Fail

**Notes:**
_______________________________________________________________________

---

### 3. Console Errors Check ✓
**Requirement:** 9.4 - Verify no console errors

**Steps:**
1. Open browser DevTools (F12)
2. Go to Console tab
3. Clear console
4. Load http://localhost:3001/screener
5. Interact with page (search, sort, filter, paginate)
6. Check for any errors or warnings

**Expected Results:**
- No console errors related to:
  - Missing CSS variables
  - Undefined properties
  - React warnings
  - Network errors (except expected API failures)
- Only expected logs/warnings allowed

**Status:** [ ] Pass [ ] Fail

**Errors Found:**
_______________________________________________________________________

---

### 4. Sorting Functionality ✓
**Requirement:** 9.5 - Test sorting

**Test 4.1: Symbol Sorting**
**Steps:**
1. Click "SYMBOL" column header
2. Verify sort icon changes (ArrowUp/ArrowDown)
3. Verify symbols are sorted alphabetically (A-Z)
4. Click "SYMBOL" header again
5. Verify symbols are sorted reverse alphabetically (Z-A)

**Expected Results:**
- Sort icon updates correctly
- Data sorts in correct order
- No console errors
- Smooth transition

**Status:** [ ] Pass [ ] Fail

---

**Test 4.2: Price Sorting**
**Steps:**
1. Click "PRICE" column header
2. Verify prices are sorted descending (high to low)
3. Click "PRICE" header again
4. Verify prices are sorted ascending (low to high)

**Expected Results:**
- Numeric sorting works correctly
- Sort icon reflects current direction
- No layout shifts

**Status:** [ ] Pass [ ] Fail

---

**Test 4.3: Change Sorting**
**Steps:**
1. Click "CHANGE" column header
2. Verify changes are sorted descending (highest gains first)
3. Click again to sort ascending (highest losses first)

**Expected Results:**
- Percentage values sort correctly
- Profit/loss colors remain correct
- Icons (TrendingUp/Down) display correctly

**Status:** [ ] Pass [ ] Fail

---

**Test 4.4: Volume Sorting**
**Steps:**
1. Click "VOLUME" column header
2. Verify volumes are sorted correctly
3. Check Indian notation (Cr, L, K) doesn't affect sorting

**Expected Results:**
- Numeric sorting works despite formatted display
- High volumes appear first (descending default)

**Status:** [ ] Pass [ ] Fail

---

**Test 4.5: Market Cap Sorting**
**Steps:**
1. Click "MKT CAP" column header
2. Verify market caps are sorted correctly

**Expected Results:**
- Large caps appear first (descending default)
- Sorting works correctly with Indian notation

**Status:** [ ] Pass [ ] Fail

---

**Test 4.6: Technical Indicator Sorting (RSI, EMA20, EMA50, MACD, ADX)**
**Steps:**
1. Click each technical indicator column header
2. Verify sorting works for each
3. Check null/zero values appear at end

**Expected Results:**
- All technical indicators sort correctly
- Null values handled properly (appear at end)
- Color coding remains correct (RSI >70 red, <30 green)

**Status:** [ ] Pass [ ] Fail

---

### 5. Filtering Functionality ✓
**Requirement:** 9.5 - Test filtering

**Test 5.1: Search Filter**
**Steps:**
1. Type "SBIN" in search box
2. Wait for debounce (300ms)
3. Verify results filter to matching symbols
4. Clear search
5. Verify all results return

**Expected Results:**
- Search filters results correctly
- Debounce prevents excessive API calls
- Loading indicator appears during search
- Results count updates

**Status:** [ ] Pass [ ] Fail

---

**Test 5.2: Universe Filter**
**Steps:**
1. Select "NIFTY 50" from dropdown
2. Verify results show only NIFTY 50 stocks
3. Check results count matches index size
4. Select "NIFTY BANK" from dropdown
5. Verify results update to bank stocks
6. Try other indices (NIFTY IT, NIFTY AUTO, etc.)

**Expected Results:**
- Universe filter works correctly
- Results count updates
- Page resets to 1 when changing universe
- Loading indicator appears during filter

**Status:** [ ] Pass [ ] Fail

---

**Test 5.3: Combined Filters**
**Steps:**
1. Select "NIFTY 50" universe
2. Type "REL" in search
3. Verify results show only NIFTY 50 stocks matching "REL"
4. Sort by price
5. Verify sorting works with filters active

**Expected Results:**
- Multiple filters work together
- Sorting works with active filters
- Results count reflects combined filters

**Status:** [ ] Pass [ ] Fail

---

### 6. Pagination ✓
**Requirement:** 9.5 - Test interactive elements

**Steps:**
1. Verify pagination controls appear (if > 25 results)
2. Click "Next" button
3. Verify page increments and new results load
4. Verify "Previous" button becomes enabled
5. Click "Previous" button
6. Verify page decrements
7. Verify "Previous" disabled on page 1
8. Navigate to last page
9. Verify "Next" disabled on last page

**Expected Results:**
- Pagination works correctly
- Buttons enable/disable appropriately
- Page number displays correctly
- Results load smoothly

**Status:** [ ] Pass [ ] Fail

---

### 7. Interactive States ✓
**Requirement:** 9.5 - Test interactive elements

**Test 7.1: Row Hover**
**Steps:**
1. Hover over table rows
2. Verify hover state applies (bg-background-tertiary)
3. Verify cursor changes to pointer

**Expected Results:**
- Hover state visible and smooth
- Background color changes correctly
- Cursor indicates clickability

**Status:** [ ] Pass [ ] Fail

---

**Test 7.2: Row Click Navigation**
**Steps:**
1. Click on a stock row
2. Verify navigation to Terminal page with symbol parameter
3. Check URL: /terminal?symbol=SYMBOL

**Expected Results:**
- Navigation works correctly
- Symbol parameter passed correctly

**Status:** [ ] Pass [ ] Fail

---

**Test 7.3: Button States**
**Steps:**
1. Test pagination buttons (hover, active, disabled states)
2. Verify disabled buttons have reduced opacity
3. Verify hover states on enabled buttons

**Expected Results:**
- All button states work correctly
- Visual feedback is clear
- Disabled state prevents interaction

**Status:** [ ] Pass [ ] Fail

---

### 8. WebSocket Live Updates ✓
**Requirement:** 9.5 - Test interactive elements

**Steps:**
1. Verify "LIVE" badge appears when WebSocket connected
2. Watch for price updates in real-time
3. Verify change percentages update
4. Verify volume updates
5. Check profit/loss colors update dynamically

**Expected Results:**
- Live badge displays when connected
- Prices update in real-time
- No flickering or layout shifts
- Colors update correctly

**Status:** [ ] Pass [ ] Fail

---

### 9. Typography Verification ✓
**Requirement:** 3.6 - Financial data typography

**Steps:**
1. Inspect price column
2. Verify font-family includes monospace (DM Mono)
3. Verify tabular-nums is applied
4. Check all numeric columns use monospace
5. Verify rupee symbol uses correct size

**Expected Results:**
- All financial data uses monospace font
- Numbers align properly in columns
- Tabular nums formatting applied
- Consistent typography across all numeric data

**Status:** [ ] Pass [ ] Fail

---

### 10. Accessibility Check ✓
**Requirement:** 9.6 - Verify proper contrast ratios

**Steps:**
1. Use browser DevTools Accessibility panel
2. Check contrast ratios for:
   - Header text on background
   - Table text on background
   - Profit/loss text on background
   - Button text on button background
3. Verify all meet WCAG AA standards (4.5:1 for normal text)

**Expected Results:**
- All text meets minimum contrast ratios
- Interactive elements have visible focus indicators
- Color is not the only means of conveying information

**Status:** [ ] Pass [ ] Fail

---

## Summary

**Total Tests:** 10 main categories, 20+ individual tests
**Passed:** ___
**Failed:** ___
**Blocked:** ___

**Overall Status:** [ ] Pass [ ] Fail

**Critical Issues:**
_______________________________________________________________________

**Minor Issues:**
_______________________________________________________________________

**Recommendations:**
_______________________________________________________________________

---

## Sign-off

**Tester:** _______________
**Date:** _______________
**Approved:** [ ] Yes [ ] No
