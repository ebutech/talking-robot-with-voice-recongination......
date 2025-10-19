# ==============================================================================
# Robust, Self-Calibrating Voice Chat Client with Blinking LED Indicator
#
# IMPORTANT: This script requires GPIO access. Run it with sudo on a Raspberry Pi:
# sudo python voice_chat_client.py
# ==============================================================================

import asyncio
import websockets
import base64
import pyaudio
import json
import sys
import wave
import io
import webrtcvad
import subprocess
from collections import deque
import signal
import math
import struct
import os

# --- NEW: GPIOManager Class for clean and safe LED control ---
class GPIOManager:
    def __init__(self, pin):
        self.led_pin = pin
        self.is_pi = False
        try:
            import RPi.GPIO as GPIO
            self.GPIO = GPIO
            self.is_pi = True
            print("[System]: RPi.GPIO library loaded successfully.")
            self.GPIO.setmode(self.GPIO.BCM)
            self.GPIO.setup(self.led_pin, self.GPIO.OUT)
            self.GPIO.output(self.led_pin, self.GPIO.LOW)
            self.GPIO.setwarnings(False)
            print(f"[GPIO]: Pin {self.led_pin} initialized as OUTPUT.")
        except (ImportError, RuntimeError) as e:
            print(f"[System]: RPi.GPIO library not found or failed to load: {e}. Running without LED support.")
            self.is_pi = False

    def led_on(self):
        if self.is_pi:
            self.GPIO.output(self.led_pin, self.GPIO.HIGH)
    
    def led_off(self):
        if self.is_pi:
            self.GPIO.output(self.led_pin, self.GPIO.LOW)

    def cleanup(self):
        if self.is_pi:
            print("[GPIO]: Cleaning up GPIO pins.")
            self.GPIO.cleanup()

# Context manager to suppress ALSA/Jack startup and shutdown warnings
class SilenceAlsa:
    def __enter__(self):
        self.original_stderr_fd = sys.stderr.fileno()
        self.saved_stderr_fd = os.dup(self.original_stderr_fd)
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, self.original_stderr_fd)
        os.close(devnull_fd)
    def __exit__(self, exc_type, exc_val, exc_tb):
        os.dup2(self.saved_stderr_fd, self.original_stderr_fd)
        os.close(self.saved_stderr_fd)

# --- Constants and Tuning Parameters ---
LED_PIN = 18
LED_BLINK_INTERVAL_S = 0.1
BASE_URL = "https://speech.morphyn.net"
WS_URL = "wss://speech.morphyn.net/ws"
CHANNELS = 1
RATE = 16000
FORMAT = pyaudio.paInt16
VAD_FRAME_MS = 30
VAD_CHUNK_SIZE = int(RATE * VAD_FRAME_MS / 1000)
VAD_AGGRESSIVENESS = 3
SILENCE_DURATION_S = 1.2
PADDING_FRAMES_COUNT = 15
MAX_RECORDING_S = 10.0
CALIBRATION_SECONDS = 3
NOISE_THRESHOLD_MULTIPLIER = 3.0
RECALIBRATION_AUDIO_MS = 5000
RECALIBRATION_SMOOTHING_FACTOR = 0.3
VAD_TRIGGER_CONFIRMATION_FRAMES = 3
MIC_POST_PLAYBACK_COOLDOWN_S = 0.5
WEBSOCKET_OPEN_TIMEOUT = 20
WEBSOCKET_PING_TIMEOUT = 20
WEBSOCKET_PING_INTERVAL = 25

# --- All other functions are correct and unchanged ---
def calculate_rms(frame):
    count = len(frame) // 2; format_str = f"<{count}h"; shorts = struct.unpack(format_str, frame)
    sum_squares = sum(s**2 for s in shorts); return math.sqrt(sum_squares / count)
def calibrate_noise_level(stream):
    print(f"[System]: Calibrating background noise for {CALIBRATION_SECONDS} seconds. Please be quiet.")
    rms_values = []; num_frames = int(CALIBRATION_SECONDS * 1000 / VAD_FRAME_MS)
    for _ in range(num_frames):
        try: frame = stream.read(VAD_CHUNK_SIZE, exception_on_overflow=False); rms = calculate_rms(frame); rms_values.append(rms)
        except IOError as e: print(f"[Warning]: IOError during calibration: {e}")
    if not rms_values: print("[Warning]: Could not collect audio data for calibration. Using a default threshold."); return 50
    average_rms = sum(rms_values) / len(rms_values); noise_threshold = average_rms * NOISE_THRESHOLD_MULTIPLIER
    print(f"[System]: Calibration complete. Average noise level: {average_rms:.2f}, Threshold set to: {noise_threshold:.2f}")
    return noise_threshold
