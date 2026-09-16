"""Read-only capability probe for the packaged SQLite/FTS runtime."""

import sqlite3


def probe_sqlite_runtime():
    """Return SQLite/FTS capabilities without touching the production library."""
    connection = sqlite3.connect(":memory:")
    try:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE _snipvoice_probe USING fts5(content)"
            )
            connection.execute(
                "INSERT INTO _snipvoice_probe(content) VALUES (?)", ("revisão",)
            )
            matched = connection.execute(
                "SELECT count(*) FROM _snipvoice_probe WHERE _snipvoice_probe MATCH ?",
                ("revisão",),
            ).fetchone()[0]
            if matched != 1:
                raise sqlite3.DatabaseError("FTS5 Unicode MATCH returned no row")
        except sqlite3.DatabaseError as error:
            return {
                "sqlite": sqlite3.sqlite_version,
                "fts5": False,
                "error": str(error),
            }
        return {"sqlite": sqlite3.sqlite_version, "fts5": True}
    finally:
        connection.close()


def main():
    capabilities = probe_sqlite_runtime()
    if not capabilities["fts5"]:
        print(f"SQLITE_RUNTIME_PROBE fail: {capabilities['error']}")
        return 1
    print(
        "SQLITE_RUNTIME_PROBE pass: "
        f"sqlite={capabilities['sqlite']} fts5={capabilities['fts5']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
