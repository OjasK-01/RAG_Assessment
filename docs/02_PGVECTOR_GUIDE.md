# pgvector: a guide for people who already know vector databases

**In one line:** pgvector turns Postgres into a vector database by adding a `vector` column type, distance
operators and ANN indexes. Your vectors, metadata, full-text index and business data then live in one table and are
queried with ordinary SQL.

This project runs **Postgres 17.11 with pgvector 0.8.7** in Docker (`pgvector/pgvector:pg17`) on `127.0.0.1:5433`.

---

## 1. Concept map: what you know, and its pgvector equivalent

| Pinecone / Qdrant / Weaviate / Chroma | pgvector |
|---|---|
| Index / collection | A **table** with a `vector(n)` column |
| Namespace | A separate table, a schema, or just a `WHERE tenant = …` column |
| Record id | Primary key column (`chunk_id TEXT PRIMARY KEY`) |
| Metadata / payload | Ordinary typed columns (`status TEXT`, `effective_date DATE`) or `JSONB` |
| `upsert(id, vector, metadata)` | `INSERT … ON CONFLICT (chunk_id) DO UPDATE SET …` |
| `query(vector, top_k=3, filter=…)` | `SELECT … WHERE status='current' ORDER BY embedding <=> $1 LIMIT 3` |
| Metadata filter DSL | SQL `WHERE`: any expression, join or subquery |
| Delete by filter | `DELETE FROM kb_chunks WHERE NOT (doc_id = ANY($1))` |
| Hybrid / sparse search | Postgres full-text (`tsvector` + GIN index) in the **same query or table**, fused in SQL or code |
| Index type (HNSW, IVF) | `CREATE INDEX … USING hnsw` or `USING ivfflat` (optional) |
| Similarity metric set per index | Chosen **per query** by operator, and per index by operator class |
| Consistency model | Full ACID transactions: an upsert and a delete commit atomically |

The big mental shift is that **there is no special query API. Search is a SQL `ORDER BY` on a distance operator.**

---

## 2. The building blocks

### 2.1 Enable it

```sql
CREATE EXTENSION IF NOT EXISTS vector;   -- once per database (we do this in db.init_schema)
```

### 2.2 Types

| Type | What it stores | Max dims | Notes |
|---|---|---|---|
| `vector(n)` | 4-byte floats | 16,000 stored · **2,000 indexable** | what we use: `vector(768)` |
| `halfvec(n)` | 2-byte floats | 16,000 stored · 4,000 indexable | half the memory, tiny recall loss |
| `sparsevec(n)` | sparse vectors (SPLADE-style) | 16,000 non-zeros indexable | learned sparse retrieval |
| `bit(n)` | binary vectors | 64,000 indexable | binary quantisation, Hamming/Jaccard |

The dimension is part of the column type, so a 1536-dim vector can't be inserted into `vector(768)`. That's why
`EMBED_DIM` is fixed in `.env` and changing it requires `ingest --reset`.

### 2.3 Distance operators

| Operator | Distance | Use with |
|---|---|---|
| `<->` | Euclidean (L2) | un-normalised embeddings |
| `<=>` | **Cosine distance** = 1 − cosine similarity | **what we use** |
| `<#>` | **Negative** inner product (so smaller = better) | normalised vectors (fastest) |
| `<+>` | Manhattan (L1) | |
| `<~>` / `<%>` | Hamming / Jaccard | `bit` vectors |

Cosine **similarity** is `1 - (a <=> b)`. That number is what the relevance gate compares with its calibrated threshold.
Gemini embeddings at reduced dimensions aren't unit-length (our stored vectors have a norm of about 0.6), and cosine distance
handles that because it normalises internally. That's why we use `<=>` and not the faster `<#>`.

### 2.4 Indexes (approximate nearest neighbour)

| | No index | HNSW | IVFFlat |
|---|---|---|---|
| Search | Exact (sequential scan) | Graph-based ANN | Clustered ANN |
| Recall | 100% | ~95–99% (tunable) | lower, depends on `probes` |
| Build | — | slower, more memory, can build on an empty table | fast, needs data first (k-means on existing rows) |
| Key knobs | — | `m` (16), `ef_construction` (64); query `SET hnsw.ef_search = 40` | `lists` (≈ rows/1000); query `SET ivfflat.probes = 1` |
| When | < ~10–100 k rows | the default choice at scale | very large, memory-constrained |

