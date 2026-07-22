# Local Neo4j setup

Run a local Neo4j developer container and seed the demo data.

1. Start Neo4j with Docker Compose:

```bash
docker compose -f docker-compose.neo4j.yml up -d
```

This will start Neo4j with username `neo4j` and password `test` (set in the compose file).

2. Seed the database using the provided script:

```bash
.venv\Scripts\python.exe scripts/seed_neo4j.py
```

Or, on Unix:

```bash
venv/bin/python scripts/seed_neo4j.py
```

3. Configure your app environment to use Neo4j (example `.env`):

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=test
```

4. Start the API and exercise endpoints. The app will automatically use `Neo4jStore` when `NEO4J_URI` is set.
