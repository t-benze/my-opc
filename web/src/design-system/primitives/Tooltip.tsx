/**
 * Tooltip primitive — shadcn/ui canonical (Radix Tooltip), styled against
 * our `popover` surface tokens.
 *
 * The TooltipProvider is mounted globally by the app shell; consumers only
 * use Tooltip + TooltipTrigger + TooltipContent.
 *
 * Usage:
 *   <Tooltip>
 *     <TooltipTrigger asChild><span ...>Disabled</span></TooltipTrigger>
 *     <TooltipContent>Coming soon</TooltipContent>
 *   </Tooltip>
 */
import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import * as React from 'react';

import { cn } from '@/lib/utils';

const TooltipProvider = TooltipPrimitive.Provider;

const Tooltip = TooltipPrimitive.Root;

const TooltipTrigger = TooltipPrimitive.Trigger;

const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      className={cn(
        'z-50 overflow-hidden rounded-sm border border-border-default bg-popover px-2 py-1 text-caption text-popover-foreground shadow-md animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95',
        className,
      )}
      {...props}
    />
  </TooltipPrimitive.Portal>
));
TooltipContent.displayName = TooltipPrimitive.Content.displayName;

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider };
