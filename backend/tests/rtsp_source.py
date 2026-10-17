"""集成测试专用：发送合成 H.264 的最小 RTSP/TCP 服务，不接触用户摄像头。"""

from __future__ import annotations

import asyncio
import base64
import re
import struct
import subprocess


class SyntheticRTSP:
    def __init__(self, ffmpeg="ffmpeg", seconds=8):
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=160x90:rate=10:duration={seconds}",
                "-an",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-g",
                "10",
                "-x264-params",
                "aud=1:repeat-headers=1",
                "-f",
                "h264",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        units = [u for u in re.split(b"\x00\x00\x00\x01|\x00\x00\x01", result.stdout) if u]
        self.sps = next(u for u in units if u[0] & 31 == 7)
        self.pps = next(u for u in units if u[0] & 31 == 8)
        self.frames = []
        for unit in units:
            if unit[0] & 31 == 9:
                self.frames.append([])
            if self.frames:
                self.frames[-1].append(unit)
        self.connections = 0
        self.clients = set()
        self.senders = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        for sender in self.senders:
            sender.cancel()
        await asyncio.gather(*self.senders, return_exceptions=True)
        for client in list(self.clients):
            client.close()
            await client.wait_closed()

    async def handle(self, reader, writer):
        self.connections += 1
        self.clients.add(writer)
        try:
            while True:
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode().split("\r\n")
                method = lines[0].split()[0]
                headers = {
                    k.lower(): v.strip() for line in lines[1:] if ":" in line for k, v in [line.split(":", 1)]
                }
                seq = headers.get("cseq", "1")
                extra = ""
                body = b""
                if method == "OPTIONS":
                    extra = "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER\r\n"
                elif method == "DESCRIBE":
                    content = f"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=synthetic\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\na=control:*\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1;profile-level-id={self.sps[1:4].hex()};sprop-parameter-sets={base64.b64encode(self.sps).decode()},{base64.b64encode(self.pps).decode()}\r\na=control:trackID=0\r\n"
                    body = content.encode()
                    extra = f"Content-Type: application/sdp\r\nContent-Base: rtsp://127.0.0.1:{self.port}/live/\r\n"
                elif method == "SETUP":
                    extra = "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: synthetic-session\r\n"
                elif method == "PLAY":
                    extra = "Session: synthetic-session\r\nRange: npt=0.000-\r\n"
                response = (
                    f"RTSP/1.0 200 OK\r\nCSeq: {seq}\r\n{extra}Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                writer.write(response)
                await writer.drain()
                if method == "PLAY":
                    self.senders.append(asyncio.create_task(self.send(writer)))
                if method == "TEARDOWN":
                    break
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()

    async def send(self, writer):
        sequence = 0
        try:
            for index, frame in enumerate(self.frames):
                for unit_index, unit in enumerate(frame):
                    if len(unit) <= 1100:
                        packets = [unit]
                    else:
                        chunks = [unit[i : i + 1098] for i in range(1, len(unit), 1098)]
                        packets = [
                            bytes(
                                [
                                    (unit[0] & 0xE0) | 28,
                                    (unit[0] & 31)
                                    | (0x80 if i == 0 else 0)
                                    | (0x40 if i == len(chunks) - 1 else 0),
                                ]
                            )
                            + chunk
                            for i, chunk in enumerate(chunks)
                        ]
                    for packet_index, packet in enumerate(packets):
                        marker = unit_index == len(frame) - 1 and packet_index == len(packets) - 1
                        rtp = (
                            struct.pack(
                                "!BBHII",
                                0x80,
                                96 | (0x80 if marker else 0),
                                sequence % 65536,
                                index * 9000,
                                0x12345678,
                            )
                            + packet
                        )
                        sequence += 1
                        writer.write(b"$\x00" + struct.pack("!H", len(rtp)) + rtp)
                await writer.drain()
                await asyncio.sleep(0.1)
        except (ConnectionError, asyncio.CancelledError):
            pass
