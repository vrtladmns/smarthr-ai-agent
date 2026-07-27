import argparse
import asyncio
import json
import math
import queue
import random
import re
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
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
    VOICE_BARGE_IN_CONFIDENCE,
    VOICE_BARGE_IN_COOLDOWN_SECONDS,
    VOICE_BARGE_IN_MIN_WORDS,
    VOICE_BARGE_IN_VERIFY_SECONDS,
    VOICE_MAX_UTTERANCE_SECONDS,
    VOICE_MIN_SPEECH_SECONDS,
    VOICE_SAMPLE_RATE,
    VOICE_SILENCE_SECONDS,
    VOICE_SHOW_TIMINGS,
    VOICE_VAD_MODE,
    RECRUITER_INTERVIEW_QUESTION_COUNT,
    RECRUITER_LONG_SILENCE_SECONDS,
    RECRUITER_MIN_ANSWER_SECONDS,
    RECRUITER_THINKING_SILENCE_SECONDS,
    WHISPER_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_MODEL,
)
from llm_factory import make_chat_model

llm = make_chat_model(max_tokens=OLLAMA_NUM_PREDICT)

INTERVIEW_MAIN_INTERRUPT_MS = 260
INTERVIEW_REPLY_INTERRUPT_MS = 500
INTERVIEW_FOLLOWUP_INTERRUPT_MS = 650
INTERVIEW_MAX_SPOKEN_WORDS = 15
INTERVIEW_QUICK_INTENT_SILENCE_SECONDS = 1.0
INTERVIEW_ANSWER_CHECK_SILENCE_SECONDS = 2.5
INTERVIEW_THINKING_EXTENSION_SECONDS = 4.0
INTERVIEW_MAX_CLARIFICATIONS_PER_QUESTION = 2
INTERVIEW_STATE_ASKING_QUESTION = "ASKING_QUESTION"
INTERVIEW_STATE_LISTENING = "LISTENING"
INTERVIEW_STATE_THINKING = "THINKING"
INTERVIEW_STATE_CLARIFYING = "CLARIFYING"
INTERVIEW_STATE_FOLLOW_UP = "FOLLOW_UP"
INTERVIEW_STATE_EVALUATING = "EVALUATING"
INTERVIEW_STATE_ACKNOWLEDGING = "ACKNOWLEDGING"
INTERVIEW_STATE_NEXT_QUESTION = "NEXT_QUESTION"
INTERVIEW_STATE_SPEAKING = "SPEAKING"
INTERVIEW_STATE_PROCESSING = "PROCESSING"
INTERVIEW_STATE_RESPONDING = "RESPONDING"
INTERVIEW_STATE_INTERRUPTED = "Interrupted"
INTERVIEW_STATE_WAITING = "Waiting"
INTERVIEW_STATE_COMPLETE = "Interview Complete"


@dataclass
class VoiceTiming:
    vad_seconds: float = 0.0
    speech_detection_seconds: float = 0.0
    transcription_seconds: float = 0.0
    thinking_seconds: float = 0.0
    llm_seconds: float = 0.0
    retrieval_seconds: float = 0.0
    prompt_seconds: float = 0.0
    first_llm_token_seconds: float = 0.0
    full_llm_completion_seconds: float = 0.0
    tts_seconds: float = 0.0
    first_spoken_audio_seconds: float = 0.0
    playback_seconds: float = 0.0
    interruptions: int = 0
    total_seconds: float = 0.0
    states: list[str] = field(default_factory=list)

    def mark_state(self, state: str):
        if not self.states or self.states[-1] != state:
            self.states.append(state)

    def as_dict(self) -> dict:
        return {
            "vad_seconds": round(self.vad_seconds, 3),
            "speech_detection_seconds": round(self.speech_detection_seconds, 3),
            "transcription_seconds": round(self.transcription_seconds, 3),
            "thinking_seconds": round(self.thinking_seconds, 3),
            "llm_seconds": round(self.llm_seconds, 3),
            "retrieval_seconds": round(self.retrieval_seconds, 3),
            "prompt_seconds": round(self.prompt_seconds, 3),
            "first_llm_token_seconds": round(self.first_llm_token_seconds, 3),
            "full_llm_completion_seconds": round(self.full_llm_completion_seconds, 3),
            "tts_seconds": round(self.tts_seconds, 3),
            "first_spoken_audio_seconds": round(self.first_spoken_audio_seconds, 3),
            "playback_seconds": round(self.playback_seconds, 3),
            "interruptions": self.interruptions,
            "total_seconds": round(self.total_seconds, 3),
            "states": self.states,
        }


@dataclass
class CandidateTurn:
    text: str
    pcm_audio: bytes
    timing: VoiceTiming
    completion: dict = field(default_factory=dict)
    thinking_prompts: int = 0
    stt_confidence: float = 0.0


@dataclass
class ConversationMemory:
    current_question_number: int = 0
    current_question: str = ""
    previous_question_number: int | None = None
    previous_question: str = ""
    waiting_for: str = "answer"
    paused: bool = False
    partial_answer: str = ""
    confused_count: int = 0
    events: list[dict] = field(default_factory=list)
    questions: dict[int, str] = field(default_factory=dict)


@dataclass
class ConversationDecision:
    action: str
    response: str = ""
    reason: str = ""
    target_question_number: int | None = None
    target_question: str = ""
    should_evaluate_answer: bool = False
    should_advance: bool = False
    status: str = ""


@dataclass
class InterviewMetrics:
    response_latencies: list[float] = field(default_factory=list)
    interviewer_speech_seconds: list[float] = field(default_factory=list)
    candidate_speech_seconds: list[float] = field(default_factory=list)
    llm_seconds: list[float] = field(default_factory=list)
    tts_seconds: list[float] = field(default_factory=list)
    interruptions: int = 0
    turns: int = 0

    @staticmethod
    def average(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def add_turn(self, timing: VoiceTiming, candidate_audio: bytes = b""):
        self.turns += 1
        self.response_latencies.append(timing.first_spoken_audio_seconds or 0.0)
        self.interviewer_speech_seconds.append(timing.playback_seconds)
        self.candidate_speech_seconds.append(len(candidate_audio) / (VOICE_SAMPLE_RATE * 2) if candidate_audio else 0.0)
        self.llm_seconds.append(timing.llm_seconds)
        self.tts_seconds.append(timing.tts_seconds)
        self.interruptions += timing.interruptions

    def summary(self) -> dict:
        interviewer_total = sum(self.interviewer_speech_seconds)
        candidate_total = sum(self.candidate_speech_seconds)
        talk_total = interviewer_total + candidate_total
        interviewer_ratio = interviewer_total / talk_total if talk_total else 0.0
        return {
            "turns": self.turns,
            "avg_response_latency_seconds": round(self.average(self.response_latencies), 3),
            "avg_interviewer_speech_seconds": round(self.average(self.interviewer_speech_seconds), 3),
            "avg_candidate_speech_seconds": round(self.average(self.candidate_speech_seconds), 3),
            "interviewer_talk_ratio": round(interviewer_ratio, 3),
            "candidate_talk_ratio": round(1.0 - interviewer_ratio, 3) if talk_total else 0.0,
            "interviewer_under_25_percent": interviewer_ratio < 0.25 if talk_total else False,
            "avg_llm_seconds": round(self.average(self.llm_seconds), 3),
            "avg_tts_seconds": round(self.average(self.tts_seconds), 3),
            "interruption_rate": round(self.interruptions / self.turns, 3) if self.turns else 0.0,
            "total_interruptions": self.interruptions,
        }


def extract_json_object(response: str) -> str:
    import re

    match = re.search(r"\{.*\}", response, flags=re.S)
    if not match:
        raise ValueError(f"LLM did not return JSON: {response}")
    return match.group(0)


def split_spoken_sentences(text: str) -> list[str]:
    normalized = " ".join((text or "").split())
    if not normalized:
        return []
    pieces = re.split(r"(?<=[.!?])\s+", normalized)
    sentences = [piece.strip() for piece in pieces if piece.strip()]
    return sentences or [normalized]


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text or ""))


