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


def seed_admin_user(
    db,
    *,
    email: str,
    first_name: str,
    last_name: str,
    username: str,
    password: str,
    force_reset: bool = False,
) -> str:
    subscriber = db.query(Subscriber).filter(Subscriber.email == email).first()
    if not subscriber:
        subscriber = Subscriber(
            first_name=first_name,
            last_name=last_name,
            email=email,
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
        credential.username = username
        credential.password_hash = hash_password(password)
        credential.must_change_password = force_reset
        credential.is_active = True
        credential.failed_login_attempts = 0
        credential.locked_until = None
        db.commit()
        return "Admin user updated."

    credential = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=hash_password(password),
        must_change_password=force_reset,
    )
    db.add(credential)
    db.commit()
    return "Admin user created."


def main():
    load_dotenv()
    args = parse_args()
    db = SessionLocal()
    try:
        print(
            seed_admin_user(
                db,
                email=args.email,
                first_name=args.first_name,
                last_name=args.last_name,
                username=args.username,
                password=args.password,
                force_reset=args.force_reset,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
