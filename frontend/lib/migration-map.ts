/**
 * Migration Mapping Utility
 *
 * This file provides a mapping from legacy Raycast CSS variables to the unified design token system.
 * Use this mapping to migrate existing code from Raycast variables to unified tokens.
 *
 * @example
 * // Before migration:
 * style={{ background: 'var(--raycast-bg-primary)' }}
 *
 * // After migration:
 * style={{ background: 'var(--color-background)' }}
 *
 * // Or using the mapping:
 * const oldVar = 'var(--raycast-bg-primary)';
 * const newVar = raycastToUnifiedMapping[oldVar]; // 'var(--color-background)'
 *
 * @see design-tokens.ts for TypeScript type definitions
 * @see globals.css for actual CSS variable definitions
 */

// ============================================================================
// Raycast to Unified Token Mapping
// ============================================================================

/**
 * Complete mapping from Raycast CSS variables to unified design tokens.
 *
 * This mapping covers all known Raycast variables used in the SmartTrader codebase.
 * Each Raycast variable is mapped to its semantic equivalent in the unified design system.
 *
 * **Usage Guidelines:**
 * - Use this mapping as a reference when migrating components
 * - Replace Raycast variables with their unified equivalents
 * - Prefer using Tailwind classes over inline CSS variables when possible
 * - For programmatic usage, use the helper functions from design-tokens.ts
 *
 * **Migration Process:**
 * 1. Search for Raycast variable usage in your component
 * 2. Find the corresponding unified token in this mapping
 * 3. Replace the Raycast variable with the unified token
 * 4. Test the component in both light and dark modes
 * 5. Verify visual consistency with the design system
 */
