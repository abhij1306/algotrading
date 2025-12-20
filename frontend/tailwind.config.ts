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
                    DEFAULT: '#0F1115',      // Deepest Void - Base Layer
                    surface: '#16191F',      // Elevated Layer - Tables/Cards
                    elevated: '#1C1F26',     // Hover/Active States
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
                'ui': ['Inter', '-apple-system', 'BlinkMacSystemFont', 'sans-serif'],
                'data': ['JetBrains Mono', 'Fira Code', 'monospace'],
                'sans': ['Inter', 'sans-serif'],
                'mono': ['JetBrains Mono', 'monospace'],
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
            // ANIMATION SYSTEM
            // ============================================================
            animation: {
                'fade-in': 'fadeIn 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                'slide-in': 'slideIn 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                'slide-up': 'slideUp 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                'pulse-glow': 'pulseGlow 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
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
            },

            // ============================================================
            // BOX SHADOW SYSTEM (Glow Effects)
            // ============================================================
            boxShadow: {
                'glow-sm': '0 0 10px rgba(59, 130, 246, 0.3)',
                'glow': '0 0 20px rgba(59, 130, 246, 0.4)',
                'glow-lg': '0 0 30px rgba(59, 130, 246, 0.5)',
                'glow-profit': '0 0 20px rgba(0, 227, 150, 0.4)',
                'glow-loss': '0 0 20px rgba(255, 69, 96, 0.4)',
            },
        },
    },
    plugins: [],
    darkMode: 'class',
}

export default config
