import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


def _positive_int(name, default):
    value = os.environ.get(name, str(default))
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"{name} 必须是正整数") from None
    if parsed <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return parsed


def _positive_float(name, default):
    value = os.environ.get(name, str(default))
    try:
        parsed = float(value)
    except ValueError:
        raise ValueError(f"{name} 必须是正数") from None
    if parsed <= 0:
        raise ValueError(f"{name} 必须是正数")
    return parsed


def _config():
    rtsp_url = os.environ.get("CAW_TEST_RTSP_URL", "").strip()
    if not rtsp_url:
        raise ValueError("未配置 CAW_TEST_RTSP_URL，请通过环境变量提供摄像头 RTSP 地址")
    parsed = urlsplit(rtsp_url)
    if parsed.scheme.lower() != "rtsp" or not parsed.hostname:
        raise ValueError("CAW_TEST_RTSP_URL 必须是有效的 rtsp:// 地址")
    output_dir = Path(
        os.environ.get("CAW_TEST_OUTPUT_DIR", str(Path(__file__).resolve().parent / "摄像头测试"))
    )
    return (
        rtsp_url,
        output_dir,
        _positive_int("CAW_TEST_TOTAL_FRAMES", 120),
        _positive_float("CAW_TEST_FPS", 1),
    )


def _display_url(rtsp_url):
    parsed = urlsplit(rtsp_url)
    host = parsed.hostname or "未知主机"
    port = f":{parsed.port}" if parsed.port else ""
    return f"rtsp://{host}{port}{parsed.path}"


def main():
    try:
        rtsp_url, output_dir, total_frames, fps = _config()
    except ValueError as error:
        print(f"配置错误: {error}", file=sys.stderr, flush=True)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = str(output_dir / "frame_%03d.jpg")

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-vf",
        f"fps={fps}",
        "-vframes",
        str(total_frames),
        "-q:v",
        "2",
        output_pattern,
    ]

    print("=== 摄像头抽帧任务开始 ===", flush=True)
    print(f"视频流地址: {_display_url(rtsp_url)} (凭据来自环境变量)", flush=True)
    print(f"目标目录: {output_dir}", flush=True)
    print(f"抓取计划: 频率 {fps:g} 帧/秒，目标总帧数: {total_frames} 帧", flush=True)
    print(f"命名规则: frame_001.jpg ~ frame_{total_frames:03d}.jpg", flush=True)
    print("============================", flush=True)

    start_time = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    last_count = -1
    while proc.poll() is None:
        time.sleep(5)
        current_files = list(output_dir.glob("frame_*.jpg"))
        count = len(current_files)
        if count != last_count:
            elapsed = time.time() - start_time
            print(f"[{elapsed:5.1f}s] 当前已保存: {count}/{total_frames} 帧", flush=True)
            last_count = count

    proc.wait()
    elapsed = time.time() - start_time

    saved_files = sorted(list(output_dir.glob("frame_*.jpg")))
    print("\n=== 抽帧完成 ===", flush=True)
    print(f"总耗时: {elapsed:.1f} 秒", flush=True)
    print(f"实际捕获帧数: {len(saved_files)}/{total_frames}", flush=True)
    if saved_files:
        print(f"首帧: {saved_files[0].name} ({saved_files[0].stat().st_size / 1024:.1f} KB)", flush=True)
        print(f"末帧: {saved_files[-1].name} ({saved_files[-1].stat().st_size / 1024:.1f} KB)", flush=True)

    if proc.returncode != 0:
        print(f"FFmpeg 退出代码: {proc.returncode}", flush=True)
        return proc.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
