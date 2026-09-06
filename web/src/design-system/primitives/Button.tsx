/**
 * Button primitive — shadcn/ui canonical, with variants mapped onto our
 * semantic token CSS vars (defined in `tokens.css`).
 *
 * Compatibility rename map from the previous `components/Button.tsx`:
 *   primary  → default
 *   secondary→ secondary
 *   ghost    → ghost
 *   danger   → destructive
 *
 * `outline` and `link` are retained for shadcn ergonomic parity. Pasture's
 * `.btn-danger-outline` is exposed as `destructiveOutline`.
 */
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { LoaderCircle } from 'lucide-react';
import * as React from 'react';

import { cn } from '@/lib/utils';

const buttonVariants = cva(
  // Pasture §4.14: modest 8px corners, 13px/600 type, and a 7px icon gap.
  // Pills are reserved for badges, chips, tabs, and status indicators.
  'focus-visible:ring-ring gap-button-gap pasture-button-type inline-flex items-center justify-center rounded-sm font-semibold whitespace-nowrap border transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        default:
          'bg-primary text-primary-foreground border-transparent hover:bg-primary/90 active:bg-primary/90',
        destructive:
          'bg-destructive text-destructive-foreground border-transparent hover:bg-destructive/90 active:bg-destructive/90',
        destructiveOutline:
          'text-destructive border-destructive/40 bg-background hover:border-destructive hover:bg-danger-soft active:bg-danger-soft',
        outline:
          'border-input bg-background hover:border-text-muted hover:bg-secondary hover:text-secondary-foreground active:bg-secondary',
        secondary:
          'bg-secondary text-secondary-foreground border-border hover:border-text-muted hover:bg-secondary/80 active:bg-secondary/80',
        ghost:
          'bg-transparent text-text-secondary border-transparent hover:bg-background hover:text-foreground active:bg-background',
        link:
          'text-primary border-transparent bg-transparent underline-offset-4 hover:underline',
      },
      size: {
        default: 'px-button-inline py-button-block',
        sm: 'px-button-sm-inline py-button-sm-block text-xs',
        lg: 'px-button-lg-inline py-button-lg-block text-sm',
        icon: 'size-button-icon-size p-button-icon-padding',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** Render as a Radix `Slot` so the consumer can pass an `<a>` or `<NavLink>` child. */
  asChild?: boolean;
  /** Show the canonical spinner and suppress activation while work is pending. */
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading = false, disabled, children, onClick, ...props }, ref) => {
    const inactive = disabled || loading;
    const classes = cn(buttonVariants({ variant, size, className }));
    if (asChild) {
      return (
        <Slot
          ref={ref}
          className={classes}
          aria-busy={loading || undefined}
          aria-disabled={inactive || undefined}
          onClick={(event) => {
            if (inactive) {
              event.preventDefault();
              return;
            }
            onClick?.(event as React.MouseEvent<HTMLButtonElement>);
          }}
          {...props}
        >
          {children}
        </Slot>
      );
    }
    return (
      <button
        ref={ref}
        className={classes}
        aria-busy={loading || undefined}
        disabled={inactive}
        onClick={onClick}
        {...props}
      >
        {loading && <LoaderCircle data-button-spinner aria-hidden="true" className="animate-spin" />}
        {children}
      </button>
    );
  },
);
Button.displayName = 'Button';

export { buttonVariants };
