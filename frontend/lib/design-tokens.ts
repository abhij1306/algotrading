/**
 * Design Token Type Definitions
 *
 * This file contains TypeScript type definitions for all design tokens used in the SmartTrader design system.
 * These types ensure type safety when referencing design tokens programmatically and provide autocomplete support.
 *
 * @see globals.css for the actual CSS variable definitions
 */

// ============================================================================
// Color Tokens
// ============================================================================

/**
 * Color tokens for the unified design system.
 * All color tokens are defined as CSS variables in globals.css and support both light and dark modes.
 */
export type ColorToken =
  // Background colors
  | 'background'
  | 'background-secondary'
  | 'background-tertiary'
  | 'surface'
  | 'elevated'

  // Foreground (text) colors
  | 'foreground'
  | 'foreground-secondary'
  | 'foreground-tertiary'
  | 'foreground-muted'

  // Border colors
  | 'border'
  | 'border-subtle'
  | 'border-focus'

  // Primary brand colors
  | 'primary'
  | 'primary-hover'
  | 'primary-light'
  | 'primary-foreground'

  // Semantic financial colors
  | 'profit'
  | 'profit-bg'
  | 'loss'
  | 'loss-bg'
  | 'warning'
  | 'warning-bg';

/**
 * Helper type to generate CSS variable references for color tokens.
 * @example
 * const bgColor: ColorVariable = 'var(--color-background)';
 */
export type ColorVariable = `var(--color-${ColorToken})`;

// ============================================================================
// Spacing Tokens
// ============================================================================

/**
 * Spacing tokens following a consistent 4px-based scale.
 * All spacing values are multiples of 4px for visual consistency.
 *
 * @example
 * 1 = 4px
 * 2 = 8px
 * 3 = 12px
 * 4 = 16px
 * 5 = 20px
 * 6 = 24px
 * 8 = 32px
 * 10 = 40px
 * 12 = 48px
 */
export type SpacingToken = 1 | 2 | 3 | 4 | 5 | 6 | 8 | 10 | 12;

/**
 * Helper type to generate CSS variable references for spacing tokens.
 * @example
 * const padding: SpacingVariable = 'var(--spacing-4)';
 */
export type SpacingVariable = `var(--spacing-${SpacingToken})`;

// ============================================================================
// Border Radius Tokens
// ============================================================================

/**
 * Border radius tokens for consistent component shapes.
 *
 * @example
 * 'sm' = 4px  - Small radius for subtle rounding
 * 'md' = 6px  - Medium radius (default for most components)
 * 'lg' = 8px  - Large radius for cards and containers
 * 'xl' = 12px - Extra large radius for prominent elements
 * 'full' = 9999px - Full radius for circular elements
 */
export type RadiusToken = 'sm' | 'md' | 'lg' | 'xl' | 'full';

/**
 * Helper type to generate CSS variable references for radius tokens.
 * @example
 * const borderRadius: RadiusVariable = 'var(--radius-md)';
 */
export type RadiusVariable = `var(--radius-${RadiusToken})`;

// ============================================================================
// Shadow Tokens
// ============================================================================

/**
 * Shadow tokens for elevation and depth.
 * Shadows are adjusted for both light and dark modes.
 *
 * @example
 * 'sm' - Subtle shadow for slight elevation
 * 'md' - Medium shadow for cards and dropdowns
 * 'lg' - Large shadow for modals and popovers
 * 'xl' - Extra large shadow for prominent overlays
 */
export type ShadowToken = 'sm' | 'md' | 'lg' | 'xl';

/**
 * Helper type to generate CSS variable references for shadow tokens.
 * @example
 * const boxShadow: ShadowVariable = 'var(--shadow-md)';
 */
export type ShadowVariable = `var(--shadow-${ShadowToken})`;

// ============================================================================
// Typography Tokens
// ============================================================================

/**
 * Font family tokens.
 *
 * @example
 * 'sans' - IBM Plex Sans for general UI text
 * 'mono' - DM Mono for financial data and code
 */
export type FontFamily = 'sans' | 'mono';

/**
 * Helper type to generate CSS variable references for font family tokens.
 * @example
 * const fontFamily: FontFamilyVariable = 'var(--font-sans)';
 */
export type FontFamilyVariable = `var(--font-${FontFamily})`;