def shorten_spoken_text(text: str, max_words: int = INTERVIEW_MAX_SPOKEN_WORDS) -> str:
    words = re.findall(r"\S+", " ".join((text or "").split()))
    if len(words) <= max_words:
        return " ".join(words)
    short = " ".join(words[:max_words]).rstrip(" ,;:")
    if not short.endswith((".", "?", "!")):
        short += "?"
    return short


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
        self.audio_executor = ThreadPoolExecutor(max_workers=1)
        self.llm_executor = ThreadPoolExecutor(max_workers=1)
        self.tts_executor = ThreadPoolExecutor(max_workers=2)
        self.warmup_executor = ThreadPoolExecutor(max_workers=2)
        self.tts_cache_dir = Path(tempfile.gettempdir()) / "ai_recruiter_tts_cache"
        self.tts_cache_dir.mkdir(parents=True, exist_ok=True)
        self.tts_lock = threading.Lock()
        self.tts_futures = {}
        pygame.mixer.init()
        self.warmup_future = self.warmup_executor.submit(self.warmup_models)

    def close(self):
        self.audio_executor.shutdown(wait=False, cancel_futures=True)
        self.llm_executor.shutdown(wait=False, cancel_futures=True)
        self.tts_executor.shutdown(wait=False, cancel_futures=True)
        self.warmup_executor.shutdown(wait=False, cancel_futures=True)

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

    @traceable(name="speech_to_text_with_confidence")
    def transcribe_with_confidence(self, pcm_audio: bytes) -> dict:
        audio = np.frombuffer(pcm_audio, np.int16).astype(np.float32) / 32768.0
        segments, _ = self.transcriber.transcribe(
            audio,
            language="en",
            vad_filter=True,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
        )
        texts = []
        confidences = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            texts.append(text)
            avg_logprob = getattr(segment, "avg_logprob", -1.2)
            no_speech_prob = getattr(segment, "no_speech_prob", 0.5)
            confidence = max(0.0, min(1.0, math.exp(avg_logprob) * (1.0 - no_speech_prob)))
            confidences.append(confidence)
        transcript = " ".join(texts).strip()
        return {
            "text": transcript,
            "confidence": max(confidences) if confidences else 0.0,
            "word_count": len(re.findall(r"[A-Za-z0-9']+", transcript)),
        }

    def normalized_words(self, text: str) -> list[str]:
        return [word.lower() for word in re.findall(r"[A-Za-z0-9']+", text or "")]

    def looks_like_tts_echo(self, candidate_text: str, spoken_text: str) -> bool:
        candidate_words = self.normalized_words(candidate_text)
        if not candidate_words:
            return False
        spoken_words = set(self.normalized_words(spoken_text))
        if not spoken_words:
            return False
        overlap = sum(1 for word in candidate_words if word in spoken_words)
        return overlap / max(1, len(candidate_words)) >= 0.75

    @traceable(name="verify_barge_in")
    def verify_barge_in(self, pcm_audio: bytes, spoken_text: str) -> dict:
        result = self.transcribe_with_confidence(pcm_audio)
        text = result["text"].strip()
        lowered = text.lower()
        interrupt_phrases = [
            "wait",
            "sorry",
            "excuse me",
            "one second",
            "actually",
            "hold on",
            "yes",
            "repeat",
            "skip",
            "next",
            "stop",
            "move on",
            "i don't know",
        ]
        phrase_hit = any(phrase in lowered for phrase in interrupt_phrases)
        echo = self.looks_like_tts_echo(text, spoken_text)
        meaningful = (
            bool(text)
            and not echo
            and (
                phrase_hit
                or (result["confidence"] >= VOICE_BARGE_IN_CONFIDENCE and result["word_count"] >= 3)
            )
        )
        return {
            "interrupt": meaningful,
            "text": text,
            "confidence": round(result["confidence"], 3),
            "word_count": result["word_count"],
            "phrase_hit": phrase_hit,
            "echo_rejected": echo,
        }

    async def _make_speech_file(self, text: str, path: Path):
        communicate = edge_tts.Communicate(text, TTS_VOICE)
        await communicate.save(str(path))

    def speech_cache_path(self, text: str) -> Path:
        import hashlib

        key = hashlib.sha256(f"{TTS_VOICE}:{text}".encode("utf-8")).hexdigest()
        return self.tts_cache_dir / f"{key}.mp3"

    def valid_speech_file(self, path: Path) -> bool:
        try:
            if not path.exists() or path.stat().st_size < 256:
                return False
            with path.open("rb") as speech_file:
                header = speech_file.read(3)
            return header == b"ID3" or header[:2] == b"\xff\xfb" or header[:2] == b"\xff\xf3"
        except OSError:
            return False

    def delete_speech_cache(self, text: str):
        path = self.speech_cache_path(text)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def ensure_speech_file(self, text: str, force: bool = False) -> Path:
        speech_path = self.speech_cache_path(text)
        if force or not self.valid_speech_file(speech_path):
            tmp_path = speech_path.with_suffix(f".{uuid4().hex}.tmp.mp3")
            try:
                asyncio.run(self._make_speech_file(text, tmp_path))
                if not self.valid_speech_file(tmp_path):
                    raise RuntimeError(f"TTS generated invalid mp3: {tmp_path}")
                tmp_path.replace(speech_path)
            finally:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
        return speech_path

    def prefetch_speech(self, text: str):
        speech_path = self.speech_cache_path(text)
        if self.valid_speech_file(speech_path):
            future = Future()
            future.set_result(speech_path)
            return future
        with self.tts_lock:
            future = self.tts_futures.get(text)
            if future:
                if not future.done():
                    return future
                try:
                    future.result()
                    if self.valid_speech_file(speech_path):
                        return future
                except Exception:
                    pass
                self.tts_futures.pop(text, None)
            future = self.tts_executor.submit(self.ensure_speech_file, text)
            self.tts_futures[text] = future
            return future

    def warmup_models(self):
        try:
            self.transcribe(b"\x00" * int(VOICE_SAMPLE_RATE * 0.25 * 2))
        except Exception:
            pass
        for text in ["Take your time.", "I'm listening.", "No rush.", "Whenever you're ready."]:
            try:
                self.ensure_speech_file(text)
            except Exception:
                pass
        try:
            llm.invoke("Reply with only: ready")
        except Exception:
            pass

    def play_speech_file(
        self,
        speech_path: Path,
        speech_text: str,
        mic: Microphone,
        spoken_text: str,
        interrupt_ms: int,
        timing: VoiceTiming | None,
        cancel_event: threading.Event,
        speech_request_started_at: float,
    ) -> bool:
        if timing:
            timing.mark_state(INTERVIEW_STATE_SPEAKING)
        playback_started_at = time.perf_counter()
        try:
            pygame.mixer.music.load(str(speech_path))
        except pygame.error:
            self.delete_speech_cache(speech_text)
            speech_path = self.ensure_speech_file(speech_text, force=True)
            pygame.mixer.music.load(str(speech_path))
        pygame.mixer.music.play()
        if timing and not timing.first_spoken_audio_seconds:
            timing.first_spoken_audio_seconds = time.perf_counter() - speech_request_started_at

        consecutive_speech = 0
        needed_frames = max(2, int(interrupt_ms / VOICE_FRAME_MS))
        verify_frames_min = max(needed_frames, int(0.35 * 1000 / VOICE_FRAME_MS))
        verify_frames_max = max(verify_frames_min, int(VOICE_BARGE_IN_VERIFY_SECONDS * 1000 / VOICE_FRAME_MS))
        possible_speech_frames = []
        verification_future = None
        last_verification_at = 0.0

        while pygame.mixer.music.get_busy() and not cancel_event.is_set():
            frame = mic.read_frame(timeout=0.005)
            if frame:
                vad_started_at = time.perf_counter()
                is_speech = self._is_speech(frame)
                if timing:
                    timing.vad_seconds += time.perf_counter() - vad_started_at
            else:
                is_speech = False

            if frame and is_speech:
                consecutive_speech += 1
                possible_speech_frames.append(frame)
                possible_speech_frames = possible_speech_frames[-verify_frames_max:]
            else:
                consecutive_speech = 0

            now = time.monotonic()
            can_verify = (
                consecutive_speech >= needed_frames
                and len(possible_speech_frames) >= verify_frames_min
                and verification_future is None
                and now - last_verification_at >= VOICE_BARGE_IN_COOLDOWN_SECONDS
            )
            if can_verify:
                verification_future = self.audio_executor.submit(
                    self.verify_barge_in,
                    b"".join(possible_speech_frames),
                    spoken_text,
                )
                last_verification_at = now
                possible_speech_frames = []

            if verification_future and verification_future.done():
                try:
                    verification = verification_future.result()
                except Exception as exc:
                    verification = {
                        "interrupt": False,
                        "text": "",
                        "error": str(exc),
                    }
                verification_future = None
                if not verification.get("interrupt"):
                    continue
                cancel_event.set()
                pygame.mixer.music.stop()
                mic.clear()
                print(f"\nInterrupted by candidate: {verification.get('text')}")
                if timing:
                    timing.interruptions += 1
                    timing.mark_state(INTERVIEW_STATE_INTERRUPTED)
                    timing.playback_seconds += time.perf_counter() - playback_started_at
                return True

        if cancel_event.is_set():
            pygame.mixer.music.stop()
            return True
        if timing:
            timing.playback_seconds += time.perf_counter() - playback_started_at
        return False

    @traceable(name="text_to_speech")
    def speak(self, text: str, mic: Microphone, interrupt_ms: int = 450, timing: VoiceTiming | None = None) -> bool:
        speech_started_at = time.perf_counter()
        cancel_event = threading.Event()
        try:
            if timing and timing.states and timing.states[-1] == INTERVIEW_STATE_PROCESSING:
                timing.mark_state(INTERVIEW_STATE_RESPONDING)
            sentences = split_spoken_sentences(text)
            if not sentences:
                return False
            next_future = self.prefetch_speech(sentences[0])
            for index, sentence in enumerate(sentences):
                tts_wait_started_at = time.perf_counter()
                speech_path = next_future.result()
                if timing:
                    timing.tts_seconds += time.perf_counter() - tts_wait_started_at
                next_future = self.prefetch_speech(sentences[index + 1]) if index + 1 < len(sentences) else None
                if self.play_speech_file(
                    speech_path,
                    sentence,
                    mic,
                    text,
                    interrupt_ms,
                    timing,
                    cancel_event,
                    speech_started_at,
                ):
                    if next_future:
                        next_future.cancel()
                    return True
            return False
        finally:
            pass

    def print_timing_breakdown(self, label: str, timing: VoiceTiming):
        if not VOICE_SHOW_TIMINGS:
            return
        data = timing.as_dict()
        print(
            "[voice timing] "
            f"{label} | "
            f"vad={data['vad_seconds']:.3f}s "
            f"speech={data['speech_detection_seconds']:.3f}s "
            f"whisper={data['transcription_seconds']:.3f}s "
            f"thinking={data['thinking_seconds']:.3f}s "
            f"prompt={data['prompt_seconds']:.3f}s "
            f"retrieval={data['retrieval_seconds']:.3f}s "
            f"llm_first={data['first_llm_token_seconds']:.3f}s "
            f"llm_full={data['full_llm_completion_seconds']:.3f}s "
            f"llm={data['llm_seconds']:.3f}s "
            f"tts_wait={data['tts_seconds']:.3f}s "
            f"first_audio={data['first_spoken_audio_seconds']:.3f}s "
            f"playback={data['playback_seconds']:.3f}s "
            f"interruptions={data['interruptions']} "
            f"total={data['total_seconds']:.3f}s"
        )

    @traceable(name="voice_policy_turn")
    def answer_turn(self, user_text: str) -> str:
        
        response = llm.invoke(user_text)

        return response.content
        # return ask_policy(
        #     user_text,
        #     thread_id=self.thread_id
        # )

    @traceable(name="stream_voice_policy_turn_to_speech")
    def stream_answer_to_speech(self, user_text: str, mic: Microphone) -> tuple[str, VoiceTiming, bool]:
        timing = VoiceTiming()
        turn_started_at = time.perf_counter()
        token_started_at = time.perf_counter()
        chunks = []
        sentence_queue = queue.Queue()
        stop_event = threading.Event()

        def producer():
            buffer = ""
            try:
                for chunk in llm.stream(user_text):
                    if stop_event.is_set():
                        break
                    content = getattr(chunk, "content", "") or ""
                    if not content:
                        continue
                    if not timing.first_llm_token_seconds:
                        timing.first_llm_token_seconds = time.perf_counter() - token_started_at
                    chunks.append(content)
                    buffer += content
                    while True:
                        match = re.search(r"(.+?[.!?])(\s+|$)", buffer, flags=re.S)
                        if not match:
                            break
                        sentence = " ".join(match.group(1).split())
                        if sentence:
                            sentence_queue.put(sentence)
                        buffer = buffer[match.end():]
                remaining = " ".join(buffer.split())
                if remaining and not stop_event.is_set():
                    sentence_queue.put(remaining)
            finally:
                timing.full_llm_completion_seconds = time.perf_counter() - token_started_at
                timing.llm_seconds = timing.full_llm_completion_seconds
                sentence_queue.put(None)

        future = self.llm_executor.submit(producer)
        interrupted = False
        try:
            while True:
                sentence = sentence_queue.get()
                if sentence is None:
                    break
                interrupted = self.speak(sentence, mic, timing=timing)
                if interrupted:
                    stop_event.set()
                    future.cancel()
                    break
        finally:
            timing.total_seconds = time.perf_counter() - turn_started_at
        return "".join(chunks).strip(), timing, interrupted

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
                answer, stream_timing, was_interrupted = self.stream_answer_to_speech(user_text, mic)
                answer_finished_at = time.perf_counter()
                print(f"\nAI: {answer}")
                self.print_timing_breakdown("voice policy turn", stream_timing)
                if not was_interrupted:
                    mic.clear()

                if VOICE_SHOW_TIMINGS:
                    print(
                        "[timing] "
                        f"listen={transcribe_started_at - turn_started_at:.2f}s "
                        f"stt={transcribe_finished_at - transcribe_started_at:.2f}s "
                        f"stream+tts+play={answer_finished_at - answer_started_at:.2f}s"
                    )


