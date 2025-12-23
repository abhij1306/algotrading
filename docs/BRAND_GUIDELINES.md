# SmarTrader Design System & Brand Guidelines ("QuantOS Glass")

## 1. Core Philosophy: "Neon Obsidian"
**Concept**: A futuristic, agent-driven terminal ("Cyberpunk Financial").
**Values**: Dense, Information-Rich, Keyboard-First, Glass Architecture.

## 2. Interaction Model ("Raycast-Style")
1.  **Keyboard First**: The Command Palette (`Ctrl+K`) is the OS nucleus.
2.  **Modal Discipline**: Actions open in centered glass modals, preserving context.
3.  **Dense Lists**: Data tables resemble the Command Palette—compact, high-contrast, hover states (`bg-white/5`).
4.  **Z-Index Hierarchy**: Strict layering to prevent "UI Drift".

## 3. The Strict Z-Index Scale
Stop using arbitrary values. Use ONLY these levels:

| Level | Z-Index | Usage | Tailwwind Variable |
| :--- | :--- | :--- | :--- |
| **Level 5** | `z-100` | **Toasts/Notifications** | `--z-toast` |
| **Level 4** | `z-50` | **Overlays (Command Palette, Modals)** | `--z-overlay` |
| **Level 3** | `z-40` | **Dropdowns (Select, Autocomplete)** | `--z-dropdown` |
| **Level 2** | `z-10` | **Sticky Headers, Sidebar** | `--z-sticky` |
| **Level 1** | `z-0` | **Content (Cards, Charts)** | `--z-content` |
| **Level 0** | `Auto` | **Background** | `--z-base` |

## 4. Typography Scale
Use `Inter` for UI, `JetBrains Mono` for Data.

- **H1 (Page Title)**: `text-2xl font-bold tracking-tight text-white`
- **H2 (Section Header)**: `text-lg font-semibold text-white`
- **H3 (Card Header)**: `text-sm font-medium text-gray-400 uppercase tracking-wider`
- **Body**: `text-sm (14px) text-gray-300`
- **Data/Mono**: `text-xs (12px) font-mono text-cyan-400`

## 5. Component Standards
Refactor raw divs into these containers:

- **`<GlassCard>`**: `border border-white/5 bg-gradient-to-br from-white/5 to-transparent rounded-xl`
- **`<PageContainer>`**: `max-w-7xl mx-auto px-4 py-6`
- **`<MetricBadge>`**: `bg-cyan-500/10 text-cyan-400 px-2 py-0.5 rounded-full text-xs`

## 6. Color Palette
- **Background**: `#050505` (Deep Space)
- **Panel**: `#0A0A0A` (Obsidian)
- **Glass**: `bg-white/5 backdrop-blur-md`
- **Accents**: Cyan (`#06B6D4`), Purple (`#8B5CF6`)
