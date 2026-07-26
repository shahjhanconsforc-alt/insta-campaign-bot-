"""Seed the store with sample records:  python -m backend.data.seed"""
from backend import db

SAMPLES = [
    {"type": "note", "title": "Agent booted", "body": "First run of the insta-bot agent."},
    {"type": "target", "title": "Instagram home", "body": "https://www.instagram.com"},
    {"type": "note", "title": "Storage", "body": "MinIO for handles/bios, JSON file for runs."},
]


def main() -> None:
    db.clear()
    for sample in SAMPLES:
        db.insert(sample)
    print(f"Seeded {len(SAMPLES)} records -> {db.status()['file']}")


if __name__ == "__main__":
    main()
