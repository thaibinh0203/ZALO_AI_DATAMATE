"""Synthetic model responses for contract-level tests, never benchmark answers."""
from datamate_v2.data_pipeline.schema_generation.schema_validator import required_topics

def schema_fixture(table):
    return {'description':'Synthetic fixture table for testing.','grain':'Each fixture row is one source record.',
       'columns':[{'sql_column':c['sql_column'],'description':'Fixture column meaning.','unit':None,'cautions':['Fixture interpretation only.']} for c in table['columns']],
       'usage_rules':[{'topic':topic,'status':'unresolved','explanation':'This fixture does not claim business semantics.','evidence':[]} for topic in sorted(required_topics(table))],
       'data_quality_notes':['Fixture data still requires validation.'],'assumptions':[]}
