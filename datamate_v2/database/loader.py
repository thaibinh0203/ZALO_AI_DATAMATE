"""Atomic schema publication, immutable by the QA read-only role."""
from psycopg import sql
from datamate_v2.common import dumps,PipelineError
from .connection import connect

SYSTEM_COLUMNS={"_source_row":"BIGINT","_is_summary_row":"BOOLEAN","_summary_status":"TEXT","_raw":"JSONB","_quality":"JSONB"}

def load_business(metadata,clean_tables,settings):
    schema=metadata["schema"]
    with connect(settings,admin=True) as db:
        # Fingerprints avoid in-place replacement of tables visible to active readers.
        db.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {} ").format(sql.Identifier(schema)))
        db.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        for table in metadata["tables"]:
            name=table["sql_table"]
            definitions=[sql.SQL("{} {}").format(sql.Identifier(c["sql_column"]),sql.SQL(c["type"])) for c in table["columns"]]
            definitions += [sql.SQL("{} {}").format(sql.Identifier(k),sql.SQL(t)) for k,t in SYSTEM_COLUMNS.items()]
            db.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(sql.Identifier(schema),sql.Identifier(name),sql.SQL(',').join(definitions)))
            # Re-ingest identical fingerprint is idempotent in a single transaction.
            db.execute(sql.SQL("TRUNCATE {}.{}").format(sql.Identifier(schema),sql.Identifier(name)))
            names=[c["sql_column"] for c in table["columns"]]+list(SYSTEM_COLUMNS)
            statement=sql.SQL("COPY {}.{} ({}) FROM STDIN").format(sql.Identifier(schema),sql.Identifier(name),sql.SQL(',').join(map(sql.Identifier,names)))
            with db.cursor().copy(statement) as copy:
                for row in clean_tables[name]:
                    copy.write_row([dumps(row[k]) if k in ('_raw','_quality') else row.get(k) for k in names])
        role=settings["readonly_role"]
        db.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
        db.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(sql.Identifier(schema),sql.Identifier(role)))
    return schema

def schema_exists(schema,settings):
    with connect(settings,admin=True) as db:
        return db.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)",(schema,)).fetchone()[0]
