"""Generated data sources (in-memory/temporary files) and their project tokens.

Rasters (and later meshes) are backed by files the plugin generates, which do
not survive a QGIS restart. When a project is saved, each such source is
written as a token naming what it was built from, ``geoh5:<file>|<uid>|<part>``;
when the project is reopened the token is turned back into a freshly built
source (see restore.preprocess_source), so the layer opens valid.
"""

TOKEN_PREFIX = "geoh5:"
_SEPARATOR = "|"

# generated source -> (geoh5 path, object UID, part)
_GENERATED: dict[str, tuple[str, str, str]] = {}


def register_generated(source: str, path: str, uid: str, part: str) -> None:
    """Record that ``source`` was generated from a geoh5 object's layer part.

    Args:
        source: The layer's data source (e.g. a /vsimem or temporary file path).
        path: The geoh5 file.
        uid: The object UID.
        part: The layer's part (see layers.PART_PROPERTY).
    """
    _GENERATED[source] = (str(path), uid, part)


def token_for(source: str) -> str | None:
    """The project token for a generated source, or None if not generated here.

    Args:
        source: A layer data source.

    Returns:
        ``geoh5:<file>|<uid>|<part>``, or None.
    """
    origin = _GENERATED.get(source)
    return TOKEN_PREFIX + _SEPARATOR.join(origin) if origin else None


def parse_token(text: str) -> tuple[str, str, str] | None:
    """Split a project token into (geoh5 path, object UID, part).

    Args:
        text: A data source as read from a project.

    Returns:
        The parts, or None if ``text`` is not a token.
    """
    if not text.startswith(TOKEN_PREFIX):
        return None
    pieces = text[len(TOKEN_PREFIX) :].rsplit(_SEPARATOR, 2)
    return tuple(pieces) if len(pieces) == 3 else None  # type: ignore[return-value]
