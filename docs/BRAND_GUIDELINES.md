# SmarTrader Design System & Brand Guidelines

## 1. Brand Identity
**Name**: SmarTrader
**Tagline**: "AI-Powered Autonomous Trading"
**Core Concept**: A futuristic, agent-driven terminal ("Cyberpunk Financial").
**Logo**:
- **Monogram**: Stylized "ST" in a cyber-chip container.
- **Badge**: "AI NATIVE" with a pulsing status dot.

## 2. Color Palette ("Neon Obsidian")

### Backgrounds
- **Deep Space**: `#050505` (Main Background)
- **Obsidian**: `#0A0A0A` (Panel Backgrounds)
- **Glass**: `bg-white/5` with `backdrop-blur-md` (Overlays)

### Accents (Neon Gradient)
- **Cyan** (Primary): `from-cyan-500` to `text-cyan-400`
- **Purple** (Secondary): `to-purple-600` or `text-purple-400`
- **Gradient**: `bg-gradient-to-tr from-cyan-500 to-purple-600`

### Text
- **Primary**: `text-white` (High Emphasis)
- **Secondary**: `text-gray-400` (Labels)
- **Tertiary**: `text-gray-600` (Subtitles/Meta)

## 3. Layout Philosophy

### "Dense & Information Rich"
- **Row Height**: Standardize data tables to **32px** rows (Compact).
- **Padding**: Minimize padding (e.g., `p-2` or `px-4 py-2`).
- **Font Size**: Use `text-xs` (12px) for data and `text-sm` (14px) for controls.

### "Glass Architecture"
- **Panels**: All containers should have `border border-white/5` and `rounded-xl`.
- **Z-Index**: Use layering explicitly. Floating elements (Command Palette, AI) sit on top with higher blur.

## 4. Typography
- **UI/Body**: `Inter` (Clean, legible).
- **Numbers/Code**: `JetBrains Mono` (Monospaced, precise).

## 5. Agent Personas (Tabs)
Each tab represents an Autonomous Agent:
1.  **SCREENER**: The Hunter (Data rich, tabular).
2.  **ANALYST**: The Brain (Visual, dashboard, metrics).
3.  **TESTER**: The Scientist (Simulation parameters, logs).
4.  **TRADER**: The Executioner (Live pulse, swift actions).

## 6. Do's and Don'ts
- **DO**: Use gradients sparingly (borders, logos, active states).
- **DO**: Align all headers to the "Command Bar" aesthetic.
- **DON'T**: Use solid white backgrounds or heavy shadows.
- **DON'T**: Create large, sparse cards (waste of screen real estate).
