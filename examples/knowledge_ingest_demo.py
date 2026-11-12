"""Ingest the synthetic industrial sample through the real knowledge service.

This is a local, repeatable product example. It uses only the checked-in
`synthetic-industrial-v1` fixture, never reads customer material and makes no
model or network request. Run it with the server virtual environment:

    python examples/knowledge_ingest_demo.py

Optional flags:
    --data-dir PATH   Keep the generated originals and SQLite database at PATH.
    --background      Upload as background jobs and let the worker process them.
"""
import argparse
import sys
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "server"))

from app.config import Settings  # noqa: E402
from app.service import Service  # noqa: E402

FIXTURE = REPOSITORY / "fixtures" / "synthetic-industrial-v1"
DOCUMENTS = ("documents/quality-policy.md", "tables/quality-batches.csv")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--background", action="store_true", help="use the bounded background ingestion worker")
    args = parser.parse_args()

    root = args.data_dir or Path(tempfile.mkdtemp(prefix="knowledge-ingest-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    service = Service(Settings(data_dir=root, provider_settings="", model="test"))
    try:
        user = service.create_user({
            "username": "demo-owner", "display_name": "Synthetic demo owner",
            "password": "demo-password-123", "role": "member",
        })
        kb = service.knowledge.save({"name": "合成工业资料", "description": "synthetic-industrial-v1，仅用于开发验收"}, user)
        print(f"limits: {service.knowledge.ingestion_limits()}")
        for relative in DOCUMENTS:
            path = FIXTURE / relative
            data = path.read_bytes()
            document = service.knowledge.upload(kb["id"], user, path.name, data, wait=not args.background)
            if args.background:
                service.knowledge.process_pending()
                document = service.knowledge.version(document["id"], None, user)
            job = document["ingestion"]
            stages = [event["kind"] for event in job["events"] if event["kind"].startswith("stage:")]
            print(
                f"{document['name']}: v{document['version']} {document['status']} "
                f"pages={document['page_count']} chunks={document['chunk_count']} "
                f"sha256={document['sha256']} job={job['status']} attempts={job['attempts']}"
            )
            print(f"  stages: {' -> '.join(stages)}")
        result = service.knowledge.search([kb["id"]], user, "加权合格率", limit=3)
        print(f"retrieval: {result['retrieval']}")
        for hit in result["results"][:2]:
            print(f"hit: v{hit['version']} page={hit.get('page')} score={hit.get('score')} {hit.get('text', '')[:80]}")
        print(f"data-dir: {root}")
    finally:
        service.knowledge.stop_worker()


if __name__ == "__main__":
    main()
