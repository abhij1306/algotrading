# Trading Terminal Design System

## Overview

This design system provides a comprehensive, consistent foundation for building trading terminal interfaces. It follows modern design principles inspired by Bloomberg Terminal, TradingView, and Raycast.

## Design Principles

### 1. High-Density Data Display
- Optimize for information density without sacrificing readability
- Use compact spacing for data tables and lists
- Support dense, compact, and normal row heights

### 2. Financial-First Color System
- Clear profit/loss distinction with accessible colors
- Consistent semantic colors across all components
- Dark theme optimized for extended viewing

### 3. Glass Morphism
- Subtle transparency and blur effects for depth
- Clear visual hierarchy through layering
- Reduced eye strain with soft borders

### 4. Performance
- Minimal runtime overhead
- CSS-first animations
- Optimized for real-time data updates

---

## Folder Structure

```
frontend/
├── app/                      # Next.js App Router pages
│   ├── globals.css           # Global styles + design tokens
│   ├── layout.tsx            # Root layout
│   └── [route]/              # Route-specific pages
│
├── components/
│   ├── ui/                   # Base UI components (design system)
│   │   ├── index.ts          # Central export
│   │   ├── button.tsx        # Button component
│   │   ├── card.tsx          # Card components
│   │   ├── badge.tsx         # Badge/Status components
│   │   ├── input.tsx         # Input components
│   │   ├── table.tsx         # Table components
│   │   ├── tabs.tsx          # Tabs navigation
│   │   ├── slider.tsx        # Slider component
│   │   ├── scroll-area.tsx   # Scroll area component
│   │   ├── label.tsx         # Form label
│   │   ├── textarea.tsx      # Textarea component
│   │   ├── price.tsx         # Price display components
│   │   ├── skeleton.tsx      # Loading skeletons
│   │   └── tooltip.tsx       # Tooltip component
│   │
│   ├── charts/               # Chart components
│   ├── layout/               # Layout components (Sidebar, Header)
│   ├── trading/              # Trading-specific components
│   └── shared/               # Shared business components
│
├── lib/
│   └── utils.ts              # Utility functions (cn, formatters)
│
├── hooks/                    # Custom React hooks
├── utils/                    # Utility modules
└── public/                   # Static assets
```

---

## Design Tokens

### Colors

#### Base Surfaces (Dark Theme)
```css
--color-void: #000000;        /* Deepest background */
--color-base: #0A0A0B;        /* Page background */
--color-surface: #111113;     /* Card/panel background */
--color-elevated: #18181B;    /* Hover/active states */
--color-overlay: #1F1F23;     /* Modal/overlay background */
```

#### Financial Colors
```css
/* Profit (Green) */
--color-profit: #22C55E;
--color-profit-bright: #4ADE80;
--color-profit-muted: #16A34A;
--color-profit-bg: rgba(34, 197, 94, 0.1);

/* Loss (Red) */
--color-loss: #EF4444;
--color-loss-bright: #F87171;
--color-loss-muted: #DC2626;
--color-loss-bg: rgba(239, 68, 68, 0.1);
```

#### Accent Colors
```css
--color-primary: #3B82F6;     /* Blue - Primary actions */
--color-secondary: #8B5CF6;   /* Purple - Secondary */
--color-accent: #06B6D4;      /* Cyan - Accent */
--color-warning: #F59E0B;     /* Amber - Warning */
```

#### Text Colors
```css
--text-primary: #FAFAFA;      /* Primary text */
--text-secondary: #A1A1AA;    /* Secondary text */
--text-tertiary: #71717A;     /* Tertiary text */
--text-muted: #52525B;        /* Muted/disabled text */
```

### Typography

```css
--font-sans: "Inter", -apple-system, BlinkMacSystemFont, sans-serif;
--font-mono: "JetBrains Mono", "Fira Code", monospace;
```

### Spacing

```css
--space-1: 0.25rem;   /* 4px */
--space-2: 0.5rem;    /* 8px */
--space-3: 0.75rem;   /* 12px */
--space-4: 1rem;      /* 16px */
--space-6: 1.5rem;    /* 24px */
--space-8: 2rem;      /* 32px */
```

### Border Radius

```css
--radius-sm: 0.25rem;   /* 4px */
--radius-md: 0.375rem;  /* 6px */
--radius-lg: 0.5rem;    /* 8px */
--radius-xl: 0.75rem;   /* 12px */
--radius-2xl: 1rem;     /* 16px */
```

### Z-Index Scale

```css
--z-base: 0;
--z-dropdown: 10;
--z-sticky: 20;
--z-fixed: 30;
--z-modal-backdrop: 40;
--z-modal: 50;
--z-popover: 60;
--z-tooltip: 70;
--z-toast: 80;
```

---

## Components

### Button

