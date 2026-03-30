import os
import sounddevice as sd
import numpy as np
import wavio
from google.cloud import speech_v1p1beta1 as speech # Use speech_v1p1beta1 for better results (e.g. word offsets)
from google.oauth2 import service_account # For explicit credential loading

# Placeholder for Google Cloud credentials and Gemini API key
# IMPORTANT:
# 1. Set GOOGLE_APPLICATION_CREDENTIALS environment variable to the path of your Google Cloud service account key file (JSON).
#    E.g., export GOOGLE_APPLICATION_CREDENTIALS="/path/to/your/keyfile.json"
# 2. Set GEMINI_API_KEY environment variable with your Gemini API key.
#    E.g., export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"

# --- Audio Recording and Playback Test (using sounddevice) ---

def record_audio(filename="output.wav", duration=5, samplerate=44100, channels=1):
    """Records audio from the microphone for a given duration."""
    print(f"Recording for {duration} seconds...")
    audio_data = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=channels, dtype='int16')
    sd.wait()  # Wait until recording is finished
    # Convert numpy array to bytes for wavio
    wavio.write(filename, audio_data, samplerate, sampwidth=2)
    print(f"Recording saved to {filename}")
    return filename

def play_audio(filename="output.wav"):
    """Plays an audio file."""
    if not os.path.exists(filename):
        print(f"Error: Audio file {filename} not found.")
        return

    print(f"Playing {filename}...")
    try:
        wav = wavio.read(filename)
        # Ensure data is in a format sounddevice can play
        sd.play(wav.data / np.max(np.abs(wav.data)), wav.rate) # Normalize for playback
        sd.wait()
        print("Playback finished.")
    except Exception as e:
        print(f"Error playing audio: {e}")

# --- Speech-to-Text Integration ---

def speech_to_text(audio_file_path, samplerate=44100):
    """
    Transcribes audio from a local file using Google Cloud Speech-to-Text API.
    Requires GOOGLE_APPLICATION_CREDENTIALS environment variable to be set.
    """
    try:
        # Explicitly load credentials from the environment variable path
        credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not credentials_path:
            print("Error: GOOGLE_APPLICATION_CREDENTIALS environment variable not set.")
            return None

        credentials = service_account.Credentials.from_service_account_file(credentials_path)
        client = speech.SpeechClient(credentials=credentials)

        with open(audio_file_path, "rb") as audio_file:
            content = audio_file.read()

        audio = speech.RecognitionAudio(content=content)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16, # Assuming int16 output from sounddevice/wavio
            sample_rate_hertz=samplerate,
            language_code="en-US",
        )

        print(f"Sending {audio_file_path} to Google Cloud Speech-to-Text...")
        response = client.recognize(config=config, audio=audio)

        transcript = ""
        for result in response.results:
            transcript += result.alternatives[0].transcript
        
        if transcript:
            print(f"Transcription: '{transcript}'")
            return transcript
        else:
            print("No speech detected or transcribed.")
            return None

    except Exception as e:
        print(f"Error during Speech-to-Text: {e}")
        return None

if __name__ == "__main__":
    print("Starting voice agent setup...")

    # Check for environment variables
    credentials_warning = False
    if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        print("Warning: GOOGLE_APPLICATION_CREDENTIALS environment variable not set.")
        print("       Google Cloud services (STT/TTS) will not function without it.")
        credentials_warning = True

    if "GEMINI_API_KEY" not in os.environ:
        print("Warning: GEMINI_API_KEY environment variable not set.")
        print("       Gemini API will not function without it.")

    print("\n--- Testing Audio Recording and Playback ---")
    recorded_file = record_audio(duration=3) # Record for 3 seconds
    #play_audio(recorded_file) # Temporarily disable playback to focus on STT

    print("\n--- Testing Speech-to-Text ---")
    if not credentials_warning:
        transcribed_text = speech_to_text(recorded_file)
        if transcribed_text:
            print(f"Successfully transcribed: {transcribed_text}")
        else:
            print("Speech-to-Text failed or returned no transcription.")
    else:
        print("Skipping Speech-to-Text due to missing GOOGLE_APPLICATION_CREDENTIALS.")

    print("\nVoice agent setup test complete.")
    print("Next steps: Integrate Gemini API and Text-to-Speech.")