export const raycastToUnifiedMapping: Record<string, string> = {
  // ============================================================================
  // Background Colors
  // ============================================================================

  /**
   * Primary background - Main application background
   * Raycast: Deep dark background
   * Unified: Semantic background token that adapts to theme
   */
  'var(--raycast-bg-primary)': 'var(--color-background)',

  /**
   * Secondary background - Slightly elevated surfaces
   * Raycast: Lighter than primary for cards and panels
   * Unified: Secondary background for layered content
   */
  'var(--raycast-bg-secondary)': 'var(--color-background-secondary)',

  /**
   * Tertiary background - Further elevated surfaces
   * Raycast: Even lighter for nested elements
   * Unified: Tertiary background for nested content
   */
  'var(--raycast-bg-tertiary)': 'var(--color-background-tertiary)',

  /**
   * Hover background - Interactive element hover state
   * Raycast: Subtle highlight on hover
   * Unified: Maps to tertiary background for consistency
   */
  'var(--raycast-bg-hover)': 'var(--color-background-tertiary)',

  /**
   * Surface background - Card and panel surfaces
   * Raycast: Distinct surface color
   * Unified: Dedicated surface token for cards
   */
  'var(--raycast-surface)': 'var(--color-surface)',

  /**
   * Elevated background - Modals and popovers
   * Raycast: Highest elevation level
   * Unified: Elevated surface for overlays
   */
  'var(--raycast-elevated)': 'var(--color-elevated)',

  // ============================================================================
  // Foreground (Text) Colors
  // ============================================================================

  /**
   * Primary foreground - Main text color
   * Raycast: Highest contrast text
   * Unified: Primary text color with optimal contrast
   */
  'var(--raycast-fg-primary)': 'var(--color-foreground)',

  /**
   * Secondary foreground - Secondary text
   * Raycast: Slightly muted text
   * Unified: Secondary text for less important content
   */
  'var(--raycast-fg-secondary)': 'var(--color-foreground-secondary)',

  /**
   * Tertiary foreground - Tertiary text
   * Raycast: More muted text
   * Unified: Tertiary text for subtle content
   */
  'var(--raycast-fg-tertiary)': 'var(--color-foreground-tertiary)',

  /**
   * Muted foreground - Placeholder and disabled text
   * Raycast: Most muted text
   * Unified: Muted text for placeholders and disabled states
   */
  'var(--raycast-fg-muted)': 'var(--color-foreground-muted)',

  // ============================================================================
  // Border Colors
  // ============================================================================

  /**
   * Default border - Standard borders
   * Raycast: Subtle border for separation
   * Unified: Standard border color
   */
  'var(--raycast-border)': 'var(--color-border)',

  /**
   * Subtle border - Very subtle borders
   * Raycast: Barely visible borders
   * Unified: Subtle border for minimal separation
   */
  'var(--raycast-border-subtle)': 'var(--color-border-subtle)',

  /**
   * Focus border - Focus ring color
   * Raycast: Accent color for focus states
   * Unified: Focus indicator color
   */
  'var(--raycast-border-focus)': 'var(--color-border-focus)',

  // ============================================================================
  // Brand Colors
  // ============================================================================

  /**
   * Blue - Primary brand color
   * Raycast: Blue accent color
   * Unified: Primary brand color
   */
  'var(--raycast-blue)': 'var(--color-primary)',

  /**
   * Blue hover - Primary brand hover state
   * Raycast: Darker blue on hover
   * Unified: Primary hover state
   */
  'var(--raycast-blue-hover)': 'var(--color-primary-hover)',

  /**
   * Blue light - Primary brand light variant
   * Raycast: Light blue background
   * Unified: Primary light background
   */
  'var(--raycast-blue-light)': 'var(--color-primary-light)',

  /**
   * Primary foreground - Text on primary background
   * Raycast: White text on blue
   * Unified: Text color for primary buttons
   */
  'var(--raycast-primary-fg)': 'var(--color-primary-foreground)',

  // ============================================================================
  // Semantic Financial Colors
  // ============================================================================

  /**
   * Green - Profit/positive color
   * Raycast: Green for positive values
   * Unified: Profit color for financial gains
   */
  'var(--raycast-green)': 'var(--color-profit)',

  /**
   * Green light - Profit background
   * Raycast: Light green background
   * Unified: Profit background for badges and highlights
   */
  'var(--raycast-green-light)': 'var(--color-profit-bg)',

  /**
   * Green background - Alternative profit background
   * Raycast: Green background variant
   * Unified: Same as profit background
   */
  'var(--raycast-green-bg)': 'var(--color-profit-bg)',

  /**
   * Red - Loss/negative color
   * Raycast: Red for negative values
   * Unified: Loss color for financial losses
   */
  'var(--raycast-red)': 'var(--color-loss)',

  /**
   * Red light - Loss background
   * Raycast: Light red background
   * Unified: Loss background for badges and highlights
   */
  'var(--raycast-red-light)': 'var(--color-loss-bg)',

  /**
   * Red background - Alternative loss background
   * Raycast: Red background variant
   * Unified: Same as loss background
   */
  'var(--raycast-red-bg)': 'var(--color-loss-bg)',

  /**
   * Orange - Warning color
   * Raycast: Orange for warnings
   * Unified: Warning color for alerts
   */
  'var(--raycast-orange)': 'var(--color-warning)',

  /**
   * Orange light - Warning background
   * Raycast: Light orange background
   * Unified: Warning background for badges and alerts
   */
  'var(--raycast-orange-light)': 'var(--color-warning-bg)',

  /**
   * Yellow - Alternative warning color
   * Raycast: Yellow for warnings
   * Unified: Maps to warning color
   */
  'var(--raycast-yellow)': 'var(--color-warning)',

  /**
   * Yellow light - Alternative warning background
   * Raycast: Light yellow background
   * Unified: Maps to warning background
   */
  'var(--raycast-yellow-light)': 'var(--color-warning-bg)',

  // ============================================================================
  // Border Radius
  // ============================================================================

  /**
   * Default radius - Standard border radius
   * Raycast: Medium border radius
   * Unified: Medium radius (6px)
   */
  'var(--raycast-radius)': 'var(--radius-md)',

  /**
   * Small radius - Subtle rounding
   * Raycast: Small border radius
   * Unified: Small radius (4px)
   */
  'var(--raycast-radius-sm)': 'var(--radius-sm)',

  /**
   * Large radius - Prominent rounding
   * Raycast: Large border radius
   * Unified: Large radius (8px)
   */
  'var(--raycast-radius-lg)': 'var(--radius-lg)',

  /**
   * Extra large radius - Very prominent rounding
   * Raycast: Extra large border radius
   * Unified: Extra large radius (12px)
   */
  'var(--raycast-radius-xl)': 'var(--radius-xl)',

  /**
   * Full radius - Circular elements
   * Raycast: Fully rounded (pills, circles)
   * Unified: Full radius (9999px)
   */
  'var(--raycast-radius-full)': 'var(--radius-full)',

  // ============================================================================
  // Shadows
  // ============================================================================

  /**
   * Small shadow - Subtle elevation
   * Raycast: Small shadow for slight elevation
   * Unified: Small shadow
   */
  'var(--raycast-shadow-sm)': 'var(--shadow-sm)',

  /**
   * Medium shadow - Standard elevation
   * Raycast: Medium shadow for cards
   * Unified: Medium shadow
   */
  'var(--raycast-shadow-md)': 'var(--shadow-md)',

  /**
   * Large shadow - Prominent elevation
   * Raycast: Large shadow for modals
   * Unified: Large shadow
   */
  'var(--raycast-shadow-lg)': 'var(--shadow-lg)',

  /**
   * Extra large shadow - Maximum elevation
   * Raycast: Extra large shadow for overlays
   * Unified: Extra large shadow
   */
  'var(--raycast-shadow-xl)': 'var(--shadow-xl)',

  // ============================================================================
  // Typography (if Raycast had typography variables)
  // ============================================================================

  /**
   * Sans font - UI font family
   * Raycast: Sans-serif font
   * Unified: Inter font family
   */
  'var(--raycast-font-sans)': 'var(--font-sans)',

  /**
   * Mono font - Code and financial data font
   * Raycast: Monospace font
   * Unified: JetBrains Mono font family
   */
  'var(--raycast-font-mono)': 'var(--font-mono)',

  // ============================================================================
  // Spacing (if Raycast had spacing variables)
  // ============================================================================

  /**
   * Spacing scale mappings
   * Note: Raycast may have used different spacing values
   * These are approximate mappings to the unified 4px-based scale
   */
  'var(--raycast-spacing-1)': 'var(--spacing-1)', // 4px
  'var(--raycast-spacing-2)': 'var(--spacing-2)', // 8px
  'var(--raycast-spacing-3)': 'var(--spacing-3)', // 12px
  'var(--raycast-spacing-4)': 'var(--spacing-4)', // 16px
  'var(--raycast-spacing-5)': 'var(--spacing-5)', // 20px
  'var(--raycast-spacing-6)': 'var(--spacing-6)', // 24px
  'var(--raycast-spacing-8)': 'var(--spacing-8)', // 32px
  'var(--raycast-spacing-10)': 'var(--spacing-10)', // 40px
  'var(--raycast-spacing-12)': 'var(--spacing-12)', // 48px
};

