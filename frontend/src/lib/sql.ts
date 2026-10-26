// Tiny SQL highlighter: returns HTML-safe token list, rendered by <Sql/>.
const KW = new Set(('select from where and or not in is null like ilike between join left right inner outer full cross on as ' +
  'group by order having limit offset distinct union all insert into values update set delete returning with case when then else end ' +
  'asc desc exists count sum avg min max over partition create index table using true false any cast for').split(' '))
export type Tok = { t: 'kw' | 'str' | 'num' | 'id' | 'param' | 'cmt' | 'txt'; v: string }
export function tokenizeSql(sql: string): Tok[] {
  const out: Tok[] = []
  const re = /(--[^\n]*)|('(?:[^']|'')*')|("(?:[^"]|"")*"|`[^`]*`)|(%s|\$\d+|\?|:\w+)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][\w]*)|(\s+|.)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(sql))) {
    if (m[1]) out.push({ t: 'cmt', v: m[1] })
    else if (m[2]) out.push({ t: 'str', v: m[2] })
    else if (m[3]) out.push({ t: 'id', v: m[3] })
    else if (m[4]) out.push({ t: 'param', v: m[4] })
    else if (m[5]) out.push({ t: 'num', v: m[5] })
    else if (m[6]) out.push({ t: KW.has(m[6].toLowerCase()) ? 'kw' : 'txt', v: m[6] })
    else out.push({ t: 'txt', v: m[7] })
  }
  return out
}
export function formatSql(sql: string): string {
  return sql.replace(/\s+/g, ' ').replace(/\s(FROM|WHERE|GROUP BY|ORDER BY|HAVING|LIMIT|OFFSET|LEFT JOIN|INNER JOIN|JOIN|UNION|RETURNING)\s/gi, '\n$1 ').replace(/\s(AND|OR)\s/g, '\n  $1 ').trim()
}
