// Fixed offline example generated with scripts/build_release_fixtures.py.
export const SQL_EXAMPLE = "from sqlalchemy import text\n\nconn.execute(text(\"INSERT INTO dbo.items (name) OUTPUT inserted.id VALUES (:name) -- bind :name\"), {\"name\": \"example\"})\n"
export const SQL_EXAMPLE_REPORT = {
  "findings": [
    {
      "id": "93d945a424c078264cba",
      "kind": "bind-in-comment",
      "file": "input.py",
      "line": 3,
      "detail": "Bind-like text in a SQL comment can cause compiler/driver parameter mismatches.",
      "confidence": "medium",
      "fingerprint": "INSERT INTO dbo.items (name) OUTPUT inserted.id VALUES (...)"
    },
    {
      "id": "343a76c3111a0f4fe15e",
      "kind": "output-without-into",
      "file": "input.py",
      "line": 3,
      "detail": "Bare OUTPUT is incompatible with enabled triggers on the target. Verify metadata before treating this as an error.",
      "confidence": "medium",
      "fingerprint": "INSERT INTO dbo.items (name) OUTPUT inserted.id VALUES (...)"
    }
  ],
  "skipped": [],
  "statements": 1,
  "note": "Offline candidates only; schema and triggers are not checked. Fixed generated example in demo mode; arbitrary source needs a backend."
}
