import asyncio
import queue
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import edge_tts
import numpy as np
import pygame
import webrtcvad
from faster_whisper import WhisperModel
from langsmith import traceable

# from agent import ask_policy
from config import (
    OLLAMA_NUM_PREDICT,
    TTS_VOICE,
    VOICE_FRAME_MS,
    VOICE_MAX_UTTERANCE_SECONDS,
    VOICE_MIN_SPEECH_SECONDS,
    VOICE_SAMPLE_RATE,
    VOICE_SILENCE_SECONDS,
    VOICE_SHOW_TIMINGS,
    VOICE_VAD_MODE,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_MODEL,
)
from llm_factory import make_chat_model

llm = make_chat_model(max_tokens=OLLAMA_NUM_PREDICT)

def _load_sounddevice():
    try:
        import sounddevice as sd
    except OSError as exc:
        raise RuntimeError(
            "Microphone support needs PortAudio. On Ubuntu/Debian run: "
            "sudo apt-get install portaudio19-dev libportaudio2"
        ) from exc
    except ImportError as exc:
        raise RuntimeError(
            "The Python package sounddevice is missing. Install it with: "
            "pip install sounddevice"
        ) from exc

    return sd


class Microphone:
    def __init__(self):
        self.sd = _load_sounddevice()
        self.frame_bytes = int(VOICE_SAMPLE_RATE * VOICE_FRAME_MS / 1000) * 2
        self.frames = queue.Queue()
        self.stream = None

    def __enter__(self):
        def callback(indata, frames, time_info, status):
            if status:
                print(f"[mic] {status}")
            self.frames.put(bytes(indata))

        self.stream = self.sd.RawInputStream(
            samplerate=VOICE_SAMPLE_RATE,
            blocksize=int(VOICE_SAMPLE_RATE * VOICE_FRAME_MS / 1000),
            dtype="int16",
            channels=1,
            callback=callback,
        )
        self.stream.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def read_frame(self, timeout=0.1) -> bytes | None:
        try:
            frame = self.frames.get(timeout=timeout)
        except queue.Empty:
            return None

        if len(frame) != self.frame_bytes:
            return None

        return frame

    def clear(self):
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return


class VoicePolicyAgent:
    def __init__(self):
        self.vad = webrtcvad.Vad(VOICE_VAD_MODE)
        self.transcriber = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
        self.thread_id = str(uuid4())
        pygame.mixer.init()

    def _is_speech(self, frame: bytes) -> bool:
        try:
            return self.vad.is_speech(frame, VOICE_SAMPLE_RATE)
        except Exception:
            return False

    def listen_for_utterance(self, mic: Microphone) -> bytes | None:
        print("\nListening...")

        speech_frames = []
        speech_frame_count = 0
        has_started = False
        started_at = None
        last_speech_at = None
        min_speech_frames = max(1, int(VOICE_MIN_SPEECH_SECONDS * 1000 / VOICE_FRAME_MS))

        while True:
            frame = mic.read_frame()
            if frame is None:
                continue

            now = time.monotonic()
            is_speech = self._is_speech(frame)

            if is_speech:
                if not has_started:
                    started_at = now
                has_started = True
                speech_frame_count += 1
                last_speech_at = now
                speech_frames.append(frame)
            elif has_started:
                speech_frames.append(frame)

            if has_started and started_at and last_speech_at:
                silence_for = now - last_speech_at
                utterance_for = now - started_at

                if silence_for >= VOICE_SILENCE_SECONDS:
                    if speech_frame_count >= min_speech_frames:
                        return b"".join(speech_frames)
                    return None

                if utterance_for >= VOICE_MAX_UTTERANCE_SECONDS:
                    return b"".join(speech_frames)

    @traceable(name="speech_to_text")
    def transcribe(self, pcm_audio: bytes) -> str:
        audio = np.frombuffer(pcm_audio, np.int16).astype(np.float32) / 32768.0
        segments, _ = self.transcriber.transcribe(
            audio,
            language="en",
            vad_filter=True,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def _make_speech_file(self, text: str, path: Path):
        communicate = edge_tts.Communicate(text, TTS_VOICE)
        await communicate.save(str(path))

    @traceable(name="text_to_speech")
    def speak(self, text: str, mic: Microphone) -> bool:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as speech_file:
            speech_path = Path(speech_file.name)

        try:
            asyncio.run(self._make_speech_file(text, speech_path))
            pygame.mixer.music.load(str(speech_path))
            pygame.mixer.music.play()

            consecutive_speech = 0
            needed_frames = max(2, int(180 / VOICE_FRAME_MS))

            while pygame.mixer.music.get_busy():
                frame = mic.read_frame(timeout=0.03)
                if frame and self._is_speech(frame):
                    consecutive_speech += 1
                else:
                    consecutive_speech = 0

                if consecutive_speech >= needed_frames:
                    pygame.mixer.music.stop()
                    mic.clear()
                    print("\nInterrupted. Listening...")
                    return True

                time.sleep(0.01)

            return False
        finally:
            try:
                speech_path.unlink()
            except FileNotFoundError:
                pass

    @traceable(name="voice_policy_turn")
    def answer_turn(self, user_text: str) -> str:
        
        response = llm.invoke(user_text)

        return response.content
        # return ask_policy(
        #     user_text,
        #     thread_id=self.thread_id
        # )

    def run(self):
        print("Voice policy agent is ready. Press Ctrl+C to stop.")

        with Microphone() as mic:
            while True:
                turn_started_at = time.perf_counter()
                pcm_audio = self.listen_for_utterance(mic)
                if not pcm_audio:
                    continue

                transcribe_started_at = time.perf_counter()
                user_text = self.transcribe(pcm_audio)
                if not user_text:
                    continue
                transcribe_finished_at = time.perf_counter()

                print(f"\nEmployee: {user_text}")

                if user_text.lower() in {"exit", "quit", "stop"}:
                    print("Goodbye.")
                    break

                answer_started_at = time.perf_counter()
                answer = self.answer_turn(user_text)
                answer_finished_at = time.perf_counter()
                print(f"\nAI: {answer}")

                speak_started_at = time.perf_counter()
                was_interrupted = self.speak(answer, mic)
                speak_finished_at = time.perf_counter()
                if not was_interrupted:
                    mic.clear()

                if VOICE_SHOW_TIMINGS:
                    print(
                        "[timing] "
                        f"listen={transcribe_started_at - turn_started_at:.2f}s "
                        f"stt={transcribe_finished_at - transcribe_started_at:.2f}s "
                        f"rag={answer_finished_at - answer_started_at:.2f}s "
                        f"tts+play={speak_finished_at - speak_started_at:.2f}s"
                    )


if __name__ == "__main__":
    try:
        VoicePolicyAgent().run()
    except KeyboardInterrupt:
        print("\nStopped.")
