"""Labelled raw SQL: ``# expect: <rule>`` marks a statement the linter should report."""


def write(conn):
    conn.execute("INSERT INTO t (a) OUTPUT inserted.id VALUES (1)")  # expect: output-without-into
    conn.execute("INSERT INTO t (a) OUTPUT inserted.id INTO @ids VALUES (1)")
    conn.execute("SELECT 1 -- :ghost")  # expect: bind-in-comment
    conn.execute("INSERT INTO a SELECT * FROM b")  # expect: select-star-in-insert-select
    conn.execute("INSERT INTO a (x) SELECT x FROM b")
    table = "audit"
    conn.execute("SELECT * FROM %s" % table)
    conn.execute(" ".join(["SELECT x", "FROM audit"]))