def pcm_to_wav(frames, sample_width):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as wf:
        wf.setnchannels(CHANNELS); wf.setsampwidth(sample_width); wf.setframerate(RATE); wf.writeframes(b''.join(frames))
    return buffer.getvalue()
async def send_audio(ws, stop_event, stream, audio_instance, initial_noise_threshold, is_playing_event):
    loop = asyncio.get_running_loop(); vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    padding_frames = deque(maxlen=PADDING_FRAMES_COUNT); silence_frames_needed = int(SILENCE_DURATION_S * 1000 / VAD_FRAME_MS)
    max_frames = int(MAX_RECORDING_S * 1000 / VAD_FRAME_MS); sample_width = audio_instance.get_sample_size(FORMAT)
    noise_threshold = initial_noise_threshold; recalibration_buffer = deque()
    recalibration_bytes_trigger = int((RATE * 2) * (RECALIBRATION_AUDIO_MS / 1000)); voiced_confirmation_frames = 0
    print(f"[VAD]: Listening for speech...")
    try:
        while not stop_event.is_set():
            if is_playing_event.is_set():
                print(f"\r[VAD]: Playback detected, microphone muted...{' '*40}", end="", flush=True)
                await is_playing_event.wait()
                print(f"\r[VAD]: Listening for speech...{' '*60}", end="", flush=True)
                padding_frames.clear(); recalibration_buffer.clear(); voiced_confirmation_frames = 0
            frame = await loop.run_in_executor(None, stream.read, VAD_CHUNK_SIZE, False)
            if is_playing_event.is_set(): continue
            is_speech_and_loud = vad.is_speech(frame, RATE) and calculate_rms(frame) > noise_threshold
            padding_frames.append(frame)
            if is_speech_and_loud: voiced_confirmation_frames += 1
            else:
                voiced_confirmation_frames = 0; recalibration_buffer.append(frame)
                current_recal_size = sum(len(b) for b in recalibration_buffer)
                if current_recal_size > recalibration_bytes_trigger:
                    print(f"\r[VAD]: Performing auto-recalibration...{' '*40}", end="", flush=True); pcm_data = b''.join(recalibration_buffer); recalibration_buffer.clear()
                    new_baseline_rms = calculate_rms(pcm_data); old_avg_noise = noise_threshold / NOISE_THRESHOLD_MULTIPLIER
                    new_avg_noise = (old_avg_noise * (1 - RECALIBRATION_SMOOTHING_FACTOR)) + (new_baseline_rms * RECALIBRATION_SMOOTHING_FACTOR)
                    noise_threshold = new_avg_noise * NOISE_THRESHOLD_MULTIPLIER
                    print(f"\r[VAD]: Auto-recalibration complete. New threshold: {noise_threshold:.2f}{' '*30}", end="", flush=True); await asyncio.sleep(0.5)
                    print(f"\r[VAD]: Listening for speech...{' '*60}", end="", flush=True)
            if voiced_confirmation_frames >= VAD_TRIGGER_CONFIRMATION_FRAMES:
                print(f"\r[VAD]: Sustained speech detected, recording...{' '*30}", end="", flush=True)
                voiced_frames = list(padding_frames); silent_frames_count = 0; voiced_confirmation_frames = 0
                while not stop_event.is_set():
                    frame = await loop.run_in_executor(None, stream.read, VAD_CHUNK_SIZE, False)
                    if is_playing_event.is_set(): voiced_frames = []; break
                    voiced_frames.append(frame)
                    if vad.is_speech(frame, RATE) and calculate_rms(frame) > noise_threshold: silent_frames_count = 0
                    else: silent_frames_count += 1
                    if silent_frames_count >= silence_frames_needed or len(voiced_frames) >= max_frames:
                        if not voiced_frames: break
                        msg = "Max duration reached" if len(voiced_frames) >= max_frames else "Silence detected"
                        print(f"\r[VAD]: {msg}, sending audio ({len(voiced_frames) * VAD_FRAME_MS}ms)...{' '*20}", end="", flush=True)
                        wav_data = pcm_to_wav(voiced_frames, sample_width); encoded_data = base64.b64encode(wav_data).decode('utf-8')
                        await ws.send(json.dumps({"type": "audio", "payload": encoded_data}))
                        print(f"\r[VAD]: Listening for speech...{' '*60}", end="", flush=True); break
    except (websockets.ConnectionClosed, asyncio.CancelledError): pass
    except Exception as e: print(f"\n[Error in send_audio]: {e}")
    finally: stop_event.set()

