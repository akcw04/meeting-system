"""Central configuration loaded once from environment / .env file.

Every other module imports `settings` from here, so we never sprinkle
os.environ calls or hardcoded paths across the codebase.
"""
import os
import shutil
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    hf_token: str = ""

    # Optional absolute path to ffmpeg.exe. Leave blank to look it up on PATH.
    # Useful when PATH can't be modified, or PATH propagation is unreliable
    # (e.g. OneDrive-synced install locations on Windows).
    ffmpeg_path: str = ""

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    # Per-LLM-call HTTP timeout (s) and max output tokens. Constrained generation
    # on the 8B model is slow for Chinese (~2.5 tok/s), so a long timeout prevents
    # premature ReadTimeout; num_predict caps any runaway (bug #23).
    ollama_timeout: float = 1800.0
    ollama_num_predict: int = 2048
    ollama_repeat_penalty: float = 1.3  # >1 discourages repetition loops (esp. Chinese)

    whisper_model: str = "large-v3"  # IR-specified ASR model (Int8); 'small' = fast iteration
    whisper_device: str = "cuda"
    whisper_compute_type: str = "int8_float16"
    # Silero VAD pre-filter threshold inside Faster-Whisper (0-1; higher = stricter
    # about what counts as speech). 0.5 is Silero's default. Tunable for Phase 10.
    whisper_vad_threshold: float = 0.5
    # Batched transcription: parallel VAD windows for ~3-4x speed on long audio.
    # Set to 1 to disable (sequential); lower (e.g. 4) if VRAM is tight.
    whisper_batch_size: int = 8

    # Speaker over-detection guard (Phase 9 polish). After diarization, any
    # speaker whose total talk-time is below this many seconds is treated as a
    # phantom (an over-segmentation artifact) and merged into its acoustically
    # nearest neighbour via speaker embeddings. Applied ONLY when the user did
    # not supply expected_speakers (an explicit count already forces exact
    # clustering). Tunable here so Phase 10 evaluation can sweep it.
    phantom_max_seconds: float = 3.0

    # Maximum upload size in GB, enforced server-side during the streaming write.
    # Raise for larger recordings; mind free disk + processing time.
    max_upload_gb: float = 4.0

    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/app.db")
    upload_dir: Path = Path("./data/uploads")
    output_dir: Path = Path("./data/outputs")
    models_dir: Path = Path("./data/models")
    # User-uploaded export templates (.docx). Filled with a meeting's data at
    # export time so organisations can produce minutes in their own house style.
    templates_dir: Path = Path("./data/templates")

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.output_dir,
                  self.models_dir, self.templates_dir):
            d.mkdir(parents=True, exist_ok=True)

    def setup_ffmpeg_path(self) -> None:
        """Make sure ffmpeg is reachable by ANY subprocess we spawn.

        Several libraries (whisperx, torchcodec) call a bare `ffmpeg`
        command internally and ignore our FFMPEG_PATH setting. To cover
        all of them at once, we prepend ffmpeg's directory to this
        process's PATH. Child processes inherit it.

        No-op if ffmpeg is already resolvable on PATH.
        """
        ffmpeg_dir: str | None = None
        if self.ffmpeg_path and Path(self.ffmpeg_path).is_file():
            ffmpeg_dir = str(Path(self.ffmpeg_path).parent)
        elif shutil.which("ffmpeg") is None:
            # Not on PATH and no explicit path given - nothing we can do here;
            # _resolve_ffmpeg() in audio.py will raise a clear error later.
            return

        if ffmpeg_dir:
            current = os.environ.get("PATH", "")
            if ffmpeg_dir not in current.split(os.pathsep):
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + current


settings = Settings()
# Run once at import time so ffmpeg is on PATH before any library is used.
settings.setup_ffmpeg_path()
