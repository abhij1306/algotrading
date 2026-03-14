# SmartTrader Design System

**Status:** Canonical
**Source of truth:** `frontend/app/globals.css`
**Last updated:** 2026-02-19

## Scope
This file is the single design-system reference for colors, typography, spacing, radius, shadows, and usage rules.

## Token Source
- All runtime token values are defined in `frontend/app/globals.css` (`:root`, `.dark`, `@theme`).
- If code changes token values/names, update this doc in the same change.

## Colors
### Light
- `--color-background`: `#ffffff`
- `--color-background-secondary`: `#f8fafc`
- `--color-background-tertiary`: `#f1f5f9`
- `--color-surface`: `#ffffff`
- `--color-elevated`: `#ffffff`
- `--color-foreground`: `rgba(15, 23, 42, 0.8)`
- `--color-foreground-secondary`: `#334155`
- `--color-foreground-tertiary`: `#64748b`
- `--color-foreground-muted`: `#94a3b8`
- `--color-border`: `#e2e8f0`
- `--color-border-subtle`: `#f1f5f9`
- `--color-border-focus`: `#3b82f6`
- `--color-primary`: `#2563eb`
- `--color-primary-hover`: `#1d4ed8`
- `--color-primary-light`: `#eff6ff`
- `--color-primary-foreground`: `#ffffff`
- `--color-profit`: `#15803d`
- `--color-profit-bg`: `#dcfce7`
- `--color-loss`: `#b91c1c`
- `--color-loss-bg`: `#fee2e2`
- `--color-warning`: `#b45309`
- `--color-warning-bg`: `#fef3c7`

### Dark
- `--color-background`: `#0a0a0a`
- `--color-background-secondary`: `#141414`
- `--color-background-tertiary`: `#1c1c1c`
- `--color-surface`: `#141414`
- `--color-elevated`: `#1f1f1f`
- `--color-foreground`: `rgba(250, 250, 250, 0.8)`
- `--color-foreground-secondary`: `#a0a0a0`
- `--color-foreground-tertiary`: `#8a8a8a`
- `--color-foreground-muted`: `#444444`
- `--color-border`: `#2a2a2a`
- `--color-border-subtle`: `#1f1f1f`
- `--color-border-focus`: `#3b82f6`
- `--color-primary`: `#60a5fa`
- `--color-primary-hover`: `#3b82f6`
- `--color-primary-light`: `rgba(59, 130, 246, 0.15)`
- `--color-primary-foreground`: `#0a0a0a`
- `--color-profit`: `#22c55e`
- `--color-profit-bg`: `rgba(34, 197, 94, 0.15)`
- `--color-loss`: `#ef4444`
- `--color-loss-bg`: `rgba(239, 68, 68, 0.15)`
- `--color-warning`: `#f59e0b`
- `--color-warning-bg`: `rgba(245, 158, 11, 0.15)`

## Typography
- `--font-sans`: `var(--font-sans), "IBM Plex Sans", system-ui, -apple-system, sans-serif`
- `--font-mono`: `var(--font-mono), "DM Mono", "Consolas", monospace`
- Sizes:
  - `--text-xxs`: `10px`
  - `--text-xs`: `11px`
  - `--text-sm`: `13px`
  - `--text-base`: `15px`
  - `--text-lg`: `17px`
  - `--text-xl`: `20px`
  - `--text-2xl`: `24px`
- Line heights:
  - `--leading-tight`: `1.2`
  - `--leading-normal`: `1.5`
  - `--leading-relaxed`: `1.75`
- Weights: `400`, `500`, `600`, `700`
- Financial numeric class: `.mono-num` (tabular numbers + mono rendering)

## Spacing
- `--spacing-1`: `4px`
- `--spacing-2`: `8px`
- `--spacing-3`: `12px`
- `--spacing-4`: `16px`
- `--spacing-5`: `20px`
- `--spacing-6`: `24px`
- `--spacing-8`: `32px`
- `--spacing-10`: `40px`
- `--spacing-12`: `48px`

## Radius
- `--radius-sm`: `4px`
- `--radius-md`: `6px`
- `--radius-lg`: `8px`
- `--radius-xl`: `12px`
- `--radius-full`: `9999px`

## Shadows
### Light
- `--shadow-sm`: `0 1px 3px 0 rgba(0, 0, 0, 0.08), 0 1px 2px 0 rgba(0, 0, 0, 0.04)`
- `--shadow-md`: `0 4px 6px -1px rgba(0, 0, 0, 0.08), 0 2px 4px -1px rgba(0, 0, 0, 0.04)`
- `--shadow-lg`: `0 10px 15px -3px rgba(0, 0, 0, 0.08), 0 4px 6px -2px rgba(0, 0, 0, 0.04)`
- `--shadow-xl`: `0 20px 25px -5px rgba(0, 0, 0, 0.08), 0 10px 10px -5px rgba(0, 0, 0, 0.04)`

### Dark
- `--shadow-sm`: `0 1px 2px 0 rgba(0, 0, 0, 0.3)`
- `--shadow-md`: `0 4px 6px -1px rgba(0, 0, 0, 0.4)`
- `--shadow-lg`: `0 10px 15px -3px rgba(0, 0, 0, 0.5)`
- `--shadow-xl`: `0 20px 25px -5px rgba(0, 0, 0, 0.6)`

## Implementation Rules
1. Use token-backed classes/utilities only; no hardcoded colors/sizes in app UI.
2. For numbers/prices/P&L, use centralized formatting utils and `.mono-num`/tabular numeric styles.
3. Keep semantic classes (`text-profit`, `text-loss`, etc.) mapped to tokens.
4. Any token change must include:
   - update in `frontend/app/globals.css`
   - update in this file
   - UI smoke check on Dashboard, Screener, Terminal.
5. Shared components must not introduce hardcoded palette drift such as raw green/red/gray utility colors or non-token brand gradients.
