"""Local visual-similarity RAG based on sampled-frame perceptual hashes."""
from __future__ import annotations

import io
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image

from video_crawler import ffmpeg_executable


def difference_hash(image: Image.Image, size: int = 8) -> str:
    gray = image.convert("L").resize((size + 1, size))
    pixels = list(gray.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | int(pixels[row * (size + 1) + col] > pixels[row * (size + 1) + col + 1])
    return f"{bits:0{size * size // 4}x}"


def hash_similarity(left: str, right: str) -> float:
    width = max(len(left), len(right)) * 4
    if not left or not right or width <= 0:
        return 0.0
    return 1.0 - ((int(left, 16) ^ int(right, 16)).bit_count() / width)


def extract_video_hashes(video_path: Path, max_frames: int = 12) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="paqu-rag-") as temp_dir:
        pattern = str(Path(temp_dir) / "frame_%03d.jpg")
        subprocess.run(
            [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-i", str(video_path),
             "-vf", "fps=1/5,scale=320:-2", "-frames:v", str(max_frames), pattern],
            check=True,
        )
        hashes = []
        for frame in sorted(Path(temp_dir).glob("frame_*.jpg")):
            with Image.open(frame) as image:
                hashes.append(difference_hash(image))
        if not hashes:
            raise ValueError("无法从视频中提取关键帧。")
        return hashes


def hash_image_url(url: str, timeout: float = 15.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read(10 * 1024 * 1024)
    with Image.open(io.BytesIO(content)) as image:
        return difference_hash(image)


def best_match(query_hash: str, samples: list[tuple[int, list[str]]]) -> tuple[int | None, float]:
    best_id = None
    best_score = 0.0
    for sample_id, hashes in samples:
        for frame_hash in hashes:
            score = hash_similarity(query_hash, frame_hash)
            if score > best_score:
                best_id, best_score = sample_id, score
    return best_id, best_score
