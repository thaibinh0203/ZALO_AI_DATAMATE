"""Opt-in semantic-plan pipeline (P0-P9 of docs/DATA_PIPELINE_REVISION_DESIGN.md).

The v4 pipeline in ``datamate_data`` is untouched and its implementation hash only
covers ``datamate_data/*.py``; this subpackage is versioned separately so v2 outputs
and readers keep working for comparison.
"""

PIPELINE_VERSION = 'postgres-pipeline-v5-semplan'
PLAN_SCHEMA_VERSION = 'business-semantic-plan-v1'
MANIFEST_VERSION = 'published-business-v1'
ROW_DECISION_VERSION = 'row-decisions-v1'
QUALITY_VERSION = 'quality-registry-v1'
TEMPORAL_VERSION = 'partial-date-v1'
STATUS_MAPPING_VERSION = 'scoped-status-v1'
CHECKS_VERSION = 'scoped-checks-v1'