/**
 * Font size tokens following a consistent type scale.
 *
 * @example
 * 'xxs' = 10px - Extra extra small (labels, captions)
 * 'xs'  = 11px - Extra small (secondary text)
 * 'sm'  = 13px - Small (body text, buttons)
 * 'base' = 15px - Base size (primary body text)
 * 'lg'  = 17px - Large (subheadings)
 * 'xl'  = 20px - Extra large (headings)
 * '2xl' = 24px - 2x extra large (page titles)
 */
export type FontSize = 'xxs' | 'xs' | 'sm' | 'base' | 'lg' | 'xl' | '2xl';

/**
 * Helper type to generate CSS variable references for font size tokens.
 * @example
 * const fontSize: FontSizeVariable = 'var(--text-base)';
 */
export type FontSizeVariable = `var(--text-${FontSize})`;

/**
 * Font weight tokens.
 *
 * @example
 * 'normal' = 400 - Regular text
 * 'medium' = 500 - Slightly emphasized text
 * 'semibold' = 600 - Emphasized text, buttons
 * 'bold' = 700 - Strong emphasis, headings
 */
export type FontWeight = 'normal' | 'medium' | 'semibold' | 'bold';

/**
 * Helper type to generate CSS variable references for font weight tokens.
 * @example
 * const fontWeight: FontWeightVariable = 'var(--font-semibold)';
 */
export type FontWeightVariable = `var(--font-${FontWeight})`;

/**
 * Line height tokens for consistent vertical rhythm.
 *
 * @example
 * 'tight' = 1.2 - Tight spacing for headings
 * 'normal' = 1.5 - Normal spacing for body text
 * 'relaxed' = 1.75 - Relaxed spacing for long-form content
 */
export type LineHeight = 'tight' | 'normal' | 'relaxed';

/**
 * Helper type to generate CSS variable references for line height tokens.
 * @example
 * const lineHeight: LineHeightVariable = 'var(--leading-normal)';
 */
export type LineHeightVariable = `var(--leading-${LineHeight})`;

// ============================================================================
// Composite Types
// ============================================================================

/**
 * Complete typography configuration.
 * Used for defining text styles programmatically.
 */
export interface TypographyStyle {
  fontFamily: FontFamily;
  fontSize: FontSize;
  fontWeight: FontWeight;
  lineHeight: LineHeight;
}

/**
 * All design token types combined.
 * Useful for generic token handling.
 */
export type DesignToken =
  | ColorToken
  | SpacingToken
  | RadiusToken
  | ShadowToken
  | FontFamily
  | FontSize
  | FontWeight
  | LineHeight;

/**
 * All CSS variable types combined.
 * Useful for generic CSS variable handling.
 */
export type DesignTokenVariable =
  | ColorVariable
  | SpacingVariable
  | RadiusVariable
  | ShadowVariable
  | FontFamilyVariable
  | FontSizeVariable
  | FontWeightVariable
  | LineHeightVariable;

// ============================================================================
// Utility Functions
// ============================================================================

/**
 * Generate a CSS variable reference for a color token.
 * @param token - The color token name
 * @returns CSS variable reference string
 *
 * @example
 * colorVar('background') // Returns: 'var(--color-background)'
 */
export function colorVar(token: ColorToken): ColorVariable {
  return `var(--color-${token})`;
}

/**
 * Generate a CSS variable reference for a spacing token.
 * @param token - The spacing token value
 * @returns CSS variable reference string
 *
 * @example
 * spacingVar(4) // Returns: 'var(--spacing-4)'
 */
export function spacingVar(token: SpacingToken): SpacingVariable {
  return `var(--spacing-${token})`;
}

/**
 * Generate a CSS variable reference for a radius token.
 * @param token - The radius token name
 * @returns CSS variable reference string
 *
 * @example
 * radiusVar('md') // Returns: 'var(--radius-md)'
 */
export function radiusVar(token: RadiusToken): RadiusVariable {
  return `var(--radius-${token})`;
}

/**
 * Generate a CSS variable reference for a shadow token.
 * @param token - The shadow token name
 * @returns CSS variable reference string
 *
 * @example
 * shadowVar('md') // Returns: 'var(--shadow-md)'
 */
export function shadowVar(token: ShadowToken): ShadowVariable {
  return `var(--shadow-${token})`;
}

/**
 * Generate a CSS variable reference for a font family token.
 * @param token - The font family token name
 * @returns CSS variable reference string
 *
 * @example
 * fontFamilyVar('sans') // Returns: 'var(--font-sans)'
 */
