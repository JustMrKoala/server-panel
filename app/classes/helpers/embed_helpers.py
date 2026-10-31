"""Pure helpers for building Open Graph / Twitter-card embed metadata.

Kept free of Tornado/DB imports so it can be unit-tested in isolation.
"""

DEFAULT_TITLE = "Server Panel"
DEFAULT_DESCRIPTION = "Manage your game servers."
DEFAULT_IMAGE = "/static/assets/images/Crafty_4-0.png"
DEFAULT_COLOR = "#005cd1"


def build_embed_meta(settings, base_url, page_path):
    """Resolve raw embed settings into render-ready values.

    Args:
        settings: dict from ManagementController.get_embed_settings() with keys
            enabled, title, description, image, color.
        base_url: absolute origin, e.g. "https://panel.example.com" (no
            trailing slash).
        page_path: path for og:url, e.g. "/login" or "/status".

    Returns:
        dict with title, description, image, url, color - or None if embeds
        are disabled.
    """
    if not settings.get("enabled"):
        return None

    image = settings.get("image") or ""
    if image:
        image_path = f"/static/assets/images/embed/{image}"
    else:
        image_path = DEFAULT_IMAGE

    return {
        "title": settings.get("title") or DEFAULT_TITLE,
        "description": settings.get("description") or DEFAULT_DESCRIPTION,
        "image": f"{base_url}{image_path}",
        "url": f"{base_url}{page_path}",
        "color": settings.get("color") or DEFAULT_COLOR,
    }
