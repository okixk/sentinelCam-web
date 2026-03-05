#!/bin/bash

# 1. Check if all required files are already present
ALL_PRESENT=true
for FILE in webcam.py webcam.properties run.sh run.bat; do
    if [ ! -f "$FILE" ]; then
        ALL_PRESENT=false
        break
    fi
done

if [ "$ALL_PRESENT" = true ]; then
    echo "sentinelCam-worker bereits installiert."
    exit 0
fi

# 2. Ask if dependencies should be installed
echo "sentinelCam-worker nicht gefunden."
read -p "Möchtest du die Dependencies installieren? (j/n): " INSTALL_DEPS
if [ "$INSTALL_DEPS" != "j" ]; then
    echo "Installation Übersprungen."
    exit 0
fi

# 3. Clone repository into temp folder
echo "Klone Repository..."
if ! git clone https://github.com/okixk/sentinelCam-worker.git _temp_clone; then
    echo "Fehler beim Klonen des Repositories. Ist git installiert?"
    exit 1
fi

# 4. Copy only required files from temp folder
for FILE in webcam.py webcam.properties run.sh run.bat; do
    if [ -f "_temp_clone/$FILE" ]; then
        cp "_temp_clone/$FILE" .
    else
        echo "Warnung: $FILE nicht im Repository gefunden."
    fi
done

# 5. Delete temp folder
rm -rf _temp_clone

# 6. Set permissions for copied files
for FILE in webcam.py webcam.properties run.sh run.bat; do
    if [ -f "$FILE" ]; then
        chown "$(id -u):$(id -g)" "$FILE"
        chmod u+rw "$FILE"
    fi
done

echo "Dependencies erfolgreich installiert."

# 8. Start run.sh
echo "Starte run.sh..."
bash run.sh --web --stream webrtc --port 8080 --webrtc-codec auto


