from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from app.config import Settings


@dataclass
class SileroStreamState:
    speech_ms: float = 0.0
    silence_ms: float = 0.0
    active: bool = False
    last_prob: float = 0.0


class SileroVadService:
    def __init__(self, settings: Settings) -> None:
        self.enabled = bool(settings.enable_silero_vad)
        self.threshold = float(settings.silero_vad_threshold)
        self.min_speech_ms = int(settings.silero_vad_min_speech_ms)
        self.hangover_ms = int(settings.silero_vad_hangover_ms)
        self.target_sample_rate = int(settings.silero_vad_sample_rate)
        self.available = False

        self._model: Any | None = None
        self._torch: Any | None = None

        if not self.enabled:
            return

        try:
            from silero_vad import load_silero_vad

            self._model = load_silero_vad(onnx=True)
            try:
                import torch

                self._torch = torch
            except Exception:
                self._torch = None
            self.available = True
        except Exception:
            self._model = None
            self._torch = None
            self.available = False

    def new_stream(self) -> SileroStreamState:
        return SileroStreamState()

    def is_ready(self) -> bool:
        return self.enabled and self.available and self._model is not None

    def process_chunk(
        self,
        state: SileroStreamState,
        samples: np.ndarray,
        sample_rate: int,
    ) -> tuple[bool, float, bool]:
        if not self.is_ready():
            return state.active, 0.0, False

        audio = self._to_target_rate(samples, int(sample_rate or 0))
        if audio.size == 0:
            return state.active, state.last_prob, False

        duration_ms = (audio.shape[0] / float(self.target_sample_rate)) * 1000.0
        prob = self._predict_prob(audio)
        state.last_prob = prob
        is_speech = prob >= self.threshold

        if is_speech:
            state.speech_ms += duration_ms
            state.silence_ms = 0.0
        else:
            state.silence_ms += duration_ms
            if not state.active:
                state.speech_ms = 0.0

        previous = state.active
        if not state.active and state.speech_ms >= self.min_speech_ms:
            state.active = True
        elif state.active and state.silence_ms >= self.hangover_ms:
            state.active = False
            state.speech_ms = 0.0

        changed = previous != state.active
        return state.active, prob, changed

    def _predict_prob(self, audio: np.ndarray) -> float:
        model = self._model
        if model is None:
            return 0.0

        try:
            if self._torch is not None:
                tensor = self._torch.from_numpy(audio).float()
                result = model(tensor, self.target_sample_rate)
            else:
                result = model(audio, self.target_sample_rate)
            return self._to_float(result)
        except Exception:
            self.available = False
            return 0.0

    @staticmethod
    def _to_float(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (float, int)):
            return float(value)

        # torch.Tensor or numpy array like outputs
        for attr in ("item",):
            if hasattr(value, attr):
                try:
                    return float(getattr(value, attr)())
                except Exception:
                    pass
        try:
            arr = np.asarray(value, dtype=np.float32).reshape(-1)
            if arr.size:
                return float(arr[0])
        except Exception:
            return 0.0
        return 0.0

    def _to_target_rate(self, samples: np.ndarray, sample_rate: int) -> np.ndarray:
        if samples.ndim != 1:
            samples = samples.reshape(-1)
        if samples.size == 0:
            return np.asarray([], dtype=np.float32)

        source_rate = sample_rate if sample_rate > 0 else self.target_sample_rate
        if source_rate == self.target_sample_rate:
            return samples.astype(np.float32, copy=False)

        ratio = float(self.target_sample_rate) / float(source_rate)
        out_len = max(1, int(samples.shape[0] * ratio))
        xp = np.linspace(0.0, 1.0, num=samples.shape[0], endpoint=False)
        x = np.linspace(0.0, 1.0, num=out_len, endpoint=False)
        resampled = np.interp(x, xp, samples).astype(np.float32)
        return resampled


_service: SileroVadService | None = None


def get_silero_vad_service(settings: Settings) -> SileroVadService:
    global _service
    if _service is None:
        _service = SileroVadService(settings)
    return _service

