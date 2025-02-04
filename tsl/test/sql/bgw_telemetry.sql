-- This file and its contents are licensed under the Timescale License.
-- Please see the included NOTICE for copyright information and
-- LICENSE-TIMESCALE for a copy of the license.

\c :TEST_DBNAME :ROLE_SUPERUSER

-- Check that we can use run_job() with the telemetry job, so first
-- locate the job id for it (should be 1, but who knows, and it is not
-- important for this test).
SELECT id AS job_id FROM _timescaledb_config.bgw_job
 WHERE proc_schema = '_timescaledb_internal'
   AND proc_name = 'policy_telemetry' \gset

-- It should be possible to run it twice and running it should change
-- the last_finish time. Since job_stats can be empty to start with,
-- we run it once first to populate job_stats.
CALL run_job(:job_id);

SELECT last_finish AS last_finish
  FROM _timescaledb_internal.bgw_job_stat
 WHERE job_id = :job_id \gset
SELECT pg_sleep(1);

CALL run_job(:job_id);

SELECT last_finish > :'last_finish' AS job_executed,
       last_run_success
  FROM _timescaledb_internal.bgw_job_stat
 WHERE job_id = :job_id;

-- Running it as the default user should fail since they do not own
-- the job. This should be the case also for the telemetry job, which
-- is a little special.
\c :TEST_DBNAME :ROLE_DEFAULT_PERM_USER
\set ON_ERROR_STOP 0
CALL run_job(:job_id);
\set ON_ERROR_STOP 1

