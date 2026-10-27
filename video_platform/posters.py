"""CPU first-frame extraction from this project's existing result videos."""

from pathlib import Path
import tempfile
from threading import Lock

_poster_lock = Lock()


def ensure_poster(video_path):
    video_path = Path(video_path)
    poster = video_path.parent / "poster.jpg"
    with _poster_lock:
        if poster.is_file() and not poster.is_symlink():
            return poster
        if poster.is_symlink() or video_path.is_symlink():
            raise ValueError("Do not follow result symlinks.")
        import imageio.v2 as imageio
        from PIL import Image

        reader = imageio.get_reader(str(video_path), format="ffmpeg")
        try:
            image = Image.fromarray(reader.get_data(0)).convert("RGB")
        finally:
            reader.close()
        image.thumbnail((960, 960))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=poster.parent, prefix=".poster-", suffix=".jpg", delete=False
            ) as stream:
                temporary = Path(stream.name)
                image.save(stream, format="JPEG", quality=92)
            temporary.replace(poster)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return poster
