from pathlib import Path
from app.services.graph_store import GraphStore
from app.services.ingestion import seed_demo_data

store = GraphStore(data_dir=Path('data'))
seed_demo_data(store, data_dir=Path('data'))
print(len(store.nodes))
for key, value in store.nodes.items():
    print(key, value.get('label'))
