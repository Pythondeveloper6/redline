"""A deterministic DBAPI with pyodbc's call shape, for the mssql overhead cell.

No server, no network and no table storage: it answers SQLAlchemy's SQL Server setup
queries and returns one row for SELECTs. The value is the instrumentation cost under the
same cursor.execute/fetchall pattern a real pyodbc application uses.
"""

from __future__ import annotations

_DESCRIPTION = [("value", str, None, None, None, None, None)]


class Cursor:
    arraysize = 1
    description = None
    rowcount = -1

    def __init__(self):
        self._rows = []

    def execute(self, sql, params=None):
        upper = sql.upper()
        if "PRODUCTVERSION" in upper:
            rows = [("16.0.1000.6",)]
        elif "SCHEMA_NAME" in upper:
            rows = [("dbo",)]
        elif "SYS.SYSTEM_VIEWS" in upper:
            rows = [("dm_exec_sessions",)]
        elif "TRANSACTION_ISOLATION_LEVEL" in upper:
            rows = [("READ COMMITTED",)]
        elif upper.lstrip().startswith("SELECT"):
            rows = [(1, "row")]
        else:
            rows = []
        self._rows = rows
        self.description = _DESCRIPTION if rows else None
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, size=1):
        return self._rows[:size]

    def nextset(self):
        return None

    def setinputsizes(self, *args):
        pass

    def setoutputsize(self, *args):
        pass

    def close(self):
        self._rows = []


class Connection:
    def cursor(self):
        return Cursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def add_output_converter(self, *args):
        pass

    def getinfo(self, *args):
        return 0


class DBAPI:
    Error = Exception
    Warning = Warning
    ProgrammingError = Exception
    apilevel = "2.0"
    threadsafety = 2
    paramstyle = "qmark"
    version = "4.0.0"
    Cursor = Cursor
    Connection = Connection

    @staticmethod
    def connect(*args, **kwargs):
        return Connection()
