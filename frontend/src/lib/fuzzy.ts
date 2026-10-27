/** Subsequence fuzzy match. Returns a score (higher is better) or -1 when `q` does not match `text`. */
export function fuzzyScore(q: string, text: string): number {
  const query = q.trim().toLowerCase()
  if (!query) return 0
  const t = text.toLowerCase()
  const whole = t.indexOf(query)
  if (whole >= 0) return 1000 - whole - t.length * 0.01 + (whole === 0 ? 200 : 0) + (whole > 0 && /[\s/_\-.:]/.test(t[whole - 1]) ? 100 : 0)
  // every whitespace separated term must match somewhere
  const terms = query.split(/\s+/)
  if (terms.length > 1) {
    let sum = 0
    for (const term of terms) { const s = fuzzyScore(term, text); if (s < 0) return -1; sum += s }
    return sum / terms.length
  }
  let score = 0; let ti = 0; let run = 0
  for (const ch of query) {
    const at = t.indexOf(ch, ti)
    if (at < 0) return -1
    run = at === ti ? run + 1 : 0
    score += 10 + run * 5 - Math.min(at - ti, 10)
    ti = at + 1
  }
  return score - t.length * 0.01
}