// ============================================================================
// Utility Functions
// ============================================================================

/**
 * Convert a Raycast CSS variable to its unified equivalent.
 *
 * @param raycastVar - The Raycast CSS variable (e.g., 'var(--raycast-bg-primary)')
 * @returns The unified design token equivalent, or the original value if no mapping exists
 *
 * @example
 * migrateVariable('var(--raycast-bg-primary)') // Returns: 'var(--color-background)'
 * migrateVariable('var(--unknown-var)') // Returns: 'var(--unknown-var)' (unchanged)
 */
export function migrateVariable(raycastVar: string): string {
  return raycastToUnifiedMapping[raycastVar] || raycastVar;
}

/**
 * Check if a CSS variable is a Raycast variable that needs migration.
 *
 * @param cssVar - The CSS variable to check
 * @returns True if the variable is a Raycast variable
 *
 * @example
 * isRaycastVariable('var(--raycast-bg-primary)') // Returns: true
 * isRaycastVariable('var(--color-background)') // Returns: false
 */
export function isRaycastVariable(cssVar: string): boolean {
  return cssVar.includes('--raycast-');
}

/**
 * Extract all Raycast variables from a CSS string.
 *
 * @param cssString - CSS string that may contain Raycast variables
 * @returns Array of Raycast variables found in the string
 *
 * @example
 * const css = 'background: var(--raycast-bg-primary); color: var(--raycast-fg-primary);';
 * extractRaycastVariables(css) // Returns: ['var(--raycast-bg-primary)', 'var(--raycast-fg-primary)']
 */
export function extractRaycastVariables(cssString: string): string[] {
  const regex = /var\(--raycast-[a-z-]+\)/g;
  return cssString.match(regex) || [];
}

/**
 * Migrate all Raycast variables in a CSS string to unified tokens.
 *
 * @param cssString - CSS string containing Raycast variables
 * @returns CSS string with all Raycast variables replaced with unified tokens
 *
 * @example
 * const oldCss = 'background: var(--raycast-bg-primary); color: var(--raycast-fg-primary);';
 * migrateCssString(oldCss)
 * // Returns: 'background: var(--color-background); color: var(--color-foreground);'
 */
