# Spacing, Radius, and Shadow Tokens

This document describes the spacing, border radius, and shadow design tokens used throughout SmartTrader.

## Spacing Scale

All spacing tokens follow a **4px base unit** for consistent spacing throughout the application.

### Token Reference

| Token | Value | Multiplier | Usage |
|-------|-------|------------|-------|
| `--spacing-1` | 4px | 1× | Minimal spacing, tight layouts |
| `--spacing-2` | 8px | 2× | Small gaps, compact components |
| `--spacing-3` | 12px | 3× | Default component padding |
| `--spacing-4` | 16px | 4× | Standard spacing, card padding |
| `--spacing-5` | 20px | 5× | Medium spacing |
| `--spacing-6` | 24px | 6× | Large spacing, section gaps |
| `--spacing-8` | 32px | 8× | Extra large spacing |
| `--spacing-10` | 40px | 10× | Page margins |
| `--spacing-12` | 48px | 12× | Major section separation |

### Usage Examples

**CSS Variables:**
```css
.component {
  padding: var(--spacing-4);
  margin-bottom: var(--spacing-6);
  gap: var(--spacing-2);
}
```

**Tailwind Classes:**
```tsx
<div className="p-4 mb-6 gap-2">
  {/* Uses spacing-4, spacing-6, spacing-2 */}
</div>
```

### Design Principles

1. **Consistency**: Always use spacing tokens, never arbitrary values
2. **Rhythm**: The 4px base creates visual harmony across the UI
3. **Flexibility**: The scale provides enough options without overwhelming choice
4. **Predictability**: Developers can quickly learn the scale pattern

---

## Border Radius

Border radius tokens define the roundness of component corners, creating visual hierarchy and style consistency.

### Token Reference

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-sm` | 4px | Small elements (badges, tags) |
| `--radius-md` | 6px | Default components (buttons, inputs) |
| `--radius-lg` | 8px | Cards, panels |
| `--radius-xl` | 12px | Large surfaces, modals |
| `--radius-full` | 9999px | Pills, circular buttons |

### Usage Examples

**CSS Variables:**
```css
.button {
  border-radius: var(--radius-md);
}

.card {
  border-radius: var(--radius-lg);
}

.badge {
  border-radius: var(--radius-full);
}
```

**Tailwind Classes:**
```tsx
<button className="rounded-md">Button</button>
<div className="rounded-lg">Card</div>
<span className="rounded-full">Badge</span>
```

### Design Principles

1. **Subtle**: Radius values are conservative for a professional financial app
2. **Hierarchy**: Larger surfaces use larger radius values
3. **Consistency**: All components use the same radius scale
4. **Modern**: Rounded corners feel contemporary without being playful

---

## Shadows

Shadow tokens create depth and elevation in the UI. Different values are used for light and dark modes to maintain appropriate contrast.

### Token Reference

| Token | Light Mode | Dark Mode | Usage |
|-------|------------|-----------|-------|
| `--shadow-sm` | `0 1px 2px 0 rgba(0,0,0,0.05)` | `0 1px 2px 0 rgba(0,0,0,0.3)` | Subtle elevation (inputs, badges) |
| `--shadow-md` | `0 4px 6px -1px rgba(0,0,0,0.1)` | `0 4px 6px -1px rgba(0,0,0,0.4)` | Standard elevation (cards, buttons) |
| `--shadow-lg` | `0 10px 15px -3px rgba(0,0,0,0.1)` | `0 10px 15px -3px rgba(0,0,0,0.5)` | High elevation (dropdowns, popovers) |
| `--shadow-xl` | `0 20px 25px -5px rgba(0,0,0,0.1)` | `0 20px 25px -5px rgba(0,0,0,0.6)` | Maximum elevation (modals, dialogs) |

### Usage Examples

**CSS Variables:**
```css
.card {
  box-shadow: var(--shadow-md);
}

.modal {
  box-shadow: var(--shadow-xl);
}

.dropdown {
  box-shadow: var(--shadow-lg);
}
```

**Tailwind Classes:**
```tsx
<div className="shadow-md">Card</div>
<div className="shadow-xl">Modal</div>
<div className="shadow-lg">Dropdown</div>
```

### Design Principles

1. **Depth**: Shadows create visual hierarchy through elevation
2. **Subtlety**: Light mode uses subtle shadows to avoid harshness
3. **Contrast**: Dark mode uses stronger shadows for better separation
4. **Consistency**: All elevated surfaces use the shadow scale

### Dark Mode Considerations

Dark mode shadows are **more pronounced** (higher opacity) because:
- Dark backgrounds need stronger shadows for contrast
- Subtle shadows disappear against dark surfaces
- Stronger shadows maintain visual hierarchy

---

## Tailwind Integration

All tokens are mapped to Tailwind classes via the `@theme` directive in `globals.css`:

```css
@theme {
  /* Spacing */
  --spacing-1: var(--spacing-1);
  --spacing-2: var(--spacing-2);
  /* ... etc */

  /* Radius */
  --radius-sm: var(--radius-sm);
  --radius-md: var(--radius-md);
  /* ... etc */

  /* Shadows */
  --shadow-sm: var(--shadow-sm);
  --shadow-md: var(--shadow-md);
  /* ... etc */
}
```

This allows using tokens as Tailwind utilities:
- Spacing: `p-4`, `m-6`, `gap-2`
- Radius: `rounded-sm`, `rounded-md`, `rounded-lg`
- Shadows: `shadow-sm`, `shadow-md`, `shadow-lg`

---

## Validation

Run the verification script to ensure all tokens are correctly defined:

```bash
cd frontend
node scripts/verify-spacing-radius-shadow.js
```

This validates:
- ✓ Spacing scale uses 4px multiples
- ✓ All radius tokens are defined
- ✓ Shadow tokens exist for both light and dark modes
- ✓ All tokens are mapped in @theme directive

---

## Migration Guide

### From Hardcoded Values

**Before:**
```css
.component {
  padding: 16px;
  border-radius: 8px;
  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
}
```

**After:**
```css
.component {
  padding: var(--spacing-4);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
}
```

### From Raycast Variables

**Before:**
```css
.component {
  padding: var(--raycast-spacing-md);
  border-radius: var(--raycast-radius);
  box-shadow: var(--raycast-shadow-xl);
}
```

**After:**
```css
.component {
  padding: var(--spacing-4);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xl);
}
```

---

## Best Practices

### DO ✅

- Use spacing tokens for all padding, margin, and gap values
- Use radius tokens for all border-radius values
- Use shadow tokens for all box-shadow values
- Use Tailwind classes when possible for consistency
- Test components in both light and dark modes

### DON'T ❌

- Don't use arbitrary values like `p-[16px]`
- Don't hardcode pixel values in CSS
- Don't create custom shadow values
- Don't mix spacing systems (tokens + hardcoded)
- Don't use Raycast variables

---

## Related Documentation

- [Design System Overview](../docs/DESIGN_SYSTEM.md)
- [Typography Tokens](./TYPOGRAPHY_IMPLEMENTATION.md)
- [Color Tokens](../docs/DESIGN_SYSTEM.md#color-tokens)
- [Component Library](../components/ui/README.md)

---

**Validates Requirements:** 2.3, 2.4, 2.5
