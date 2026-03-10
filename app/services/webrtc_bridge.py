import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import numpy as np
from fastapi import HTTPException

from app.config import get_settings
from app.services.silero_vad_service import SileroStreamState, get_silero_vad_service

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
except Exception:  # pragma: no cover - optional dependency
    RTCPeerConnection = None  # type: ignore[assignment]
    RTCSessionDescription = None  # type: ignore[assignment]


@dataclass
class WebRtcPeer:
    peer_id: str
    pc: Any
    audio_channel: Any | None = None
    last_seen: float = field(default_factory=lambda: time.time())
    vad_stream: SileroStreamState | None = None
    vad_state: bool = False
    last_vad_emit: float = 0.0
    energy_floor: float = 0.0025
    energy_speech_ms: float = 0.0
    energy_silence_ms: float = 0.0


class WebRtcBridge:
    def __init__(self) -> None:
        self._peers: dict[str, WebRtcPeer] = {}
        self._lock = asyncio.Lock()
        self._settings = get_settings()
        self._vad = get_silero_vad_service(self._settings)

    @property
    def is_available(self) -> bool:
        return RTCPeerConnection is not None and RTCSessionDescription is not None

    async def create_answer(self, sdp: str, type_: str) -> dict[str, str]:
        if not self.is_available:
            raise HTTPException(
                status_code=503,
                detail="WebRTC bridge not available. Install aiortc to enable WebRTC transport.",
            )

        pc = RTCPeerConnection()
        peer_id = uuid4().hex
        peer = WebRtcPeer(peer_id=peer_id, pc=pc, vad_stream=self._vad.new_stream())

        @pc.on("datachannel")
        def on_datachannel(channel):
            if channel.label not in {"audio_downlink", "luma-audio"}:
                return
            peer.audio_channel = channel
            peer.last_seen = time.time()

            @channel.on("close")
            def _on_close():
                peer.audio_channel = None

            @channel.on("message")
            def _on_message(_msg):
                peer.last_seen = time.time()

            # Frontend can immediately know server-side VAD is wired.
            try:
                source = "silero" if self._vad.is_ready() else "energy"
                channel.send(
                    json.dumps(
                        {
                            "type": "vad_state",
                            "speech": False,
                            "score": 0.0,
                            "source": source,
                            "enabled": True,
                        }
                    )
                )
            except Exception:
                pass

        @pc.on("track")
        def on_track(track):
            if track.kind != "audio":
                return

            async def _drain_audio():
                try:
                    while True:
                        frame = await track.recv()
                        peer.last_seen = time.time()
                        await self._process_vad_frame(peer, frame)
                except Exception:
                    return

            asyncio.create_task(_drain_audio())

        @pc.on("connectionstatechange")
        async def on_state_change():
            state = pc.connectionState
            if state in {"failed", "closed", "disconnected"}:
                await self.close_peer(peer_id)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        async with self._lock:
            self._peers[peer_id] = peer

        return {
            "peer_id": peer_id,
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    async def close_peer(self, peer_id: str) -> None:
        async with self._lock:
            peer = self._peers.pop(peer_id, None)
        if not peer:
            return
        try:
            await peer.pc.close()
        except Exception:
            return

    async def send_audio_chunk(self, peer_id: str, payload: dict) -> bool:
        if not peer_id:
            return False
        async with self._lock:
            peer = self._peers.get(peer_id)
        if not peer:
            return False
        channel = peer.audio_channel
        if not channel or getattr(channel, "readyState", "") != "open":
            return False
        try:
            channel.send(json.dumps(payload))
            peer.last_seen = time.time()
            return True
        except Exception:
            return False

    async def _process_vad_frame(self, peer: WebRtcPeer, frame: Any) -> None:
        samples, sample_rate = self._frame_to_mono_float32(frame)
        if samples.size == 0:
            return

        if self._vad.is_ready():
            if peer.vad_stream is None:
                peer.vad_stream = self._vad.new_stream()
            speaking, score, changed = self._vad.process_chunk(peer.vad_stream, samples, sample_rate)
            source = "silero"
        else:
            speaking, score, changed = self._process_energy_vad(peer, samples, sample_rate)
            source = "energy"

        now = time.time()
        emit_interval = 0.22
        should_emit = changed or (speaking and now - peer.last_vad_emit >= emit_interval)
        if should_emit:
            await self._send_vad_state(peer, speaking=speaking, score=score, source=source)

    def _process_energy_vad(self, peer: WebRtcPeer, samples: np.ndarray, sample_rate: int) -> tuple[bool, float, bool]:
        if samples.size == 0:
            return peer.vad_state, 0.0, False
        duration_ms = (samples.shape[0] / float(sample_rate or 16000)) * 1000.0
        rms = float(np.sqrt(np.mean(samples * samples)))

        if not peer.vad_state:
            peer.energy_floor = 0.95 * peer.energy_floor + 0.05 * rms
        else:
            peer.energy_floor = 0.985 * peer.energy_floor + 0.015 * rms

        speech_threshold = max(0.0095, peer.energy_floor * 3.1)
        release_threshold = max(0.006, peer.energy_floor * 2.0)
        is_speech = rms >= speech_threshold if not peer.vad_state else rms >= release_threshold

        if is_speech:
            peer.energy_speech_ms += duration_ms
            peer.energy_silence_ms = 0.0
        else:
            peer.energy_silence_ms += duration_ms
            if not peer.vad_state:
                peer.energy_speech_ms = 0.0

        previous = peer.vad_state
        if not peer.vad_state and peer.energy_speech_ms >= float(self._settings.silero_vad_min_speech_ms):
            peer.vad_state = True
        elif peer.vad_state and peer.energy_silence_ms >= float(self._settings.silero_vad_hangover_ms):
            peer.vad_state = False
            peer.energy_speech_ms = 0.0

        return peer.vad_state, rms, previous != peer.vad_state

    async def _send_vad_state(self, peer: WebRtcPeer, speaking: bool, score: float, source: str) -> None:
        channel = peer.audio_channel
        if not channel or getattr(channel, "readyState", "") != "open":
            peer.vad_state = speaking
            peer.last_vad_emit = time.time()
            return
        payload = {
            "type": "vad_state",
            "speech": bool(speaking),
            "score": round(float(score), 4),
            "source": source,
            "enabled": True,
        }
        try:
            channel.send(json.dumps(payload))
            peer.vad_state = bool(speaking)
            peer.last_vad_emit = time.time()
        except Exception:
            return

    @staticmethod
    def _frame_to_mono_float32(frame: Any) -> tuple[np.ndarray, int]:
        try:
            raw = frame.to_ndarray()
        except Exception:
            return np.asarray([], dtype=np.float32), 0

        arr = np.asarray(raw)
        if arr.size == 0:
            return np.asarray([], dtype=np.float32), 0

        if arr.ndim == 1:
            mono = arr
        elif arr.ndim == 2:
            # aiortc audio frame is typically [channels, samples].
            mono = arr.mean(axis=0) if arr.shape[0] <= arr.shape[1] else arr.mean(axis=1)
        else:
            mono = arr.reshape(-1)

        if np.issubdtype(mono.dtype, np.integer):
            scale = float(np.iinfo(mono.dtype).max or 1)
            mono = mono.astype(np.float32) / scale
        else:
            mono = mono.astype(np.float32, copy=False)

        mono = np.clip(mono, -1.0, 1.0)
        sample_rate = int(getattr(frame, "sample_rate", 0) or 0)
        return mono, sample_rate


_webrtc_bridge: WebRtcBridge | None = None


def get_webrtc_bridge() -> WebRtcBridge:
    global _webrtc_bridge
    if _webrtc_bridge is None:
        _webrtc_bridge = WebRtcBridge()
    return _webrtc_bridge
