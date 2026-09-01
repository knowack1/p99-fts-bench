-- Kept separate from schema.cql so the ingest benchmark can choose ordering:
--   index first, then load  -> exercises the CDC tail path
--   load first, then index  -> exercises the bootstrap base-table scan
-- Wait for the index to reach SERVING before querying (vector-store returns
-- 503 until its initial scan completes).

CREATE CUSTOM INDEX IF NOT EXISTS articles_body_fts
  ON wiki.articles(body) USING 'fulltext_index';
