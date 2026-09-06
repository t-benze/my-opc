/**
 * Textarea primitive — shadcn/ui canonical, styled against
 * `components.textarea` tokens. The default scale is body_lg ("composers
 * feel like writing tools" per DESIGN.md).
 */
import * as React from 'react';

import { cn } from '@/lib/utils';

export type TextareaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement>;

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, ...props }, ref) => {
    return (
      <textarea
        className={cn(
          'flex min-h-[6rem] w-full rounded-sm border border-border-default bg-surface-raised px-3 py-2 text-body-lg text-text-primary placeholder:text-text-muted focus:border-accent-default focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50',
          className,
        )}
        ref={ref}
        {...props}
      />
    );
  },
);
Textarea.displayName = 'Textarea';
