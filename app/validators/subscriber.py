from fastapi import HTTPException


def validate_subscriber_person_id(person_id):
    """Validate that person_id is provided for subscriber."""
    if not person_id:
        raise HTTPException(
            status_code=400,
            detail="Subscriber requires person_id",
        )
