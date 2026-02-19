import { render, screen } from '@testing-library/react';
import { Button } from '../button';

describe('Button Component', () => {
  it('renders all variants correctly', () => {
    const variants = ['primary', 'secondary', 'ghost', 'profit', 'loss', 'outline', 'link', 'destructive'] as const;

    variants.forEach((variant) => {
      const { container } = render(<Button variant={variant}>{variant}</Button>);
      expect(container.firstChild).toBeInTheDocument();
    });
  });

  it('renders all sizes correctly', () => {
    const sizes = ['xs', 'sm', 'default', 'lg', 'icon'] as const;

    sizes.forEach((size) => {
      const { container } = render(<Button size={size}>Button</Button>);
      expect(container.firstChild).toBeInTheDocument();
    });
  });

  it('applies focus ring on focus', () => {
    render(<Button>Focus Test</Button>);
    const button = screen.getByText('Focus Test');
    expect(button).toHaveClass('focus-visible:ring-2');
    expect(button).toHaveClass('focus-visible:ring-primary/20');
  });

  it('uses design tokens for all variants', () => {
    const { container } = render(<Button variant="primary">Primary</Button>);
    const button = container.firstChild as HTMLElement;

    // Check that it uses Tailwind classes that map to design tokens
    expect(button.className).toContain('bg-primary');
    expect(button.className).toContain('text-primary-fg');
    expect(button.className).toContain('hover:bg-primary-hover');
  });

  it('handles disabled state correctly', () => {
    render(<Button disabled>Disabled</Button>);
    const button = screen.getByText('Disabled');
    expect(button).toBeDisabled();
    expect(button).toHaveClass('disabled:opacity-50');
    expect(button).toHaveClass('disabled:pointer-events-none');
  });
});