export function migrateCssString(cssString: string): string {
  let result = cssString;

  // Extract all Raycast variables
  const raycastVars = extractRaycastVariables(cssString);

  // Replace each Raycast variable with its unified equivalent
  raycastVars.forEach(raycastVar => {
    const unifiedVar = migrateVariable(raycastVar);
    result = result.replace(new RegExp(escapeRegExp(raycastVar), 'g'), unifiedVar);
  });

  return result;
}

/**
 * Get migration statistics for a CSS string.
 *
 * @param cssString - CSS string to analyze
 * @returns Object containing migration statistics
 *
 * @example
 * const css = 'background: var(--raycast-bg-primary); color: var(--color-foreground);';
 * getMigrationStats(css)
 * // Returns: { totalVariables: 2, raycastVariables: 1, unifiedVariables: 1, needsMigration: true }
 */
export function getMigrationStats(cssString: string): {
  totalVariables: number;
  raycastVariables: number;
  unifiedVariables: number;
  needsMigration: boolean;
} {
  const allVars = cssString.match(/var\(--[a-z-]+\)/g) || [];
  const raycastVars = extractRaycastVariables(cssString);
  const unifiedVars = allVars.filter(v => !isRaycastVariable(v));

  return {
    totalVariables: allVars.length,
    raycastVariables: raycastVars.length,
    unifiedVariables: unifiedVars.length,
    needsMigration: raycastVars.length > 0,
  };
}

/**
 * Escape special regex characters in a string.
 * Helper function for migrateCssString.
 *
 * @param string - String to escape
 * @returns Escaped string safe for use in RegExp
 */
function escapeRegExp(string: string): string {
  return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// ============================================================================
// Reverse Mapping (for reference only)
// ============================================================================

/**
 * Reverse mapping from unified tokens to Raycast variables.
 * This is provided for reference only and should NOT be used for migration.
 *
 * **Note:** Multiple Raycast variables may map to the same unified token,
 * so this reverse mapping may not be complete or accurate.
 *
 * @internal
 */
export const unifiedToRaycastMapping: Record<string, string[]> = Object.entries(
  raycastToUnifiedMapping
).reduce((acc, [raycast, unified]) => {
  if (!acc[unified]) {
    acc[unified] = [];
  }
  acc[unified].push(raycast);
  return acc;
}, {} as Record<string, string[]>);

// ============================================================================
// Migration Checklist
// ============================================================================

/**
 * Migration checklist for developers.
 *
 * Use this checklist when migrating components from Raycast to unified design system:
 *
 * 1. ✅ Search for all Raycast variable usage in the component
 * 2. ✅ Replace each Raycast variable with its unified equivalent using this mapping
 * 3. ✅ Remove any inline style attributes with CSS variables (use Tailwind classes instead)
 * 4. ✅ Update component to use UI components from the component library
 * 5. ✅ Test component in both light and dark modes
 * 6. ✅ Verify hover, focus, and active states work correctly
 * 7. ✅ Check for any console errors related to missing CSS variables
 * 8. ✅ Verify visual consistency with the design system
 * 9. ✅ Run linting and type checking
 * 10. ✅ Update component documentation if needed
 *
 * **Common Pitfalls:**
 * - Forgetting to test in dark mode
 * - Not removing inline styles after migration
 * - Using hardcoded colors instead of design tokens
 * - Not updating hover/focus states
 * - Mixing Raycast and unified variables
 *
 * **Best Practices:**
 * - Prefer Tailwind classes over inline CSS variables
 * - Use component library components instead of custom styled elements
 * - Test thoroughly in both light and dark modes
 * - Verify accessibility (contrast ratios, focus indicators)
 * - Document any design decisions or deviations
 */

// ============================================================================
// Type Exports
// ============================================================================

/**
 * Type for Raycast CSS variable strings.
 * Used for type safety when working with Raycast variables.
 */
export type RaycastVariable = keyof typeof raycastToUnifiedMapping;

/**
 * Type for unified design token CSS variable strings.
 * Used for type safety when working with unified tokens.
 */
export type UnifiedVariable = typeof raycastToUnifiedMapping[RaycastVariable];
