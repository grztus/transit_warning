"""Privacy guards shared by ordinary application API contracts."""


PRIVATE_KEY_TOKENS = (
    "latitude", "longitude", "observer_lat", "observer_lon",
    "filesystem_path", "file_path", "manifest_path", "session_directory",
    "bot_token", "telegram_token", "chat_id", "secret",
)


def assert_public_payload(value):
    """Reject keys reserved for private/internal data in ordinary API DTOs."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(token in normalized for token in PRIVATE_KEY_TOKENS):
                raise ValueError("Private field in public application payload")
            assert_public_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_public_payload(item)
    return value
