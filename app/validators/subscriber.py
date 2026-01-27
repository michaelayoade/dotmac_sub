from fastapi import HTTPException


def validate_subscriber_email(email: str | None):
    """Validate that email is provided for subscriber."""
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Subscriber requires email",
        )


def validate_subscriber_name(first_name: str | None, last_name: str | None):
    """Validate that name is provided for subscriber."""
    if not first_name or not last_name:
        raise HTTPException(
            status_code=400,
            detail="Subscriber requires first_name and last_name",
        )
