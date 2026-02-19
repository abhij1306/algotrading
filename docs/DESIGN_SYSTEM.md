# SmartTrader Design System

**Version**: 3.0 - Unified Token System
**Last Updated**: February 19, 2026
**Status**: Active

---

## Table of Contents

1. [Introduction](#introduction)
2. [Design Philosophy](#design-philosophy)
3. [Token-Based Architecture](#token-based-architecture)
4. [Design Tokens](#design-tokens)
5. [Component Library](#component-library)
6. [Typography System](#typography-system)
7. [Migration Guide](#migration-guide)
8. [Guidelines](#guidelines)
9. [Performance](#performance)
10. [Accessibility](#accessibility)

---

## Introduction

This is the single source of truth for SmartTrader's unified design system. It defines the visual language, design tokens, component library, and interaction patterns used across the entire application.

### What's New in Version 3.0

- **Unified Design Tokens**: Single, consistent token system replacing legacy token variants
- **Linear-Inspired Dark Theme**: Professional, refined dark mode with vibrant financial colors
- **Modern Typography**: IBM Plex Sans for UI with DM Mono for financial data
- **Token-Based Architecture**: Three-tier token system for flexible theming
- **Complete Component Library**: Fully documented, token-based UI components

### Implementation Parity Rule

- Canonical implementation source: `frontend/app/globals.css`
- Documentation examples in this file must match the token names and values defined there.
- If code and docs diverge, update docs in the same change as token updates.

### Purpose

- Ensure consistency across all pages and features
- Speed up development with reusable patterns
- Maintain high performance and accessibility standards
- Provide clear guidelines for future development
- Enable easy theming through design tokens

### Inspiration

Our design system is inspired by:
- **Linear**: Compact, fast, information-dense, refined dark theme
- **Bloomberg Terminal**: Professional, data-focused
- **Modern Financial Apps**: Clean, minimal, keyboard-first

---

## Design Philosophy

### Core Principles

1. **Token-Based Design**
   - All visual properties defined as semantic tokens
   - Single source of truth for colors, spacing, typography
   - Easy theming and maintenance

2. **Compact & Dense**
   - Maximize information density without overwhelming
   - Use space efficiently
   - Prioritize content over chrome

3. **Fast & Responsive**
   - Instant feedback on interactions
   - Smooth animations (< 200ms)
   - Optimized for performance

4. **Consistent & Predictable**
   - Same patterns everywhere
   - Familiar interactions
   - No surprises

5. **Performance-First**
   - Fast load times
   - Smooth scrolling
   - Efficient rendering

### Values

- **Real Data Only**: No mock data in production
- **Keyboard-First**: All actions accessible via keyboard
- **Dark-First**: Optimized for dark mode (light mode supported)
- **Mobile-Ready**: Responsive design for all screen sizes
- **Token-Driven**: All styling uses design tokens

---

## Token-Based Architecture

The design system uses a three-tier token architecture for maximum flexibility and maintainability:

### 1. Primitive Tokens
Raw values defined in CSS variables:
```css
:root {
  --color-background: #ffffff;
  --spacing-4: 16px;
  --radius-md: 6px;
}
```

### 2. Semantic Tokens
Purpose-driven tokens that reference primitives:
```css
--color-foreground: var(--color-foreground);
--color-primary: var(--color-primary);
```

### 3. Component Tokens
Component-specific tokens mapped through Tailwind's `@theme` directive:
```css
@theme {
  --color-background: var(--color-background);
  --color-foreground: var(--color-foreground);
}
```

### Benefits

- **Flexible Theming**: Change theme by updating primitive tokens
- **Consistent Naming**: Semantic tokens remain constant across themes
- **Type Safety**: TypeScript definitions for all tokens
- **Easy Maintenance**: Single source of truth for all values

---

## Design Tokens

All visual properties are defined as design tokens. Use these tokens consistently throughout the application.

### Color Tokens

#### Light Mode Colors

```css
:root {
  /* Backgrounds */
  --color-background: #ffffff;           /* Primary background */
  --color-background-secondary: #f8fafc; /* Secondary surfaces */
  --color-background-tertiary: #f1f5f9;  /* Tertiary surfaces */
  --color-surface: #ffffff;              /* Card surfaces */
  --color-elevated: #ffffff;             /* Elevated surfaces */

  /* Foregrounds */
  --color-foreground: #0f172a;           /* Primary text */
  --color-foreground-secondary: #334155; /* Secondary text */
  --color-foreground-tertiary: #64748b;  /* Tertiary text */
  --color-foreground-muted: #94a3b8;     /* Muted text */

  /* Borders */
  --color-border: #e2e8f0;               /* Standard borders */
  --color-border-subtle: #f1f5f9;        /* Subtle borders */
  --color-border-focus: #3b82f6;         /* Focus rings */

  /* Primary */
  --color-primary: #2563eb;              /* Primary actions */
  --color-primary-hover: #1d4ed8;        /* Primary hover */
  --color-primary-light: #eff6ff;        /* Primary backgrounds */
  --color-primary-foreground: #ffffff;   /* Text on primary */

  /* Semantic Financial Colors */
  --color-profit: #16a34a;               /* Gains, positive */
  --color-profit-bg: #dcfce7;            /* Profit backgrounds */
  --color-loss: #dc2626;                 /* Losses, negative */
  --color-loss-bg: #fee2e2;              /* Loss backgrounds */
  --color-warning: #d97706;              /* Warnings */
  --color-warning-bg: #fef3c7;           /* Warning backgrounds */
}
```

#### Dark Mode Colors (Linear-Inspired)

```css
.dark {
  /* Backgrounds - Deep blacks with subtle gradients */
  --color-background: #0a0a0a;           /* Deep black primary */
  --color-background-secondary: #141414; /* Slightly lighter */
  --color-background-tertiary: #1c1c1c;  /* Tertiary surfaces */
  --color-surface: #141414;              /* Card surfaces */
  --color-elevated: #1f1f1f;             /* Elevated surfaces */

  /* Foregrounds - Refined grays */
  --color-foreground: #fafafa;           /* Primary text */
  --color-foreground-secondary: #a0a0a0; /* Secondary text */
  --color-foreground-tertiary: #666666;  /* Tertiary text */
  --color-foreground-muted: #444444;     /* Muted text */

  /* Borders - Subtle separation */
  --color-border: #2a2a2a;               /* Standard borders */
  --color-border-subtle: #1f1f1f;        /* Subtle borders */
  --color-border-focus: #3b82f6;         /* Focus rings */

  /* Primary - Vibrant blue */
  --color-primary: #3b82f6;              /* Primary actions */
  --color-primary-hover: #60a5fa;        /* Primary hover */
  --color-primary-light: rgba(59, 130, 246, 0.15); /* Primary backgrounds */
  --color-primary-foreground: #ffffff;   /* Text on primary */

  /* Semantic Financial Colors - Vibrant for dark backgrounds */
  --color-profit: #00d084;               /* Vibrant green */
  --color-profit-bg: rgba(0, 208, 132, 0.15); /* Profit backgrounds */
  --color-loss: #ff4757;                 /* Vibrant red */
  --color-loss-bg: rgba(255, 71, 87, 0.15); /* Loss backgrounds */
  --color-warning: #ffa502;              /* Vibrant orange */
  --color-warning-bg: rgba(255, 165, 2, 0.15); /* Warning backgrounds */
}
```

#### Usage in Code

**CSS Variables:**
```css
.my-component {
  background-color: var(--color-background);
  color: var(--color-foreground);
  border: 1px solid var(--color-border);
}
```

**Tailwind Classes:**
```tsx
<div className="bg-background text-foreground border border-border">
  Content
</div>
```

#### Color Token Guidelines

**DO**:
- Always use design tokens for colors
- Use semantic colors for financial data (profit = green, loss = red)
- Test in both dark and light modes
- Use `bg-` prefix for backgrounds, `text-` for text, `border-` for borders

**DON'T**:
- Hardcode color values (#ffffff, rgb(), etc.)
- Use arbitrary color values in Tailwind (bg-[#050505])
- Mix token systems (no Raycast variables)
- Forget to test contrast ratios

### Typography Tokens

#### Font Families

```css
:root {
  /* Sans-serif for UI */
  --font-sans: var(--font-sans), "IBM Plex Sans", system-ui, -apple-system, sans-serif;

  /* Monospace for financial data */
  --font-mono: var(--font-mono), "DM Mono", "Consolas", monospace;
}
```

**Usage:**
```tsx
<div className="font-sans">UI Text</div>
<div className="font-mono mono-num">₹1,234.56</div>
```

#### Font Sizes

```css
:root {
  --text-xxs: 10px;   /* Labels, captions, metadata */
  --text-xs: 11px;    /* Small text, table cells */
  --text-sm: 13px;    /* Body text, buttons */
  --text-base: 15px;  /* Default body text */
  --text-lg: 17px;    /* Headings, emphasis */
  --text-xl: 20px;    /* Page titles */
  --text-2xl: 24px;   /* Large headings */
}
```

**Usage:**
```tsx
<h1 className="text-2xl">Page Title</h1>
<p className="text-sm">Body text</p>
<span className="text-xs">Label</span>
```

#### Font Weights

```css
:root {
  --font-normal: 400;    /* Body text */
  --font-medium: 500;    /* Labels, emphasis */
  --font-semibold: 600;  /* Headings, buttons */
  --font-bold: 700;      /* Strong emphasis */
}
```

**Usage:**
```tsx
<p className="font-normal">Regular text</p>
<button className="font-semibold">Button</button>
<h2 className="font-bold">Heading</h2>
```

#### Line Heights

```css
:root {
  --leading-tight: 1.2;    /* Headings, compact text */
  --leading-normal: 1.5;   /* Body text, default */
  --leading-relaxed: 1.75; /* Long-form content */
}
```

**Usage:**
```tsx
<h2 className="leading-tight">Compact Heading</h2>
<p className="leading-normal">Body paragraph</p>
```

### Spacing Tokens

All spacing uses a 4px base unit for consistency.

```css
:root {
  --spacing-1: 4px;    /* 0.25rem - Minimal */
  --spacing-2: 8px;    /* 0.5rem  - Tight */
  --spacing-3: 12px;   /* 0.75rem - Compact */
  --spacing-4: 16px;   /* 1rem    - Default */
  --spacing-5: 20px;   /* 1.25rem - Comfortable */
  --spacing-6: 24px;   /* 1.5rem  - Generous */
  --spacing-8: 32px;   /* 2rem    - Large */
  --spacing-10: 40px;  /* 2.5rem  - Extra large */
  --spacing-12: 48px;  /* 3rem    - Section */
}
```

**Usage:**
```tsx
<div className="p-4">Padding 16px</div>
<div className="mb-6">Margin bottom 24px</div>
<div className="gap-3">Gap 12px</div>
```

**Common Patterns:**
- Cards: `p-4` or `p-6` (16px or 24px padding)
- Buttons: `px-4 py-2` (16px horizontal, 8px vertical)
- Inputs: `px-3 py-2` (12px horizontal, 8px vertical)
- Section spacing: `mb-6` (24px)
- Element spacing: `mb-4` (16px)
- Related items: `mb-2` (8px)

### Border Radius Tokens

```css
:root {
  --radius-sm: 4px;     /* Small elements (badges, tags) */
  --radius-md: 6px;     /* Standard elements (buttons, inputs) */
  --radius-lg: 8px;     /* Cards, panels */
  --radius-xl: 12px;    /* Large cards */
  --radius-full: 9999px; /* Pills, avatars */
}
```

**Usage:**
```tsx
<div className="rounded-lg">Card</div>
<button className="rounded-md">Button</button>
<span className="rounded-sm">Badge</span>
<div className="rounded-full">Avatar</div>
```

### Shadow Tokens

```css
:root {
  --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
  --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
  --shadow-xl: 0 20px 25px -5px rgba(0, 0, 0, 0.1);
}

.dark {
  --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.3);
  --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.4);
  --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.5);
  --shadow-xl: 0 20px 25px -5px rgba(0, 0, 0, 0.6);
}
```

**Usage:**
```tsx
<div className="shadow-sm">Subtle elevation</div>
<div className="shadow-md">Card elevation</div>
<div className="shadow-lg">Modal elevation</div>
```

**Guidelines:**
- Use shadows sparingly
- Prefer borders for separation
- Shadows are stronger in dark mode

---

## Component Library

All components use the unified design token system and are fully typed with TypeScript.

### Button Component

Location: `frontend/components/ui/button.tsx`

Buttons trigger actions. Use semantic variants for different contexts.

#### Variants

```tsx
import { Button } from "@/components/ui/button";

// Primary - Main actions
<Button variant="primary">Primary Action</Button>

// Secondary - Secondary actions (default)
<Button variant="secondary">Secondary Action</Button>

// Ghost - Tertiary actions
<Button variant="ghost">Ghost Action</Button>

// Profit - Positive financial actions
<Button variant="profit">Buy</Button>

// Loss - Negative financial actions
<Button variant="loss">Sell</Button>

// Outline - Outlined buttons
<Button variant="outline">Outline</Button>

// Link - Link-style buttons
<Button variant="link">Link Action</Button>

// Destructive - Dangerous actions
<Button variant="destructive">Delete</Button>
```

#### Sizes

```tsx
<Button size="xs">Extra Small</Button>
<Button size="sm">Small</Button>
<Button size="default">Default</Button>
<Button size="lg">Large</Button>
<Button size="icon">
  <Icon className="h-4 w-4" />
</Button>
```

#### States

```tsx
// Disabled
<Button disabled>Disabled</Button>

// Loading
<Button disabled>
  <Loader2 className="w-4 h-4 animate-spin mr-2" />
  Loading...
</Button>
```

#### Implementation Details

- Uses `class-variance-authority` for variant management
- All colors reference unified design tokens
- Includes focus ring with `focus-visible:ring-2 focus-visible:ring-primary/20`
- Active state scales to 98% for tactile feedback
- Fully keyboard accessible

### Card Component

Location: `frontend/components/ui/card.tsx`

Container for grouped content with multiple variants and sub-components.

#### Variants

```tsx
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from "@/components/ui/card";

// Default card
<Card variant="default">
  <CardContent>Content</CardContent>
</Card>

// Glass effect
<Card variant="glass">
  <CardContent>Glass effect with backdrop blur</CardContent>
</Card>

// Elevated
<Card variant="elevated">
  <CardContent>Elevated with stronger shadow</CardContent>
</Card>

// Outline
<Card variant="outline">
  <CardContent>Transparent with border</CardContent>
</Card>

// Void (no background or border)
<Card variant="void">
  <CardContent>No background</CardContent>
</Card>

// Flat
<Card variant="flat">
  <CardContent>Flat secondary background</CardContent>
</Card>
```

#### With Structure

```tsx
<Card>
  <CardHeader>
    <CardTitle>Card Title</CardTitle>
    <CardDescription>Card description text</CardDescription>
  </CardHeader>
  <CardContent>
    Main card content goes here
  </CardContent>
  <CardFooter>
    Footer content or actions
  </CardFooter>
</Card>
```

#### Metric Card

Specialized card for displaying KPIs:

```tsx
import { MetricCard } from "@/components/ui/card";

<MetricCard
  label="Total Value"
  value="₹1,234,567"
  change={2.45}
  subValue="Last updated 5 min ago"
  icon={<TrendingUp className="h-4 w-4" />}
/>
```

### Input Component

Location: `frontend/components/ui/input.tsx`

Text inputs for user data entry with label and error support.

#### Basic Usage

```tsx
import { Input } from "@/components/ui/input";

<Input
  type="text"
  placeholder="Enter symbol..."
/>
```

#### With Label

```tsx
<Input
  label="Symbol"
  type="text"
  placeholder="SBIN"
/>
```

#### With Error

```tsx
<Input
  label="Symbol"
  type="text"
  error="Symbol is required"
/>
```

#### Implementation Details

- Rounded corners with `rounded-xl`
- Elevated background for depth
- Focus state with subtle ring
- Error state changes background and border to loss colors
- Label uses uppercase tracking for consistency
- Fully accessible with proper label associations

### Badge Component

Location: `frontend/components/ui/badge.tsx`

Small status indicators and labels for trading applications.

#### Variants

```tsx
import { Badge } from "@/components/ui/badge";

// Default
<Badge variant="default">Default</Badge>

// Secondary
<Badge variant="secondary">Secondary</Badge>

// Outline
<Badge variant="outline">Outline</Badge>

// Profit (Success)
<Badge variant="profit">+2.45%</Badge>

// Loss (Danger)
<Badge variant="loss">-1.23%</Badge>

// Warning
<Badge variant="warning">Warning</Badge>

// Neutral
<Badge variant="neutral">Neutral</Badge>

// Live with pulse
<Badge variant="live" pulse>Live</Badge>

// Muted
<Badge variant="muted">Muted</Badge>
```

#### Sizes

```tsx
<Badge size="xs">Extra Small</Badge>
<Badge size="sm">Small</Badge>
<Badge size="default">Default</Badge>
<Badge size="lg">Large</Badge>
```

#### With Icon

```tsx
<Badge icon={<TrendingUp className="h-3 w-3" />}>
  Trending
</Badge>
```

#### Specialized Badges

```tsx
import { StatusBadge, ChangeBadge } from "@/components/ui/badge";

// Status badge (auto-detects variant)
<StatusBadge status="active" />
<StatusBadge status="closed" />
<StatusBadge status="pending" />

// Change badge (auto-detects profit/loss)
<ChangeBadge value={2.45} />  // Shows +2.45% in green
<ChangeBadge value={-1.23} /> // Shows -1.23% in red
```

### Component Guidelines

**DO**:
- Use components from the component library
- Pass variant props for different styles
- Use semantic variants (profit/loss for financial data)
- Compose components (Card with CardHeader, CardContent, etc.)

**DON'T**:
- Create custom styled divs/buttons
- Hardcode colors or spacing
- Use inline styles
- Bypass the component library

---

## Typography System

SmartTrader uses a modern typography system optimized for financial data display.

### Font Families

#### Inter - UI Font

Inter is used for all UI text, providing excellent readability and a modern feel.

**Loading:**
```tsx
// app/layout.tsx
import { Inter } from 'next/font/google';

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-sans',
  display: 'swap',
});
```

**Features:**
- Variable font with multiple weights
- Optimized for screen display
- Excellent number legibility
- Professional appearance

#### DM Mono - Financial Data Font

DM Mono is used for all financial data (prices, percentages, quantities).

**Loading:**
```tsx
// Loaded through CSS token fallback stack in globals.css
// --font-mono: var(--font-mono), "DM Mono", "Consolas", monospace;
```

**Features:**
- Monospaced for aligned columns
- Tabular numbers for consistent width
- Slashed zero for clarity
- Excellent for financial data

### Type Scale

The type scale follows a consistent progression for harmonious typography:

| Token | Size | Line Height | Usage |
|-------|------|-------------|-------|
| `text-xxs` | 10px | 14px | Labels, captions, metadata |
| `text-xs` | 11px | 16px | Small text, table cells |
| `text-sm` | 13px | 18px | Body text, buttons |
| `text-base` | 15px | 22px | Default body text |
| `text-lg` | 17px | 24px | Headings, emphasis |
| `text-xl` | 20px | 28px | Page titles |
| `text-2xl` | 24px | 32px | Large headings |

### Font Features

#### Tabular Numbers

For financial data, always use tabular numbers to ensure columns align:

```tsx
<div className="font-mono mono-num">
  ₹1,234.56
</div>
```

The `.mono-num` class enables:
- `font-variant-numeric: tabular-nums` - Fixed-width numbers
- `font-feature-settings: "tnum", "zero", "ss01", "liga"` - OpenType features
- Slashed zero for clarity
- Proper ligatures

#### Font Smoothing

All text uses optimized font smoothing:

```css
body {
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
}
```

### Typography Usage Examples

#### Page Titles

```tsx
<h1 className="text-2xl font-bold tracking-tight text-foreground">
  Dashboard
</h1>
```

#### Section Headings

```tsx
<h2 className="text-xl font-bold tracking-tight text-foreground">
  Market Overview
</h2>

<h3 className="text-lg font-semibold tracking-tight text-foreground">
  Subsection
</h3>
```

#### Body Text

```tsx
<p className="text-sm text-foreground">
  Regular paragraph text
</p>

<p className="text-sm text-foreground-secondary">
  Secondary text
</p>
```

#### Labels

```tsx
<label className="text-xxs font-black text-foreground-tertiary uppercase tracking-wider">
  Symbol
</label>
```

#### Financial Data

```tsx
// Price display
<span className="font-mono mono-num text-lg">
  ₹1,234.56
</span>

// Percentage change
<span className="font-mono mono-num text-sm text-profit">
  +2.45%
</span>

// Table cell with numbers
<td className="font-mono mono-num text-sm text-right">
  1,234,567
</td>
```

### Typography Guidelines

**DO**:
- Use IBM Plex Sans for all UI text
- Use DM Mono for financial data
- Apply `.mono-num` class to numerical data
- Use consistent type scale
- Apply proper font weights for hierarchy

**DON'T**:
- Use arbitrary font sizes (text-[11px])
- Mix font families inconsistently
- Forget tabular numbers for aligned columns
- Use system fonts directly

---

## Migration Guide

### Migrating from Raycast Variables

If you're updating existing code that uses Raycast variables, follow this guide.

#### Step 1: Identify Raycast Variables

Search for patterns like:
```css
var(--raycast-bg-primary)
var(--raycast-fg-secondary)
var(--raycast-border)
```

#### Step 2: Use Migration Mapping

Reference the migration mapping in `frontend/lib/migration-map.ts`:

```typescript
export const raycastToUnifiedMapping: Record<string, string> = {
  // Backgrounds
  'var(--raycast-bg-primary)': 'var(--color-background)',
  'var(--raycast-bg-secondary)': 'var(--color-background-secondary)',
  'var(--raycast-bg-tertiary)': 'var(--color-background-tertiary)',

  // Foregrounds
  'var(--raycast-fg-primary)': 'var(--color-foreground)',
  'var(--raycast-fg-secondary)': 'var(--color-foreground-secondary)',

  // Borders
  'var(--raycast-border)': 'var(--color-border)',

  // Colors
  'var(--raycast-green)': 'var(--color-profit)',
  'var(--raycast-red)': 'var(--color-loss)',
  'var(--raycast-blue)': 'var(--color-primary)',
};
```

#### Step 3: Replace Variables

**Before:**
```tsx
<div style={{ backgroundColor: 'var(--raycast-bg-primary)' }}>
  <span style={{ color: 'var(--raycast-fg-secondary)' }}>Text</span>
</div>
```

**After:**
```tsx
<div className="bg-background">
  <span className="text-foreground-secondary">Text</span>
</div>
```

#### Step 4: Update Components

Replace custom styled elements with component library:

**Before:**
```tsx
<button className="px-4 py-2 rounded-md bg-blue-500 text-white">
  Click Me
</button>
```

**After:**
```tsx
<Button variant="primary">
  Click Me
</Button>
```

#### Step 5: Remove Inline Styles

**Before:**
```tsx
<div style={{
  backgroundColor: 'var(--raycast-bg-secondary)',
  padding: '16px',
  borderRadius: '8px'
}}>
  Content
</div>
```

**After:**
```tsx
<Card>
  <CardContent>
    Content
  </CardContent>
</Card>
```

### Common Migration Patterns

#### Pattern 1: Background Colors

```tsx
// Before
style={{ backgroundColor: 'var(--raycast-bg-primary)' }}

// After
className="bg-background"
```

#### Pattern 2: Text Colors

```tsx
// Before
style={{ color: 'var(--raycast-fg-secondary)' }}

// After
className="text-foreground-secondary"
```

#### Pattern 3: Borders

```tsx
// Before
style={{ border: '1px solid var(--raycast-border)' }}

// After
className="border border-border"
```

#### Pattern 4: Financial Colors

```tsx
// Before
style={{ color: value >= 0 ? 'var(--raycast-green)' : 'var(--raycast-red)' }}

// After
className={value >= 0 ? 'text-profit' : 'text-loss'}
```

### Migration Checklist

- [ ] Search for `--raycast-` in codebase
- [ ] Replace all Raycast variables with unified tokens
- [ ] Update components to use component library
- [ ] Remove inline styles with CSS variables
- [ ] Apply financial data typography (`.mono-num`)
- [ ] Test in both light and dark modes
- [ ] Verify no console errors
- [ ] Check accessibility (contrast, focus states)
- [ ] Run linting and type checking
- [ ] Test all interactive states

---

### Page Layout

Standard page structure for consistency.

```tsx
<div className="flex flex-col h-screen bg-primary">
  {/* Header */}
  <div className="flex items-center justify-between px-6 py-3 border-b border-subtle">
    <h1 className="text-xl font-semibold text-primary">Page Title</h1>
    <div className="flex items-center gap-3">
      {/* Actions */}
    </div>
  </div>

  {/* Content */}
  <div className="flex-1 overflow-auto p-6">
    {/* Page content */}
  </div>

  {/* Footer (optional) */}
  <div className="flex items-center justify-between px-6 py-3 border-t border-subtle">
    {/* Footer content */}
  </div>
</div>
```

### Grid Layout

Responsive grid for cards.

```tsx
<div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
  {/* Cards */}
</div>
```

### Split Layout

Two-column layout.

```tsx
<div className="flex h-screen">
  {/* Left panel */}
  <div className="w-1/3 border-r border-subtle overflow-auto">
    {/* Sidebar content */}
  </div>

  {/* Right panel */}
  <div className="flex-1 overflow-auto">
    {/* Main content */}
  </div>
</div>
```

### Loading State

Show loading indicator while fetching data.

```tsx
{isLoading ? (
  <div className="flex items-center justify-center p-8">
    <Loader2 className="w-6 h-6 animate-spin text-secondary" />
  </div>
) : (
  <div>{/* Content */}</div>
)}
```

### Empty State

Show when no data is available.

```tsx
<div className="flex flex-col items-center justify-center p-12 text-center">
  <Icon className="w-12 h-12 text-tertiary mb-4" />
  <h3 className="text-lg font-semibold text-primary mb-2">No Data</h3>
  <p className="text-sm text-secondary mb-4">
    Description of why there's no data
  </p>
  <button className="px-4 py-2 rounded-md text-sm font-medium bg-blue-500 text-white">
    Take Action
  </button>
</div>
```

### Error State

Show when an error occurs.

```tsx
<div className="flex flex-col items-center justify-center p-12 text-center">
  <AlertCircle className="w-12 h-12 text-red mb-4" />
  <h3 className="text-lg font-semibold text-primary mb-2">Error</h3>
  <p className="text-sm text-secondary mb-4">
    {error.message}
  </p>
  <button
    onClick={retry}
    className="px-4 py-2 rounded-md text-sm font-medium bg-blue-500 text-white"
  >
    Retry
  </button>
</div>
```

---

## Guidelines

### Animation

Keep animations fast and subtle for professional feel.

#### Timing

```css
/* Hover states */
transition: background-color 150ms ease-in-out;
transition: color 150ms ease-in-out;

/* Opacity changes */
transition: opacity 200ms ease-in-out;

/* Transform changes */
transition: transform 200ms ease-in-out;

/* All properties */
transition: all 200ms ease-in-out;
```

#### Rules

- **DO**: Use animations for feedback (hover, focus, loading)
- **DO**: Keep animations under 200ms
- **DO**: Use `ease-in-out` for smooth feel
- **DON'T**: Animate layout properties (causes reflow)
- **DON'T**: Use animations longer than 300ms
- **DON'T**: Animate on scroll (performance issues)

#### Custom Animations

```tsx
// Fade in
<div className="animate-fade-in">Content</div>

// Slide up
<div className="animate-slide-up">Content</div>

// Scale in
<div className="animate-scale-in">Content</div>
```

### Responsive Design

Mobile-first approach with breakpoints.

#### Breakpoints

```typescript
sm: '640px'   // Small devices
md: '768px'   // Tablets
lg: '1024px'  // Laptops
xl: '1280px'  // Desktops
2xl: '1536px' // Large desktops
```

#### Usage

```tsx
// Mobile-first
<div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3">

// Hide on mobile
<div className="hidden md:block">

// Show only on mobile
<div className="block md:hidden">

// Responsive spacing
<div className="p-3 md:p-4 lg:p-6">
```

### Data Formatting

Consistent formatting for numbers and currency.

#### Currency

```typescript
import { formatCurrency } from '@/lib/utils';

formatCurrency(1234567); // ₹12,34,567
```

#### Percentages

```typescript
import { formatPercent } from '@/lib/utils';

formatPercent(2.45);  // +2.45%
formatPercent(-1.23); // -1.23%
```

#### Large Numbers

```typescript
import { formatLargeNumber } from '@/lib/utils';

formatLargeNumber(1234567890); // ₹1.23B
```

### Error Handling

Always handle errors gracefully.

```tsx
try {
  const response = await fetch('/api/endpoint');
  if (!response.ok) {
    throw new Error('Failed to fetch data');
  }
  const data = await response.json();
  setData(data);
} catch (error) {
  setError(error instanceof Error ? error.message : 'An error occurred');
  console.error('Fetch error:', error);
}
```

### Code Quality

#### ESLint Rules

SmartTrader enforces custom ESLint rules:

1. **No Hardcoded Colors** - Use design tokens
2. **No Arbitrary Text Sizes** - Use type scale
3. **Use Format Utilities** - Centralized formatting
4. **Use UI Components** - Standardized components

See [CODE_QUALITY.md](CODE_QUALITY.md) for details.

---

## Performance

### Bundle Size

Target: < 500KB initial bundle (gzipped)

#### Optimization Strategies

1. **Code Splitting**
   ```tsx
   const LazyChart = dynamic(() => import('@/components/charts/LazyChart'), {
     loading: () => <Skeleton />
   });
   ```

2. **Tree Shaking**
   ```tsx
   // Good: Import only what you need
   import { useState, useEffect } from 'react';

   // Bad: Import everything
   import * as React from 'react';
   ```

3. **Lazy Loading**
   ```tsx
   // Load heavy components only when needed
   const HeavyComponent = lazy(() => import('./HeavyComponent'));
   ```

### Runtime Performance

Target: 60fps for all interactions

#### Optimization Strategies

1. **Memoization**
   ```tsx
   const expensiveValue = useMemo(() => {
     return computeExpensiveValue(data);
   }, [data]);
   ```

2. **Debouncing**
   ```tsx
   const debouncedSearch = useMemo(
     () => debounce((query) => search(query), 300),
     []
   );
   ```

3. **Virtual Scrolling**
   - Use for lists > 100 items
   - Consider `react-window` or `react-virtual`

### Load Times

Target metrics:
- First Contentful Paint: < 1.5s
- Time to Interactive: < 3s
- Largest Contentful Paint: < 2.5s

---

## Accessibility

### Keyboard Navigation

All interactive elements must be keyboard accessible.

#### Requirements

- **Tab Order**: Logical and predictable
- **Focus Indicators**: Visible on all interactive elements
- **Escape Key**: Closes modals and dropdowns
- **Enter/Space**: Activates buttons and links
- **Arrow Keys**: Navigate lists and menus

#### Implementation

```tsx
// Focus indicator
<button className="focus:outline-none focus:ring-2 focus:ring-blue-500">
  Button
</button>

// Keyboard handler
<div
  role="button"
  tabIndex={0}
  onKeyDown={(e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      handleClick();
    }
  }}
>
  Clickable Div
</div>
```

### Screen Readers

Use semantic HTML and ARIA labels.

#### Requirements

- **Semantic HTML**: Use proper elements (`<button>`, `<nav>`, `<main>`)
- **ARIA Labels**: Add labels for icon-only buttons
- **Alt Text**: Provide for all images
- **Descriptive Links**: Avoid "click here"

#### Implementation

```tsx
// Icon button with label
<button aria-label="Close modal">
  <X className="w-4 h-4" />
</button>

// Image with alt text
<img src="/logo.png" alt="SmartTrader logo" />

// Descriptive link
<a href="/screener">View stock screener</a>
```

### Color Contrast

Ensure sufficient contrast for readability.

#### Requirements

- **Text**: Minimum 4.5:1 contrast ratio
- **Interactive Elements**: Minimum 3:1 contrast ratio
- **Test**: Use color blindness simulators

#### Tools

- Chrome DevTools: Lighthouse audit
- WebAIM Contrast Checker
- Stark (Figma plugin)

---

## Migration Guide

### For Existing Pages

1. **Review Design System**: Read this document thoroughly
2. **Audit Current Page**: Check spacing, colors, typography
3. **Apply Patterns**: Use standard layouts and components
4. **Test**: Verify in dark/light modes, test accessibility
5. **Document**: Note any deviations or new patterns

### For New Features

1. **Start with Design System**: Use existing components and patterns
2. **Real Data Only**: Connect to API from the start
3. **Implement States**: Loading, empty, error states
4. **Performance First**: Lazy load, memoize, debounce
5. **Test Accessibility**: Keyboard navigation, screen readers

---

## Resources

### Internal

- [Architecture](ARCHITECTURE.md) - System architecture
- [Data Flow](DATA_ARCHITECTURE.md) - Data architecture
- [API Reference](FYERS_API_REFERENCE.md) - Fyers API docs
- [Audit Report](learnings/design-system-audit-2026-02-18.md) - Design system audit

### External

- [Tailwind CSS](https://tailwindcss.com/docs) - Utility-first CSS
- [Lucide Icons](https://lucide.dev/) - Icon library
- [IBM Plex Sans](https://fonts.google.com/specimen/IBM+Plex+Sans) - Typography
- [WCAG Guidelines](https://www.w3.org/WAI/WCAG21/quickref/) - Accessibility

---

## Changelog

### Version 2.0 (February 18, 2026)
- Complete rewrite with Linear-inspired design
- Consolidated from multiple design documents
- Added comprehensive component library
- Added performance and accessibility guidelines
- Removed all dummy data references

### Version 1.0 (Previous)
- Initial design system (deprecated)

---

**Design System Version**: 2.0
**Last Updated**: February 18, 2026
**Maintained By**: SmartTrader Development Team


## Performance

### Bundle Size

Target: < 200KB First Load JS per page

#### Optimization Strategies

1. **Code Splitting**
   ```tsx
   const LazyChart = dynamic(() => import('@/components/charts/Chart'), {
     loading: () => <Skeleton />,
     ssr: false
   });
   ```

2. **Tree Shaking**
   ```tsx
   // Good: Import only what you need
   import { useState, useEffect } from 'react';

   // Bad: Import everything
   import * as React from 'react';
   ```

3. **Lazy Loading**
   ```tsx
   // Load heavy components only when needed
   const HeavyComponent = lazy(() => import('./HeavyComponent'));
   ```

4. **Font Optimization**
   - Use `font-display: swap` for custom fonts
   - Preload critical fonts
   - Subset fonts to reduce size

### Runtime Performance

Target: 60fps for all interactions

#### Optimization Strategies

1. **Memoization**
   ```tsx
   const expensiveValue = useMemo(() => {
     return computeExpensiveValue(data);
   }, [data]);
   ```

2. **Debouncing**
   ```tsx
   const debouncedSearch = useMemo(
     () => debounce((query) => search(query), 300),
     []
   );
   ```

3. **Virtual Scrolling**
   - Use for lists > 100 items
   - Consider `react-window` or `react-virtual`

4. **Efficient Re-renders**
   - Use `React.memo` for expensive components
   - Avoid inline object/array creation in props
   - Use callback refs instead of inline functions

### Load Times

Target metrics:
- First Contentful Paint: < 1.5s
- Time to Interactive: < 3s
- Largest Contentful Paint: < 2.5s

#### Monitoring

```bash
# Check bundle size
npm run build
node scripts/check-bundle-size.js
```

---

## Accessibility

### Keyboard Navigation

All interactive elements must be keyboard accessible.

#### Requirements

- **Tab Order**: Logical and predictable
- **Focus Indicators**: Visible on all interactive elements
- **Escape Key**: Closes modals and dropdowns
- **Enter/Space**: Activates buttons and links
- **Arrow Keys**: Navigate lists and menus

#### Implementation

```tsx
// Focus indicator (built into Button component)
<Button>Accessible Button</Button>

// Custom keyboard handler
<div
  role="button"
  tabIndex={0}
  onKeyDown={(e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      handleClick();
    }
  }}
>
  Clickable Div
</div>
```

### Screen Readers

Use semantic HTML and ARIA labels.

#### Requirements

- **Semantic HTML**: Use proper elements (`<button>`, `<nav>`, `<main>`)
- **ARIA Labels**: Add labels for icon-only buttons
- **Alt Text**: Provide for all images
- **Descriptive Links**: Avoid "click here"

#### Implementation

```tsx
// Icon button with label
<Button aria-label="Close modal">
  <X className="w-4 h-4" />
</Button>

// Image with alt text
<img src="/logo.png" alt="SmartTrader logo" />

// Descriptive link
<a href="/screener">View stock screener</a>
```

### Color Contrast

Ensure sufficient contrast for readability.

#### Requirements

- **Text**: Minimum 4.5:1 contrast ratio (WCAG AA)
- **Interactive Elements**: Minimum 3:1 contrast ratio
- **Test**: Use color blindness simulators

#### Verification

All color tokens in the unified design system meet WCAG AA standards:

- Light mode: Dark text on light backgrounds
- Dark mode: Light text on dark backgrounds
- Financial colors: Vibrant enough for visibility

#### Tools

- Chrome DevTools: Lighthouse audit
- WebAIM Contrast Checker
- axe DevTools extension

### Focus Management

Proper focus management for modals and dynamic content.

```tsx
// Trap focus in modal
import { FocusTrap } from '@/components/ui/focus-trap';

<FocusTrap>
  <Modal>
    <ModalContent />
  </Modal>
</FocusTrap>
```

---

## Visual Examples

### Dark Theme Showcase

The Linear-inspired dark theme features:

- **Deep Black Backgrounds**: `#0a0a0a` for primary surface
- **Subtle Gradients**: Layered backgrounds for depth
- **Refined Grays**: `#fafafa` to `#444444` for text hierarchy
- **Vibrant Financial Colors**:
  - Profit: `#00d084` (vibrant green)
  - Loss: `#ff4757` (vibrant red)
  - Warning: `#ffa502` (vibrant orange)

### Component Variants

See the component library section for visual examples of:
- Button variants (primary, secondary, ghost, profit, loss, outline, link, destructive)
- Card variants (default, glass, elevated, outline, void, flat)
- Badge variants (default, secondary, outline, profit, loss, warning, neutral, live, muted)
- Input states (normal, focus, error)

### Color Palette

#### Light Mode
- Background: White to light gray progression
- Text: Dark slate to medium gray
- Borders: Light gray with subtle variations

#### Dark Mode
- Background: Deep black to dark gray progression
- Text: Near-white to medium gray
- Borders: Dark gray with subtle variations

---

## Resources

### Internal Documentation

- [Architecture](ARCHITECTURE.md) - System architecture
- [Data Flow](DATA_ARCHITECTURE.md) - Data architecture
- [Code Quality](CODE_QUALITY.md) - Quality checks and standards
- [API Reference](FYERS_API_REFERENCE.md) - Fyers API docs

### External Resources

- [Tailwind CSS](https://tailwindcss.com/docs) - Utility-first CSS framework
- [Lucide Icons](https://lucide.dev/) - Icon library
- [IBM Plex Sans](https://fonts.google.com/specimen/IBM+Plex+Sans) - UI typography
- [DM Mono](https://fonts.google.com/specimen/DM+Mono) - Code/data typography
- [WCAG Guidelines](https://www.w3.org/WAI/WCAG21/quickref/) - Accessibility standards
- [Linear Design](https://linear.app/) - Design inspiration

### Design Tokens Reference

- Token definitions: `frontend/app/globals.css`
- TypeScript types: `frontend/lib/design-tokens.ts`
- Migration mapping: `frontend/lib/migration-map.ts`

---

## Changelog

### Version 3.0 (February 18, 2026)
- **BREAKING**: Replaced Raycast variables with unified token system
- Added Linear-inspired dark theme
- Implemented modern typography with IBM Plex Sans and DM Mono
- Created comprehensive component library documentation
- Added token-based architecture explanation
- Added migration guide from Raycast variables
- Added visual examples and color palette documentation
- Updated all code examples to use unified tokens

### Version 2.0 (Previous)
- Initial consolidated design system
- Raycast-based variables (deprecated)

### Version 1.0 (Legacy)
- Original design system (deprecated)

---

## Maintenance

### Updating Design Tokens

To update design tokens:

1. Edit `frontend/app/globals.css`
2. Update both `:root` (light mode) and `.dark` (dark mode)
3. Update TypeScript types in `frontend/lib/design-tokens.ts`
4. Test in both themes
5. Update documentation if adding new tokens

### Adding New Components

To add new components:

1. Create component in `frontend/components/ui/`
2. Use unified design tokens exclusively
3. Add TypeScript types
4. Document all variants and props
5. Add usage examples to this document
6. Test accessibility (keyboard, screen reader, contrast)

### Reporting Issues

If you find inconsistencies or issues:

1. Check if it's documented in this guide
2. Verify against the actual implementation
3. Report in the project issue tracker
4. Include screenshots and code examples

---

**Design System Version**: 3.0 - Unified Token System
**Last Updated**: February 18, 2026
**Maintained By**: SmartTrader Development Team