class AIRecruiterInterviewAgent(VoicePolicyAgent):
    def __init__(self, application_id: int):
        super().__init__()
        from recruiter_agent import RecruiterDatabase

        self.application_id = application_id
        self.db = RecruiterDatabase()
        self.db.init_schema()
        application = self.db.application_with_requirement(application_id)
        if not application:
            raise RuntimeError(f"Application not found: {application_id}")
        self.application = application
        self.report_llm = make_chat_model(json_mode=True, max_tokens=max(OLLAMA_NUM_PREDICT, 1800))
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.report_warmup_future = self.executor.submit(self.warmup_report_llm)
        self.questions_future = self.executor.submit(self.build_questions)
        self.pending_next_question_future: Future | None = None
        self.answers: list[dict] = []
        self.metrics = InterviewMetrics()
        self.interview_state = INTERVIEW_STATE_WAITING
        self.interview_memory = {
            "questions_asked": [],
            "technologies_mentioned": [],
            "strengths": [],
            "weak_areas": [],
            "notes": [],
        }
        self.conversation = ConversationMemory()
        for phrase in self.acknowledgement_options() + self.transition_options():
            self.prefetch_speech(phrase)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.db.close()
        super().close()

    def warmup_report_llm(self):
        try:
            self.report_llm.invoke('{"ready": true}')
        except Exception:
            pass

    @traceable(name="interview_state_transition")
    def trace_state_transition(self, previous_state: str, next_state: str, reason: str) -> dict:
        return {
            "previous_state": previous_state,
            "next_state": next_state,
            "reason": reason,
        }

    def set_interview_state(self, state: str, reason: str, timing: VoiceTiming | None = None):
        previous = self.interview_state
        if previous != state:
            self.interview_state = state
            self.trace_state_transition(previous, state, reason)
        if timing:
            timing.mark_state(state)

    @traceable(name="interview_intent_decision")
    def trace_intent_decision(self, transcript: str, intent: str, source: str, reason: str) -> dict:
        return {
            "transcript": transcript,
            "intent": intent,
            "source": source,
            "reason": reason,
            "state": self.interview_state,
        }

    @traceable(name="conversation_memory_update")
    def trace_conversation_memory_update(self, event: dict, memory: dict) -> dict:
        return {"event": event, "memory": memory}

    @traceable(name="conversation_decision")
    def trace_conversation_decision(self, intent: dict, decision: dict, memory: dict) -> dict:
        return {"intent": intent, "decision": decision, "memory": memory}

    @traceable(name="main_question_transition_prepared")
    def trace_main_question_transition_prepared(self, index: int, question: str, seconds: float) -> dict:
        return {"index": index, "question": question, "seconds": round(seconds, 3)}

    @traceable(name="main_question_transition_masked")
    def trace_main_question_transition_masked(self, index: int, fillers_used: int, seconds: float) -> dict:
        return {"index": index, "fillers_used": fillers_used, "seconds": round(seconds, 3)}

    def conversation_snapshot(self) -> dict:
        return {
            "current_question_number": self.conversation.current_question_number,
            "current_question": self.conversation.current_question,
            "previous_question_number": self.conversation.previous_question_number,
            "previous_question": self.conversation.previous_question,
            "waiting_for": self.conversation.waiting_for,
            "paused": self.conversation.paused,
            "partial_answer": self.conversation.partial_answer[-500:],
            "confused_count": self.conversation.confused_count,
            "recent_events": self.conversation.events[-8:],
        }

    def remember_conversation_event(
        self,
        role: str,
        text: str,
        intent: str | None = None,
        action: str | None = None,
        question_number: int | None = None,
        metadata: dict | None = None,
    ):
        event = {
            "role": role,
            "text": (text or "").strip(),
            "intent": intent,
            "action": action,
            "question_number": question_number,
            "state": self.interview_state,
            "waiting_for": self.conversation.waiting_for,
            "paused": self.conversation.paused,
            "metadata": metadata or {},
        }
        self.conversation.events.append(event)
        self.conversation.events = self.conversation.events[-40:]
        self.trace_conversation_memory_update(event, self.conversation_snapshot())

    def set_active_question(self, number: int, question: str):
        question = self.shorten_question(question)
        if self.conversation.current_question_number and self.conversation.current_question_number != number:
            self.conversation.previous_question_number = self.conversation.current_question_number
            self.conversation.previous_question = self.conversation.current_question
        self.conversation.current_question_number = number
        self.conversation.current_question = question
        self.conversation.questions[number] = question
        self.conversation.waiting_for = "answer"

    def question_by_reference(self, user_text: str) -> tuple[int | None, str]:
        text = " ".join((user_text or "").lower().split())
        ordinal_map = {
            "first": 1,
            "question one": 1,
            "one": 1,
            "second": 2,
            "question two": 2,
            "two": 2,
            "third": 3,
            "question three": 3,
            "three": 3,
        }
        for phrase, number in ordinal_map.items():
            if phrase in text and number in self.conversation.questions:
                return number, self.conversation.questions[number]
        if "previous question" in text or "last question" in text or "earlier question" in text:
            number = self.conversation.previous_question_number
            if number and number in self.conversation.questions:
                return number, self.conversation.questions[number]
        return None, ""

    def rule_based_conversation_intent(self, user_text: str) -> dict | None:
        text = " ".join((user_text or "").lower().split())
        if not text:
            return {"intent": "empty", "is_answer": False, "reason": "No transcript", "source": "rules"}
        phrase_groups = {
            "ready_to_resume": [
                "ready now",
                "i am ready",
                "i'm ready",
                "ready to continue",
                "we can continue",
                "continue now",
                "okay ready",
                "ok ready",
                "go ahead",
            ],
            "pause_request": [
                "give me",
                "one second",
                "just a moment",
                "hold on",
                "wait",
                "two minutes",
                "fixing my headphone",
                "fixing my headphones",
                "checking my headphone",
                "checking my headphones",
                "let me think",
                "i am thinking",
                "i'm thinking",
            ],
            "previous_question_reference": [
                "previous question",
                "last question",
                "earlier question",
                "talking about the previous",
                "talking about previous",
                "talking about last",
            ],
            "repeat_specific_question": [
                "first question",
                "question one",
                "second question",
                "question two",
                "third question",
                "question three",
            ],
        }
        for intent, phrases in phrase_groups.items():
            if intent == "pause_request":
                matched = False
                for phrase in phrases:
                    if phrase in {"wait", "give me", "hold on"}:
                        matched = (
                            text == phrase
                            or text.startswith(f"{phrase} ")
                            or (phrase in text and word_count(text) <= 8)
                        )
                    else:
                        matched = phrase in text
                    if matched:
                        break
                if matched:
                    return {"intent": intent, "is_answer": False, "reason": f"Matched {intent}", "source": "rules"}
                continue
            if any(phrase in text for phrase in phrases):
                return {"intent": intent, "is_answer": False, "reason": f"Matched {intent}", "source": "rules"}
        fast = self.fast_intent(user_text)
        if fast:
            return fast
        return None

    @traceable(name="understand_candidate_intent")
    def understand_candidate_intent(self, question: str, user_text: str) -> dict:
        text = (user_text or "").strip()
        rule_intent = self.rule_based_conversation_intent(text)
        if rule_intent:
            self.trace_intent_decision(text, rule_intent.get("intent", "unknown"), rule_intent.get("source", "rules"), rule_intent.get("reason", ""))
            return rule_intent

        response = self.report_llm.invoke(
            f"""
Return one valid JSON object only.
You are managing a live technical interview conversation.
Classify what the candidate is trying to do right now.
Do not treat clarification, repetition, pause, readiness, or question references as answers.
If the candidate makes a relevant technical attempt, even weak or incomplete, classify as answer.

Allowed intents:
answer, partial_answer, thinking, pause_request, ready_to_resume,
repeat_question, repeat_specific_question, previous_question_reference,
clarification, candidate_question, next_question, end_interview,
correction, greeting, empty, unknown

JSON schema:
{{
  "intent": "one allowed intent",
  "is_answer": true,
  "reason": "short reason",
  "suggested_reply": "short response if the interviewer should reply before continuing, otherwise null",
  "target_question_number": null
}}

Current conversation memory:
{json.dumps(self.conversation_snapshot(), default=str)}

Current interview question:
{question}

Candidate utterance:
{text}
"""
        ).content
        try:
            decision = json.loads(extract_json_object(response))
            decision.setdefault("source", "llm")
            self.trace_intent_decision(text, decision.get("intent", "unknown"), "llm", decision.get("reason", ""))
            return decision
        except Exception:
            decision = {
                "intent": "answer" if self.attempted_answer(text) else "unknown",
                "is_answer": self.attempted_answer(text),
                "reason": "Fallback semantic intent",
                "suggested_reply": None,
                "source": "fallback",
            }
            self.trace_intent_decision(text, decision["intent"], "fallback", decision["reason"])
            return decision

    def decide_conversation_action(self, intent: dict, question: str, user_text: str, low_confidence: bool = False) -> ConversationDecision:
        intent_name = (intent.get("intent") or "unknown").lower()
        response = (intent.get("suggested_reply") or "").strip()
        target_number, target_question = self.question_by_reference(user_text)

        if low_confidence and intent_name not in {"next_question", "repeat_question", "repeat_specific_question", "end_interview"}:
            decision = ConversationDecision("clarify", "I didn't quite catch that. Could you say that again?", "low STT confidence")
        elif intent_name == "end_interview":
            decision = ConversationDecision("stop", response or "Sure, we can stop here.", "candidate asked to stop", status="stopped_by_candidate")
        elif intent_name in {"next_question", "skip_question"}:
            decision = ConversationDecision("skip", response or "No problem, let's move on.", "candidate requested next question", should_advance=True, status="skipped_by_candidate")
        elif intent_name in {"pause_request", "thinking"}:
            self.conversation.paused = True
            self.conversation.waiting_for = "ready"
            decision = ConversationDecision("pause", response or "Of course. Take your time. Let me know when you're ready.", "candidate requested time")
        elif intent_name == "ready_to_resume":
            self.conversation.paused = False
            self.conversation.waiting_for = "answer"
            decision = ConversationDecision("resume", "Great, let's continue.", "candidate is ready")
        elif intent_name in {"repeat_specific_question", "previous_question_reference"}:
            number = target_number or intent.get("target_question_number")
            target = target_question or self.conversation.questions.get(number or 0, "")
            if target:
                decision = ConversationDecision(
                    "return_to_question" if intent_name == "previous_question_reference" else "repeat_question",
                    f"Sure. {target}",
                    f"candidate referenced question {number}",
                    target_question_number=number,
                    target_question=target,
                )
            else:
                decision = ConversationDecision("repeat_question", f"Sure. {question}", "question reference not found")
        elif intent_name == "repeat_question":
            decision = ConversationDecision("repeat_question", f"Sure, let me repeat that. {question}", "candidate asked repeat")
        elif intent_name in {"clarification", "candidate_question"}:
            decision = ConversationDecision("clarify", response or self.clarification_then_question(question, user_text), "candidate asked clarification")
        elif intent_name == "greeting":
            decision = ConversationDecision("wait_for_answer", response or "Yes, I'm here. Please go ahead.", "candidate greeting or audio check")
        elif intent_name in {"answer", "answer_finished"} or intent.get("is_answer") or self.attempted_answer(user_text):
            decision = ConversationDecision("accept_answer", "", "candidate gave an answer", should_evaluate_answer=True, should_advance=True, status="answered")
        elif intent_name in {"partial_answer", "unknown", "empty"}:
            decision = ConversationDecision("wait_for_answer", response or "Please continue, I'm listening.", "candidate has not completed an answer")
        else:
            decision = ConversationDecision("wait_for_answer", response or "Please continue, I'm listening.", f"unhandled intent {intent_name}")

        self.trace_conversation_decision(intent, decision.__dict__, self.conversation_snapshot())
        return decision

    def context_payload(self) -> dict:
        return {
            "application_id": self.application_id,
            "candidate_name": self.application.get("full_name"),
            "candidate_email": self.application.get("candidate_email") or self.application.get("source_email"),
            "role": self.application.get("requirement_position") or self.application.get("matched_position") or self.application.get("detected_position"),
            "job_description": (self.application.get("job_description") or "")[:5000],
            "cv_summary": self.application.get("cv_summary") or self.application.get("ai_short_description"),
            "cv_text": (self.application.get("raw_cv_text") or "")[:9000],
            "screening_details": self.application.get("screening_details"),
            "ats_score": self.application.get("ats_score"),
            "jd_match_score": self.application.get("jd_match_score"),
        }

    @traceable(name="build_recruiter_interview_questions")
    def build_questions(self) -> list[str]:
        response = self.report_llm.invoke(
            f"""
Return one valid JSON object only.
Create {RECRUITER_INTERVIEW_QUESTION_COUNT} easy-to-medium interview questions for this candidate.
Questions must be based on the job description and CV.
Include a mix of introduction, project discussion, Python fundamentals, practical problem solving, framework/database/API questions, and role-fit questions.
Do not ask trick questions.
Each question must be conversational and maximum 15 spoken words.
Do not read resume details back to the candidate.

JSON schema:
{{
  "questions": ["question 1", "question 2"]
}}

Context:
{json.dumps(self.context_payload(), default=str)}
"""
        ).content
        try:
            data = json.loads(extract_json_object(response))
            questions = data.get("questions") or []
        except Exception:
            questions = []
        questions = [str(question).strip() for question in questions if str(question).strip()]
        if len(questions) < RECRUITER_INTERVIEW_QUESTION_COUNT:
            questions = [
                "Tell me about your Python experience.",
                "Describe one Python project you owned.",
                "How do lists and dictionaries differ?",
                "How do you handle production errors?",
                "How would you design a REST endpoint?",
                "How do you optimize slow database queries?",
                "Describe a performance issue you solved.",
                "How do you test Python code?",
                "How would you debug a production API issue?",
                "Why does this role interest you?",
            ]
        return [self.shorten_question(question) for question in questions[:RECRUITER_INTERVIEW_QUESTION_COUNT]]

    def shorten_question(self, question: str) -> str:
        text = " ".join((question or "").split())
        text = re.sub(r"^(can you|could you|please)\s+", "", text, flags=re.I)
        text = re.sub(r"\b(resulting in|which resulted in|where you)\b.*?,", "", text, flags=re.I)
        if word_count(text) <= INTERVIEW_MAX_SPOKEN_WORDS:
            return text
        first_sentence = split_spoken_sentences(text)[0]
        if word_count(first_sentence) <= INTERVIEW_MAX_SPOKEN_WORDS:
            return first_sentence
        return shorten_spoken_text(first_sentence, INTERVIEW_MAX_SPOKEN_WORDS)

    def intro_text(self) -> str:
        role = self.context_payload().get("role") or "this role"
        return (
            f"Great, thank you. Welcome to your interview for {role}. "
            "I will ask a few short questions. Please answer naturally."
        )

    def greeting_text(self) -> str:
        candidate_name = self.application.get("full_name")
        if candidate_name:
            return f"Hello {candidate_name}, can you hear me?"
        return "Hello, can you hear me?"

    def start_interview_conversation(self, mic: Microphone):
        greeting = self.greeting_text()
        intro = self.intro_text()
        self.prefetch_speech(intro)
        self.remember_conversation_event("interviewer", greeting, action="greeting", question_number=0)
        self.speak(greeting, mic, interrupt_ms=INTERVIEW_MAIN_INTERRUPT_MS)
        greeting_reply = self.listen_and_transcribe_for(mic, max_seconds=20)
        print(f"Candidate: {greeting_reply}")
        greeting_intent = self.understand_candidate_intent("Opening greeting", greeting_reply)
        self.remember_conversation_event("candidate", greeting_reply, intent=greeting_intent.get("intent"), question_number=0)
        self.answers.append(
            {
                "question_number": 0,
                "question": "Opening greeting",
                "answer": greeting_reply,
                "status": "greeting",
            }
        )
        if not greeting_reply:
            self.speak(
                "I could not hear you clearly, but I will continue. Please let me know if there is any audio issue.",
                mic,
                interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS,
            )
        self.remember_conversation_event("interviewer", intro, action="intro", question_number=0)
        self.speak(intro, mic, interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS)

    def closing_text(self) -> str:
        return (
            "Thank you for your time today. I really appreciate you sharing your experience with me. "
            "We will review everything and get back to you with feedback soon."
        )

    def print_interview_summary(self):
        summary = self.metrics.summary()
        print(
            "[interview summary] "
            f"turns={summary['turns']} "
            f"avg_response_latency={summary['avg_response_latency_seconds']:.3f}s "
            f"avg_interviewer_speech={summary['avg_interviewer_speech_seconds']:.3f}s "
            f"avg_candidate_speech={summary['avg_candidate_speech_seconds']:.3f}s "
            f"interviewer_talk_ratio={summary['interviewer_talk_ratio']:.3f} "
            f"candidate_talk_ratio={summary['candidate_talk_ratio']:.3f} "
            f"under_25_percent={summary['interviewer_under_25_percent']} "
            f"avg_llm={summary['avg_llm_seconds']:.3f}s "
            f"avg_tts={summary['avg_tts_seconds']:.3f}s "
            f"interruption_rate={summary['interruption_rate']:.3f} "
            f"total_interruptions={summary['total_interruptions']}"
        )

    def fast_intent(self, user_text: str) -> dict | None:
        text = " ".join((user_text or "").lower().split())
        if not text:
            return {"intent": "empty", "is_answer": False, "reason": "No transcript"}

        end_phrases = ["end interview", "stop interview", "quit interview", "i want to stop", "let's stop", "we can stop"]
        repeat_phrases = ["repeat", "come again", "say that again", "say again", "i missed", "did not hear", "didn't hear", "can you repeat"]
        skip_phrases = ["skip", "next question", "move on", "go next", "ask next", "leave this", "don't know", "i don't know", "no idea"]
        greeting_phrases = ["hello", "hi", "yes i can hear", "i can hear", "yes"]
        hold_phrases = ["give me", "one second", "just a moment", "hold on", "wait", "two minutes", "let me think", "i am thinking", "i'm thinking"]
        ready_phrases = ["ready now", "i am ready", "i'm ready", "ready to continue", "we can continue", "continue now", "okay ready", "ok ready"]
        previous_phrases = ["previous question", "last question", "earlier question", "talking about the previous", "talking about previous"]
        clarification_phrases = [
            "what do you mean",
            "can you explain",
            "could you explain",
            "explain the question",
            "do you mean",
            "did you mean",
            "are you asking",
            "which tool",
            "which technology",
            "what is",
        ]
        finished_phrases = [
            "that's all",
            "that is all",
            "i am done",
            "i'm done",
            "that's it",
            "that is it",
            "that's my answer",
            "that is my answer",
            "that's everything",
            "that is everything",
            "that's all from my side",
            "that is all from my side",
        ]
        exact_finished_phrases = {"done", "finished"}
        word_count = len(re.findall(r"\b\w+\b", text))

        if any(phrase in text for phrase in end_phrases):
            decision = {"intent": "end_interview", "is_answer": False, "reason": "Candidate asked to end", "suggested_reply": "Sure, we can stop here.", "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        if any(phrase in text for phrase in skip_phrases):
            decision = {"intent": "next_question", "is_answer": word_count > 12, "reason": "Candidate asked to move on", "suggested_reply": "No problem, let's move on.", "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        if any(phrase in text for phrase in ready_phrases):
            decision = {"intent": "ready_to_resume", "is_answer": False, "reason": "Candidate is ready to continue", "suggested_reply": None, "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        if any(phrase in text for phrase in previous_phrases):
            decision = {"intent": "previous_question_reference", "is_answer": False, "reason": "Candidate referenced an earlier question", "suggested_reply": None, "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        repeat_requested = any(phrase in text for phrase in repeat_phrases)
        mostly_repeat_request = word_count <= 12 or re.search(
            r"(repeat|come again|say that again|say again|can you repeat)\??$",
            text,
        )
        if repeat_requested and mostly_repeat_request:
            decision = {"intent": "repeat_question", "is_answer": False, "reason": "Candidate asked repeat", "suggested_reply": None, "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        if any(phrase in text for phrase in hold_phrases) and word_count <= 12:
            decision = {"intent": "pause_request", "is_answer": False, "reason": "Candidate needs time or is thinking", "suggested_reply": "Of course. Take your time. Let me know when you're ready.", "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        if any(phrase in text for phrase in clarification_phrases) and word_count <= 14:
            decision = {"intent": "clarification", "is_answer": False, "reason": "Candidate asked clarification", "suggested_reply": None, "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        if any(phrase in text for phrase in finished_phrases) or text in exact_finished_phrases:
            decision = {"intent": "answer_finished", "is_answer": True, "reason": "Candidate indicated completion", "suggested_reply": None, "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        if word_count(text) <= 3 and any(text == phrase or text.startswith(f"{phrase} ") for phrase in greeting_phrases):
            decision = {"intent": "greeting", "is_answer": False, "reason": "Greeting or audio confirmation", "suggested_reply": None, "source": "rules"}
            self.trace_intent_decision(user_text, decision["intent"], "rules", decision["reason"])
            return decision
        return None

    def repeat_or_rephrase_question(self, question: str, user_text: str) -> str:
        prompt = user_text.strip().lower()
        if any(phrase in prompt for phrase in ["come again", "repeat", "missed", "say again", "did not hear", "didn't hear"]):
            return f"Sure, let me repeat that. {question}"
        response = llm.invoke(
            f"""
You are a human HR technical interviewer.
The candidate asked for clarification during an interview.
Answer briefly and helpfully.
Do not repeat the full original question.
Keep it under 10 spoken words.

Original question:
{question}

Candidate said:
{user_text}
"""
        ).content.strip()
        return shorten_spoken_text(response or "Could you explain that differently?", 10)

    def answer_clarification(self, question: str, user_text: str) -> str:
        text = (user_text or "").strip().lower()
        if "do you mean" in text or "did you mean" in text or "are you asking" in text:
            return "Yes, answer in that context."
        if "repeat" in text or "come again" in text:
            return f"Sure. {question}"
        if "explain" in text or "what do you mean" in text:
            return "I mean your practical experience with it."
        response = llm.invoke(
            f"""
You are a concise human technical interviewer.
The candidate asked a clarification question.
Answer it in one short sentence, under 12 spoken words.
Do not score the candidate.
Do not move to the next question.

Original interview question:
{question}

Candidate clarification:
{user_text}
"""
        ).content.strip()
        return shorten_spoken_text(response or "I mean your practical experience with it.", 12)

    def clarification_then_question(self, question: str, user_text: str, repeat: bool = False) -> str:
        if repeat:
            return f"Sure. {question}"
        clarification = self.answer_clarification(question, user_text)
        return f"{clarification} {question}"

    def clarification_prompt(self, count: int = 0) -> str:
        prompts = [
            "I didn't quite catch that. Could you elaborate?",
            "Could you explain that differently?",
            "Could you add one specific example?",
        ]
        return prompts[count % len(prompts)]

    def acknowledgement_text(self, result: dict) -> str:
        answer = (result.get("answer") or "").strip()
        if not answer:
            return "Alright, let's move on."
        return random.choice(self.acknowledgement_options())

    def acknowledgement_options(self) -> list[str]:
        return [
            "Understood.",
            "Thanks for explaining.",
            "Alright.",
            "Got it.",
            "That makes sense.",
            "Thanks, that helps.",
        ]

    def transition_text(self) -> str:
        return random.choice(self.transition_options())

    def transition_options(self) -> list[str]:
        return [
            "Let me move to the next one.",
            "I'll ask the next question now.",
            "Let's go to the next area.",
            "I'll keep this moving.",
        ]

    def attempted_answer(self, text: str) -> bool:
        fast = self.fast_intent(text)
        if fast and fast.get("intent") in {
            "next_question",
            "repeat_question",
            "repeat_specific_question",
            "previous_question_reference",
            "end_interview",
            "clarification",
            "greeting",
            "empty",
            "pause_request",
            "ready_to_resume",
        }:
            return False
        return word_count(text) >= 3

    @traceable(name="classify_interview_turn")
    def classify_interview_turn(self, question: str, user_text: str) -> dict:
        text = (user_text or "").strip()
        if not text:
            return {"intent": "empty", "is_answer": False, "reason": "No transcript"}
        fast = self.fast_intent(text)
        if fast:
            return fast

        response = self.report_llm.invoke(
            f"""
Return one valid JSON object only.
Classify the candidate turn in an interview by meaning, not by exact phrase matching.
The candidate may answer the question, ask to repeat/rephrase, ask clarification, ask to skip, request a pause, ask to end the interview, ask an interviewer question, go off topic, or provide too little speech/noise.
Be flexible with wording, grammar mistakes, and speech-to-text errors.
If the candidate makes any relevant attempt, even incomplete or weak, classify it as answer.
If the candidate asks what the question means, asks if you mean a specific thing, or asks for repeat/explanation, classify it as clarification or repeat_question, never as answer.

Examples:
- "next one please", "leave this", "can we go ahead", "ask another" => next_question
- "skip", "I don't know", "move on" => next_question
- "stop interview", "end interview" => end_interview
- "give me two minutes", "hold this meeting", "wait I am busy" => hold_request
- "come again", "I missed", "say that again" => repeat_question
- "what is Django", "which tool did you mean", "can you explain that" => clarification
- "hello", "yes I can hear you" at the beginning => greeting
- a real attempt to answer the technical question => answer

JSON schema:
{{
  "intent": "answer/next_question/repeat_question/clarification/end_interview/greeting/hold_request/interviewer_question/too_short/off_topic/unknown",
  "is_answer": true,
  "reason": "short reason",
  "suggested_reply": "very short reply only if not an answer, otherwise null"
}}

Question:
{question}

Candidate turn:
{text}
"""
        ).content
        try:
            decision = json.loads(extract_json_object(response))
            self.trace_intent_decision(text, decision.get("intent", "unknown"), "llm", decision.get("reason", ""))
            return decision
        except Exception:
            decision = {
                "intent": "answer" if len(text.split()) >= 4 else "too_short",
                "is_answer": len(text.split()) >= 4,
                "reason": "Fallback classification",
                "suggested_reply": None,
            }
            self.trace_intent_decision(text, decision["intent"], "fallback", decision["reason"])
            return decision

    def listen_and_transcribe_once(self, mic: Microphone) -> str:
        pcm_audio = self.listen_for_utterance(mic)
        if not pcm_audio:
            return ""
        return self.transcribe(pcm_audio).strip()

    def listen_and_transcribe_for(self, mic: Microphone, max_seconds: float) -> str:
        started_at = time.monotonic()
        while time.monotonic() - started_at < max_seconds:
            frame = mic.read_frame(timeout=0.1)
            if frame and self._is_speech(frame):
                mic.frames.put(frame)
                return self.listen_and_transcribe_once(mic)
        return ""

    def has_hesitation_marker(self, text: str) -> bool:
        lowered = text.lower().strip()
        hesitation_phrases = [
            "let me think",
            "one second",
            "just a moment",
            "trying to remember",
            "i am thinking",
            "i'm thinking",
            "wait",
        ]
        tail = lowered[-100:].strip(" .,!?:;")
        if any(phrase in tail for phrase in hesitation_phrases):
            return True
        tail_words = self.normalized_words(tail)
        if not tail_words:
            return False
        filler_endings = {"um", "uh", "hmm", "hmmm", "so", "actually", "wait", "like", "means"}
        if tail_words[-1] in filler_endings:
            return True
        return tail.endswith(("and", "or", "because", "then", "for", "i think"))

    def thinking_prompt(self, count: int) -> str:
        prompts = ["Take your time.", "I'm listening.", "No rush.", "Whenever you're ready."]
        return prompts[count % len(prompts)]

    @traceable(name="thinking_pause_detected")
    def trace_thinking_pause_detected(self, transcript: str, silence_seconds: float) -> dict:
        return {"transcript": transcript, "silence_seconds": round(silence_seconds, 3)}

    @traceable(name="thinking_timeout_extended")
    def trace_thinking_timeout_extended(self, reason: str, silence_seconds: float) -> dict:
        return {"reason": reason, "silence_seconds": round(silence_seconds, 3)}

    @traceable(name="candidate_resumed_speaking")
    def trace_candidate_resumed_speaking(self, pause_seconds: float) -> dict:
        return {"pause_seconds": round(pause_seconds, 3)}

    @traceable(name="turn_finalized")
    def trace_turn_finalized(
        self,
        reason: str,
        transcript: str,
        silence_seconds: float,
        resumed_after_thinking: bool = False,
        answer_duration_seconds: float = 0.0,
        continuous_speech_seconds: float = 0.0,
        speech_detected: bool = False,
    ) -> dict:
        return {
            "reason": reason,
            "transcript": transcript,
            "silence_seconds": round(silence_seconds, 3),
            "resumed_after_thinking": resumed_after_thinking,
            "answer_duration_seconds": round(answer_duration_seconds, 3),
            "continuous_speech_seconds": round(continuous_speech_seconds, 3),
            "speech_detected": speech_detected,
        }

    def transcript_appears_complete(self, text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped or self.has_hesitation_marker(stripped):
            return False
        words = word_count(stripped)
        if words < 6:
            return False
        if stripped.rstrip().endswith((".", "!", "?")):
            return True
        completion_starters = [
            "i used",
            "i worked",
            "i created",
            "i built",
            "i handled",
            "i implemented",
            "we used",
            "we built",
            "my role",
            "the challenge",
            "the main challenge",
            "because",
        ]
        lowered = stripped.lower()
        has_explanation_signal = any(signal in lowered for signal in completion_starters)
        return words >= 12 and has_explanation_signal

    def answer_completion_confidence(self, transcript: str, silence_seconds: float) -> dict:
        text = (transcript or "").strip()
        fast = self.fast_intent(text)
        if fast and fast.get("intent") in {"clarification", "repeat_question"}:
            return {"complete": False, "confidence": 0.0, "reason": fast.get("intent")}
        if fast and fast.get("intent") in {"answer_finished", "next_question", "end_interview"}:
            return {"complete": True, "confidence": 1.0, "reason": fast.get("reason", "explicit completion")}
        if self.has_hesitation_marker(text):
            return {"complete": False, "confidence": 0.1, "reason": "thinking phrase detected"}
        if self.transcript_appears_complete(text):
            confidence = 0.9 if silence_seconds >= INTERVIEW_QUICK_INTENT_SILENCE_SECONDS else 0.75
            return {"complete": silence_seconds >= INTERVIEW_QUICK_INTENT_SILENCE_SECONDS, "confidence": confidence, "reason": "semantic complete answer"}
        return {"complete": False, "confidence": 0.25, "reason": "semantic answer incomplete"}

    def evaluate_turn_completion(
        self,
        question: str,
        transcript: str,
        silence_seconds: float,
        answer_seconds: float,
        speech_detected: bool = False,
        safety_timeout_reached: bool = False,
    ) -> dict:
        text = (transcript or "").strip()
        fast = self.fast_intent(text)
        if fast:
            intent = fast.get("intent")
            if intent == "answer_finished":
                return {"complete": True, "reason": "explicit finished", "intent": fast}
            if intent == "next_question":
                return {"complete": True, "reason": "explicit next question", "intent": fast}
            if intent == "end_interview":
                return {"complete": True, "reason": "explicit end interview", "intent": fast}
            if intent == "repeat_question":
                return {"complete": True, "reason": "explicit repeat request", "intent": fast}
            if intent == "clarification":
                return {"complete": True, "reason": "explicit clarification request", "intent": fast}
            if intent in {"pause_request", "ready_to_resume", "previous_question_reference", "repeat_specific_question", "greeting"}:
                return {"complete": True, "reason": f"explicit {intent}", "intent": fast}

        if speech_detected:
            return {"complete": False, "reason": "active speech detected"}
        completion_confidence = self.answer_completion_confidence(text, silence_seconds)
        if completion_confidence.get("complete"):
            return {
                "complete": True,
                "reason": completion_confidence.get("reason", "semantic complete answer"),
                "completion_confidence": completion_confidence.get("confidence", 0.0),
            }
        if self.has_hesitation_marker(text):
            return {"complete": False, "reason": "thinking phrase detected", "completion_confidence": completion_confidence.get("confidence", 0.0)}
        if safety_timeout_reached and silence_seconds >= RECRUITER_THINKING_SILENCE_SECONDS and self.transcript_appears_complete(text):
            return {"complete": True, "reason": "interviewer safety timeout after prolonged continuous speech"}
        if silence_seconds >= RECRUITER_LONG_SILENCE_SECONDS and self.transcript_appears_complete(text):
            return {"complete": True, "reason": "long silence + complete transcript"}
        if silence_seconds >= RECRUITER_LONG_SILENCE_SECONDS:
            return {"complete": False, "reason": "long silence but transcript incomplete", "completion_confidence": completion_confidence.get("confidence", 0.0)}
        if silence_seconds >= INTERVIEW_ANSWER_CHECK_SILENCE_SECONDS and self.transcript_appears_complete(text):
            return {"complete": True, "reason": "complete transcript + pause"}
        return {"complete": False, "reason": completion_confidence.get("reason", "candidate may still be thinking"), "completion_confidence": completion_confidence.get("confidence", 0.0)}

    @traceable(name="candidate_answer_completion")
    def is_candidate_turn_complete(self, question: str, transcript: str, silence_seconds: float) -> dict:
        return self.evaluate_turn_completion(question, transcript, silence_seconds, 0.0)

    @traceable(name="listen_candidate_turn")
    def listen_candidate_turn(self, mic: Microphone, question: str) -> CandidateTurn:
        timing = VoiceTiming()
        timing.mark_state(INTERVIEW_STATE_LISTENING)
        turn_started_at = time.perf_counter()
        detection_started_at = time.perf_counter()
        speech_frames = []
        speech_frame_count = 0
        has_started = False
        started_at = None
        last_speech_at = None
        last_candidate_speech_at = None
        last_support_prompt_at = None
        thinking_prompt_count = 0
        completion = {"complete": False, "reason": "No completion decision yet"}
        completion_speech_detected = False
        last_partial_check_at = None
        last_partial_text = ""
        thinking_started_at = None
        resumed_after_thinking = False
        continuous_speech_started_at = None
        continuous_speech_seconds = 0.0
        safety_timeout_reached = False
        speech_detected = False
        min_speech_frames = max(1, int(RECRUITER_MIN_ANSWER_SECONDS * 1000 / VOICE_FRAME_MS))

        print("\nListening...")
        while True:
            frame = mic.read_frame(timeout=0.1)
            if frame is None:
                continue

            now = time.monotonic()
            vad_started_at = time.perf_counter()
            is_speech = self._is_speech(frame)
            timing.vad_seconds += time.perf_counter() - vad_started_at
            if is_speech:
                speech_detected = True
                if continuous_speech_started_at is None:
                    continuous_speech_started_at = now
                continuous_speech_seconds = now - continuous_speech_started_at
                if not has_started:
                    started_at = now
                has_started = True
                if thinking_started_at:
                    self.trace_candidate_resumed_speaking(now - thinking_started_at)
                    completion = {"complete": False, "reason": "candidate resumed after thinking"}
                    thinking_started_at = None
                    resumed_after_thinking = True
                    timing.mark_state(INTERVIEW_STATE_LISTENING)
                speech_frame_count += 1
                last_speech_at = now
                last_candidate_speech_at = now
                speech_frames.append(frame)
            elif has_started:
                speech_detected = False
                speech_frames.append(frame)

            if not has_started or not started_at or not last_speech_at or not last_candidate_speech_at:
                continue

            silence_for = now - last_candidate_speech_at
            answer_for = now - started_at
            if answer_for >= VOICE_MAX_UTTERANCE_SECONDS and not safety_timeout_reached:
                safety_timeout_reached = True
                self.trace_thinking_timeout_extended("safety timeout reached; waiting for speech to stop", silence_for)

            quick_intent_check = (
                silence_for >= INTERVIEW_QUICK_INTENT_SILENCE_SECONDS
                and speech_frame_count >= min_speech_frames
                and (last_partial_check_at is None or now - last_partial_check_at >= 0.8)
            )
            if quick_intent_check:
                last_partial_check_at = now
                timing.speech_detection_seconds += time.perf_counter() - detection_started_at
                transcribe_started_at = time.perf_counter()
                partial_audio = b"".join(speech_frames)
                partial_text = self.transcribe(partial_audio)
                last_partial_text = partial_text
                timing.transcription_seconds += time.perf_counter() - transcribe_started_at
                completion = self.evaluate_turn_completion(
                    question,
                    partial_text,
                    silence_for,
                    answer_for,
                    speech_detected=speech_detected,
                    safety_timeout_reached=safety_timeout_reached,
                )
                if completion.get("complete"):
                    completion_speech_detected = speech_detected
                    break

            should_check_completion = (
                silence_for >= INTERVIEW_ANSWER_CHECK_SILENCE_SECONDS
                and speech_frame_count >= min_speech_frames
                and (
                    last_support_prompt_at is None
                    or now - last_support_prompt_at >= RECRUITER_THINKING_SILENCE_SECONDS
                )
            )
            if not should_check_completion:
                continue

            timing.speech_detection_seconds += time.perf_counter() - detection_started_at
            transcribe_started_at = time.perf_counter()
            partial_audio = b"".join(speech_frames)
            partial_text = self.transcribe(partial_audio)
            last_partial_text = partial_text
            timing.transcription_seconds += time.perf_counter() - transcribe_started_at
            completion_started_at = time.perf_counter()
            completion = self.evaluate_turn_completion(
                question,
                partial_text,
                silence_for,
                answer_for,
                speech_detected=speech_detected,
                safety_timeout_reached=safety_timeout_reached,
            )
            completion_seconds = time.perf_counter() - completion_started_at
            timing.llm_seconds += completion_seconds
            timing.full_llm_completion_seconds += completion_seconds
            if completion.get("complete"):
                completion_speech_detected = speech_detected
                break

            timing.mark_state(INTERVIEW_STATE_THINKING)
            if not thinking_started_at:
                thinking_started_at = now
                self.trace_thinking_pause_detected(partial_text, silence_for)
            timing.thinking_seconds += silence_for
            completion_confidence = completion.get("completion_confidence", 0.0)
            if silence_for >= RECRUITER_THINKING_SILENCE_SECONDS and completion_confidence < 0.65:
                self.trace_thinking_timeout_extended(completion.get("reason", "candidate may still be thinking"), silence_for)
                prompt = self.thinking_prompt(thinking_prompt_count)
                thinking_prompt_count += 1
                self.speak(prompt, mic, interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS, timing=timing)
                mic.clear()
                last_support_prompt_at = time.monotonic()
            elif completion_confidence >= 0.65:
                completion = {"complete": True, "reason": "semantic complete answer", "completion_confidence": completion_confidence}
                break
            else:
                last_support_prompt_at = now
            last_speech_at = last_support_prompt_at
            detection_started_at = time.perf_counter()

        timing.mark_state(INTERVIEW_STATE_PROCESSING)
        final_audio = b"".join(speech_frames)
        transcribe_started_at = time.perf_counter()
        confidence_result = self.transcribe_with_confidence(final_audio) if final_audio else {"text": "", "confidence": 0.0}
        final_text = confidence_result.get("text", "")
        timing.transcription_seconds += time.perf_counter() - transcribe_started_at
        timing.total_seconds = time.perf_counter() - turn_started_at
        if not completion.get("complete"):
            completion = self.evaluate_turn_completion(
                question,
                final_text or last_partial_text,
                silence_for if has_started else 0.0,
                time.monotonic() - started_at if started_at else 0.0,
                speech_detected=False,
                safety_timeout_reached=safety_timeout_reached,
            )
        completion["resumed_after_thinking"] = resumed_after_thinking
        completion["answer_duration_seconds"] = round(time.monotonic() - started_at, 3) if started_at else 0.0
        completion["continuous_speech_seconds"] = round(continuous_speech_seconds, 3)
        completion["silence_seconds"] = round(silence_for if has_started else 0.0, 3)
        completion["speech_detected"] = bool(completion_speech_detected)
        completion["safety_timeout_reached"] = safety_timeout_reached
        completion["completion_confidence"] = round(float(completion.get("completion_confidence", 0.0)), 3)
        self.trace_turn_finalized(
            completion.get("reason", "unknown"),
            final_text or last_partial_text,
            silence_for if has_started else 0.0,
            resumed_after_thinking,
            completion["answer_duration_seconds"],
            completion["continuous_speech_seconds"],
            completion["speech_detected"],
        )
        completion_lines = [f"✓ {completion.get('reason', 'unknown')}"]
        if resumed_after_thinking:
            completion_lines.append("✓ candidate resumed after thinking")
        print("Completed because:\n" + "\n".join(completion_lines))
        print(
            "[turn completion] "
            f"answer_duration={completion['answer_duration_seconds']:.3f}s "
            f"continuous_speech={completion['continuous_speech_seconds']:.3f}s "
            f"silence={completion['silence_seconds']:.3f}s "
            f"speech_detected={completion['speech_detected']} "
            f"safety_timeout={completion['safety_timeout_reached']} "
            f"completion_confidence={completion['completion_confidence']:.3f} "
            f"reason={completion.get('reason', 'unknown')}"
        )
        return CandidateTurn(
            text=final_text,
            pcm_audio=final_audio,
            timing=timing,
            completion=completion,
            thinking_prompts=thinking_prompt_count,
            stt_confidence=confidence_result.get("confidence", 0.0),
        )

    @traceable(name="decide_interview_followup")
    def decide_followup_question(self, question: str, answer_text: str) -> dict:
        if not answer_text.strip():
            return {"ask_followup": False, "followup_question": None, "reason": "No answer"}
        fast = self.fast_intent(answer_text)
        if fast and fast.get("intent") in {"next_question", "end_interview", "repeat_question", "clarification"}:
            return {"ask_followup": False, "followup_question": None, "reason": f"Fast intent: {fast.get('intent')}"}
        response = self.report_llm.invoke(
            f"""
Return one valid JSON object only.
You are a human technical interviewer.
Decide whether the answer needs one deeper cross-question before moving on.
Ask a follow-up only if it will reveal important missing depth, such as:
- vague answer with no concrete implementation detail
- incorrect or confused technical concept
- candidate mentioned a tool/project but did not explain their own work
- answer misses the main point of the original question

Do not ask a follow-up just to be strict.
Do not ask more than one follow-up for this answer.
Ask only for weak, vague, or confused answers.
If asking, make it short, natural, and under 15 spoken words.
Do not repeat the original question.

JSON schema:
{{
  "ask_followup": true,
  "followup_question": "one short follow-up question or null",
  "reason": "short reason"
}}

Original question:
{question}

Candidate answer:
{answer_text}

Candidate/JD context:
{json.dumps(self.context_payload(), default=str)}
"""
        ).content
        try:
            data = json.loads(extract_json_object(response))
        except Exception:
            return {"ask_followup": False, "followup_question": None, "reason": "Could not parse follow-up decision"}
        if not data.get("ask_followup") or not data.get("followup_question"):
            return {"ask_followup": False, "followup_question": None, "reason": data.get("reason")}
        data["followup_question"] = self.shorten_question(data["followup_question"])
        return data

    @traceable(name="update_interview_memory")
    def update_interview_memory(self, turn_result: dict):
        self.interview_memory["questions_asked"].append(turn_result.get("question"))
        response = self.report_llm.invoke(
            f"""
Return one valid JSON object only.
Update concise structured interview memory from this turn.
Keep it short and useful for avoiding duplicate questions.

JSON schema:
{{
  "technologies_mentioned": [],
  "strengths": [],
  "weak_areas": [],
  "notes": []
}}

Existing memory:
{json.dumps(self.interview_memory, default=str)}

Latest turn:
{json.dumps(turn_result, default=str)}
"""
        ).content
        try:
            data = json.loads(extract_json_object(response))
        except Exception:
            return
        for key in ["technologies_mentioned", "strengths", "weak_areas", "notes"]:
            existing = self.interview_memory.setdefault(key, [])
            for item in data.get(key, []):
                item = str(item).strip()
                if item and item not in existing:
                    existing.append(item)
            self.interview_memory[key] = existing[-12:]

    @traceable(name="generate_adaptive_interview_question")
    def generate_adaptive_question(self, index: int, fallback_question: str | None = None) -> str:
        response = self.report_llm.invoke(
            f"""
Return one valid JSON object only.
Generate the next interview question.
It must be one short easy-to-medium question in conversational language.
Use the JD, CV, previous answers, and memory.
Avoid duplicate topics already asked.
Prefer a useful next question over a generic one.
Use maximum 15 spoken words.
Do not read long resume details back to the candidate.

JSON schema:
{{
  "question": "string"
}}

Question number:
{index}

Application context:
{json.dumps(self.context_payload(), default=str)}

Interview memory:
{json.dumps(self.interview_memory, default=str)}

Previous transcript:
{json.dumps(self.answers, default=str)}
"""
        ).content
        try:
            question = json.loads(extract_json_object(response)).get("question")
        except Exception:
            question = None
        return self.shorten_question(str(question or fallback_question or "Describe one relevant Python project you worked on. What was your role?").strip())

    def merge_turn_timing(self, target: VoiceTiming, source: VoiceTiming):
        target.tts_seconds += source.tts_seconds
        target.vad_seconds += source.vad_seconds
        target.playback_seconds += source.playback_seconds
        target.transcription_seconds += source.transcription_seconds
        target.thinking_seconds += source.thinking_seconds
        target.llm_seconds += source.llm_seconds
        target.full_llm_completion_seconds += source.full_llm_completion_seconds
        target.interruptions += source.interruptions
        if source.first_spoken_audio_seconds and not target.first_spoken_audio_seconds:
            target.first_spoken_audio_seconds = source.first_spoken_audio_seconds
        for state in source.states:
            target.mark_state(state)

    @traceable(name="prepare_next_main_question")
    def prepare_next_main_question(self, index: int, fallback_question: str | None, turn_result: dict) -> dict:
        started_at = time.perf_counter()
        self.update_interview_memory(turn_result)
        question = self.generate_adaptive_question(index, fallback_question)
        speech_future = self.prefetch_speech(question)
        if speech_future:
            try:
                speech_future.result(timeout=8)
            except Exception:
                pass
        seconds = time.perf_counter() - started_at
        self.trace_main_question_transition_prepared(index, question, seconds)
        return {"index": index, "question": question, "seconds": round(seconds, 3)}

    def start_next_question_preparation(
        self,
        index: int | None,
        fallback_question: str | None,
        turn_result: dict,
    ) -> Future | None:
        if not index:
            return None
        future = self.executor.submit(self.prepare_next_main_question, index, fallback_question, turn_result)
        self.pending_next_question_future = future
        return future

    def wait_for_prepared_question(self, mic: Microphone, future: Future, index: int) -> str:
        started_at = time.perf_counter()
        fillers_used = 0
        while not future.done():
            fillers_used += 1
            self.speak(
                self.transition_text(),
                mic,
                interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS,
            )
            if fillers_used >= 2:
                break
        prepared = future.result()
        self.trace_main_question_transition_masked(index, fillers_used, time.perf_counter() - started_at)
        return prepared["question"]

    def finish_question_result(
        self,
        index: int,
        question: str,
        answer_text: str,
        status: str,
        turn: dict,
        candidate_turn: CandidateTurn,
        started_at: float,
        attempts: int,
    ) -> dict:
        candidate_turn.timing.total_seconds = time.perf_counter() - started_at
        result = {
            "question_number": index,
            "question": question,
            "answer": answer_text,
            "status": status,
            "turn_classification": turn,
            "attempts": attempts,
            "completion": candidate_turn.completion,
            "thinking_prompts": candidate_turn.thinking_prompts,
            "conversation_memory": self.conversation_snapshot(),
            "timing": candidate_turn.timing.as_dict(),
        }
        self.metrics.add_turn(candidate_turn.timing, candidate_turn.pcm_audio)
        self.print_timing_breakdown(f"question {index}", candidate_turn.timing)
        return result

    def evaluate_answer_and_followup(
        self,
        mic: Microphone,
        index: int,
        question: str,
        answer_text: str,
        turn: dict,
        candidate_turn: CandidateTurn,
        started_at: float,
        attempts: int,
        next_question_index: int | None = None,
        next_fallback_question: str | None = None,
    ) -> dict:
        self.set_interview_state(INTERVIEW_STATE_EVALUATING, "candidate answer accepted for evaluation", candidate_turn.timing)
        result = {
            "question_number": index,
            "question": question,
            "answer": answer_text,
            "status": "answered",
            "turn_classification": turn,
            "attempts": attempts,
            "completion": candidate_turn.completion,
            "thinking_prompts": candidate_turn.thinking_prompts,
            "conversation_memory": self.conversation_snapshot(),
            "timing": candidate_turn.timing.as_dict(),
        }
        followup_started_at = time.perf_counter()
        followup = self.decide_followup_question(question, answer_text)
        followup_seconds = time.perf_counter() - followup_started_at
        candidate_turn.timing.llm_seconds += followup_seconds
        candidate_turn.timing.full_llm_completion_seconds += followup_seconds
        if followup.get("ask_followup") and followup.get("followup_question"):
            self.set_interview_state(INTERVIEW_STATE_FOLLOW_UP, "asking one follow-up", candidate_turn.timing)
            followup_question = followup["followup_question"]
            print(f"Follow-up {index}: {followup_question}")
            self.remember_conversation_event("interviewer", followup_question, action="follow_up", question_number=index)
            self.speak(
                followup_question,
                mic,
                interrupt_ms=INTERVIEW_FOLLOWUP_INTERRUPT_MS,
                timing=candidate_turn.timing,
            )
            self.set_interview_state(INTERVIEW_STATE_LISTENING, "waiting for follow-up answer", candidate_turn.timing)
            followup_turn = self.listen_candidate_turn(mic, followup_question)
            followup_answer = followup_turn.text
            print(f"Candidate: {followup_answer}")
            self.merge_turn_timing(candidate_turn.timing, followup_turn.timing)
            followup_classify_started_at = time.perf_counter()
            followup_intent = self.understand_candidate_intent(followup_question, followup_answer)
            followup_classify_seconds = time.perf_counter() - followup_classify_started_at
            candidate_turn.timing.llm_seconds += followup_classify_seconds
            candidate_turn.timing.full_llm_completion_seconds += followup_classify_seconds
            self.remember_conversation_event(
                "candidate",
                followup_answer,
                intent=followup_intent.get("intent"),
                question_number=index,
                metadata={"followup": True},
            )
            if (followup_intent.get("intent") or "").lower() in {"clarification", "repeat_question", "candidate_question"}:
                self.set_interview_state(INTERVIEW_STATE_CLARIFYING, "clarification during follow-up", candidate_turn.timing)
                clarification_reply = (
                    f"Sure. {followup_question}"
                    if (followup_intent.get("intent") or "").lower() == "repeat_question"
                    else self.clarification_then_question(followup_question, followup_answer)
                )
                self.remember_conversation_event("interviewer", clarification_reply, action="clarify_followup", question_number=index)
                self.speak(
                    clarification_reply,
                    mic,
                    interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS,
                    timing=candidate_turn.timing,
                )
                self.set_interview_state(INTERVIEW_STATE_LISTENING, "waiting for follow-up answer after clarification", candidate_turn.timing)
                followup_turn = self.listen_candidate_turn(mic, followup_question)
                followup_answer = followup_turn.text
                print(f"Candidate: {followup_answer}")
                self.merge_turn_timing(candidate_turn.timing, followup_turn.timing)
                followup_classify_started_at = time.perf_counter()
                followup_intent = self.understand_candidate_intent(followup_question, followup_answer)
                followup_classify_seconds = time.perf_counter() - followup_classify_started_at
                candidate_turn.timing.llm_seconds += followup_classify_seconds
                candidate_turn.timing.full_llm_completion_seconds += followup_classify_seconds
                self.remember_conversation_event(
                    "candidate",
                    followup_answer,
                    intent=followup_intent.get("intent"),
                    question_number=index,
                    metadata={"followup": True, "after_clarification": True},
                )
            result["followup"] = {
                "question": followup_question,
                "answer": followup_answer,
                "reason": followup.get("reason"),
                "turn_classification": followup_intent,
                "completion": followup_turn.completion,
                "thinking_prompts": followup_turn.thinking_prompts,
                "timing": followup_turn.timing.as_dict(),
            }

        if next_question_index:
            self.start_next_question_preparation(next_question_index, next_fallback_question, result)

        candidate_turn.timing.total_seconds = time.perf_counter() - started_at
        self.set_interview_state(INTERVIEW_STATE_ACKNOWLEDGING, "acknowledging answer before next question", candidate_turn.timing)
        acknowledgement = self.acknowledgement_text(result)
        self.remember_conversation_event("interviewer", acknowledgement, action="acknowledge", question_number=index)
        self.speak(acknowledgement, mic, interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS, timing=candidate_turn.timing)
        self.set_interview_state(INTERVIEW_STATE_NEXT_QUESTION, "answer completed", candidate_turn.timing)
        result["conversation_memory"] = self.conversation_snapshot()
        result["timing"] = candidate_turn.timing.as_dict()
        self.metrics.add_turn(candidate_turn.timing, candidate_turn.pcm_audio)
        self.print_timing_breakdown(f"question {index}", candidate_turn.timing)
        return result

    @traceable(name="recruiter_interview_question_turn")
    def ask_question_humanly(
        self,
        mic: Microphone,
        index: int,
        question: str,
        next_question_index: int | None = None,
        next_fallback_question: str | None = None,
    ) -> dict:
        attempts = 0
        clarification_count = 0
        question = self.shorten_question(question)
        self.set_active_question(index, question)
        prompt = question
        asked_question = False
        full_turn_started_at = time.perf_counter()
        last_candidate_turn: CandidateTurn | None = None
        last_turn: dict = {"intent": "empty", "is_answer": False, "reason": "No candidate turn yet"}

        while attempts <= INTERVIEW_MAX_CLARIFICATIONS_PER_QUESTION + 4:
            attempts += 1
            prompt_timing = VoiceTiming()
            print(f"\nQuestion {index}: {question}")
            if prompt:
                self.set_interview_state(INTERVIEW_STATE_ASKING_QUESTION, "interviewer response from conversation decision", prompt_timing)
                self.remember_conversation_event("interviewer", prompt, action="prompt", question_number=index)
                was_interrupted = self.speak(
                    prompt,
                    mic,
                    interrupt_ms=INTERVIEW_MAIN_INTERRUPT_MS,
                    timing=prompt_timing,
                )
                if not was_interrupted:
                    mic.clear()
                asked_question = True

            self.set_interview_state(INTERVIEW_STATE_LISTENING, "waiting for candidate conversational turn", prompt_timing)
            candidate_turn = self.listen_candidate_turn(mic, question)
            last_candidate_turn = candidate_turn
            self.merge_turn_timing(candidate_turn.timing, prompt_timing)
            answer_text = candidate_turn.text
            print(f"Candidate: {answer_text}")
            low_confidence = bool(answer_text) and candidate_turn.stt_confidence < 0.35
            if answer_text.lower().strip() in {"exit", "quit", "stop"}:
                turn = {"intent": "end_interview", "is_answer": False, "reason": "Candidate used stop command"}
                self.remember_conversation_event("candidate", answer_text, intent="end_interview", question_number=index)
                return self.finish_question_result(index, question, answer_text, "stopped_by_candidate", turn, candidate_turn, full_turn_started_at, attempts)

            classify_started_at = time.perf_counter()
            self.set_interview_state(INTERVIEW_STATE_EVALUATING, "understanding candidate intent", candidate_turn.timing)
            turn = self.understand_candidate_intent(question, answer_text)
            last_turn = turn
            classify_seconds = time.perf_counter() - classify_started_at
            candidate_turn.timing.llm_seconds += classify_seconds
            candidate_turn.timing.full_llm_completion_seconds += classify_seconds
            intent = (turn.get("intent") or "unknown").lower()
            self.remember_conversation_event("candidate", answer_text, intent=intent, question_number=index)
            decision = self.decide_conversation_action(turn, question, answer_text, low_confidence=low_confidence)
            self.remember_conversation_event(
                "interviewer_decision",
                decision.response,
                intent=intent,
                action=decision.action,
                question_number=decision.target_question_number or index,
                metadata={"reason": decision.reason},
            )

            if decision.action == "stop":
                self.speak(decision.response, mic, interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS, timing=candidate_turn.timing)
                return self.finish_question_result(index, question, answer_text, decision.status or "stopped_by_candidate", turn, candidate_turn, full_turn_started_at, attempts)

            if decision.action == "skip":
                self.set_interview_state(INTERVIEW_STATE_NEXT_QUESTION, "candidate requested next question", candidate_turn.timing)
                self.speak(decision.response, mic, interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS, timing=candidate_turn.timing)
                return self.finish_question_result(index, question, answer_text, decision.status or "skipped_by_candidate", turn, candidate_turn, full_turn_started_at, attempts)

            if decision.action == "pause":
                self.set_interview_state(INTERVIEW_STATE_THINKING, "candidate asked to pause or think", candidate_turn.timing)
                self.speak(decision.response, mic, interrupt_ms=INTERVIEW_REPLY_INTERRUPT_MS, timing=candidate_turn.timing)
                prompt = ""
                attempts -= 1
                continue

            if decision.action == "resume":
                self.set_interview_state(INTERVIEW_STATE_RESPONDING, "candidate resumed after pause", candidate_turn.timing)
                prompt = f"{decision.response} {question}" if asked_question else f"{decision.response} {question}"
                attempts -= 1
                continue

            if decision.action in {"repeat_question", "return_to_question"}:
                self.set_interview_state(INTERVIEW_STATE_CLARIFYING, decision.reason or "candidate requested a question reference", candidate_turn.timing)
                if decision.target_question_number and decision.target_question:
                    index = decision.target_question_number
                    question = self.shorten_question(decision.target_question)
                    self.set_active_question(index, question)
                prompt = decision.response or f"Sure. {question}"
                clarification_count += 1
                attempts -= 1
                continue

            if decision.action == "clarify":
                self.set_interview_state(INTERVIEW_STATE_CLARIFYING, decision.reason or "candidate asked clarification", candidate_turn.timing)
                prompt = decision.response or self.clarification_then_question(question, answer_text)
                clarification_count += 1
                attempts -= 1
                continue

            if decision.action == "wait_for_answer":
                self.set_interview_state(INTERVIEW_STATE_THINKING, decision.reason or "waiting for real answer", candidate_turn.timing)
                prompt = decision.response
                if intent == "empty":
                    clarification_count += 1
                attempts -= 1
                if clarification_count >= INTERVIEW_MAX_CLARIFICATIONS_PER_QUESTION + 2 and last_candidate_turn:
                    return self.finish_question_result(index, question, answer_text, "unclear_answer", turn, candidate_turn, full_turn_started_at, attempts)
                continue

            if decision.action == "accept_answer":
                self.conversation.paused = False
                self.conversation.waiting_for = "next_question"
                self.conversation.partial_answer = ""
                return self.evaluate_answer_and_followup(
                    mic,
                    index,
                    question,
                    answer_text,
                    turn,
                    candidate_turn,
                    full_turn_started_at,
                    attempts,
                    next_question_index,
                    next_fallback_question,
                )

            prompt = self.clarification_prompt(clarification_count)

        fallback_timing = last_candidate_turn.timing if last_candidate_turn else VoiceTiming()
        fallback_timing.total_seconds = time.perf_counter() - full_turn_started_at
        if last_candidate_turn:
            self.metrics.add_turn(fallback_timing, last_candidate_turn.pcm_audio)
        else:
            self.metrics.add_turn(fallback_timing)
        self.print_timing_breakdown(f"question {index}", fallback_timing)
        return {
            "question_number": index,
            "question": question,
            "answer": last_candidate_turn.text if last_candidate_turn else "",
            "status": "no_clear_answer_after_retries",
            "turn_classification": last_turn,
            "attempts": attempts,
            "conversation_memory": self.conversation_snapshot(),
            "timing": fallback_timing.as_dict(),
        }

    @traceable(name="generate_recruiter_interview_report")
    def generate_report(self) -> dict:
        response = self.report_llm.invoke(
            f"""
Return one valid JSON object only.
You are an HR technical interviewer. Evaluate this interview fairly.
Use only the candidate answers, CV, and JD context provided.
Consider answer status, adaptive follow-up answers, clarification requests, repeated questions, and no-clear-answer retries when scoring technical depth, communication, and confidence.
Do not invent camera or eye movement observations. Set camera_monitoring.available=false unless observations are provided.

JSON schema:
{{
  "overall_score": 0,
  "technical_score": 0,
  "communication_score": 0,
  "role_fit_score": 0,
  "recommendation": "strong_hire/hire/hold/reject",
  "summary": "short HR summary",
  "plus_points": [],
  "negative_points": [],
  "question_reviews": [
    {{"question": "string", "answer_summary": "string", "score": 0, "notes": "string"}}
  ],
  "camera_monitoring": {{
    "available": false,
    "eye_movement_summary": "Not captured in this voice interview mode.",
    "unusual_activity": []
  }},
  "final_notes_for_hr": "string"
}}

Application context:
{json.dumps(self.context_payload(), default=str)}

Interview transcript:
{json.dumps(self.answers, default=str)}

Structured interview memory:
{json.dumps(self.interview_memory, default=str)}
"""
        ).content
        report = json.loads(extract_json_object(response))
        report["application_id"] = self.application_id
        report["question_count"] = len(self.answers)
        report["transcript"] = self.answers
        report["interview_memory"] = self.interview_memory
        report["voice_metrics"] = self.metrics.summary()
        return report

    def run(self):
        print(f"AI recruiter interview ready for application {self.application_id}. Press Ctrl+C to stop.")
        try:
            with Microphone() as mic:
                self.start_interview_conversation(mic)
                mic.clear()
                seed_questions = self.questions_future.result()
                prepared_question_future: Future | None = None
                for index in range(1, RECRUITER_INTERVIEW_QUESTION_COUNT + 1):
                    if prepared_question_future:
                        question = self.wait_for_prepared_question(mic, prepared_question_future, index)
                        prepared_question_future = None
                    else:
                        fallback_question = seed_questions[index - 1] if index - 1 < len(seed_questions) else None
                        question = self.generate_adaptive_question(index, fallback_question) if index > 1 else (fallback_question or self.generate_adaptive_question(index))
                        self.prefetch_speech(question)

                    next_index = index + 1 if index < RECRUITER_INTERVIEW_QUESTION_COUNT else None
                    next_fallback = seed_questions[next_index - 1] if next_index and next_index - 1 < len(seed_questions) else None
                    self.pending_next_question_future = None
                    result = self.ask_question_humanly(mic, index, question, next_index, next_fallback)
                    if result.get("status") == "stopped_by_candidate":
                        self.answers.append(result)
                        break
                    self.answers.append(result)
                    prepared_question_future = self.pending_next_question_future
                    if not prepared_question_future:
                        self.update_interview_memory(result)
                    mic.clear()

                self.speak(self.closing_text(), mic)
                self.print_interview_summary()
            report = self.generate_report()
            self.db.update_interview_report(self.application_id, report)
            print("\nInterview report saved.")
            print(json.dumps(report, indent=2, default=str))
        finally:
            self.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI voice agent")
    parser.add_argument("--recruiter-interview", type=int, help="Run AI voice interview for recruiter application id")
    args = parser.parse_args()
    try:
        if args.recruiter_interview:
            AIRecruiterInterviewAgent(args.recruiter_interview).run()
        else:
            VoicePolicyAgent().run()
    except KeyboardInterrupt:
        print("\nStopped.")
