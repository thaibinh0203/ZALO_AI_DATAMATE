import os
import psycopg
from datamate_v2.common import PipelineError

def connect(settings,*,admin=False):
    key=settings["admin_dsn_env" if admin else "readonly_dsn_env"]
    dsn=os.environ.get(key)
    if not dsn: raise PipelineError(f"Database DSN missing: set environment variable {key}")
    return psycopg.connect(dsn,connect_timeout=settings.get("connect_timeout_seconds",10))
