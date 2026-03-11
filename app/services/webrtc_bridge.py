import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

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


class WebRtcBridge:
    def __init__(self) -> None:
        self._peers: dict[str, WebRtcPeer] = {}
        self._lock = asyncio.Lock()

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
        peer = WebRtcPeer(peer_id=peer_id, pc=pc)

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

        @pc.on("track")
        def on_track(track):
            if track.kind != "audio":
                return

            async def _drain_audio():
                try:
                    while True:
                        await track.recv()
                        peer.last_seen = time.time()
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


_webrtc_bridge: WebRtcBridge | None = None


def get_webrtc_bridge() -> WebRtcBridge:
    global _webrtc_bridge
    if _webrtc_bridge is None:
        _webrtc_bridge = WebRtcBridge()
    return _webrtc_bridge

