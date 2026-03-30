import asyncio
import os
import subprocess
import sounddevice as sd
import wavio
import openai
from elevenlabs.client import ElevenLabs
from abc import ABC, abstractmethod
from typing import AsyncGenerator
import numpy as np

# --- Configuration ---
# IMPORTANT: Set your API keys as environment variables before running the script.
# export OPENAI_API_KEY="your_openai_key"
# export ELEVEN_API_KEY="your_elevenlabs_key"
RECORD_DURATION = 5  # seconds
SAMPLE_RATE = 44100
AUDIO_FILE = "input.wav"

# --- Real STT/TTS Implementations ---

class BaseSTT(ABC):
    @abstractmethod
    async def transcribe(self, audio_file_path: str) -> str:
        pass

class OpenAIWhisperSTT(BaseSTT):
    def __init__(self, api_key: str):
        openai.api_key = api_key

    async def transcribe(self, audio_file_path: str) -> str:
        print("STT: Transcribing audio with OpenAI Whisper...")
        try:
            with open(audio_file_path, "rb") as audio_file:
                transcript = await asyncio.to_thread(openai.audio.transcriptions.create,
                    model="whisper-1",
                    file=audio_file
                )
            print(f"STT: Transcription successful: '{transcript.text}'")
            return transcript.text
        except Exception as e:
            print(f"STT Error: {e}")
            return ""

class BaseTTS(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        pass

class ElevenLabsTTS(BaseTTS):
    def __init__(self, api_key: str):
        self.client = ElevenLabs(api_key=api_key)

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        print(f"TTS: Synthesizing text with ElevenLabs: '{text}'")
        try:
            audio_stream = await asyncio.to_thread(self.client.generate,
                text=text,
                model="eleven_turbo_v2",
                stream=True
            )
            for chunk in audio_stream:
                yield chunk
        except Exception as e:
            print(f"TTS Error: {e}")

# --- Audio IO ---

def record_audio(filename: str, duration: int, samplerate: int):
    print(f"Recording for {duration} seconds... Please speak now.")
    audio_data = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=1, dtype='int16')
    sd.wait()
    wavio.write(filename, audio_data, samplerate, sampwidth=2)
    print(f"Recording saved to {filename}")

async def play_audio_stream(audio_stream: AsyncGenerator[bytes, None]):
    # This is a simplified playback function. A real implementation would handle
    # audio format conversion and more robust streaming.
    # For now, we'll collect chunks and play them.
    print("AUDIO: Playing back synthesized audio...")
    full_audio = b"".join([chunk async for chunk in audio_stream])
    # This assumes the audio is in a format that can be played back directly.
    # ElevenLabs often returns MP3, which sounddevice can't play directly.
    # We would need another library like 'pydub' to convert it first.
    # For now, this will likely fail but demonstrates the flow.
    try:
        # A more robust solution would be needed here.
        print("AUDIO: Note - Direct playback of MP3/AAC stream is not supported by sounddevice.")
        print("         A conversion step (e.g., with pydub) would be needed in a full implementation.")
        pass # Not attempting to play the raw stream to avoid errors.
    except Exception as e:
        print(f"Audio playback error: {e}")


# --- OS Control Workflow ---

class VoiceWorkflowBase(ABC):
    @abstractmethod
    async def run(self, stt_output: str) -> AsyncGenerator[str, None]:
        pass

class OsControlWorkflow(VoiceWorkflowBase):
    def _get_intent(self, text: str) -> dict:
        text = text.lower().strip()
        if "list" in text and "files" in text:
            return {"intent": "execute_command", "command": "ls -l"}
        elif "what is your ip" in text:
            return {"intent": "execute_command", "command": "hostname -I"}
        elif "who am i" in text:
            return {"intent": "execute_command", "command": "whoami"}
        else:
            return {"intent": "unsupported", "original_text": text}

    async def run(self, stt_output: str) -> AsyncGenerator[str, None]:
        if not stt_output:
            yield "I didn't catch that. Please try again."
            return

        intent_data = self._get_intent(stt_output)
        intent = intent_data.get("intent")

        if intent == "execute_command":
            command = intent_data.get("command")
            print(f"WORKFLOW: Executing command: '{command}'")
            try:
                process = await asyncio.create_subprocess_shell(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = await process.communicate()
                if process.returncode == 0:
                    response = stdout.decode().strip() or f"Command '{command}' executed successfully."
                    yield response
                else:
                    yield f"Error: {stderr.decode().strip()}"
            except Exception as e:
                yield f"Execution error: {str(e)}"
        elif intent == "unsupported":
            yield f"Sorry, I don't know how to handle: '{intent_data.get('original_text')}'"
        else:
            yield "Sorry, I couldn't understand the request."

# --- Voice Pipeline ---

class VoicePipelineConfig:
    def __init__(self, stt_model: BaseSTT, tts_model: BaseTTS, workflow: VoiceWorkflowBase):
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.workflow = workflow

class VoicePipeline:
    def __init__(self, config: VoicePipelineConfig):
        self.config = config

    async def start(self, audio_file_path: str):
        stt_output = await self.config.stt_model.transcribe(audio_file_path)
        if stt_output:
            async for workflow_output in self.config.workflow.run(stt_output):
                audio_stream = self.config.tts_model.synthesize(workflow_output)
                await play_audio_stream(audio_stream)

async def main():
    print("Initializing OS Control Voice Agent...")
    openai_key = os.environ.get("OPENAI_API_KEY")
    eleven_key = os.environ.get("ELEVEN_API_KEY")

    if not openai_key or not eleven_key:
        print("ERROR: OPENAI_API_KEY and ELEVEN_API_KEY environment variables must be set.")
        return

    config = VoicePipelineConfig(
        stt_model=OpenAIWhisperSTT(api_key=openai_key),
        tts_model=ElevenLabsTTS(api_key=eleven_key),
        workflow=OsControlWorkflow()
    )
    pipeline = VoicePipeline(config)

    print("\n--- Starting Live Voice Control Session ---")
    record_audio(AUDIO_FILE, duration=RECORD_DURATION, samplerate=SAMPLE_RATE)
    await pipeline.start(AUDIO_FILE)
    print("\n--- Session finished ---")

if __name__ == "__main__":
    asyncio.run(main())