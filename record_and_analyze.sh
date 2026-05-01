#!/bin/bash
# BirdNET Record → Analyze → Push to Dashboard
# Place on Pi at ~/record_and_analyze.sh

RECORDINGS_DIR="$HOME/birdnet_recordings"
RESULTS_DIR="$HOME/birdnet_results"
BIRDNET_DIR="$HOME/BirdNET-Analyzer"
LATITUDE=12.97    # Change to your latitude
LONGITUDE=77.59   # Change to your longitude

# ---- Dashboard settings ----
DASHBOARD_URL="https://YOUR-APP.up.railway.app"   # Replace after Railway deploy
API_KEY="changeme-secret-key"                       # Must match BIRDNET_API_KEY on Railway

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
AUDIO_FILE="$RECORDINGS_DIR/recording_${TIMESTAMP}.wav"
RESULT_FILE="$RESULTS_DIR/result_${TIMESTAMP}.csv"

# 1. Record 15 seconds
arecord -D hw:1,0 -f S16_LE -r 48000 -c 1 -d 15 "$AUDIO_FILE" 2>/dev/null

# 2. Analyze
cd "$BIRDNET_DIR"
source venv/bin/activate
python analyze.py --i "$AUDIO_FILE" --o "$RESULT_FILE" \
  --lat $LATITUDE --lon $LONGITUDE --locale en

# 3. Push results to dashboard
if [ -f "$RESULT_FILE" ]; then
    # BirdNET CSV columns: Start (s), End (s), Scientific name, Common name, Confidence
    # Skip header, build JSON array
    PAYLOAD="["
    FIRST=true
    while IFS=$'\t' read -r start end sci_name common_name confidence; do
        # Skip header line
        [[ "$start" == "Start"* ]] && continue
        # Skip low-confidence detections
        (( $(echo "$confidence < 0.25" | bc -l) )) && continue

        SPECIES="${common_name:-$sci_name}"
        TS=$(date -u +%Y-%m-%dT%H:%M:%S)

        if [ "$FIRST" = true ]; then
            FIRST=false
        else
            PAYLOAD+=","
        fi
        PAYLOAD+="{\"species\":\"$SPECIES\",\"confidence\":$confidence,\"timestamp\":\"$TS\",\"latitude\":$LATITUDE,\"longitude\":$LONGITUDE,\"audio_file\":\"recording_${TIMESTAMP}.wav\"}"
    done < "$RESULT_FILE"
    PAYLOAD+="]"

    # Only POST if we have detections
    if [ "$FIRST" = false ]; then
        curl -s -X POST "$DASHBOARD_URL/api/detect" \
          -H "Content-Type: application/json" \
          -H "X-API-Key: $API_KEY" \
          -d "$PAYLOAD" >> "$HOME/birdnet_push.log" 2>&1
        echo " [$(date)] Pushed detections" >> "$HOME/birdnet_push.log"
    fi
fi

# 4. Cleanup old recordings (>7 days)
find "$RECORDINGS_DIR" -name "*.wav" -mtime +7 -delete
