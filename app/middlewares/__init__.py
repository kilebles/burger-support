from app.middlewares.album import AlbumMiddleware
from app.middlewares.dedup import DeduplicationMiddleware

__all__ = ["AlbumMiddleware", "DeduplicationMiddleware"]