async def receive_messages(ws, stop_event, is_playing_event):
    loop = asyncio.get_running_loop(); ffplay_process = None
    try:
        async for message in ws:
            print(f"\r{' ' * 80}\r", end=""); msg = json.loads(message); msg_type = msg.get("type")
            if msg_type == "transcript": print(f"[You said]: {msg.get('text')}"); print("[Bot]: ...", end="", flush=True)
            elif msg_type == "partial": print(f"\r[Bot]: {msg.get('text')}", end="", flush=True)
            elif msg_type == 'audio-stream-start':
                is_playing_event.set(); print(f"\r[Player]: Receiving audio stream...{' '*40}", end="", flush=True)
                command = ["ffplay", "-nodisp", "-autoexit", "-v", "quiet", "-i", "-"]
                ffplay_process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif msg_type == 'audio-chunk':
                if ffplay_process and ffplay_process.stdin and not ffplay_process.stdin.closed:
                    try: audio_chunk = base64.b64decode(msg['data']); await loop.run_in_executor(None, ffplay_process.stdin.write, audio_chunk)
                    except (IOError, BrokenPipeError): ffplay_process = None
            elif msg_type == 'audio-stream-end':
                print(f"\r[Player]: Audio stream finished.{' '*50}", end="", flush=True)
                if ffplay_process:
                    if ffplay_process.stdin: ffplay_process.stdin.close()
                    await loop.run_in_executor(None, ffplay_process.wait)
                ffplay_process = None
                await asyncio.sleep(MIC_POST_PLAYBACK_COOLDOWN_S); is_playing_event.clear(); print(f"\r[VAD]: Listening for speech...{' '*60}", end="", flush=True)
            elif msg_type == "error": print(f"\n[Server Error]: {msg.get('message')}")
    except (websockets.ConnectionClosed, asyncio.CancelledError): pass
    except Exception as e: print(f"\n[Error in receive_messages]: {e}")
    finally:
        if ffplay_process:
            if ffplay_process.stdin and not ffplay_process.stdin.closed: ffplay_process.stdin.close()
            ffplay_process.terminate(); await loop.run_in_executor(None, ffplay_process.wait)
        is_playing_event.clear(); stop_event.set()

async def blink_led_task(is_playing_event, stop_event, gpio_manager):
    if not gpio_manager.is_pi: return
    try:
        while not stop_event.is_set():
            await is_playing_event.wait()
            while is_playing_event.is_set() and not stop_event.is_set():
                gpio_manager.led_on()
                await asyncio.sleep(LED_BLINK_INTERVAL_S)
                if not is_playing_event.is_set(): break 
                gpio_manager.led_off()
                await asyncio.sleep(LED_BLINK_INTERVAL_S)
    except asyncio.CancelledError: pass
    finally: gpio_manager.led_off()

async def main():
    gpio_manager = GPIOManager(LED_PIN)
    try:
        while True:
            audio, stream, stop_event, is_playing_event, tasks = None, None, asyncio.Event(), asyncio.Event(), []
            try:
                with SilenceAlsa():
                    audio = pyaudio.PyAudio()
                    stream = audio.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=VAD_CHUNK_SIZE)
                noise_threshold = calibrate_noise_level(stream)
                async with websockets.connect(
                    WS_URL, open_timeout=WEBSOCKET_OPEN_TIMEOUT, ping_interval=WEBSOCKET_PING_INTERVAL, ping_timeout=WEBSOCKET_PING_TIMEOUT
                ) as ws:
                    print(f"[System]: Connection to {WS_URL} established.")
                    tasks = [
                        asyncio.create_task(send_audio(ws, stop_event, stream, audio, noise_threshold, is_playing_event)),
                        asyncio.create_task(receive_messages(ws, stop_event, is_playing_event)),
                        asyncio.create_task(blink_led_task(is_playing_event, stop_event, gpio_manager))
                    ]
                    await stop_event.wait()
            except KeyboardInterrupt:
                print("\n[System]: Keyboard interrupt detected. Shutting down."); stop_event.set(); break
            except (websockets.WebSocketException, asyncio.TimeoutError, OSError) as e: print(f"\n[System]: Connection lost or failed: {e}")
            except Exception as e: print(f"\n[Main Error]: An unexpected error occurred: {e}"); stop_event.set(); break
            finally:
                print("[System]: Cleaning up resources for this attempt...")
                if not stop_event.is_set(): stop_event.set()
                if tasks:
                    for task in tasks: task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                with SilenceAlsa():
                    if stream: stream.stop_stream(); stream.close()
                    if audio: audio.terminate()
            if stop_event.is_set() and any(isinstance(task.exception(), (KeyboardInterrupt, asyncio.CancelledError)) for task in tasks if task.done()): break
            print("[System]: Attempting to reconnect in 5 seconds..."); await asyncio.sleep(5)
    finally:
        gpio_manager.cleanup()
        print("\n[System]: Program has exited.")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
