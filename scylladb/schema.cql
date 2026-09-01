-- Wikipedia corpus schema for the FTS benchmark.
-- article_id is the sole partition key: M1 FTS rejects any WHERE restriction
-- besides the BM25() clause, so the key is pure identity (uuid5 of the
-- Wikipedia page id, assigned by ftsbench.prepare_corpus).
-- RF=1 is for the single-node pipeline shakeout; the real benchmark locks
-- node count and RF per the fairness rules in the concept doc.

CREATE KEYSPACE IF NOT EXISTS wiki
  WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 1};

CREATE TABLE IF NOT EXISTS wiki.articles (
    article_id uuid PRIMARY KEY,
    page_id bigint,
    title text,
    body text
);
