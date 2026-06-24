"""Local video intake: a drop folder -> transcript + sample frames -> a creative brief.

Pipeline (all local, no Meta API): extract audio + a few frames with ffmpeg, transcribe with
faster-whisper, and write a structured "creative brief" JSON. The agent (Claude Code) then reads
the brief to generate the 5 primary texts / headlines / descriptions and to pick the best ad set.

Heavy dependencies are intentionally NOT imported at module load:
- ffmpeg / ffprobe: system binaries (install separately; checked at runtime).
- faster-whisper: optional `media` extra (`pip install -e .[media]`); imported lazily.
Both ffmpeg calls and transcription are injectable (``runner`` / ``transcriber``) so the logic is
unit-testable without the binaries or model installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .config import PROJECT_ROOT
from .utils import ensure_dir, write_json

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
DEFAULT_INTAKE_ROOT = PROJECT_ROOT / "data" / "video_intake"


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(
            f"`{name}` was not found on PATH. Install ffmpeg (which provides ffmpeg + ffprobe) "
            "to process videos locally."
        )


def get_duration_seconds(video_path: Path, *, runner: Callable = subprocess.run) -> float:
    """Video duration in seconds via ffprobe."""
    _require_binary("ffprobe")
    completed = runner(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True, check=False,
    )
    raw = (getattr(completed, "stdout", "") or "").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


def frame_timestamps(duration: float, count: int) -> list[float]:
    """Evenly spaced timestamps (avoiding the very start/end) for `count` frames."""
    if count <= 0 or duration <= 0:
        return []
    return [round(duration * (i + 1) / (count + 1), 2) for i in range(count)]


def extract_frames(
    video_path: Path, out_dir: Path, *, count: int = 4, duration: float | None = None,
    runner: Callable = subprocess.run,
) -> list[Path]:
    """Grab `count` evenly-spaced JPEG frames; returns their paths."""
    _require_binary("ffmpeg")
    ensure_dir(out_dir)
    if duration is None:
        duration = get_duration_seconds(video_path, runner=runner)
    paths: list[Path] = []
    for i, ts in enumerate(frame_timestamps(duration, count)):
        out = out_dir / f"frame_{i + 1:02d}.jpg"
        runner(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(out)],
            capture_output=True, text=True, check=False,
        )
        paths.append(out)
    return paths


def extract_audio(video_path: Path, out_path: Path, *, runner: Callable = subprocess.run) -> Path:
    """Extract mono 16kHz wav audio (what whisper wants)."""
    _require_binary("ffmpeg")
    ensure_dir(out_path.parent)
    runner(
        ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(out_path)],
        capture_output=True, text=True, check=False,
    )
    return out_path


def transcribe(audio_path: Path, *, model_size: str = "base", transcriber: Callable | None = None) -> dict[str, Any]:
    """Transcribe audio. Inject ``transcriber`` for tests; otherwise lazy-load faster-whisper."""
    if transcriber is not None:
        return transcriber(audio_path)
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "faster-whisper is not installed. Install the media extra: pip install -e .[media]"
        ) from exc
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(audio_path))
    seg_list = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    return {
        "text": " ".join(s["text"] for s in seg_list).strip(),
        "language": getattr(info, "language", None),
        "duration": getattr(info, "duration", None),
        "segments": seg_list,
    }


def build_brief(
    video_path: Path, *, transcript: dict[str, Any], frame_paths: list[Path], account_slug: str | None,
    duration: float | None = None,
) -> dict[str, Any]:
    """Assemble the creative brief the agent will turn into ad copy + an ad-set choice."""
    return {
        "schema_version": 1,
        "kind": "creative_brief",
        "account_slug": account_slug,
        "generated_at": _now_iso(),
        "video": {"file": video_path.name, "path": str(video_path), "duration_seconds": duration},
        "transcript": transcript.get("text", ""),
        "language": transcript.get("language"),
        "segments": transcript.get("segments", []),
        "frames": [str(p) for p in frame_paths],
        "next_steps": (
            "Agent: read the transcript (and optionally the frames) and the account profile + "
            "knowledge/ad_copy_best_practices.md, then propose 5 primary texts, 5 headlines, and 5 "
            "descriptions, plus the best-fit ad set. Then use propose-video-ad with the chosen copy."
        ),
        "copy_options": {"primary_texts": [], "headlines": [], "descriptions": []},
        "suggested_adset": None,
    }


def process_video(
    video_path: Path, *, account_slug: str | None = None, work_dir: Path | None = None,
    model_size: str = "base", frame_count: int = 4,
    runner: Callable = subprocess.run, transcriber: Callable | None = None,
) -> dict[str, Any]:
    """Full local pipeline for one video; writes <work_dir>/creative_brief.json and returns the brief."""
    video_path = Path(video_path)
    if work_dir is None:
        work_dir = DEFAULT_INTAKE_ROOT / (account_slug or "_") / "processed" / video_path.stem
    work_dir = Path(work_dir)
    ensure_dir(work_dir)

    duration = get_duration_seconds(video_path, runner=runner)
    audio_path = extract_audio(video_path, work_dir / "audio.wav", runner=runner)
    transcript = transcribe(audio_path, model_size=model_size, transcriber=transcriber)
    if duration <= 0:
        duration = transcript.get("duration") or 0.0
    frame_paths = extract_frames(video_path, work_dir / "frames", count=frame_count, duration=duration, runner=runner)

    brief = build_brief(
        video_path, transcript=transcript, frame_paths=frame_paths, account_slug=account_slug, duration=duration
    )
    write_json(work_dir / "creative_brief.json", brief)
    brief["brief_path"] = str(work_dir / "creative_brief.json")
    return brief


def inbox_dir(account_slug: str, intake_root: Path = DEFAULT_INTAKE_ROOT) -> Path:
    return intake_root / account_slug / "inbox"


def list_inbox_videos(account_slug: str, intake_root: Path = DEFAULT_INTAKE_ROOT) -> list[Path]:
    folder = inbox_dir(account_slug, intake_root)
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS)