```tsx
import { Button } from '@/components/ui';

// Variants
<Button variant="default">Primary</Button>
<Button variant="secondary">Secondary</Button>
<Button variant="ghost">Ghost</Button>
<Button variant="outline">Outline</Button>
<Button variant="profit">Buy</Button>
<Button variant="loss">Sell</Button>
<Button variant="glass">Glass</Button>

// Sizes
<Button size="xs">Extra Small</Button>
<Button size="sm">Small</Button>
<Button size="default">Default</Button>
<Button size="lg">Large</Button>

// States
<Button loading>Loading</Button>
<Button disabled>Disabled</Button>
```

### Badge

```tsx
import { Badge, StatusBadge, ChangeBadge } from '@/components/ui';

// Variants
<Badge variant="default">Default</Badge>
<Badge variant="profit">+2.5%</Badge>
<Badge variant="loss">-1.2%</Badge>
<Badge variant="warning">Warning</Badge>

// Status Badge
<StatusBadge status="online" />
<StatusBadge status="offline" />
<StatusBadge status="loading" />

// Change Badge (for price changes)
<ChangeBadge value={2.5} />
<ChangeBadge value={-1.2} suffix="today" />
```

### Card

```tsx
import { Card, MetricCard } from '@/components/ui';

// Variants
<Card variant="default">Default card</Card>
<Card variant="glass">Glass card</Card>
<Card variant="elevated">Elevated card</Card>

// Metric Card
<MetricCard
  title="Total P&L"
  value="$12,450"
  change={2.5}
  changeLabel="today"
  variant="profit"
/>
```

### Price

```tsx
import { Price, PriceChange, TickerPrice } from '@/components/ui';

// Basic price
<Price value={1234.56} />
<Price value={1234.56} format="currency" />
<Price value={1234567} format="compact" />

// Price with change
<PriceChange
  value={1234.56}
  change={12.34}
  changePercent={1.01}
/>

// Ticker display
<TickerPrice
  symbol="RELIANCE"
  price={2456.75}
  change={23.45}
  changePercent={0.96}
/>
```

### Table

```tsx
import { 
  Table, TableHeader, TableBody, TableRow, 
  TableHead, TableCell 
} from '@/components/ui';

<Table>
  <TableHeader>
    <TableRow>
      <TableHead>Symbol</TableHead>
      <TableHead numeric>Price</TableHead>
      <TableHead numeric>Change</TableHead>
    </TableRow>
  </TableHeader>
  <TableBody>
    <TableRow>
      <TableCell>RELIANCE</TableCell>
      <TableCell numeric>2456.75</TableCell>
      <TableCell numeric profit>+1.2%</TableCell>
    </TableRow>
  </TableBody>
</Table>
```

---

## Utility Classes

### Glass Effects

```css
.glass           /* Standard glass panel */
.glass-strong    /* Strong glass (modals) */
.glass-subtle    /* Subtle glass (headers) */
.glass-card      /* Glass card with hover */
```

### Financial

```css
.price           /* Monospace price display */
.price-profit    /* Green price */
.price-loss      /* Red price */
.glow-profit     /* Green glow effect */
.glow-loss       /* Red glow effect */
.bg-profit       /* Profit background tint */
.bg-loss         /* Loss background tint */
```

### Tables

```css
.row-dense       /* 32px row height */
.row-compact     /* 40px row height */
.row-normal      /* 48px row height */
.row-ghost       /* Minimal row styling */
.row-active      /* Active/selected row */
```

### Animations

```css
.animate-fade-in
.animate-slide-in
.animate-slide-up
.animate-pulse
.animate-shimmer
```

---

## Best Practices

### 1. Use Design Tokens
Always use CSS variables instead of hardcoded values:

```css
/* ❌ Bad */
color: #22C55E;

/* ✅ Good */
color: var(--color-profit);
```

### 2. Consistent Component Imports
Import from the central index:

```tsx
// ❌ Bad
import { Button } from '@/components/ui/button';

// ✅ Good
import { Button, Card, Badge } from '@/components/ui';
```

### 3. Use Utility Functions
Use the provided utility functions for formatting:

```tsx
import { cn, formatPrice, formatPercent } from '@/lib/utils';

<div className={cn('base-class', condition && 'conditional-class')} />
<span>{formatPrice(1234.56)}</span>
```

### 4. Semantic HTML
Use appropriate HTML elements:

```tsx
// ❌ Bad
<div onClick={handleClick}>Click me</div>

// ✅ Good
<Button onClick={handleClick}>Click me</Button>
```

### 5. Accessibility
Always include proper ARIA attributes:

```tsx
<Button aria-label="Close dialog">
  <XIcon />
</Button>
```

---

## Migration Guide

### From Old Components

1. Replace `@/components/ui/Button` with `@/components/ui` (lowercase)
2. Replace `GlassCard` with `<Card variant="glass">`
3. Replace `MetricBadge` with `MetricCard`
4. Replace hardcoded colors with CSS variables

### File Naming Convention

All component files should use lowercase with hyphens:
- ✅ `button.tsx`, `card.tsx`, `scroll-area.tsx`
- ❌ `Button.tsx`, `Card.tsx`, `ScrollArea.tsx`

---

## Version History

- **v2.0.0** - Complete redesign with Tailwind v4, React 19, Next.js 16
- **v1.0.0** - Initial design system
