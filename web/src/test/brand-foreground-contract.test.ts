import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, test } from 'vitest';

const srcRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path: string): string => readFileSync(join(srcRoot, path), 'utf8');

const REFERENCE_SHA256 =
  '87e23c25c95b22bdd46570314f6c649667d787ef89128504dbb52905cd52045a';
const TOKEN = '--color-brand-foreground';
const UTILITY = 'text-brand-foreground';

describe('founder-approved brand foreground contract', () => {
  test('pins the immutable reference and one light/dark semantic token authority', () => {
    expect(REFERENCE_SHA256).toHaveLength(64);
    const tokens = read('design-system/tokens/tokens.css');
    const definitions = [...tokens.matchAll(/--color-brand-foreground:\s*([^;]+);/g)];

    expect(definitions.map((match) => match[1].trim())).toEqual([
      'oklch(0.45 0.10 152)',
      'oklch(0.80 0.12 152)',
    ]);
  });

  test('covers every intended live Sidebar and Onboarding brand consumer', () => {
    const sidebar = read('design-system/layouts/AppShell/Sidebar.tsx');
    const onboarding = read('features/onboarding/OnboardingPage.tsx');
    const liveSources = `${sidebar}\n${onboarding}`;

    expect(sidebar.match(new RegExp(UTILITY, 'g'))).toHaveLength(2);
    expect(onboarding.match(new RegExp(UTILITY, 'g'))).toHaveLength(1);
    expect(liveSources).not.toContain('#4ade80');
    expect(liveSources).not.toMatch(/RanchLogo className="text-accent\b/);
  });

  test('does not alias brand identity back to the generic accent token', () => {
    const tokens = read('design-system/tokens/tokens.css');
    const definitions = [...tokens.matchAll(new RegExp(`${TOKEN}:\\s*([^;]+);`, 'g'))];

    expect(definitions.every((match) => !match[1].includes('var(--color-accent'))).toBe(true);
  });
});
