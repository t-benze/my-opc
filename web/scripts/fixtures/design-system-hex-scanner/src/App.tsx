export function App() {
  const style = { color: '#aabbcc' }
  return (
    <main className="bg-[#123456] text-[#abc]">
      <input className="from-[#112233] via-[#223344] to-[#334455] divide-[#445566] placeholder-[#556677] ring-offset-[#667788]" />
      {/* GitHub issue #123 and PR #abcdef are references, not colors. */}
      <p>&#123; &#xabc; &#XABC;</p>
      <svg fill="#010203" />
      <span style={style}>fixture</span>
    </main>
  )
}
