from pydantic import ValidationError

from app.settings import Settings


def validate_configuration() -> tuple[bool, list[str]]:
    try:
        settings = Settings()
    except ValidationError as error:
        messages = [
            f"{'.'.join(str(part) for part in item['loc']) or 'configuration'}: {item['msg']}"
            for item in error.errors()
        ]
        return False, messages
    if settings.environment != "production":
        return False, ["environment: preflight requires COMPANY_AGENT_ENVIRONMENT=production"]
    return True, []


def main() -> int:
    valid, messages = validate_configuration()
    if valid:
        print("production configuration preflight passed")
        return 0
    print("production configuration preflight failed")
    for message in messages:
        print(f"- {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