export function fontFamilyVar(token: FontFamily): FontFamilyVariable {
  return `var(--font-${token})`;
}

/**
 * Generate a CSS variable reference for a font size token.
 * @param token - The font size token name
 * @returns CSS variable reference string
 *
 * @example
 * fontSizeVar('base') // Returns: 'var(--text-base)'
 */
export function fontSizeVar(token: FontSize): FontSizeVariable {
  return `var(--text-${token})`;
}

/**
 * Generate a CSS variable reference for a font weight token.
 * @param token - The font weight token name
 * @returns CSS variable reference string
 *
 * @example
 * fontWeightVar('semibold') // Returns: 'var(--font-semibold)'
 */
export function fontWeightVar(token: FontWeight): FontWeightVariable {
  return `var(--font-${token})`;
}

/**
 * Generate a CSS variable reference for a line height token.
 * @param token - The line height token name
 * @returns CSS variable reference string
 *
 * @example
 * lineHeightVar('normal') // Returns: 'var(--leading-normal)'
 */
export function lineHeightVar(token: LineHeight): LineHeightVariable {
  return `var(--leading-${token})`;
}

// ============================================================================
// Type Guards
// ============================================================================

/**
 * Type guard to check if a string is a valid color token.
 * @param value - The value to check
 * @returns True if the value is a valid color token
 */
export function isColorToken(value: string): value is ColorToken {
  const validTokens: ColorToken[] = [
    'background', 'background-secondary', 'background-tertiary', 'surface', 'elevated',
    'foreground', 'foreground-secondary', 'foreground-tertiary', 'foreground-muted',
    'border', 'border-subtle', 'border-focus',
    'primary', 'primary-hover', 'primary-light', 'primary-foreground',
    'profit', 'profit-bg', 'loss', 'loss-bg', 'warning', 'warning-bg'
  ];
  return validTokens.includes(value as ColorToken);
}

/**
 * Type guard to check if a number is a valid spacing token.
 * @param value - The value to check
 * @returns True if the value is a valid spacing token
 */
export function isSpacingToken(value: number): value is SpacingToken {
  const validTokens: SpacingToken[] = [1, 2, 3, 4, 5, 6, 8, 10, 12];
  return validTokens.includes(value as SpacingToken);
}

/**
 * Type guard to check if a string is a valid radius token.
 * @param value - The value to check
 * @returns True if the value is a valid radius token
 */
export function isRadiusToken(value: string): value is RadiusToken {
  const validTokens: RadiusToken[] = ['sm', 'md', 'lg', 'xl', 'full'];
  return validTokens.includes(value as RadiusToken);
}

/**
 * Type guard to check if a string is a valid shadow token.
 * @param value - The value to check
 * @returns True if the value is a valid shadow token
 */
export function isShadowToken(value: string): value is ShadowToken {
  const validTokens: ShadowToken[] = ['sm', 'md', 'lg', 'xl'];
  return validTokens.includes(value as ShadowToken);
}

/**
 * Type guard to check if a string is a valid font family token.
 * @param value - The value to check
 * @returns True if the value is a valid font family token
 */
export function isFontFamily(value: string): value is FontFamily {
  const validTokens: FontFamily[] = ['sans', 'mono'];
  return validTokens.includes(value as FontFamily);
}

/**
 * Type guard to check if a string is a valid font size token.
 * @param value - The value to check
 * @returns True if the value is a valid font size token
 */
export function isFontSize(value: string): value is FontSize {
  const validTokens: FontSize[] = ['xxs', 'xs', 'sm', 'base', 'lg', 'xl', '2xl'];
  return validTokens.includes(value as FontSize);
}

/**
 * Type guard to check if a string is a valid font weight token.
 * @param value - The value to check
 * @returns True if the value is a valid font weight token
 */
export function isFontWeight(value: string): value is FontWeight {
  const validTokens: FontWeight[] = ['normal', 'medium', 'semibold', 'bold'];
  return validTokens.includes(value as FontWeight);
}

/**
 * Type guard to check if a string is a valid line height token.
 * @param value - The value to check
 * @returns True if the value is a valid line height token
 */
export function isLineHeight(value: string): value is LineHeight {
  const validTokens: LineHeight[] = ['tight', 'normal', 'relaxed'];
  return validTokens.includes(value as LineHeight);
}
