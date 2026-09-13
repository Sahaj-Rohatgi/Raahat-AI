#!/bin/bash
# Regenerate the IVR voice prompts.
#
# Defaults to eSpeak NG, which is open source and packaged for every platform
# (brew install espeak-ng / apt install espeak-ng), so the prompts are reproducible
# by anyone who clones this repo rather than only on the machine they were made on.
#
# macOS's built-in `say` sounds considerably better, so it stays available as an
# opt-in for anyone on a Mac who is generating prompts for a demo:
#
#     ./generate.sh            # eSpeak NG (default, works anywhere)
#     TTS=say ./generate.sh    # macOS `say` (nicer voice, macOS only)
#
# Output is 8 kHz mono 16-bit PCM WAV, the format FreeSWITCH plays without transcoding.
set -e
cd "$(dirname "$0")"

TTS="${TTS:-espeak}"
VOICE="${VOICE:-en-us}"       # eSpeak NG voice; try en-gb or en-us+f3 for a female voice
RATE="${RATE:-145}"           # words per minute — slower than default, these are read to someone in distress

if [ "$TTS" = "say" ]; then
  command -v say >/dev/null || { echo "TTS=say needs macOS's \`say\`; unset TTS to use eSpeak NG." >&2; exit 1; }
  command -v afconvert >/dev/null || { echo "TTS=say needs macOS's \`afconvert\`." >&2; exit 1; }
else
  command -v espeak-ng >/dev/null || {
    echo "espeak-ng not found. Install it (brew install espeak-ng / apt install espeak-ng)," >&2
    echo "or run TTS=say ./generate.sh on macOS." >&2
    exit 1
  }
fi

gen() {
  local name="$1" text="$2"
  if [ "$TTS" = "say" ]; then
    say "$text" -o "${name}.aiff"
    afconvert "${name}.aiff" "${name}.wav" -d LEI16@8000 -c 1 -f WAVE
    rm -f "${name}.aiff"
  else
    # eSpeak NG emits at its own sample rate, so resample to the 8 kHz FreeSWITCH wants.
    espeak-ng -v "$VOICE" -s "$RATE" -w "${name}.raw.wav" "$text"
    python3 resample.py "${name}.raw.wav" "${name}.wav"
    rm -f "${name}.raw.wav"
  fi
  echo "  generated ${name}.wav"
}

echo "Generating IVR prompts with ${TTS}..."
gen welcome "Welcome to Raahat. For a wellbeing check in, press 1. For emergency help, press 9."
gen ask_mood "How is your mood today. Press 0 for good, 1 for okay, 2 for low, or 3 for struggling."
gen ask_sleep "How has your sleep been. Press 0 for restful, 1 for a little disturbed, 2 for poor, or 3 for very poor."
gen ask_safety "Do you feel safe right now. Press 0 for safe, 1 for somewhat unsafe, or 2 for unsafe."
gen ask_anxiety "How anxious do you feel. Press 0 for calm, up to 2 for very anxious."
gen ask_pressure "How much pressure do you feel from your case right now. Press 0 for none, up to 3 for a lot."
gen checkin_done "Thank you. Your check in has been recorded. A member of the care team will follow up if needed. Goodbye."
gen sos_done "Your emergency alert has been sent to the care team immediately. If you are in danger, please try to reach a safe place. Goodbye."
gen invalid "Sorry, that was not a valid option. Goodbye."
echo "Done."
