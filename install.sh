#!/bin/bash

# 1. Check if sentinelCam-worker is already installed
if [ -f ".sentinelCam-installed" ]; then
    echo "sentinelCam-worker bereits installiert."
    exit 0
fi

# 2. Ask if dependencies should be installed
echo "sentinelCam-worker nicht gefunden."
read -p "Moechtest du die Dependencies installieren? (j/n): " INSTALL_DEPS
if [ "$INSTALL_DEPS" != "j" ]; then
    echo "Installation uebersprungen."
    exit 0
fi

# 3. Clone repository into temp folder
echo "Klone Repository..."
if ! git clone https://github.com/okixk/sentinelCam-worker.git _temp_clone; then
    echo "Fehler beim Klonen des Repositories. Ist git installiert?"
    exit 1
fi

# 4. Move all files from temp folder to current directory
cp -a _temp_clone/. .

# 5. Delete temp folder
rm -rf _temp_clone

# 6. Set permissions for current user
chown -R "$(id -u):$(id -g)" .
chmod -R u+rwX .
find . -type f -exec chmod u+rw {} +

# 7. Create marker file
touch .sentinelCam-installed

echo "Dependencies erfolgreich installiert."

# 8. Start run.sh
echo "Starte run.sh..."
bash run.sh


