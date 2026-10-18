import subprocess
import time
import webview

# 1. Start Streamlit silently in the background
proc = subprocess.Popen(["streamlit", "run", "app.py", "--server.headless=true"])

# 2. Wait 2 seconds for local engine boot
time.sleep(2)

# 3. Open a native macOS window pointing to the app
webview.create_window(
    "Life Smart - השוואת ביטוח חיים",
    "http://localhost:8501",
    width=1100,
    height=820,
    resizable=True
)
webview.start()

# 4. Clean up background process when the window closes
proc.terminate()