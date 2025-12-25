import type { Config } from 'tailwindcss'

const config: Config = {
    content: [
        './pages/**/*.{js,ts,jsx,tsx,mdx}',
        './components/**/*.{js,ts,jsx,tsx,mdx}',
        './app/**/*.{js,ts,jsx,tsx,mdx}',
    ],
    theme: {
        extend: {
            // ============================================================
            // OBSIDIAN COLOR SYSTEM (Raycast-Inspired)
            // ============================================================
            colors: {
                // Base Layers (The Void)
                'obsidian': {
                    'obsidian': {
                        DEFAULT: '#050505',      // Deepest Void - Base Layer
                        surface: '#0E1116',      // OCSWAP Style Lighter Surface
                        elevated: '#16191F',     // Hover/Active States
                        card: '#12141A',         // Specific card background
                    },
                },

                // Glass System (Translucent Overlays)
                'glass': {
                    border: 'rgba(255, 255, 255, 0.08)',
                    highlight: 'rgba(255, 255, 255, 0.05)',
                    strong: 'rgba(22, 25, 31, 0.8)',
                },

                // Financial Neon Colors
                'profit': {
                    DEFAULT: '#00E396',      // Neon Green (Glows)
                    muted: '#00B377',
                    bg: 'rgba(0, 227, 150, 0.1)',
                },
                'loss': {
                    DEFAULT: '#FF4560',      // Neon Red
                    muted: '#CC3750',
                    bg: 'rgba(255, 69, 96, 0.1)',
                },

                // Accent System
                'electric': {
                    blue: '#3B82F6',         // Primary Actions
                    purple: '#8B5CF6',       // Secondary
                    cyan: '#06B6D4',         // Info
                    amber: '#F59E0B',        // Warning
                },

                // Text Hierarchy
                'text': {
                    primary: '#F1F5F9',      // Off-white
                    secondary: '#94A3B8',    // Muted gray
                    tertiary: '#64748B',     // Even more muted
                    disabled: '#475569',     // Disabled state
                },
            },

            // ============================================================
            // TYPOGRAPHY SYSTEM
            // ============================================================
            fontFamily: {
                'ui': ['var(--font-inter)', 'sans-serif'],
                'data': ['var(--font-jetbrains)', 'monospace'],
                'sans': ['var(--font-inter)', 'sans-serif'],
                'mono': ['var(--font-jetbrains)', 'monospace'],
            },

            // Font feature settings for Inter
            fontFeatureSettings: {
                'inter': '"cv02", "cv03", "cv04", "cv11"',
            },

            // ============================================================
            // SPACING SYSTEM (Compact for High Density)
            // ============================================================
            spacing: {
                'compact': '0.375rem',   // 6px - Tight padding
                'dense': '0.5rem',       // 8px - Row padding
                'cozy': '0.75rem',       // 12px - Card padding
            },

            // ============================================================
            // LAYOUT SYSTEM
            // ============================================================
            height: {
                'row-dense': '2rem',     // 32px - Dense table rows
                'row-compact': '2.5rem', // 40px - Compact rows
                'row-normal': '3rem',    // 48px - Normal rows
            },

            // ============================================================
            // BORDER SYSTEM
            // ============================================================
            borderWidth: {
                'glass': '1px',
            },

            borderColor: {
                'glass': 'rgba(255, 255, 255, 0.08)',
            },

            // ============================================================
            // BACKDROP BLUR SYSTEM
            // ============================================================
            backdropBlur: {
                'glass': '24px',
                'glass-strong': '40px',
            },

            // ============================================================
            // ANIMATION SYSTEM (Including Micro-interactions)
            // ============================================================
            animation: {
                'fade-in': 'fadeIn 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                'slide-in': 'slideIn 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                'slide-up': 'slideUp 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                'pulse-glow': 'pulseGlow 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',

                // NEW: Micro-interaction animations (<200ms)
                'ripple': 'ripple 0.6s ease-out',
                'pulse-subtle': 'pulseSubtle 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
                'glow-pulse': 'glowPulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
                'bounce-soft': 'bounceSoft 0.5s ease-out',
            },

            keyframes: {
                fadeIn: {
                    '0%': { opacity: '0', transform: 'translateY(10px)' },
                    '100%': { opacity: '1', transform: 'translateY(0)' },
                },
                slideIn: {
                    '0%': { opacity: '0', transform: 'translateX(-20px)' },
                    '100%': { opacity: '1', transform: 'translateX(0)' },
                },
                slideUp: {
                    '0%': { opacity: '0', transform: 'translateY(20px)' },
                    '100%': { opacity: '1', transform: 'translateY(0)' },
                },
                pulseGlow: {
                    '0%, 100%': { opacity: '1' },
                    '50%': { opacity: '0.5' },
                },

                // NEW: Micro-interaction keyframes
                ripple: {
                    '0%': { transform: 'scale(0)', opacity: '1' },
                    '100%': { transform: 'scale(4)', opacity: '0' },
                },
                pulseSubtle: {
                    '0%, 100%': { opacity: '1' },
                    '50%': { opacity: '0.8' },
                },
                glowPulse: {
                    '0%, 100%': { boxShadow: '0 0 20px rgba(59, 130, 246, 0.4)' },
                    '50%': { boxShadow: '0 0 30px rgba(59, 130, 246, 0.7)' },
                },
                bounceSoft: {
                    '0%, 100%': { transform: 'translateY(0)' },
                    '50%': { transform: 'translateY(-5px)' },
                },
            },

            // ============================================================
            // Z-INDEX SCALE (Strict Layering)
            // ============================================================
            zIndex: {
                'toast': '100',      // Level 5: Toasts/Notifications
                'overlay': '50',     // Level 4: Modals, Command Palette
                'dropdown': '40',    // Level 3: Dropdowns, Autocomplete
                'sticky': '10',      // Level 2: Sticky Headers, Sidebars
                'content': '0',      // Level 1: Standard Content
            },

            // ============================================================
            // BACKGROUND IMAGE SYSTEM
            // ============================================================
            backgroundImage: {
                'deep-space': 'linear-gradient(135deg, #050505 0%, #0A0A0A 50%, #050505 100%)',
            },

            // ============================================================
            // BOX SHADOW SYSTEM (Glow Effects + Neumorphism)
            // ============================================================
            boxShadow: {
                // Existing glow effects
                'glow-sm': '0 0 10px rgba(59, 130, 246, 0.3)',
                'glow': '0 0 20px rgba(59, 130, 246, 0.4)',
                'glow-lg': '0 0 30px rgba(59, 130, 246, 0.5)',
                'glow-profit': '0 0 20px rgba(0, 227, 150, 0.4)',
                'glow-loss': '0 0 20px rgba(255, 69, 96, 0.4)',

                // NEW: Neumorphic shadows for soft UI depth
                // NEW: Neumorphic shadows for soft UI depth
                'neomorph-sm': '4px 4px 8px rgba(0, 0, 0, 0.4), -2px -2px 6px rgba(255, 255, 255, 0.05)',
                'neomorph': '8px 8px 16px rgba(0, 0, 0, 0.6), -4px -4px 12px rgba(255, 255, 255, 0.05)',
                'neomorph-lg': '12px 12px 24px rgba(0, 0, 0, 0.7), -6px -6px 16px rgba(255, 255, 255, 0.06)',
                'neomorph-inset': 'inset 4px 4px 8px rgba(0, 0, 0, 0.5), inset -4px -4px 8px rgba(255, 255, 255, 0.05)',

                // OCSWAP Specific Glows
                'glow-cyan': '0 0 30px rgba(6, 182, 212, 0.5), 0 0 10px rgba(6, 182, 212, 0.3)',
                'glow-blue': '0 0 30px rgba(59, 130, 246, 0.5), 0 0 10px rgba(59, 130, 246, 0.3)',
                'glow-purple': '0 0 30px rgba(139, 92, 246, 0.5)',
                'glow-amber': '0 0 20px rgba(245, 158, 11, 0.6)',
            },
        },
    },
    plugins: [],
    darkMode: 'class',
}

export default config
