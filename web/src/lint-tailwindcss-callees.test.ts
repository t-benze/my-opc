import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ESLint } from 'eslint'
import { describe, expect, it } from 'vitest'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const eslint = new ESLint({ cwd: webRoot })

async function lint(source: string, relativePath: string) {
  const [result] = await eslint.lintText(source, {
    filePath: path.join(webRoot, relativePath),
  })
  return result.messages
}

describe('Tailwind arbitrary-value callee coverage', () => {
  it.each([
    ['cn', 'tsx'],
    ['clsx', 'tsx'],
    ['cva', 'tsx'],
    ['cn', 'ts'],
  ])('reports an arbitrary value inside %s() in a feature .%s file', async (callee, extension) => {
    const messages = await lint(
      `export const probe = () => ${callee}('text-sm text-[13px]')`,
      `src/features/lint-callee-probe.${extension}`,
    )

    expect(messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ ruleId: 'tailwindcss/no-arbitrary-value' }),
    ]))
  })

  it.each(['cn', 'clsx', 'cva'])('accepts semantic utilities inside %s()', async (callee) => {
    const messages = await lint(
      `export const probe = () => ${callee}('text-sm bg-surface')`,
      'src/features/lint-callee-probe.tsx',
    )

    expect(messages.filter(({ severity }) => severity === 2)).toEqual([])
  })

  it('documents that the plugin does not infer arbitrary plain helper names', async () => {
    const messages = await lint(
      "export const statusClass = () => 'text-[13px]'",
      'src/features/lint-plain-helper-probe.ts',
    )

    expect(messages.filter(({ ruleId }) => ruleId === 'tailwindcss/no-arbitrary-value')).toEqual([])
  })

  it('freezes each current residue occurrence so additions and deletions fail', async () => {
    const file = 'src/features/dreams/DreamsPage.tsx'
    const one = "export const probe = () => cn('text-[10px]')"
    const added = `${one}\nexport const added = () => cn('text-[10px]')`
    const deleted = "export const probe = () => cn('text-xs')"

    expect((await lint(one, file)).filter(({ severity }) => severity === 2)).toEqual([])
    for (const source of [added, deleted]) {
      expect(await lint(source, file)).toEqual(expect.arrayContaining([
        expect.objectContaining({ ruleId: 'local/frozen-tailwind-arbitrary-baseline' }),
      ]))
    }
  })
})
