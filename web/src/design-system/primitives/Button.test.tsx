import { createRef } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, test, vi } from 'vitest';
import { Button, buttonVariants } from './Button';

describe('Button', () => {
  test('encodes the Pasture base geometry, typography, focus, and disabled contract', () => {
    const classes = buttonVariants();
    expect(classes).toContain('rounded-sm');
    expect(classes).toContain('gap-button-gap');
    expect(classes).toContain('px-button-inline');
    expect(classes).toContain('py-button-block');
    expect(classes).toContain('pasture-button-type');
    expect(classes).toContain('font-semibold');
    expect(classes).toContain('focus-visible:ring-2');
    expect(classes).toContain('disabled:pointer-events-none');
    expect(classes).not.toContain('rounded-full');
  });

  test.each([
    ['default', 'bg-primary'],
    ['secondary', 'bg-secondary'],
    ['outline', 'border-input'],
    ['ghost', 'bg-transparent'],
    ['destructive', 'bg-destructive'],
    ['destructiveOutline', 'text-destructive'],
    ['link', 'underline-offset-4'],
  ] as const)('renders the %s variant', (variant, expectedClass) => {
    render(<Button variant={variant}>{variant}</Button>);
    expect(screen.getByRole('button')).toHaveClass(expectedClass);
  });

  test.each([
    ['default', 'px-button-inline'],
    ['sm', 'px-button-sm-inline'],
    ['lg', 'px-button-lg-inline'],
    ['icon', 'size-button-icon-size'],
  ] as const)('renders the %s size', (size, expectedClass) => {
    render(<Button size={size} aria-label={`button-${size}`}>Save</Button>);
    expect(screen.getByRole('button')).toHaveClass(expectedClass);
  });

  test('loading is busy, disabled, and retains its accessible label', () => {
    render(<Button loading>Saving</Button>);
    const button = screen.getByRole('button', { name: 'Saving' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button.querySelector('[data-button-spinner]')).toHaveAttribute('aria-hidden', 'true');
  });

  test('preserves child icons and native disabled click behavior', async () => {
    const onClick = vi.fn();
    const { rerender } = render(<Button onClick={onClick}><svg aria-label="Save icon" />Save</Button>);
    expect(screen.getByLabelText('Save icon')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /save/i }));
    expect(onClick).toHaveBeenCalledOnce();
    rerender(<Button disabled onClick={onClick}>Save</Button>);
    fireEvent.click(screen.getByRole('button'));
    expect(onClick).toHaveBeenCalledOnce();
  });

  test('supports ref forwarding and keyboard activation', async () => {
    const ref = createRef<HTMLButtonElement>();
    const onClick = vi.fn();
    render(<Button ref={ref} onClick={onClick}>Continue</Button>);
    ref.current?.focus();
    await userEvent.keyboard('{Enter}');
    await userEvent.keyboard(' ');
    expect(ref.current).toHaveFocus();
    expect(onClick).toHaveBeenCalledTimes(2);
  });

  test('composes an anchor with asChild without nesting a button', () => {
    render(<Button asChild><a href="/tasks">Open tasks</a></Button>);
    const link = screen.getByRole('link', { name: 'Open tasks' });
    expect(link).toHaveAttribute('href', '/tasks');
    expect(link).toHaveClass('rounded-sm');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  test('suppresses activation and reports disabled semantics for a loading asChild link', async () => {
    const onClick = vi.fn();
    render(<Button asChild loading onClick={onClick}><a href="/tasks">Open tasks</a></Button>);
    const link = screen.getByRole('link', { name: 'Open tasks' });
    expect(link).toHaveAttribute('aria-busy', 'true');
    expect(link).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(link);
    expect(onClick).not.toHaveBeenCalled();
  });
});
