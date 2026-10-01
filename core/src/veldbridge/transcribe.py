"""Voice notes -> English text, locally (faster-whisper). Off the event loop, one at a time.

wa/ saves each voice note under the audio dir and passes its path along with the
message. The bridge either submits it here or discards it; every file is deleted once
it's been used, so audio never piles up on disk.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import TranscriptionConfig

log = logging.getLogger(__name__)

# (message id, English text or None, detected language or None, error or None)
OnDone = Callable[[int, str | None, str | None, str | None], None]


class Transcriber(Protocol):
    def submit(self, message_id: int, path: str) -> None: ...
    def discard(self, path: str | None) -> None: ...


def _inside(path: str, root: str) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _remove(path: str | None, root: str) -> None:
    """Delete a voice file, but only ever inside the audio dir."""
    if not path or not _inside(path, root):
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("could not delete voice file: %s", e)


class NullTranscriber:
    """Transcription off: just clean up whatever wa saved."""

    def __init__(self, audio_dir: str):
        self.audio_dir = audio_dir

    def submit(self, message_id: int, path: str) -> None:
        self.discard(path)

    def discard(self, path: str | None) -> None:
        _remove(path, self.audio_dir)


class WhisperTranscriber:
    """faster-whisper on the CPU. `translate` turns Afrikaans (or anything) into English."""

    def __init__(self, cfg: TranscriptionConfig, loop: asyncio.AbstractEventLoop, on_done: OnDone):
        self.cfg = cfg
        self.loop = loop
        self.on_done = on_done
        self._model = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")
        # Load (and on first run download) the model now, not on the first voice note.
        self._pool.submit(self._load_logged)

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # optional dependency: core[voice]

            os.makedirs(self.cfg.model_dir, exist_ok=True)
            self._model = WhisperModel(self.cfg.model, device="cpu",
                                       compute_type=self.cfg.compute_type,
                                       cpu_threads=self.cfg.cpu_threads,
                                       download_root=self.cfg.model_dir)
            log.info("whisper model %s ready", self.cfg.model)
        return self._model

    def _load_logged(self) -> None:
        try:
            self._load()
        except Exception:
            log.exception("whisper model failed to load; voice notes go out untranscribed")

    def submit(self, message_id: int, path: str) -> None:
        if not _inside(path, self.cfg.audio_dir):
            log.warning("voice file outside %s, ignored", self.cfg.audio_dir)
            self.loop.call_soon_threadsafe(self.on_done, message_id, None, None, "bad path")
            return
        self._pool.submit(self._run, message_id, path)

    def discard(self, path: str | None) -> None:
        _remove(path, self.cfg.audio_dir)

    def _run(self, message_id: int, path: str) -> None:
        text = lang = err = None
        try:
            model = self._load()
            segments, info = model.transcribe(
                path,
                task="translate" if self.cfg.translate else "transcribe",
                beam_size=self.cfg.beam_size,
                vad_filter=True,                 # skip silence: faster, fewer hallucinations
                condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip() or None
            lang = info.language
            log.info("voice #%d transcribed (%s, %.0f s audio)", message_id, lang, info.duration)
        except Exception as e:
            err = str(e)[:120] or type(e).__name__
            log.warning("voice #%d transcription failed: %s", message_id, err)
        finally:
            self.discard(path)
        self.loop.call_soon_threadsafe(self.on_done, message_id, text, lang, err)


@dataclass
class FakeTranscriber:
    """Records submissions; tests answer them with bridge.on_transcript(...)."""

    submitted: list[tuple[int, str]] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)

    def submit(self, message_id: int, path: str) -> None:
        self.submitted.append((message_id, path))

    def discard(self, path: str | None) -> None:
        if path:
            self.discarded.append(path)