```sql
-- what we would add once the KB grows past ~10k chunks (not needed for 7 rows):
CREATE INDEX kb_chunks_embedding_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops);
```

The index's **operator class must match the query operator**: `vector_cosine_ops` for `<=>`, `vector_l2_ops` for `<->`,
`vector_ip_ops` for `<#>`. The planner uses the index only for `ORDER BY embedding <op> $1 LIMIT k`.

**Filtering with an ANN index:** the index returns its nearest candidates first, and `WHERE` is then applied to them, so a selective
filter can leave fewer than `k` rows. pgvector ≥ 0.8 fixes this with iterative scans (`SET hnsw.iterative_scan = relaxed_order`), which our
0.8.7 supports. With **no** index, as here, filtering is exact: Postgres filters first, then sorts.

---

## 3. How this project uses it

### 3.1 Schema (`backend/app/db.py`)

```sql
CREATE TABLE kb_chunks (
    chunk_id       TEXT PRIMARY KEY,            -- = doc_id (one chunk per article)
    doc_id         TEXT NOT NULL,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL,               -- only 'current' is ever admitted, and queries filter on it again
    effective_date DATE NOT NULL,
    owner          TEXT NOT NULL,
    section        TEXT NOT NULL,               -- 'full' today; ready for heading-level chunks
    content        TEXT NOT NULL,               -- the article body shown to the LLM
    content_hash   TEXT NOT NULL,               -- skip re-embedding unchanged articles
    embedding      vector(768) NOT NULL,        -- the pgvector column
    tsv tsvector GENERATED ALWAYS AS            -- full-text vector, kept in sync automatically
        (to_tsvector('english', title || ' ' || content)) STORED
);
CREATE INDEX kb_chunks_tsv_idx    ON kb_chunks USING GIN (tsv);   -- keyword search
CREATE INDEX kb_chunks_status_idx ON kb_chunks (status);          -- metadata filter

CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- provider / embed_model / embed_dim / ingested_at: which model built the vectors
-- sim_threshold / grey_band / calib_*: the relevance gate calibrated for that model
```

The same database also holds the app's shared state. These are ordinary tables with no vectors, but keeping them here means
one service to run and every API worker sees the same data:

| Table | Holds |
|---|---|
| `chat_turns` | conversation history (masked question, searched query, answer, citations), keyed by conversation **and** account, purged after a TTL |
| `kv_cache` | `JSONB` cache of query embeddings and LLM responses, namespaced by provider and model |
| `rate_limits` | per-minute request counters, incremented with one atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING` |

Note the **generated `tsv` column**: Postgres maintains the keyword index itself on every insert or update, so hybrid search
needs no second system and can't drift out of sync.

### 3.2 Writing (ingestion)

```python
from pgvector.psycopg import register_vector   # teaches psycopg to send Python lists as vector
register_vector(conn)
conn.execute("""
  INSERT INTO kb_chunks (chunk_id, doc_id, title, status, effective_date, owner, section, content, content_hash, embedding)
  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
  ON CONFLICT (chunk_id) DO UPDATE SET content = EXCLUDED.content, embedding = EXCLUDED.embedding, …""",
  (..., vec))                                   # vec = list[float] from the embedding API
