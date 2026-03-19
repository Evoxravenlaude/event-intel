from app.db.session import SessionLocal
from app.services.clustering import cluster_signals


def main() -> None:
    db = SessionLocal()
    try:
        created, linked, queued = cluster_signals(db)
        print({"created": created, "linked": linked, "queued": queued})
    finally:
        db.close()


if __name__ == "__main__":
    main()
