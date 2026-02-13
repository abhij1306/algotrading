# Design System Standardization & Technical Debt Audit

**Change Intent:** Standardize the frontend design system, consolidate redundant components, and remove technical debt (hardcoded colors, raw HTML elements, direct fetch calls).
**Impact Scope:** Global (Frontend UI/UX, Component Library, Data Fetching).

---

### [Severity: Medium] Component Redundancy & Fragmentation
**Location:** frontend/components/ui/GlassCard.tsx, frontend/components/ui/card.tsx

**Category:** Architecture | Maintainability

**Problem**
The system had multiple ways to create "glass" cards: a standalone `GlassCard` component, a `variant="glass"` in the `Card` component, and a `.card` class in `globals.css`. This led to inconsistent styling and maintenance overhead.

**Impact**
Inconsistent UI behavior (hover effects, blur levels) and confusing developer experience.

**Recommendation**
Unify the styling into a single `.glass-card` (and `.card`) CSS class in `globals.css` and refactor the React components to use this class.

---

### [Severity: Medium] Inconsistent Color Tokens & Hardcoded Values
**Location:** frontend/components/ui/button.tsx, frontend/components/ui/GlassSelect.tsx

**Category:** Maintainability | Architecture

**Problem**
UI components were using hardcoded hex values (e.g., `#2563EB`) or ad-hoc Tailwind colors (e.g., `cyan-500`) instead of the centralized design tokens (`var(--color-primary)`, etc.).

**Impact**
Theme changes (e.g., changing the primary brand color) would require manual searching and replacing across multiple files.

**Recommendation**
Replace all hardcoded colors with CSS variables. Use `color-mix` for hover states as defined in `globals.css`.

---

### [Severity: Low] Underutilization of UI Library
**Location:** frontend/app/dashboard/page.tsx, frontend/app/screener/page.tsx, frontend/components/Terminal.tsx

**Category:** Maintainability

**Problem**
Main application pages were using raw HTML tags (`<table>`, `<input>`, `<select>`) and Tailwind utility classes instead of the standardized UI components (`Table`, `Input`, `GlassSelect`).

**Impact**
Inconsistent spacing, typography, and interactive states across different pages. Increased code duplication.

**Recommendation**
Refactor pages to use the standardized UI component library.

---

### [Severity: Low] Data Fetching Technical Debt
**Location:** frontend/app/dashboard/page.tsx, frontend/components/Terminal.tsx

**Category:** Architecture | Maintainability

**Problem**
Pages were using raw `fetch` calls with hardcoded local URLs or manually constructed query strings instead of the centralized `api-client.ts`.

**Impact**
Fragile error handling, lack of global loading states, and potential CORS/environment issues.

**Recommendation**
Use the `apiClient` singleton for all backend interactions.

---

### 👍 Strength
The `globals.css` file provides a robust set of design tokens and base styles that follow a clean, modern financial UI aesthetic. The centralized `api-client.ts` is well-structured with support for retries and unified error handling.

---

## Review Summary

### Risk Profile
- Security: Low
- Stability: Medium
- Maintainability: High (Improved)
- Scalability: High

### Findings
- High: 0
- Medium: 2
- Low: 2

### Assessment
The frontend codebase had significant fragmentation in its UI implementation. By unifying the component library and enforcing the use of design tokens, we have significantly improved the maintainability and visual consistency of the system. The refactoring of `Terminal.tsx` also removed significant logic duplication and potential bugs.

### Recommendation
✅ Approve