conn.execute("DELETE FROM kb_chunks WHERE NOT (doc_id = ANY(%s))", (admitted_ids,))   -- retire docs
conn.commit()                                   # upserts + deletes land atomically
```

### 3.3 Reading (hybrid retrieval, `backend/app/retrieval.py`)

**Dense leg** (semantic):
```sql
SELECT doc_id, title, content, 1 - (embedding <=> %s::vector) AS sim
FROM kb_chunks
WHERE status = 'current'
ORDER BY embedding <=> %s::vector
LIMIT 8;
```

**Sparse leg** (keywords). The question becomes an OR-query of its lexemes, so one matching term is enough:
```sql
-- 'zentunnel' | 'show' | 'zt-401'
SELECT doc_id, ts_rank_cd(tsv, q) AS r
FROM kb_chunks, to_tsquery('english', %s) q
WHERE status = 'current' AND tsv @@ q
ORDER BY r DESC LIMIT 8;
```

**Fusion:** Reciprocal Rank Fusion in Python, `score = Σ 1/(60 + rank)` over both lists. Then the **relevance gate**
keeps an article if any of these holds:
- `sim ≥ threshold`;
- the question contains an identifier (`ZT-401`, `0x80070005`) that appears in the article verbatim;
- `threshold − band ≤ sim < threshold`, it is the top vector hit, **and** the keyword leg scored it ≥ 0.2 (two matching terms);
- it was cited in the previous turn of a follow-up question.

The threshold and band are calibrated for the embedding model whenever the index is built and stored in `index_meta`
(Gemini: 0.562 and 0.04). The top 3 go to the LLM.

Both legs come from the same table, so combining them is just two SQL queries. That's why the keyword score was
available to fix the gate when cosine alone turned out to overlap between in-scope and off-topic questions.

Real scores for "ZenTunnel shows ZT-401":
```
KB-007  sim 0.657  keyword 1.000  code_match ✓   → used
KB-005  sim 0.484  keyword 0.000                 → gated out
KB-006  sim 0.482  keyword 0.000                 → gated out
```

### 3.4 Why no ANN index here

With 7 rows (176 kB including indexes), an exact sequential scan has perfect recall and took **0.075 ms**
(`EXPLAIN ANALYZE`: Seq Scan, then top-N heapsort). An HNSW index
would add build cost and approximation for no gain. The `CREATE INDEX … USING hnsw` line above is the one-line change once the KB grows.

---

## 4. Try it yourself

```bash
# open a SQL shell inside the container
docker exec -it vectra-pgvector psql -U rag -d vectra_kb
```

```sql
\dx                                                    -- installed extensions (vector 0.8.7)
\d kb_chunks                                           -- table definition
SELECT doc_id, title, status, effective_date FROM kb_chunks ORDER BY doc_id;
SELECT * FROM index_meta;                              -- which embedding model built the index
SELECT doc_id, vector_dims(embedding), round(vector_norm(embedding)::numeric, 3) FROM kb_chunks;

-- similarity between articles (no API call needed: compare stored vectors)
SELECT a.doc_id, b.doc_id, round((1 - (a.embedding <=> b.embedding))::numeric, 3) AS cosine
FROM kb_chunks a JOIN kb_chunks b ON a.doc_id < b.doc_id
ORDER BY cosine DESC LIMIT 5;

-- nearest neighbours of KB-007 (uses KB-007's own vector as the query)
SELECT doc_id, round((1 - (embedding <=> (SELECT embedding FROM kb_chunks WHERE doc_id='KB-007')))::numeric, 3) AS sim
FROM kb_chunks ORDER BY embedding <=> (SELECT embedding FROM kb_chunks WHERE doc_id='KB-007') LIMIT 3;

-- keyword leg on its own
SELECT doc_id, ts_rank_cd(tsv, q) FROM kb_chunks, to_tsquery('english', '''zt-401'' | ''zentunnel''') q
WHERE tsv @@ q ORDER BY 2 DESC;

-- see the plan (Seq Scan today; Index Scan using hnsw once that index exists)
EXPLAIN ANALYZE SELECT doc_id FROM kb_chunks ORDER BY embedding <=> (SELECT embedding FROM kb_chunks LIMIT 1) LIMIT 3;
```

---

## 5. When pgvector is (and isn't) the right choice

**Good fit (this project):** you already run Postgres or want one system. You need rich metadata filters, joins, transactions,
or keyword plus vector search together. Scale runs from thousands to tens of millions of vectors.

**Consider a dedicated vector DB when:** you have billions of vectors, need managed multi-region scaling with no DB operations,
need very high query rates on huge indexes, or want built-in features like multi-vector or late-interaction search out of the box.

**Operational notes:**
- HNSW indexes should fit in RAM (`maintenance_work_mem` speeds up builds).
- `VACUUM` and `ANALYZE` matter as with any Postgres table.
- Back up with the usual Postgres tools (`pg_dump`, WAL archiving). Your vectors are backed up with everything else.
