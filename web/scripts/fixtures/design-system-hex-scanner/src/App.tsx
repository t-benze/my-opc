export function App() {
  const style = { color: '#aabbcc' }
  return (
    <main className="bg-[#123456] text-[#abc]">
      <input className="from-[#112233] via-[#223344] to-[#334455] divide-[#445566] placeholder-[#556677] ring-offset-[#667788]" />
      <div className="dark:hover:bg-red-500/25 text-[rgb(1_2_3)] border-[oklch(50%_0.2_20)] fill-[rebeccapurple] shadow-[0_1px_rgb(4_5_6)]" />
      <div className="bg-surface-raised text-text-primary border-border-default shadow-[0_1px_2px_var(--shadow)]" />
      {/* GitHub issue #123 and PR #abcdef are references, not colors. */}
      {/* Non-colour lookalikes: text-red-carpet, bg-gradient-to-r, grid-cols-[rgb(1_2_3)]. */}
      <p>&#123; &#xabc; &#XABC;</p>
      <svg fill="#010203" stroke="tomato" color="rgb(7 8 9)" />
      <span style={style}>fixture</span>
    </main>
  )
}

export const scannerAdversarial = (
  <div className="text-transparent bg-current border-inherit text-red-123 bg-blue-999">
    <span style={{ color: 'rgb(var(--raw-rgb))', backgroundColor: 'hsl(var(--raw-hsl) / 50%)' }} />
    <span style={{ backgroundImage: 'url(/assets/red.png)', width: 'red', color: 'var(--semantic)' }} />
  </div>
)
