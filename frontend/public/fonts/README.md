# Font System - Financial App Typography

## Current Implementation: IBM Plex Sans + Source Code Pro

### IBM Plex Sans (Primary UI Font)
- **Purpose**: All UI text, labels, buttons, headers
- **Weights**: 400 (Regular), 500 (Medium), 600 (SemiBold), 700 (Bold)
- **Characteristics**:
  - Neutral, professional grotesque designed by IBM
  - Excellent legibility on screens
  - Engineered precision that conveys trust
  - Free and open source (SIL Open Font License)

### Source Code Pro (Financial Data Font)
- **Purpose**: All numerical data, prices, percentages, financial metrics
- **Weights**: 400 (Regular), 600 (SemiBold)
- **Characteristics**:
  - Fixed-width with perfect tabular alignment
  - Clean zero WITHOUT dot or slash (important for financial data)
  - Designed by Adobe, highly rated for code and data
  - Excellent readability for numbers
  - Free and open source (SIL Open Font License)

## Why This Combination?

**IBM Plex Sans**: Widely used in financial and enterprise applications
- Professional and conveys trust
- Engineered with precision for data-heavy interfaces
- Used by IBM Watson and major fintech platforms

**Source Code Pro**: Top-rated monospace font
- Ranked #2 in programming fonts by Slant community
- Clean zero character (no dot, no slash) - perfect for financial data
- Superior legibility compared to IBM Plex Mono
- Better distinction between similar characters (0, O, 1, l, I)

## Alternative Font Pairs (Free)

### Option 2: DM Sans + Space Mono
```typescript
import { DM_Sans, Space_Mono } from "next/font/google";
```
- **Style**: Modern, geometric, clean
- **Used by**: Modern fintech startups, trading dashboards

### Option 3: Work Sans + Roboto Mono
```typescript
import { Work_Sans, Roboto_Mono } from "next/font/google";
```
- **Style**: Optimized for screen, battle-tested
- **Used by**: Google Finance, various trading platforms

## Rupee Symbol (₹) Usage

**Rule**: Only display rupee symbols in portfolio values.

**Where rupee symbols appear**:
- ✅ Portfolio total value
- ✅ Day P&L
- ✅ Total Return

**Where rupee symbols are removed**:
- ❌ Buying Power (removed from dashboard)
- ❌ Stock prices in watchlists
- ❌ Market index values
- ❌ Screener table prices
- ❌ Terminal live prices

## Percentage Display

**Fixed**: Removed double plus signs (++) on positive values
- `formatPercentage()` already adds the `+` sign
- Changed percentage font from `font-medium` to `font-mono tabular-nums` for better alignment
- All percentages now use monospace font for consistent width

## Implementation

Fonts are loaded in `frontend/app/layout.tsx`:

```typescript
import { IBM_Plex_Sans, Source_Code_Pro } from "next/font/google";

const ibmPlexSans = IBM_Plex_Sans({
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
});

const sourceCodePro = Source_Code_Pro({
  weight: ["400", "600"],
  variable: "--font-mono",
});
```

Configured in `frontend/app/globals.css`:

```css
:root {
  --font-sans: var(--font-sans), "IBM Plex Sans", system-ui, sans-serif;
  --font-mono: var(--font-mono), "Source Code Pro", "Consolas", monospace;
}
```

Use in components:
```tsx
// UI text
<div className="font-sans">Dashboard</div>

// Financial data and percentages
<div className="font-mono tabular-nums">1,234.56</div>
<div className="font-mono tabular-nums">+5.23%</div>
```
