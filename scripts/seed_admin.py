import argparse

from dotenv import load_dotenv

from app.db import SessionLocal
from app.models.auth import AuthProvider, UserCredential
from app.models.subscriber import Subscriber
from app.services.auth_flow import hash_password


def parse_args():
    parser = argparse.ArgumentParser(description="Seed an admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--first-name", required=True)
    parser.add_argument("--last-name", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--force-reset", action="store_true")
    return parser.parse_args()


def main():
    load_dotenv()
    args = parse_args()
    db = SessionLocal()
    try:
        subscriber = db.query(Subscriber).filter(Subscriber.email == args.email).first()
        if not subscriber:
            subscriber = Subscriber(
                first_name=args.first_name,
                last_name=args.last_name,
                email=args.email,
            )
            db.add(subscriber)
            db.commit()
            db.refresh(subscriber)

        credential = (
            db.query(UserCredential)
            .filter(UserCredential.subscriber_id == subscriber.id)
            .filter(UserCredential.provider == AuthProvider.local)
            .first()
        )
        if credential:
            print("User credential already exists for this subscriber.")
            return

        credential = UserCredential(
            subscriber_id=subscriber.id,
            provider=AuthProvider.local,
            username=args.username,
            password_hash=hash_password(args.password),
            must_change_password=args.force_reset,
        )
        db.add(credential)
        db.commit()
        print("Admin user created.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
